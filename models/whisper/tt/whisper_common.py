import torch
import tt_lib


def linear(x, weight, bias=None):
    out_mem_config_l1 = tt_lib.tensor.MemoryConfig(True, tt_lib.tensor.BufferType.L1)

    weight = tt_lib.tensor.transpose(weight)
    x = tt_lib.tensor.matmul(x, weight)
    if bias is not None:
        x = tt_lib.tensor.bcast(
            x, bias, tt_lib.tensor.BcastOpMath.ADD, tt_lib.tensor.BcastOpDim.H
        )
    return x
