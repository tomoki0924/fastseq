# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Optimization for BART model"""

from typing import Optional, Tuple

import torch
from torch import nn
from torch import Tensor

from transformers.models.marian.modeling_marian import MarianAttention

from fastseq.logging import get_logger
from fastseq.utils.api_decorator import replace

logger = get_logger(__name__)

@replace(MarianAttention)
class MarianAttentionV2(MarianAttention):
    """"
    The Marian Model with a language modeling head. Can be used for MT.
    """

    def __init__(
        self,
        embed_dim: int,
        num_heads: int,
        dropout: float = 0.0,
        is_decoder: bool = False,  # otherwise self_attention
        bias: bool = True,
        num_beams: int = 1,
    ):
        super().__init__(
            embed_dim, num_heads, dropout, is_decoder, bias)
        self.num_beams = num_beams

    def forward(
        self,
        hidden_states: torch.Tensor,
        key_value_states: Optional[torch.Tensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        attention_mask: Optional[torch.Tensor] = None,
        layer_head_mask: Optional[torch.Tensor] = None,
        output_attentions: bool = False,
    ) -> Tuple[Tensor, Optional[Tensor]]:
        """Input shape: Batch x Time x Channel"""

        # if key_value_states are provided this layer is used as a cross-attention layer
        # for the decoder
        is_cross_attention = key_value_states is not None
        bsz, tgt_len, embed_dim = hidden_states.size()

        # get query proj
        query_states = self.q_proj(hidden_states) * self.scaling

        # get key, value proj
        if is_cross_attention and past_key_value is not None:
            # reuse k,v, cross_attentions
            key_states = past_key_value[0]
            value_states = past_key_value[1]
        elif is_cross_attention:
            # cross_attentions
            key_states = self._shape(self.k_proj(key_value_states), -1, bsz)
            value_states = self._shape(self.v_proj(key_value_states), -1, bsz)
        elif past_key_value is not None:
            # reuse k, v, self_attention
            key_states = self._shape(self.k_proj(hidden_states), -1, bsz)
            value_states = self._shape(self.v_proj(hidden_states), -1, bsz)
            key_states = torch.cat([past_key_value[0], key_states], dim=2)
            value_states = torch.cat([past_key_value[1], value_states], dim=2)
        else:
            # self_attention
            key_states = self._shape(self.k_proj(hidden_states), -1, bsz)
            value_states = self._shape(self.v_proj(hidden_states), -1, bsz)

        # Update cache
        if self.is_decoder:
            cache_bsz = (bsz // self.num_beams if self.is_decoder else bsz)
            # if cross_attention save Tuple(torch.Tensor, torch.Tensor) of all cross attention key/value_states.
            # Further calls to cross_attention layer can then reuse all cross-attention
            # key/value_states (first "if" case)
            if is_cross_attention:
                if past_key_value is None:
                    cache_shape = (cache_bsz, self.num_beams, self.num_heads, -1, self.head_dim)
                    key_states = key_states.view(cache_shape)[:, 0 : 1, :, :, :].contiguous()
                    value_states = value_states.view(cache_shape)[:, 0 : 1, :, :, :].contiguous()
                    past_key_value = (key_states, value_states)

            # if uni-directional self-attention (decoder) save Tuple(torch.Tensor, torch.Tensor) of
            # all previous decoder key/value_states. Further calls to uni-directional self-attention
            # can concat previous decoder key/value_states to current projected key/value_states (third "elif" case)
            if not is_cross_attention:
                cache_shape = (bsz, self.num_heads, -1, self.head_dim)
                key_states = key_states.view(cache_shape)
                value_states = value_states.view(cache_shape)
                past_key_value = (key_states, value_states)
        else:
            # if encoder bi-directional self-attention `past_key_value` is always `None`
            assert past_key_value is None

        proj_shape = (bsz * self.num_heads, -1, self.head_dim)
        query_states = self._shape(query_states, tgt_len, bsz).view(*proj_shape)

        if is_cross_attention:
            query_states = query_states.view(cache_bsz, self.num_beams, self.num_heads, tgt_len,
                       self.head_dim)   
            src_len = key_states.size(3)
            attn_weights = torch.einsum("bmhtd,bnhsd->bmhts", 
                                        query_states, key_states).reshape(-1, tgt_len, src_len)
            assert attn_weights.size() == (bsz * self.num_heads, tgt_len,
                                           src_len)
        else:
            key_states = key_states.view(*proj_shape)
            value_states = value_states.view(*proj_shape)
            src_len = key_states.size(1)
            attn_weights = torch.bmm(query_states, key_states.transpose(1, 2))
            assert attn_weights.size() == (bsz * self.num_heads, tgt_len,
                                           src_len)

        if attn_weights.size() != (bsz * self.num_heads, tgt_len, src_len):
            raise ValueError(
                f"Attention weights should be of size {(bsz * self.num_heads, tgt_len, src_len)}, but is {attn_weights.size()}"
            )

        if attention_mask is not None:
            if attention_mask.size() != (bsz, 1, tgt_len, src_len):
                raise ValueError(
                    f"Attention mask should be of size {(bsz, 1, tgt_len, src_len)}, but is {attention_mask.size()}"
                )
            attn_weights = attn_weights.view(bsz, self.num_heads, tgt_len, src_len) + attention_mask
            attn_weights = attn_weights.view(bsz * self.num_heads, tgt_len, src_len)

        attn_weights = nn.functional.softmax(attn_weights, dim=-1)

        if layer_head_mask is not None:
            if layer_head_mask.size() != (self.num_heads,):
                raise ValueError(
                    f"Head mask for a single layer should be of size {(self.num_heads,)}, but is {layer_head_mask.size()}"
                )
            attn_weights = layer_head_mask.view(1, -1, 1, 1) * attn_weights.view(bsz, self.num_heads, tgt_len, src_len)
            attn_weights = attn_weights.view(bsz * self.num_heads, tgt_len, src_len)

        if output_attentions:
            # this operation is a bit awkward, but it's required to
            # make sure that attn_weights keeps its gradient.
            # In order to do so, attn_weights have to be reshaped
            # twice and have to be reused in the following
            attn_weights_reshaped = attn_weights.view(bsz, self.num_heads, tgt_len, src_len)
            attn_weights = attn_weights_reshaped.view(bsz * self.num_heads, tgt_len, src_len)
        else:
            attn_weights_reshaped = None

        attn_probs = nn.functional.dropout(attn_weights, p=self.dropout, training=self.training)

        if is_cross_attention:
            attn_probs = attn_probs.view(cache_bsz, self.num_beams, self.num_heads, tgt_len, src_len)
            attn_output = torch.einsum("bmhts,bnhsd->bmhtd", attn_probs, value_states).reshape(-1, tgt_len, self.head_dim)
        else:
            attn_output = torch.bmm(attn_probs, value_states)
        
        if attn_output.size() != (bsz * self.num_heads, tgt_len, self.head_dim):
            raise ValueError(
                f"`attn_output` should be of size {(bsz, self.num_heads, tgt_len, self.head_dim)}, but is {attn_output.size()}"
            )

        attn_output = attn_output.view(bsz, self.num_heads, tgt_len, self.head_dim)
        attn_output = attn_output.transpose(1, 2)
        attn_output = attn_output.reshape(bsz, tgt_len, embed_dim)

        attn_output = self.out_proj(attn_output)

        return attn_output, attn_weights_reshaped, past_key_value
