from abc import abstractmethod
import torch
from transformers import BloomForQuestionAnswering
import math
from torch.nn import functional as F

from pymetal import ttmetal as ttm
from python_api_testing.models.bert.embeddings import PytorchEmbeddings
from python_api_testing.models.bert.mha import TtMultiHeadAttentionModel
from python_api_testing.models.bert.ffn import TtFeedForwardModel
from python_api_testing.models.bert.bert_encoder import TtBertEncoder
from python_api_testing.fused_ops.linear import Linear as ttLinear
from python_api_testing.fused_ops.softmax import softmax as tt_softmax

from utility_functions import pad_activation, pad_weight, tilize_to_list, untilize, print_diff_argmax
from utility_functions import enable_binary_cache, enable_compile_cache, get_compile_cache_enabled, get_binary_cache_enabled
import numpy as np
from typing import Optional, Tuple, Union

def dropout_add(x, residual: torch.Tensor, prob: float, training: bool) -> torch.Tensor:
    out = F.dropout(x, p=prob, training=training)
    out = residual + out
    return out

def tt_dropout_add(x: torch.Tensor, residual: torch.Tensor, prob: float, training: bool) -> ttm.tensor.Tensor:


    tt_res = tilize_to_list(pad_activation(residual))
    tt_res = ttm.tensor.Tensor(tt_res, [1,1,64,64], ttm.tensor.DataType.BFLOAT16,  ttm.tensor.Layout.TILE, device)

    out = F.dropout(x, p=prob, training=training)
    tt_out = tilize_to_list(pad_activation(out))
    tt_out = ttm.tensor.Tensor(tt_out, [1,1,64,64], ttm.tensor.DataType.BFLOAT16,  ttm.tensor.Layout.TILE, device)

    total = ttm.tensor.add(tt_res, tt_out)

    return total


def tensor_torch2tt(torch_tensor):
    tt_tensor = tilize_to_list(pad_activation(torch_tensor))
    if len(torch_tensor.shape)==4:
        tt_tensor = ttm.tensor.Tensor(tt_tensor, torch_tensor.shape, ttm.tensor.DataType.BFLOAT16,  ttm.tensor.Layout.TILE, device)
    else:
        s1 = 1
        s2 = torch_tensor.shape[0]
        s3 = torch_tensor.shape[1]
        s4 = torch_tensor.shape[2]
        tt_tensor = ttm.tensor.Tensor(tt_tensor, [s1, s2, s3, s4], ttm.tensor.DataType.BFLOAT16,  ttm.tensor.Layout.TILE, device)


    return tt_tensor

def torch2tt_tensor(tt_tensor, pytorch_shape):
    if(len(pytorch_shape)==4):
        tt_out_host = tt_tensor.to(host)
        tt_out = untilize(torch.Tensor(tt_out_host.data()).reshape(*pytorch_shape))
        return tt_out
    else:
        s1 = 1
        s2 = pytorch_out.shape[0]
        s3 = pytorch_out.shape[1]
        s4 = pytorch_out.shape[2]
        out_shape = [s1, s2, s3, s4]
        tt_out_host = tt_tensor.to(host)
        tt_out = untilize(torch.Tensor(tt_out_host.data()).reshape(*out_shape))
        return tt_out


def tt_const_tensor(value, shape):
    if (len(shape)==4):
        number_tensor = torch.full(shape, value)
        tt_number_tensor = tilize_to_list(number_tensor)
        tt_number_tensor = ttm.tensor.Tensor(tt_number_tensor, number_tensor.shape, ttm.tensor.DataType.BFLOAT16, ttm.tensor.Layout.TILE, device)

        return tt_number_tensor
    else:
        s1 = 1
        s2 = shape[0]
        s3 = shape[1]
        s4 = shape[2]
        number_tensor = torch.full([s1, s2, s3, s4], value)
        tt_number_tensor = tilize_to_list(number_tensor)
        tt_number_tensor = ttm.tensor.Tensor(tt_number_tensor, number_tensor.shape, ttm.tensor.DataType.BFLOAT16, ttm.tensor.Layout.TILE, device)

        return tt_number_tensor


def tt_baddbmm(input, batch1, batch2, beta=1, alpha=1, out=None) -> ttm.tensor.Tensor:
    tt_batch1 = tensor_torch2tt(batch1)
    tt_batch2 = tensor_torch2tt(batch2)
    tt_input = tensor_torch2tt(input)
    tt_beta = tt_const_tensor(beta, input.shape)
    tt_alpha = tt_const_tensor(alpha, input.shape)

    res1 = ttm.tensor.mul(tt_beta, tt_input)
    res2 = ttm.tensor.matmul(tt_batch1, tt_batch2)
    res3 = ttm.tensor.mul(tt_alpha, res2)
    res4 = ttm.tensor.add(res1, res3)

    return res4



