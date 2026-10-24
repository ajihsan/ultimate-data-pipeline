# Cascade fast-path rules

The decide pipeline (`connect/decide.yaml`) classifies every ticket with a
deterministic **first-match** rule table before any model call. Rules run in
priority order on the lowercased ticket text. Each rule uses a substring match.
The first hit wins, and no later rule runs.

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

- **Rules 1–6 (confident):** decide emits a `FastDecision` to
  `udp.fast_decisions` and topic `fast_decisions`. It never calls the model.

- **Rules 7–8 (flagged):** decide POSTs the ticket to `/v1/systemone`
  (`MODEL_ENDPOINT_URL`, 1500 ms timeout, 1 retry, backoff on 429/503). Every
  flagged event yields a `ModelDecision` row, with the model answers on success
  or a degraded fallback. The fallback sets `escalate_probability` and
  `scam_probability` to 0, `degraded = true`, and
  `answers = {"error": <message>}`. It applies when the endpoint fails or
  returns a 200 that the pipeline cannot read. Decide runs 4 pipeline workers
  (`pipeline.threads`), so concurrent model calls cap at 4. The server's
  `predict_batch` cohorts stay at 4 states or fewer (≤ ~310 ms, below the
  1500 ms timeout).

- Keyword lists are tuned against the synthetic generator's template pool. The
  seeded benchmark used 6000 events and seed 7, and flagged 4.98%. Real traffic
  needs new tuning. The table keeps the fast path auditable and cheap.

Degraded verdicts increment the `model_degraded_total` counter.

The mapping in `connect/decide.yaml` is the source of truth. This table is the
human-readable mirror. `tests/test_rules_doc.py` checks that the category,
confidence, and keyword lists match.
