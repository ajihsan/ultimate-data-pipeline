# Cascade fast-path rules (S4)

The decide pipeline (`connect/decide.yaml`) classifies every ticket with a
deterministic **first-match** rule table before any model call. Rules are
evaluated in priority order on the lowercased ticket text (substring match);
the first hit wins and no later rule is consulted.

| Priority | Verdict | Confidence | Keywords (lowercase substrings) |
|---|---|---|---|
| 1 | `scam_report` | 0.95 | `scam`, `fraud`, `phishing`, `fake`, `charged twice`, `malware` |
| 2 | `billing` | 0.90 | `invoice`, `charged`, `refund`, `subscription`, `declined`, `renewal`, `billing` |
| 3 | `bug` | 0.90 | `crash`, `error`, `broken`, `white screen`, `fail`, `stale`, `unreadable`, `duplicate`, `truncat`, `null pointer` |
| 4 | `access` | 0.90 | `login`, `password`, `locked`, `2fa`, `sso`, `expired`, `invite`, `session` |
| 5 | `feature_request` | 0.85 | `feature`, `request`, `would be nice`, `suggest`, `would love`, `please add`, `any chance`, `it would help`, `can we get` |
| 6 | `complaint` | 0.85 | `angry`, `terrible`, `worst`, `cancel`, `unacceptable`, `not happy`, `nothing loads`, `redesign` |
| 7 | *(no category → flag)* | 0.40 | ambiguous heuristics: garbled text (5+ consecutive consonants), >40% caps ratio, ≥5 combined `!`/`?`, or <12 chars |
| 8 | *(no category → flag)* | 0.45 | no rule matched |

Semantics:

- **Rules 1–6 (confident):** a `FastDecision` is emitted to
  `udp.fast_decisions` + topic `fast_decisions`. The model is never called.
- **Rules 7–8 (flagged):** the ticket is POSTed to `/v1/systemone`
  (`MODEL_ENDPOINT_URL`, 1500 ms timeout, 1 retry, backoff on 429/503). Every
  flagged event yields a `ModelDecision` row — the model's answers on success,
  or a degraded fallback (`escalate_probability`/`scam_probability` = 0,
  `degraded = true`, `answers = {"error": <message>}`) when the endpoint fails
  or answers a 200 the pipeline cannot read;
  degraded verdicts increment the `model_degraded_total` Prometheus counter.
  Concurrency is capped so that never happens in load: decide runs 4 pipeline
  workers (`pipeline.threads`), so concurrent model calls cap at 4 → the
  server's `predict_batch` cohorts stay ≤ 4 states (≤ ~310 ms ≪ 1500 ms).
- Keyword lists are tuned against the S2 synthetic generator's template pool
  (seeded benchmark: 6000 events, seed 7 → 4.98% flagged). Real traffic will
  need tuning; the table exists so the fast path stays auditable and cheap.

The YAML comment block in `connect/decide.yaml` is the source of truth the
pipeline executes; `docs/rules.md` is its human-readable mirror (kept in sync
by review — there is no doc-drift test).
