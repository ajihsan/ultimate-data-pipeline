# Pre-S5b eval history

This page keeps the pre-S5b gate results for the root-cause record. The current
measurements are in [../jev_eval.md](../jev_eval.md) and
[../acceptance.md](../acceptance.md). Do not use these numbers as current.

## E2E pipeline gate

Stack: `make down-v && make up-model` (fresh volumes; decide wired to the
in-network model container `http://model:8081/v1/systemone`; host-side eval URL
127.0.0.1:8082). amd-smi (venv binary) during load: GFX 86 %, 43 W, edge
72 °C, VRAM 3966/8176 MB (model container ≈ 2.9 GB of it; desktop ≈ 0.9 GB,
jellyfin idle — no other GPU processes attached, co-tenancy zero). Model
container RSS 1.53 GiB (limit 5 GiB); torch peak allocation 2.5 GiB
(reserved 2526 MiB, matches S0).

### Steady — `udp-generate --profile steady --rate 100 --count 6000 --seed 7`

> **SUPERSEDED (S5b):** the latency rows below are the PRE-S5b numbers (the
> 500 ms budget miss was the single-consumer backlog, root-caused in the
> analysis below). The S5b retune (threads 4, checkpoint_limit 1024) brought
> them to fast-path p95 81 ms / model-path p95 467 ms — see the S8 acceptance
> table in README.md. Kept for the root-cause record.

| assertion | result |
|---|---|
| tickets_raw | 6000 ✓ |
| model_decisions = flagged count | 299 = 6000 − 5701 ✓ (5 % flagged, S4 fractions) |
| degraded | **0** ✓ (299/299 POSTs answered; 13 queue-full 503s recovered by decide's backoff retry) |
| fast+model split | 5701 fast / 299 model ✓ |
| consumer lag | drains to 0 ✓ |
| model-path latency `decided_at − ingested_at` | p50 1480 ms / p95 2557 ms / max 3042 ms — **over the 500 ms budget** |
| fast-path latency (same run, zero model) | p50 881 ms / p95 2006 ms |

Latency analysis: the 500 ms budget is not reachable at 6000 @ 100 ev/s with
this decide pipeline for ANY model — the fast path alone (no model at all)
shows p95 ≈ 2.0 s, i.e. the pipeline holds a standing ~1–2 s backlog
(single-consumer serial per-message cost ≈ 10 ms vs 100 ev/s publish). The
model's marginal cost over the fast path is ≈ +550 ms (one predict_batch
cohort). Model-server service latency itself: 183/196 ms p50/p95 through HTTP
(2 questions/ticket ≈ 165 ms single-flight floor — the S0 64 ms figure was
ONE question per decision; the wire contract sends two).

### Burst drain — `--profile demo --count 18000 --seed 7` (60 s, 300 ev/s)

> **SUPERSEDED (S5b):** the 48% degraded rate below is the PRE-S5b result
> (decide's 12-thread default oversubscribing the model path). S5b retuned
> decide (threads 4, checkpoint_limit 1024 — see connect/decide.yaml) and the
> same burst now finishes with **0/348 degraded**; the S8 acceptance table in
> README.md is the current measurement. Kept for the root-cause record.

| assertion | result |
|---|---|
| flagged count | 348 (≤360 budget ✓) |
| all flagged decided ≤60 s after burst end | ✓ — last decision 3.7 s BEFORE burst end |
| consumer lag after burst | 0 ✓ |
| degraded | **167/348 (48 %) — root-caused, see below** |

At 300 ev/s the decide pipeline is 2.5× oversubscribed: kafka_franz fetch
chunks accumulate 18–30 flagged messages per cycle and the async-capable http
processor fires them as ~18–30 CONCURRENT model calls — beyond one batch-8
cohort plus the bounded queue inside decide's 750 ms request budget
(~14 tickets/s single-GPU service). The client-side timeouts are
unrecoverable for decide (it retries only on 503, not on timeouts), hence the
degraded rows. Mitigation levers: slower burst ingest, decide-side pacing
(decide.yaml — out of S5 scope), or a faster model path (TileLang fast
kernels / torch.compile — unvalidated). S6 impact: degraded rows carry
p = 0 → below both thresholds → no false alerts, only a coverage gap during
extreme bursts.
