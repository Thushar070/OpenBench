import sys

import torch

import deeptext


def test_project_imports_with_python_315() -> None:
    assert deeptext.__name__ == "deeptext"
    assert sys.version_info[:2] == (3, 15)


def test_torch_tensor_arithmetic_on_cpu_and_available_device() -> None:
    cpu_values = torch.tensor([1.0, 2.0])
    assert torch.equal(cpu_values * 2, torch.tensor([2.0, 4.0]))

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    device_values = torch.tensor([1.0, 2.0], device=device)
    assert torch.equal((device_values * 2).cpu(), torch.tensor([2.0, 4.0]))
