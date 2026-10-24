# S0 Feasibility Spike — Laya `typed-decisions` on RX 6600 XT via TheRock wheels

**Date:** 2026-10-13 · **Branch:** `feat/realtime-v1-redpanda` · **Issue:** ajihsan/ultimate-data-pipeline#5

**Decision this spike answers:** can the `POST /v1/systemone` realtime decision path
(choice / score / noul → calibrated probabilities) sustain ≥ 5 decisions/sec on the
homelab's AMD RX 6600 XT (8 GB, gfx1032), as a **host process with no Docker and no
system-wide ROCm**?

**Verdict: YES — measured 15.7 dec/s sustained (3.1× the 5 dec/s floor).**
The TheRock-wheel local-venv path works natively on gfx1032: GPU enumerates, native
target (no `HSA_OVERRIDE_GFX_VERSION`), 19/19 `rocm-sdk` integrity tests pass, zero
errors and zero non-finite outputs across sustained runs.

Binding user priority: *works on my PC first, then other*. Docker route is demoted to
a documented alternative (below, image NOT pulled — 11.7 GB).

---

## Device matrix

| profile | status | throughput | per-decision p50 / p95 | VRAM / RAM | verdict vs ≥5 dec/s |
|---|---|---|---|---|---|
| **therock-local** (uv venv + TheRock wheels + laya, host process) | **MEASURED — PRIMARY** | **15.72 dec/s** (60 s sustained) | **63.6 ms / 64.1 ms** (max iter 64.6 ms) | 2.5 GiB VRAM reserved (torch) / 3.9 GiB kernel-side peak; RSS peak 3.37 GiB | **PASS** — 3.1× headroom |
| **cpu** (same venv, 12c/12t Ryzen 5 5600G, fp32) | **MEASURED** | 0.33 dec/s | 2912 ms / 4069 ms | RSS peak 3.26 GiB | FAIL for realtime (fallback / offline only) |
| **docker-rocm-pytorch** | DOCUMENTED-UNMEASURED (image deliberately not pulled) | — | — | +11.7 GB image | documented alternative only |
| **nvidia** | note only | — | — | — | out of scope: no NV hardware on host; CUDA wheels would be a one-line index swap |
| **apple-mps** | note only | — | — | — | out of scope: no Apple silicon on host; laya supports `mps` device natively |

Measured end-to-end ceiling through a mock HTTP service: GPU p95 (64.1 ms) + HTTP p95
(0.91 ms) ≈ **65 ms per decision → ~15.4 dec/s**. HTTP overhead is negligible
(0.7 ms p50); the model forward dominates.

## Checkpoint (verified on Hugging Face)

- **`convaiinnovations/laya-typed-decisions`** — primary, no substitution needed.
  ModernBERT-large encoder (28L / hidden 1024 / vocab 50368), **421 M params**,
  `model.safetensors` = 842,609,220 bytes fp32. Custom `LayaTypedDecisions` arch with
  decision head; question types `choice` / `score` / `noul` exactly match the
  `/v1/systemone` contract.
- Consumed through the **`laya` pip package (0.4.6)**:
  `laya.load("convaiinnovations/laya-typed-decisions", device="cuda")` →
  `agent.predict(state, questions)`. No `trust_remote_code` flag is necessary.
- Observed answer shape (score type): `{"type": "score", "score": 1.64, "legend": {...},
  "probabilities": {"0": 0.015, "1": 0.33, "2": 0.655}, "confidence": 0.36, ...}`.
- Device consistency check: same fixture scores 1.6401 (GPU, bf16-AMP) vs 1.6438
  (CPU, fp32) — logits agree across backends.
- Known checkpoint quirk (recorded at load, `RuntimeWarning`): shipped temperature
  `choice:11+ = 0.1006` is clamped to 0.5 by laya's `[0.5, 5]` sanity window → that
  one entry's confidence reads as uncalibrated. Does not affect the other entries.

## Precision note

laya enables **bf16 autocast on CUDA by default** (checkpoint `amp_dtype: bf16`);
CPU runs pure **fp32** (`LAYA_CPU_AMP` unset). A fp32-only CUDA forward is not offered
by laya's shipped path, so the GPU numbers ARE the shipped configuration. bf16 was
"only if trivial" per the spike plan — it is the default, i.e. trivial.

## Resolved environment (pinned)

Installed 2026-10-13, uv 0.12.10, CPython 3.12.14 (uv-managed), **stable channel**
index `https://stable.repo.amd.com/rocm/whl-next/` (nightly NOT needed):