def tt_merge_heads(tt_x):

    num_heads = 32
    head_dim = 1024 // num_heads

    batch_size_and_num_heads, seq_length, _ = x.shape
    batch_size = batch_size_and_num_heads // num_heads

    reshaped = ttm.tensor.reshape(tt_x, batch_size, num_heads, seq_length, head_dim)
    p_reshaped = torch.Tensor(reshaped.to(host).data()).reshape(reshaped.shape())
    p_reshaped = torch.Tensor(x).reshape(batch_size, num_heads, seq_length, head_dim)

    # batch_size, num_heads, seq_length, head_dim -> batch_size, seq_length, num_heads, head_dim
    p_permuted = p_reshaped.permute(0, 2, 1, 3)

    permuted = ttm.tensor.Tensor(tilize_to_list(p_permuted), [batch_size, num_heads, seq_length, head_dim], ttm.tensor.DataType.BFLOAT16, ttm.tensor.Layout.TILE, device)

    third = num_heads*head_dim

    reshaped_2 = ttm.tensor.reshape(permuted, 1, batch_size, seq_length, num_heads*head_dim)

    res_reshaped_2 = tensor_torch2tt(reshaped_2)

    return res_reshaped_2

def split_heads(fused_qkv: torch.Tensor, num_heads, head_dim) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    Split the last dimension into (num_heads, head_dim) without making any copies, results share same memory
    storage as `fused_qkv`

    Args:
        fused_qkv (`torch.tensor`, *required*): [batch_size, seq_length, num_heads * 3 * head_dim]

    Returns:
        query: [batch_size, seq_length, num_heads, head_dim] key: [batch_size, seq_length, num_heads, head_dim]
        value: [batch_size, seq_length, num_heads, head_dim]
    """
    batch_size, seq_length, three_times_hidden_size = fused_qkv.shape
    fused_qkv = fused_qkv.view(batch_size, seq_length, num_heads, 3, head_dim)
    return fused_qkv[..., 0, :], fused_qkv[..., 1, :], fused_qkv[..., 2, :]

def merge_heads(self, x: torch.Tensor) -> torch.Tensor:
    """
    Merge heads together over the last dimenstion

    Args:
        x: (`torch.tensor`, *required*): [batch_size * num_heads, seq_length, head_dim]

    Returns:
        torch.tensor: [batch_size, seq_length, num_heads * head_dim]
    """
    # What we want to achieve is:
    # batch_size * num_heads, seq_length, head_dim -> batch_size, seq_length, num_heads * head_dim
    batch_size_and_num_heads, seq_length, _ = x.shape
    batch_size = batch_size_and_num_heads // self.num_heads

    # First view     to decompose the batch size
    # batch_size * num_heads, seq_length, head_dim -> batch_size, num_heads, seq_length, head_dim
    x = x.view(batch_size, self.num_heads, seq_length, self.head_dim)

    # batch_size, num_heads, seq_length, head_dim -> batch_size, seq_length, num_heads, head_dim
    x = x.permute(0, 2, 1, 3)

    # batch_size, seq_length, num_heads, head_dim -> batch_size, seq_length, num_heads * head_dim
    return x.reshape(batch_size, seq_length, self.num_heads * self.head_dim)

class BloomAttention(torch.nn.Module):
    def __init__(self):
        super().__init__()

        self.hidden_size = 64
        self.num_heads = 8
        self.head_dim = self.hidden_size // self.num_heads
        self.split_size = self.hidden_size
        self.hidden_dropout = 0.0
        self.inv_norm_factor = 0.0

        if self.head_dim * self.num_heads != self.hidden_size:
            raise ValueError(
                f"`hidden_size` must be divisible by num_heads (got `hidden_size`: {self.hidden_size} and `num_heads`:"
                f" {self.num_heads})."
            )

        # Layer-wise attention scaling
        self.inv_norm_factor = 1.0 / math.sqrt(self.head_dim)
        self.beta = 1.0

        self.query_key_value = torch.nn.Linear(self.hidden_size, 3 * self.hidden_size, bias=True)
        self.dense = torch.nn.Linear(self.hidden_size, self.hidden_size)
        self.attention_dropout = torch.nn.Dropout(0.0)

    def forward(
        self,
        hidden_states: torch.Tensor,
        residual: torch.Tensor,
        alibi: torch.Tensor,
        attention_mask: torch.Tensor,
        layer_past: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
        head_mask: Optional[torch.Tensor] = None,
        use_cache: bool = False,
        output_attentions: bool = False,
    ):
        fused_qkv = self.query_key_value(hidden_states)  # [batch_size, seq_length, 3 x hidden_size]

        # 3 x [batch_size, seq_length, num_heads, head_dim]
        (query_layer, key_layer, value_layer) = split_heads(fused_qkv, self.num_heads, self.head_dim)

        batch_size, q_length, _, _ = query_layer.shape

        query_layer = query_layer.transpose(1, 2).reshape(batch_size * self.num_heads, q_length, self.head_dim)
        key_layer = key_layer.permute(0, 2, 3, 1).reshape(batch_size * self.num_heads, self.head_dim, q_length)
        value_layer = value_layer.transpose(1, 2).reshape(batch_size * self.num_heads, q_length, self.head_dim)
        if layer_past is not None:
            past_key, past_value = layer_past
            # concatenate along seq_length dimension:
            #  - key: [batch_size * self.num_heads, head_dim, kv_length]
            #  - value: [batch_size * self.num_heads, kv_length, head_dim]
            key_layer = torch.cat((past_key, key_layer), dim=2)
            value_layer = torch.cat((past_value, value_layer), dim=1)

        _, _, kv_length = key_layer.shape

        # [batch_size * num_heads, q_length, kv_length]
        # we use `torch.Tensor.baddbmm` instead of `torch.baddbmm` as the latter isn't supported by TorchScript v1.11
        matmul_result = alibi.baddbmm(
            batch1=query_layer,
            batch2=key_layer,
            beta=self.beta,
            alpha=self.inv_norm_factor,
        )

        # change view to [batch_size, num_heads, q_length, kv_length]
        attention_scores = matmul_result.view(batch_size, self.num_heads, q_length, kv_length)

        # cast attention scores to fp32, compute scaled softmax and cast back to initial dtype - [batch_size, num_heads, q_length, kv_length]
        input_dtype = attention_scores.dtype
        # `float16` has a minimum value of -65504.0, whereas `bfloat16` and `float32` have a minimum value of `-3.4e+38`
        if input_dtype == torch.float16:
            attention_scores = attention_scores.to(torch.float)
        attn_weights = torch.masked_fill(attention_scores, attention_mask, torch.finfo(attention_scores.dtype).min)
        attention_probs = F.softmax(attn_weights, dim=-1, dtype=torch.float32).to(input_dtype)

        # [batch_size, num_heads, q_length, kv_length]
        attention_probs = self.attention_dropout(attention_probs)

        if head_mask is not None:
            attention_probs = attention_probs * head_mask

        # change view [batch_size x num_heads, q_length, kv_length]
        attention_probs_reshaped = attention_probs.view(batch_size * self.num_heads, q_length, kv_length)

        # matmul: [batch_size * num_heads, q_length, head_dim]
        context_layer = torch.bmm(attention_probs_reshaped, value_layer)

        # change view [batch_size, num_heads, q_length, head_dim]
        context_layer = self.merge_heads(context_layer)

        # aggregate results across tp ranks. See here: https://github.com/pytorch/pytorch/issues/76232
        if self.pretraining_tp > 1 and self.slow_but_exact:
            slices = self.hidden_size / self.pretraining_tp
            output_tensor = torch.zeros_like(context_layer)
            for i in range(self.pretraining_tp):
                output_tensor = output_tensor + F.linear(
                    context_layer[:, :, int(i * slices) : int((i + 1) * slices)],
                    self.dense.weight[:, int(i * slices) : int((i + 1) * slices)],
                )
        else:
            output_tensor = self.dense(context_layer)

        output_tensor = dropout_add(output_tensor, residual, self.hidden_dropout, self.training)

        outputs = (output_tensor, present)
        if output_attentions:
            outputs += (attention_probs,)

        return outputs

class TtBloomAttention(torch.nn.Module):
    def __init__(self, sd, device):
        super().__init__()

        self.hidden_size = 64
        self.num_heads = 8
        self.head_dim = self.hidden_size // self.num_heads
        self.split_size = self.hidden_size
        self.hidden_dropout = 0.0
        self.inv_norm_factor = 0.0


        if self.head_dim * self.num_heads != self.hidden_size:
            raise ValueError(
                f"`hidden_size` must be divisible by num_heads (got `hidden_size`: {self.hidden_size} and `num_heads`:"
                f" {self.num_heads})."
            )

        weight_q = tilize_to_list(pad_weight(sd[f"transformer.h.0.self_attention.query_key_value.weight"]))
        bias_q= tilize_to_list(pad_weight(sd[f"transformer.h.0.self_attention.query_key_value.bias"]))

        weight_d = tilize_to_list(pad_weight(sd[f"transformer.h.0.self_attention.dense.weight"]))
        bias_d = tilize_to_list(pad_weight(sd[f"transformer.h.0.self_attention.dense.bias"]))

        # Layer-wise attention scaling
        self.inv_norm_factor = 1.0 / math.sqrt(self.head_dim)
        self.beta = 1.0

        self.query_key_value = ttLinear(self.hidden_size, 3 * self.hidden_size, weight_q, bias_q, device)

        self.dense = ttLinear(self.hidden_size, self.hidden_size, weight_d, bias_d, device)
        self.attention_dropout = torch.nn.Dropout(0.0)


    def forward(
        self,
        hidden_states: torch.Tensor,
        residual: torch.Tensor,
        alibi: torch.Tensor,
        attention_mask: torch.Tensor,
        layer_past: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
        head_mask: Optional[torch.Tensor] = None,
        use_cache: bool = False,
        output_attentions: bool = False,
    ):

        pb = BloomAttention()

        s1 = hidden_states.shape[0]
        s2 = hidden_states.shape[1]
        s3 = hidden_states.shape[2]
        s4 = hidden_states.shape[3]

        tt_hidden_states = tilize_to_list(pad_activation(hidden_states))
        tt_hidden_states = ttm.tensor.Tensor(tt_hidden_states, [s1, s2, s3, s4], ttm.tensor.DataType.BFLOAT16,  ttm.tensor.Layout.TILE, device)

        tt_fused_qkv = self.query_key_value(tt_hidden_states)  # [batch_size, seq_length, 3 x hidden_size]

        f_shapes = tt_fused_qkv.shape()
        fused_qkv = torch.Tensor(tt_fused_qkv.to(host).data()).reshape([f_shapes[1], f_shapes[2], f_shapes[3]])

        # 3 x [batch_size, seq_length, num_heads, head_dim]
        (query_layer, key_layer, value_layer) = split_heads(fused_qkv, self.num_heads, self.head_dim)

        batch_size, q_length, _, _ = query_layer.shape

        #p_reshaped_query_layer = torch.Tensor(fused_qkv).reshape(1, batch_size, seq * self.num_heads,  q_length, self.head_dim)
        #query_layer = query_layer.transpose(1, 2).reshape(batch_size * self.num_heads, q_length, self.head_dim)
        s1 = query_layer.shape[0]
        s2 = query_layer.shape[1]
        s3 = query_layer.shape[2]
        s4 = query_layer.shape[3]
        print("SHAPE")
        print(s1)
        print(s2)
        print(s3)
        print(s4)

        tt_query_layer = tilize_to_list(pad_activation(query_layer))
        tt_query_layer = ttm.tensor.Tensor(tt_query_layer, [s1, s2, s3, s4], ttm.tensor.DataType.BFLOAT16,  ttm.tensor.Layout.TILE, device)

        tt_transposed_query_layer = ttm.tensor.transpose(tt_query_layer)
        tt_reshaped_query_layer = ttm.tensor.reshape(tt_transposed_query_layer, 1, batch_size * self.num_heads, q_length, self.head_dim)

        #key_layer = key_layer.permute(0, 2, 3, 1).reshape(batch_size * self.num_heads, self.head_dim, q_length)
        key_layer = key_layer.permute(0, 2, 3, 1)
        s1 = key_layer.shape[0]
        s2 = key_layer.shape[1]
        s3 = key_layer.shape[2]
        s4 = key_layer.shape[3]

        tt_key_layer = tilize_to_list(pad_activation(key_layer))
        tt_key_layer = ttm.tensor.Tensor(tt_key_layer, [s1, s2, s3, s4], ttm.tensor.DataType.BFLOAT16,  ttm.tensor.Layout.TILE, device)
        tt_reshaped_key_layer = ttm.tensor.reshape(tt_key_query_layer, 1, batch_size * self.num_heads, self.head_dim, q_length)

        #value_layer = value_layer.transpose(1, 2).reshape(batch_size * self.num_heads, q_length, self.head_dim)
        s1 = value_layer.shape[0]
        s2 = value_layer.shape[1]
        s3 = value_layer.shape[2]
        s4 = value_layer.shape[3]

        tt_value_layer = tilize_to_list(pad_activation(value_layer))
        tt_value_layer = ttm.tensor.Tensor(tt_value_layer, [s1, s2, s3, s4], ttm.tensor.DataType.BFLOAT16,  ttm.tensor.Layout.TILE, device)

        tt_transposed_value_layer = ttm.tensor.transpose(tt_value_layer)
        tt_reshaped_value_layer = ttm.tensor.reshape(tt_transposed_value_layer, 1, batch_size * self.num_heads, q_length, self.head_dim)

        # [batch_size * num_heads, q_length, kv_length]
        # we use `torch.Tensor.baddbmm` instead of `torch.baddbmm` as the latter isn't supported by TorchScript v1.11
        p_reshaped_query_layer = torch.Tensor(tt_reshaped_query_layer.to(host).data()).reshape(p_reshaped_query_layer.shape())
        p_reshaped_query_layer = torch.Tensor(p_reshaped_query_layer).reshape(1, batch_size * self.num_heads,  q_length, self.head_dim)

        p_reshaped_key_layer = torch.Tensor(tt_reshaped_key_layer.to(host).data()).reshape(p_reshaped_key_layer.shape())
        p_reshaped_key_layer = torch.Tensor(p_reshaped_key_layer).reshape(1, batch_size * self.num_heads, self.head_dim, q_length)

        matmul_result = tt_baddbmm(alibi, batch1=p_reshaped_query_layer, batch2=p_reshaped_key_layer, beta=self.beta, alpha=self.inv_norm_facto)

        # change view to [batch_size, num_heads, q_length, kv_length]
        tt_attention_scores = ttm.tensor.reshape(tt_matmul_result, 1, batch_size, self.num_heads, q_length, kv_length)
        p_attention_scores = torch2tt_tensor(tt_attention_scores, tt_attention_scores.shape())

        attention_scores = p_attention_scores.to(torch.float)

        attn_weights = torch.masked_fill(attention_scores, attention_mask, torch.finfo(attention_scores.dtype).min)


        tt_attn_weights = tensor_torch2tt(attn_weights)

        tt_attention_probs = tt_softmax.softmax(tt_attn_weights)

        #TO BE DONE
        # [batch_size, num_heads, q_length, kv_length]
        #attention_probs = self.attention_dropout(attention_probs)

        if head_mask is not None:
            tt_head_mask =  tensor_torch2tt(head_mask)
            tt_attention_probs = ttm.mul(tt_attention_probs, head_mask)

        # change view [batch_size x num_heads, q_length, kv_length]
        tt_attention_probs_reshaped = ttm.tensor.reshape(tt_attention_probs, 1, batch_size * self.num_heads, q_length, kv_length)

        # matmul: [batch_size * num_heads, q_length, head_dim]
        tt_context_layer = ttm.tensor.matmul(tt_attention_probs_reshaped, value_layer)

        # change view [batch_size, num_heads, q_length, head_dim]
        merged_context_layer = tt_merge_heads(tt_context_layer)

        output_tensor = self.dense(merged_context_layer)

        output_tensor = tt_dropout_add(output_tensor, residual, self.hidden_dropout, False)

        outputs = ttm.tensor.add(output_tensor, attention_probs)

        return outputs

        return tt_reshaped_matmul_result

def run_bloom_attention_inference():
    hugging_bloom_reference_model = BloomForQuestionAnswering.from_pretrained("bigscience/bloom-560m", torchscript=False)

    tbloom = TtBloomAttention(hugging_bloom_reference_model.state_dict(), device)

    # Prepare input
    torch.manual_seed(0)

    hidden_states = torch.rand(1024, 3072)
    residual = torch.rand(1, 192, 192, 64)
    alibi = torch.rand(1, 192, 192, 64)
    attention_mask = torch.rand(1, 192, 192, 64)

    tt_out = tbloom.forward(hidden_states, residual, alibi, attention_mask).to(host)
    print("Finished calc")

    tt_out = untilize(torch.Tensor(tt_out.data()).reshape(*pytorch_out.shape))

    pbloom = BloomAttention()

    pytorch_out = pbloom.forward(hidden_states, residual, alibi, attention_mask)

    assert np.allclose(pytorch_out.detach().numpy(), tt_out.numpy(), 1e-5, 0.17)

if __name__ == "__main__":
    # Initialize the device
    device = ttm.device.CreateDevice(ttm.device.Arch.GRAYSKULL, 0)
    ttm.device.InitializeDevice(device)
    host = ttm.device.GetHost()
    run_bloom_attention_inference()
    ttm.device.CloseDevice(device)
