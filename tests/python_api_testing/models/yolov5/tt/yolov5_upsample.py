import torch
import tt_lib

from loguru import logger
from tt_lib.fallback_ops import fallback_ops
from python_api_testing.models.utility_functions_new import (
    torch2tt_tensor,
    tt2torch_tensor,
)


class TtYolov5Upsample(torch.nn.Module):
    # Standard bottleneck
    def __init__(
        self,
        state_dict,
        base_address,
        device,
        size=None,
        scale_factor=None,
        mode="nearest",
    ):
        super().__init__()
        self.device = device
        self.upsample = torch.nn.Upsample(
            size=size, scale_factor=scale_factor, mode=mode
        )

    def forward(self, x):
        x = tt2torch_tensor(x)
        x = self.upsample(x)
        x = torch2tt_tensor(
            x, tt_device=self.device, tt_layout=tt_lib.tensor.Layout.ROW_MAJOR
        )

        return x