```
torch==2.14.0+rocm10.1.0          # ~198.8 MiB wheel
rocm-sdk-core==10.1.0             # ~399.9 MiB
rocm-sdk-libraries==10.1.0        # ~248.7 MiB
rocm-sdk-device-gfx1032==10.1.0   # ~66.9 MiB
amd-torch-device-gfx1032==2.14.0+rocm10.1.0   # ~42.0 MiB
triton==3.8.0+git669b31ac.rocm10.1.0          # ~408.1 MiB
rocm==10.1.0 / rocm-bootstrap==0.3.0          # ~2 MiB (metapackage/bootstrap)
transformers==5.19.0   tokenizers==0.23.3   accelerate==1.15.0
laya==0.4.6            safetensors==0.8.0   numpy==2.5.4
fastapi==0.143.0       uvicorn==0.54.0      httpx==0.28.1
```

- TheRock wheels download ≈ **1.37 GiB / 9 m 47 s** (cold uv cache); venv unpacked = **5.6 GiB**
  (includes `hipcc`/`amdclang` toolchain in `.venv/bin`), HF model cache = **808 MiB**
  (`HF_HOME` kept inside the project dir).
- The venv ships GPU CLI tools: `amd-smi`, `rocminfo`, `rocm-sdk`, `rocm_agent_enumerator`
  (no legacy `rocm-smi` name — `amd-smi` is the modern tool).
- No sudo, no system ROCm, no `HSA_OVERRIDE_GFX_VERSION`, no video/render group needed
  (`/dev/kfd` + `/dev/dri/renderD128` are mode 666 on this host; amdgpu in-tree driver).

## Reproduction (exact commands)

```bash
# 0) uv (user-level, no sudo) — skip if present
curl -LsSf https://astral.sh/uv/install.sh | sh

cd /home/aji/Documents/dev/ultimate-data-pipeline
export HF_HOME="$PWD/.hf-cache"          # keep weights inside the project dir
export USE_TF=0                          # laya README: avoid TF-probe deadlock

# 1) venv + TheRock stable wheels (gfx1032 native target)
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python \
  --index-url https://stable.repo.amd.com/rocm/whl-next/ "torch[device-gfx1032]"

# 2) ML stack from PyPI (torch already satisfied by the ROCm build)
uv pip install --python .venv/bin/python transformers accelerate laya sentencepiece

# 3) verify
.venv/bin/rocm-sdk test                  # 19/19 OK
.venv/bin/python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
#   -> True AMD Radeon RX 6600 XT

# 4) benchmarks (scripts inline in Appendix A of this file)
.venv/bin/python /tmp/opencode/s0/s0_bench.py gpu 60
.venv/bin/python /tmp/opencode/s0/s0_bench.py cpu 60
.venv/bin/python /tmp/opencode/s0/s0_http_bench.py 1200
```

If the stable index ever drops gfx1032 artifacts, the fallback is the nightly index
`https://nightly.repo.amd.com/rocm/whl-next/` (same package layout — both verified
reachable on 2026-10-13; stable was sufficient).

## Docker alternative (documented, NOT measured, image NOT pulled)

For Docker-first deployments the upstream image is
**`rocm/pytorch:rocm10.1.0_ubuntu24.04_py3.12_pytorch_release_2.14.0`**
digest `sha256:099f59a3bee9c436f5cc57db6bc6076fdeda579c57e9e22ae40f856d5905b3e4`
(11.7 GB compressed; pushed 2026-10-05). Run it with `--device /dev/kfd
--device /dev/dri --group-add render` and, if the container's bundled ROCm lags the
host kernel's GPU, set `HSA_OVERRIDE_GFX_VERSION=10.3.0`. Unmeasured by design: the
primary path makes Docker unnecessary for the model service, and the image costs
11.7 GB on a 94%-full disk. **nVIDIA note:** swap the index for `https://download.pytorch.org/whl/cu128` (or PyPI torch) — one-line change, no other code path. **Apple
note:** laya accepts `device="mps"` natively; untested here (no Apple hardware).

## Co-tenancy observations (shared homelab — nothing was stopped or modified)

- Containers up during measurement: jellyfin, *arr stack (sonarr/radarr/bazarr/
  prowlarr/jackett/jellyseerr), flaresolverr, qbittorrent. `docker stats` snapshot:
  all ≈ 0 % CPU except qbittorrent 1.5 %. None touched the GPU.
