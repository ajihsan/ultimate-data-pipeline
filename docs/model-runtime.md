# Model runtime and devices

The primary target is an AMD RX 6600 XT (gfx1032). The model runs in the
project `.venv` with TheRock ROCm wheels. The host does not need a system ROCm
install.

Measured on this GPU: 15.72 decisions/s. In-process p95 is 64.1 ms and HTTP
p95 is about 196 ms. The compose `model` profile runs the same runtime in a
container. It mounts the project `.venv`, the `.hf-cache` weights, `/dev/kfd`,
and the render device.

## Device matrix

| device | runtime | status |
|---|---|---|
| **AMD RX 6600 XT (gfx1032), this PC** | TheRock stable ROCm wheels (`torch 2.14.0+rocm10.1.0`) in the project `.venv`; compose `model` profile mounts `.venv` and `.hf-cache` and passes `/dev/kfd` plus the render GIDs. No system ROCm. | **primary, measured** (15.72 dec/s; p95 64.1 ms in process, 196 ms over HTTP) |
| NVIDIA | Install torch CUDA wheels into `.venv` and run the model container with `gpus: all` instead of `/dev/kfd`. The server code is device-agnostic; `cuda` works on both. | documented alternative |
| Apple Silicon | Point `laya.load` at `mps` (`DEFAULT_DEVICE` in `src/udp/model_server.py`). bf16 autocast differs on MPS; evaluate it before trusting the latency. | documented alternative, untested |
| ROCm/PyTorch baked into Docker | Bake the TheRock wheels into the model image. The base is pinned `python:3.12-bookworm@sha256:5560e9ab…` in `docker-compose.yaml`. | documented alternative |

The early spike log, install commands, and raw measurements are in
[history/s0-feasibility.md](history/s0-feasibility.md).
