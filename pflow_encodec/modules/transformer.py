import math
from functools import partial
from typing import Dict, Literal, Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import einsum, rearrange

from pflow_encodec.utils.helper import exists


class AdaptiveLayerNormProj(nn.Module):
    def __init__(self, dim: int, dim_cond: int, eps: float = 1e-6):
        super().__init__()
        self.norm = nn.LayerNorm(dim, elementwise_affine=False, eps=eps)

        self.scale = nn.Linear(dim_cond, dim)
        self.bias = nn.Linear(dim_cond, dim)

    def forward(self, x: torch.Tensor, cond: torch.Tensor) -> torch.Tensor:
        scale = self.scale(cond)
        bias = self.bias(cond)
        return self.norm(x) * (1 + scale) + bias


class AdaptiveLayerNormEmbed(nn.Module):
    def __init__(self, dim: int, eps: float = 1e-6):
        super().__init__()
        self.norm = nn.LayerNorm(dim, elementwise_affine=False, eps=eps)

        self.scale = nn.Parameter(torch.randn(1, 1, dim) / dim**0.5)
        self.bias = nn.Parameter(torch.zeros(1, 1, dim))

    def forward(self, x: torch.Tensor, cond: torch.Tensor) -> torch.Tensor:
        scale, bias = cond.chunk(2, dim=-1)
        scale = self.scale + scale
        bias = self.bias + bias
        return self.norm(x) * (1 + scale) + bias


class AdaptiveScaleProj(nn.Module):
    def __init__(self, dim: int, dim_cond: int):
        super().__init__()
        self.scale = nn.Linear(dim_cond, dim)

    def forward(self, x: torch.Tensor, cond: torch.Tensor) -> torch.Tensor:
        scale = self.scale(cond)
        return x * scale


class AdaptiveScaleEmbed(nn.Module):
    def __init__(self, dim: int):
        super().__init__()
        self.scale = nn.Parameter(torch.randn(1, 1, dim) / dim**0.5)

    def forward(self, x: torch.Tensor, cond: torch.Tensor) -> torch.Tensor:
        scale = self.scale + cond
        return x * scale


class GEGLU(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x: torch.Tensor, dim: int = -1) -> torch.Tensor:
        x, gate = x.chunk(2, dim=dim)
        return F.gelu(gate) * x


class ConvFeedForward(nn.Module):
    def __init__(self, dim: int, mult: float, kernel_size: int, groups: int = 1, dropout: float = 0.0):
        super().__init__()
        intermediate_dim = int(dim * mult * 3 / 4)
        self.conv1 = nn.Conv1d(dim, 2 * intermediate_dim, kernel_size, padding="same", groups=groups)
        self.act = GEGLU()
        self.dropout = nn.Dropout(dropout)
        self.conv2 = nn.Conv1d(intermediate_dim, dim, kernel_size, padding="same", groups=groups)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = rearrange(x, "b t d -> b d t")
        x = self.conv1(x)
        x = self.act(x, dim=1)
        x = self.dropout(x)
        x = self.conv2(x)
        x = rearrange(x, "b d t -> b t d")
        return x