- GPU baseline (idle): 1.0 GiB VRAM used (desktop/compositor), 25 W. Kernel-side VRAM
  peak during the run: 3.93 GiB → ~4.1 GiB headroom in 8 GiB.
- Interference check: GPU p95 − p50 spread was **0.46 ms over 944 decisions** —
  no measurable co-tenant jitter window during the 60 s run. Monitor per-decision
  p95 in production rather than assuming it stays this clean (jellyfin VAAPI
  transcodes can steal VRAM/compute without warning).

## PINNED conclusions

- **Default profile: `therock-local`** — confirmed stable (0 errors, 0 non-finite,
  max iteration 64.6 ms = no hangs) AND faster than cpu by ~48×.
- **Per-decision p95 budget: 65 ms** (measured 64.1 ms model + 0.9 ms HTTP).
- **Sustainable flagged-rate ceiling: ~15 dec/s** (60 s sustained, single stream,
  batch=1). Conservative service capacity with co-tenancy headroom: **10–12 dec/s**.
- **Verdict vs ≥ 5 dec/s floor: PASS** — 3.1× headroom measured; 2× even with the
  conservative haircut. CPU (0.33 dec/s) is NOT a realtime default; it is the
  emergency fallback only.
- Future lever (not exercised here): `agent.predict_batch()` shares one forward pass
  across states — should push throughput well past single-stream on the same GPU.

## Risks / follow-ups

1. TheRock wheels are AMD's dev-channel builds; pin the exact versions above and
   re-run `rocm-sdk test` after any upgrade. Stable channel had gfx1032 today.
2. Checkpoint temperature quirk (`choice:11+` clamped) — flag to S5 (quality gate).
3. Long-input behavior: fixture was 178 input tokens (state 138); `max_len 1024` is
   available but realtime budget should keep inputs ≤ 256 tokens as specced.
4. Disk is 94–95 % full (27 GiB free). This spike consumed ~6.4 GiB (venv + cache +
   uv cache). Budget for that in S1+, or prune the uv cache afterwards.
5. GPU ran warm (77 °C memory temp at idle snapshot) — watch thermals under sustained
   load in the homelab case airflow.

---

## Appendix A — benchmark scripts (as run)

**`s0_bench.py <gpu|cpu> <seconds>`** — sustained one-decision loop
(tokenizer + forward, batch=1, seq ≤ 256), instruments p50/p95, VRAM (torch peaks +
kernel sysfs sampler thread), RSS, NaN/error counters:

```python
#!/usr/bin/env python3
# (trimmed to essentials; full listing preserved in session /tmp/opencode/s0/)
import json, math, os, statistics, sys, threading, time
os.environ.setdefault("USE_TF", "0")
import laya, torch

MODE, SECONDS = sys.argv[1], int(sys.argv[2])
DEVICE = "cuda" if MODE == "gpu" else "cpu"
state = laya.email_state(subject="Invoice #4471 payment failed - retry or escalate?",
                         body="<~870-char AP escalation email>", sender="ap-automation@northwind.example")
q = {"urgency": laya.email_questions()["urgency"]}          # ONE decision per call
agent = laya.load("convaiinnovations/laya-typed-decisions", device=DEVICE)
for _ in range(5): agent.predict(state, q)                  # warmup
lat, n = [], 0
t0 = time.perf_counter()
while time.perf_counter() - t0 < SECONDS:
    i0 = time.perf_counter()
    out = agent.predict(state, q)
    if MODE == "gpu": torch.cuda.synchronize()
    lat.append(time.perf_counter() - i0); n += 1
    # finite-probability sanity check on out["answers"][*]["probabilities"]
p50 = statistics.median(lat) * 1000
p95 = statistics.quantiles(lat, n=20)[18] * 1000
peak_alloc = torch.cuda.max_memory_allocated() / 2**20 if MODE == "gpu" else 0
print(json.dumps({"mode": MODE, "decisions": n, "dec_per_s": n/(time.perf_counter()-t0),
                  "p50_ms": p50, "p95_ms": p95, "torch_peak_alloc_mib": peak_alloc}))
```

**`s0_http_bench.py [n]`** — FastAPI+uvicorn mock of `POST /v1/systemone` returning a
canned choice/score/noul distribution, httpx client, N sequential round-trips:

```python
app = FastAPI()
@app.post("/v1/systemone")
def systemone(_req: SystemOneReq): return CANNED_DIST
# uvicorn in a thread on 127.0.0.1:8971; httpx.Client posts N=1200 times, p50/p95
```