class FeedForward(nn.Module):
    def __init__(self, dim: int, mult: float, dropout: float = 0.0):
        super().__init__()
        intermediate_dim = int(dim * mult * 2 / 3)
        self.net = nn.Sequential(
            nn.Linear(dim, intermediate_dim * 2), GEGLU(), nn.Dropout(dropout), nn.Linear(intermediate_dim, dim)
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class MultiHeadAttention(nn.Module):
    def __init__(
        self,
        dim: int,
        dim_head: int,
        heads: int,
        scale: Optional[float] = None,
        dropout: float = 0.0,
        processor: Literal["naive", "sdpa", "flash"] = "naive",
    ):
        super().__init__()
        self.dim_head = dim_head
        self.scale = scale if exists(scale) else dim_head ** -0.5
        self.processor = processor
        self.heads = heads

        inner_dim = dim_head * heads
        self.to_q = nn.Linear(dim, inner_dim, bias=False)
        self.to_k = nn.Linear(dim, inner_dim, bias=False)
        self.to_v = nn.Linear(dim, inner_dim, bias=False)
        self.dropout = nn.Dropout(dropout)  # apply to attn score

        self.to_out = nn.Linear(inner_dim, dim)

        self.attn_processor_dict = {
            "naive": self.naive_attention,
            "sdpa": self.sdpa_attention,
        }

        if self.processor not in self.attn_processor_dict:
            raise NotImplementedError(f"processor {self.processor} is not implemented yet")

    def process_attn_mask_bias(self, mask, bias):
        if not exists(bias):
            return mask

        if exists(mask):
            bias = bias.masked_fill(~mask, -torch.finfo(bias.dtype).max)
        return bias

    def naive_attention(self, q, k, v, mask, bias, **attn_kwargs):
        q, k, v = map(lambda t: rearrange(t, "b n (h d) -> b h n d", h=self.heads), (q, k, v))

        attn_mask = self.process_attn_mask_bias(mask, bias)
        dots = einsum(q, k, "b h i d, b h j d -> b h i j") * self.scale

        if exists(attn_mask):
            dots.masked_fill_(~attn_mask, -torch.finfo(dots.dtype).max)

        attn = dots.softmax(dim=-1)
        attn = self.dropout(attn)
        out = einsum(attn, v, "b h i j, b h j d -> b h i d")
        out = rearrange(out, "b h n d -> b n (h d)")

        return out

    def sdpa_attention(self, q, k, v, mask, bias, **attn_kwargs):
        if not hasattr(F, "scaled_dot_product_attention"):
            raise RuntimeError(
                "torch.nn.functional.scaled_dot_product_attention is not available. Please upgrade to PyTorch 2.0.0 or later."
            )
        q, k, v = map(lambda t: rearrange(t, "b n (h d) -> b h n d", h=self.heads), (q, k, v))

        attn_mask = self.process_attn_mask_bias(mask, bias)
        return F.scaled_dot_product_attention(q, k, v, attn_mask=attn_mask, dropout_p=self.dropout.p)

    def get_attn_processor(self, processor):
        assert processor in self.attn_processor_dict, f"processor {processor} is not implemented yet"
        return self.attn_processor_dict[processor]

    def forward(
        self,
        x: torch.Tensor,
        context: Optional[torch.Tensor] = None,
        mask: Optional[torch.Tensor] = None,
        bias: Optional[torch.Tensor] = None,
        **attn_kwargs,
    ):
        if not exists(context):
            context = x

        b, t, d = x.shape
        q, k, v = self.to_q(x), self.to_k(context), self.to_v(context)

        attn_output = self.get_attn_processor(self.processor)(q, k, v, mask, bias, **attn_kwargs)

        return self.to_out(attn_output)


class Transformer(nn.Module):
    def __init__(
        self,
        depth: int,
        dim: int,
        dim_head: int,
        heads: int,
        ff_mult: float,
        attn_dropout: float,
        ff_dropout: float,
        dim_cond: Optional[int] = None,
        attn_processor: Literal["naive", "sdpa"] = "naive",
        norm_type: Literal["layer", "ada_proj", "ada_embed"] = "layer",
        ff_type: Literal["conv", "linear"] = "linear",
        ff_kernel_size: Optional[int] = None,
        ff_groups: Optional[int] = None,
        layer_norm_eps: float = 1e-6,
        scale_type: Literal["none", "ada_proj", "ada_embed"] = "none",
    ):
        super().__init__()
        self.layers = nn.ModuleList([])

        self.norm_type = norm_type
        norm_class = self.get_norm_class(norm_type, dim_cond)

        self.ff_type = ff_type
        ff_class = self.get_ff_class(ff_type, ff_kernel_size, ff_groups)

        self.scale_type = scale_type
        if self.scale_type != "none":
            assert (
                self.norm_type == self.scale_type
            ), f"norm type {self.norm_type} and scale type {self.scale_type} must be the same"
        scale_class = self.get_scale_class(scale_type, dim, dim_cond)

        self.layers = nn.ModuleList(
            [
                nn.ModuleList(
                    [
                        norm_class(dim, eps=layer_norm_eps),
                        MultiHeadAttention(
                            dim=dim,
                            dim_head=dim_head,
                            heads=heads,
                            scale=None,
                            dropout=attn_dropout,
                            processor=attn_processor,
                        ),
                        scale_class(),
                        norm_class(dim, eps=layer_norm_eps),
                        ff_class(dim=dim, mult=ff_mult, dropout=ff_dropout),
                        scale_class(),
                    ]
                )
                for _ in range(depth)
            ]
        )

        self.final_norm = (
            nn.LayerNorm(dim, eps=layer_norm_eps)
            if self.norm_type == "layer"
            else AdaptiveLayerNormProj(dim, dim_cond=dim_cond, eps=layer_norm_eps)
        )

    @staticmethod
    def expand_mask(mask: Optional[torch.Tensor] = None):
        if exists(mask):
            if mask.ndim == 2:  # B L
                mask = rearrange(mask, "b j -> b 1 1 j")
            elif mask.ndim == 3:  # B q_len k_len
                mask = rearrange(mask, "b i j -> b 1 i j")
        return mask

    @staticmethod
    def get_norm_class(norm_type, dim_cond):
        if norm_type == "layer":
            return nn.LayerNorm
        elif norm_type == "ada_proj":
            return partial(AdaptiveLayerNormProj, dim_cond=dim_cond)
        elif norm_type == "ada_embed":
            return AdaptiveLayerNormEmbed
        else:
            raise NotImplementedError(f"norm type {norm_type} is not implemented yet")

    @staticmethod
    def get_scale_class(scale_type, dim, dim_cond):
        if scale_type == "none":
            return nn.Identity
        elif scale_type == "ada_proj":
            return partial(AdaptiveScaleProj, dim=dim, dim_cond=dim_cond)
        elif scale_type == "ada_embed":
            return partial(AdaptiveScaleEmbed, dim=dim)
        else:
            raise NotImplementedError(f"scale type {scale_type} is not implemented yet")

    @staticmethod
    def get_ff_class(ff_type, kernel_size, groups):
        if ff_type == "conv":
            return partial(ConvFeedForward, kernel_size=kernel_size, groups=groups)
        elif ff_type == "linear":
            return FeedForward
        else:
            raise NotImplementedError(f"ff type {ff_type} is not implemented yet")

    def forward(
        self,
        x: torch.Tensor,
        context: Optional[torch.Tensor] = None,
        mask: Optional[torch.Tensor] = None,
        bias: Optional[torch.Tensor] = None,
        cond_input: Dict[str, torch.Tensor] = dict(),
    ):
        mask = self.expand_mask(mask)
        if exists(bias):
            assert bias.ndim == 4, f"bias must have 4 dimensions in Transformer, got {bias.ndim}"
            if exists(mask):
                assert (
                    mask.shape == bias.shape
                ), f"mask and bias must have the same shape, got {mask.shape} and {bias.shape}"

        for attn_norm, attn, attn_scale, ff_norm, ff, ff_scale in self.layers:
            residual = x
            if self.norm_type == "layer":
                x = attn_norm(x)
            else:
                x = attn_norm(x, cond=cond_input.get("attn_norm_cond", None))
            x = attn(x, context=context, mask=mask, bias=bias)
            if self.scale_type != "none":
                x = attn_scale(x, cond=cond_input.get("attn_scale_cond", None))
            x = x + residual

            residual = x
            if self.norm_type == "layer":
                x = ff_norm(x)
            else:
                x = ff_norm(x, cond=cond_input.get("ff_norm_cond", None))
            x = ff(x)
            if self.scale_type != "none":
                x = ff_scale(x, cond=cond_input.get("ff_scale_cond", None))
            x = x + residual

        final_output = (
            self.final_norm(x)
            if self.norm_type == "layer"
            else self.final_norm(x, cond=cond_input.get("final_norm_cond", None))
        )
        return final_output