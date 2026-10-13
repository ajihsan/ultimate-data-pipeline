"""Decision-quality / latency eval for the Laya model server (S5).

Generates labeled synthetic tickets (``category_hint`` = ground truth, never
published on the wire), drives them through the RUNNING model server over
HTTP, and reports choice accuracy, Brier scores, latency p50/p95, throughput,
and the p(scam)/p(escalate) distributions with threshold sweeps that back the
notify thresholds in .env.example.

Results land in ``docs/jev_eval.md`` + ``docs/jev_eval_results.json``
(``--out-dir``). Stop rule: choice accuracy < 50% (coin-flip level) writes the
report with a MODEL QUALITY RISK banner and exits 2 — thresholds must not be
tuned to hide a broken model.

escalate_now has no ground-truth label in the synthetic stream, so its Brier
score is computed against a documented proxy (label = 1 iff
``category_hint == "scam_report"`` — the only tickets describing an incident
warranting immediate human attention); treat it as a discrimination check.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.metadata
import json
import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from udp.generator.tickets import NOMINAL_RATE_EPS, STEADY, GeneratorConfig, Ticket, TicketGenerator

DEFAULT_COUNT = 200
DEFAULT_RATE = 10.0  # sustained decisions/s; 0 = closed-loop
DEFAULT_WORKERS = 8

MIN_LABELED = 150  # the eval refuses to run below this many labeled tickets

CHECKPOINT_ID = "convaiinnovations/laya-typed-decisions"

WIRE_QUESTIONS: dict[str, object] = {
    "escalate_now": {"type": "noul"},
    "category": {
        "type": "choice",
        "options": [
            "complaint",
            "scam_report",
            "billing",
            "bug",
            "access",
            "feature_request",
        ],
    },
}
"""The EXACT question payload decide.yaml sends; do not deviate."""

DEFAULT_ESCALATE_P = 0.75
DEFAULT_SCAM_P = 0.35

STOP_ACCURACY = 0.5  # choice accuracy below this is a coin flip → stop


@dataclass
class Record:
    """One completed /v1/systemone round trip (status kept so failures stay visible)."""

    label: str | None
    status: int
    latency_ms: float
    infer_ms: float
    choice: str | None
    probabilities: dict[str, float]
    p_escalate: float | None


@dataclass
class EvalStats:
    """Aggregate metrics of one eval run."""

    accuracy: float
    per_label: dict[str, dict[str, float]]
    brier_choice: float
    brier_escalate: float
    latency_p50: float
    latency_p95: float
    infer_p50: float
    infer_p95: float
    throughput: float
    errors: int
    scam_table: dict[str, dict[str, float]]
    scam_confusion: dict[str, int]
    scam_sweep: dict[str, dict[str, float]]
    best_scam_cut: float
    best_scam_f1: float
    escalate_sweep: dict[str, dict[str, float]]
    stop: bool = False


@dataclass
class SweepPoint:
    """One threshold's confusion counts (label = ground truth, pred = p ≥ cut)."""

    cut: float
    tp: int
    fp: int
    fn: int
    tn: int

    def f1(self) -> float:
        """Harmonic mean of precision and recall (0.0 when no positives predicted)."""
        if self.tp == 0:
            return 0.0
        precision = self.tp / (self.tp + self.fp)
        recall = self.tp / (self.tp + self.fn)
        return 2 * precision * recall / (precision + recall)


@dataclass
class EvalConfig:
    """All knobs for one eval run."""

    count: int = DEFAULT_COUNT
    rate: float = DEFAULT_RATE
    seed: int = 7
    workers: int = DEFAULT_WORKERS
    url: str = "http://127.0.0.1:8082/v1/systemone"
    out_dir: Path = Path("docs")


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile of ``values`` (empty list → 0.0)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def _brier_choice(probabilities: dict[str, float], label: str) -> float:
    """Brier score of the choice distribution against a one-hot label."""
    return sum((p - (1.0 if key == label else 0.0)) ** 2 for key, p in probabilities.items())


def _labeled_tickets(config: EvalConfig) -> tuple[list[Ticket], list[Ticket]]:
    """Generate ``count`` tickets, topping up to ``MIN_LABELED`` labeled ones.

    Returns ``(labeled, ambiguous)``; only ``seed`` shapes the sequence (rate/
    profile are pacing knobs the pure event stream ignores).
    """
    generator = TicketGenerator(
        GeneratorConfig(seed=config.seed, rate=NOMINAL_RATE_EPS, profile=STEADY)
    )
    tickets = list(generator.events(config.count))
    labeled = [t for t in tickets if t.category_hint is not None]
    if len(labeled) < MIN_LABELED:
        for extra in generator.stream():
            tickets.append(extra)
            if extra.category_hint is not None:
                labeled.append(extra)
            if len(labeled) >= MIN_LABELED or len(tickets) >= config.count + 1000:
                break
    ambiguous = [t for t in tickets if t.category_hint is None]
    return labeled, ambiguous


def _post_one(client: httpx.Client, url: str, state: str) -> Record:
    """POST one ticket to the model server and shape the raw answer."""
    payload = {"model": "laya", "state": state, "questions": WIRE_QUESTIONS}
    started = time.perf_counter()
    try:
        resp = client.post(url, json=payload, headers={"Content-Type": "application/json"})
        latency_ms = (time.perf_counter() - started) * 1000.0
        infer_ms = 0.0
        for part in resp.headers.get("Server-Timing", "").split(";"):
            if part.startswith("dur="):
                infer_ms = float(part[4:])
        if resp.status_code != 200:
            return Record(None, resp.status_code, latency_ms, infer_ms, None, {}, None)
        body = resp.json()["answers"]
        category = body["category"]
        probabilities = {str(k): float(v) for k, v in category["probabilities"].items()}
        escalate = body.get("escalate_now", {})
        p_escalate = float(escalate["noul"]) if "noul" in escalate else None
        return Record(
            label=None,
            status=resp.status_code,
            latency_ms=latency_ms,
            infer_ms=infer_ms,
            choice=str(category["choice"]),
            probabilities=probabilities,
            p_escalate=p_escalate,
        )
    except httpx.HTTPError as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        print(f"udp-eval: request failed: {exc}", file=sys.stderr)
        return Record(None, 0, latency_ms, 0.0, None, {}, None)


def _run(client: httpx.Client, tickets: list[Ticket], config: EvalConfig) -> list[Record]:
    """Drive tickets through the server concurrently, paced at ``rate`` (0 = closed-loop)."""
    records: list[Record] = []

    def work(ticket: Ticket) -> Record:
        record = _post_one(client, config.url, ticket.text)
        record.label = ticket.category_hint
        return record

    interval = 0.0 if config.rate <= 0 else 1.0 / config.rate
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.workers) as pool:
        futures = []
        deadline = time.monotonic()
        for ticket in tickets:
            futures.append(pool.submit(work, ticket))
            deadline += interval
            if interval > 0:
                delay = deadline - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        records = [fut.result() for fut in futures]
    return records


def _confusion(records: list[Record], key: str, cut: float) -> SweepPoint:
    """Confusion counts for one probability stream (``scam`` or ``escalate``) at one cutoff."""
    point = SweepPoint(cut=cut, tp=0, fp=0, fn=0, tn=0)
    for record in records:
        if record.label is None or record.status != 200:
            continue
        if key == "scam":
            p = record.probabilities.get("scam_report", 0.0)
            truth = record.label == "scam_report"
        else:
            if record.p_escalate is None:
                continue
            p = record.p_escalate
            truth = record.label == "scam_report"  # documented proxy label
        predicted = p >= cut
        if predicted and truth:
            point.tp += 1
        elif predicted and not truth:
            point.fp += 1
        elif not predicted and truth:
            point.fn += 1
        else:
            point.tn += 1
    return point


def _sweep(records: list[Record], key: str, cuts: list[float]) -> dict[str, dict[str, float]]:
    """Precision/recall/F1 plus confusion counts at each cutoff for one stream."""
    sweep: dict[str, dict[str, float]] = {}
    for cut in cuts:
        point = _confusion(records, key, cut)
        precision = point.tp / (point.tp + point.fp) if point.tp + point.fp else 0.0
        recall = point.tp / (point.tp + point.fn) if point.tp + point.fn else 0.0
        sweep[f"{cut:.2f}"] = {
            "tp": float(point.tp),
            "fp": float(point.fp),
            "fn": float(point.fn),
            "tn": float(point.tn),
            "precision": precision,
            "recall": recall,
            "f1": point.f1(),
        }
    return sweep


def _stats(records: list[Record], wall_s: float) -> EvalStats:
    """Aggregate records into the eval metrics."""
    ok = [r for r in records if r.status == 200]
    errors = len(records) - len(ok)
    hits = sum(1 for r in ok if r.choice == r.label)
    accuracy = hits / len(ok) if ok else 0.0

    per_label: dict[str, dict[str, float]] = {}
    for r in ok:
        assert r.label is not None
        bucket = per_label.setdefault(r.label, {"n": 0, "hits": 0})
        bucket["n"] += 1
        bucket["hits"] += 1 if r.choice == r.label else 0
    for bucket in per_label.values():
        bucket["accuracy"] = bucket["hits"] / bucket["n"]

    brier_choice = (
        sum(_brier_choice(r.probabilities, r.label) for r in ok if r.label is not None) / len(ok)
        if ok
        else 0.0
    )

    esc_pairs = [
        (r.p_escalate, 1.0 if r.label == "scam_report" else 0.0)
        for r in ok
        if r.p_escalate is not None and r.label is not None
    ]
    brier_escalate = sum((p - y) ** 2 for p, y in esc_pairs) / len(esc_pairs) if esc_pairs else 0.0

    scam_cuts = [round(0.1 * i, 2) for i in range(1, 10)] + [DEFAULT_SCAM_P]
    best = max((_confusion(ok, "scam", cut) for cut in scam_cuts), key=SweepPoint.f1)
    default_point = _confusion(ok, "scam", DEFAULT_SCAM_P)

    return EvalStats(
        accuracy=accuracy,
        per_label={k: {kk: vv for kk, vv in v.items()} for k, v in sorted(per_label.items())},
        brier_choice=brier_choice,
        brier_escalate=brier_escalate,
        latency_p50=_percentile([r.latency_ms for r in ok], 0.50),
        latency_p95=_percentile([r.latency_ms for r in ok], 0.95),
        infer_p50=_percentile([r.infer_ms for r in ok], 0.50),
        infer_p95=_percentile([r.infer_ms for r in ok], 0.95),
        throughput=len(ok) / wall_s if wall_s > 0 else 0.0,
        errors=errors,
        scam_table={
            "scam_report": _ps_stats(ok, "scam_report"),
            "other": _ps_stats(ok, None),
        },
        scam_confusion={
            "tp": default_point.tp,
            "fp": default_point.fp,
            "fn": default_point.fn,
            "tn": default_point.tn,
        },
        scam_sweep=_sweep(ok, "scam", [round(0.1 * i, 2) for i in range(1, 10)]),
        best_scam_cut=best.cut,
        best_scam_f1=best.f1(),
        escalate_sweep=_sweep(ok, "escalate", [round(0.1 * i, 2) for i in range(1, 10)]),
        stop=accuracy < STOP_ACCURACY,
    )


def _ps_stats(records: list[Record], label: str | None) -> dict[str, float]:
    """p(scam) summary (n/mean/p5/p50/p95) for one class, or all non-scam pooled."""
    if label is None:
        values = [
            r.probabilities.get("scam_report", 0.0)
            for r in records
            if r.label is not None and r.label != "scam_report"
        ]
    else:
        values = [r.probabilities.get(label, 0.0) for r in records if r.label == label]
    if not values:
        return {"n": 0.0, "mean": 0.0, "p5": 0.0, "p50": 0.0, "p95": 0.0}
    return {
        "n": float(len(values)),
        "mean": sum(values) / len(values),
        "p5": _percentile(values, 0.05),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
    }


def _escalate_ps(records: list[Record]) -> dict[str, dict[str, float]]:
    """escalate_now p(true) distribution: labeled split by the scam proxy + ambiguous pool."""
    pools: dict[str, list[float]] = {
        "scam_report": [],
        "other": [],
        "ambiguous": [],
    }
    for r in records:
        if r.status != 200 or r.p_escalate is None:
            continue
        if r.label is None:
            pools["ambiguous"].append(r.p_escalate)
        elif r.label == "scam_report":
            pools["scam_report"].append(r.p_escalate)
        else:
            pools["other"].append(r.p_escalate)
    return {name: _percentile_row(values) for name, values in pools.items()}


def _percentile_row(values: list[float]) -> dict[str, float]:
    """n/mean/p5/p50/p95 summary of one p-value pool."""
    if not values:
        return {"n": 0.0, "mean": 0.0, "p5": 0.0, "p50": 0.0, "p95": 0.0}
    return {
        "n": float(len(values)),
        "mean": sum(values) / len(values),
        "p5": _percentile(values, 0.05),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
    }


def _versions() -> dict[str, str]:
    """Resolved runtime versions (metadata only — no heavy imports)."""
    out = {"python": platform.python_version()}
    for dist in ("laya", "torch", "transformers"):
        try:
            out[dist] = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            out[dist] = "not-installed"
    return out


def _write_report(
    config: EvalConfig,
    versions: dict[str, str],
    labeled_n: int,
    ambiguous_n: int,
    stats: EvalStats,
    escalate_dist: dict[str, dict[str, float]],
) -> Path:
    """Write ``jev_eval.md`` + ``jev_eval_results.json`` into ``config.out_dir``."""
    config.out_dir.mkdir(parents=True, exist_ok=True)
    results = {
        "versions": versions,
        "checkpoint": CHECKPOINT_ID,
        "config": {
            "count": config.count,
            "rate": config.rate,
            "seed": config.seed,
            "workers": config.workers,
            "url": config.url,
            "labeled": labeled_n,
            "ambiguous": ambiguous_n,
        },
        "accuracy": stats.accuracy,
        "per_label": stats.per_label,
        "brier": {"choice": stats.brier_choice, "escalate_now": stats.brier_escalate},
        "latency_ms": {
            "p50": stats.latency_p50,
            "p95": stats.latency_p95,
            "infer_p50": stats.infer_p50,
            "infer_p95": stats.infer_p95,
        },
        "throughput_dec_per_s": stats.throughput,
        "errors": stats.errors,
        "scam": {
            "distribution": stats.scam_table,
            "confusion_at_default": stats.scam_confusion,
            "best_cut": stats.best_scam_cut,
            "best_cut_f1": stats.best_scam_f1,
        },
        "escalate_distribution": escalate_dist,
        "scam_sweep": stats.scam_sweep,
        "escalate_sweep": stats.escalate_sweep,
        "stop_rule": stats.stop,
        "defaults": {
            "THRESHOLD_ESCALATE_P": DEFAULT_ESCALATE_P,
            "THRESHOLD_SCAM_P": DEFAULT_SCAM_P,
        },
    }
    (config.out_dir / "jev_eval_results.json").write_text(json.dumps(results, indent=2) + "\n")

    risk = (
        "> ⚠️ **MODEL QUALITY RISK — choice accuracy below 50% (coin-flip level).** "
        "Do not tune thresholds to hide this; the eval stops here.\n\n"
        if stats.stop
        else ""
    )

    def dist_rows(dist: dict[str, dict[str, float]]) -> list[str]:
        """n/mean/p5/p50/p95 rows for a p-value distribution."""
        return [
            f"| {name} | {int(v['n'])} | {v['mean']:.3f} | {v['p5']:.3f} | "
            f"{v['p50']:.3f} | {v['p95']:.3f} |"
            for name, v in dist.items()
        ]

    confusion = stats.scam_confusion
    tp_fp = confusion["tp"] + confusion["fp"]
    tp_fn = confusion["tp"] + confusion["fn"]
    prec = confusion["tp"] / tp_fp if tp_fp else 0.0
    rec = confusion["tp"] / tp_fn if tp_fn else 0.0
    scam_mean = stats.scam_table["scam_report"]["mean"]
    other_mean = stats.scam_table["other"]["mean"]
    confusion_line = (
        f"TP={confusion['tp']} FP={confusion['fp']} FN={confusion['fn']} TN={confusion['tn']} "
        f"→ precision {prec:.1%}, recall {rec:.1%}"
    )
    brier_row = (
        f"| Brier choice / escalate_now (scam-proxy) | {stats.brier_choice:.4f} / "
        f"{stats.brier_escalate:.4f} |"
    )
    per_label_rows = [
        f"| {label} | {int(b['n'])} | {int(b['hits'])} | {b['accuracy']:.1%} |"
        for label, b in stats.per_label.items()
    ]
    sweep_rows = [
        f"| {cut} | {int(v['tp'])} | {int(v['fp'])} | {int(v['fn'])} | {int(v['tn'])} | "
        f"{v['precision']:.2f} | {v['recall']:.2f} | {v['f1']:.3f} |"
        for cut, v in sorted(stats.scam_sweep.items())
    ]

    doc = f"""# Jev / Laya decision eval

{risk}Checkpoint: `{CHECKPOINT_ID}`. Generated by `udp-eval`; regenerate with
`make model` running, then `uv run udp-eval`. Full JSON (sweeps, escalate
distribution): `jev_eval_results.json` next to this file.

| metric | value |
|---|---|
| versions | laya {versions["laya"]} / torch {versions["torch"]} / python {versions["python"]} |
| endpoint | `{config.url}` ({config.workers} workers paced at {config.rate:g}/s) |
| labeled tickets | {labeled_n} (+{ambiguous_n} ambiguous) |
| choice accuracy vs category_hint | {stats.accuracy:.1%} |
{brier_row}
| HTTP latency p50 / p95 | {stats.latency_p50:.1f} / {stats.latency_p95:.1f} ms |
| server inference p50 / p95 | {stats.infer_p50:.1f} / {stats.infer_p95:.1f} ms |
| achieved throughput | {stats.throughput:.2f} dec/s |
| request errors | {stats.errors} |

### Per-label choice accuracy

| ground truth | n | correct | accuracy |
|---|---|---|---|
{"\n".join(per_label_rows)}

### p(scam_report) by ground truth

| ground truth | n | mean | p5 | p50 | p95 |
|---|---|---|---|---|---|
{"\n".join(dist_rows(stats.scam_table))}

Confusion at the default p(scam) ≥ {DEFAULT_SCAM_P}: {confusion_line}.
Best-F1 cutoff in the sweep: **{stats.best_scam_cut:.2f}** (F1 {stats.best_scam_f1:.3f}).

| p(scam) cut | TP | FP | FN | TN | precision | recall | F1 |
|---|---|---|---|---|---|---|---|
{"\n".join(sweep_rows)}

### escalate_now p(true) — labeled (scam-proxy split) + ambiguous pool

| stream | n | mean | p5 | p50 | p95 |
|---|---|---|---|---|---|
{"\n".join(dist_rows(escalate_dist))}

Method: seeded tickets from the production generator (profile `steady`, seed
{config.seed}); `category_hint` is ground truth, never on the wire. Brier for
escalate_now uses the scam-proxy label (see module docstring). The chosen
`THRESHOLD_SCAM_P` sits inside the p(scam) separation band above (scam-class
mean {scam_mean:.2f} vs {other_mean:.2f} for the rest); escalate_now ships
temperature 1.98 (smoothed toward 0.5), so its cutoff needs wider margins.
"""
    md_path = config.out_dir / "jev_eval.md"
    md_path.write_text(doc)
    return md_path


def run(config: EvalConfig) -> int:
    """Run the full eval: generate → drive → aggregate → write report.

    Returns 0 on success, 2 on the stop rule, 1 on transport errors.
    """
    labeled, ambiguous = _labeled_tickets(config)
    print(
        f"udp-eval: {len(labeled)} labeled + {len(ambiguous)} ambiguous tickets "
        f"→ {config.url} at {config.rate:g}/s × {config.workers} workers",
        file=sys.stderr,
    )
    started = time.perf_counter()
    with httpx.Client(timeout=10.0, limits=httpx.Limits(max_connections=config.workers)) as client:
        records = _run(client, labeled, config)
        wall = time.perf_counter() - started
        ambiguous_records = _run(client, ambiguous, config)
    stats = _stats(records, wall)

    # Thresholds act on the flagged stream in production — observe the model's
    # p-values on the ambiguous pool too.
    escalate_dist = _escalate_ps(records + ambiguous_records)

    md_path = _write_report(config, _versions(), len(labeled), len(ambiguous), stats, escalate_dist)
    print(
        f"udp-eval: accuracy {stats.accuracy:.1%}, Brier choice {stats.brier_choice:.4f}, "
        f"latency p50/p95 {stats.latency_p50:.1f}/{stats.latency_p95:.1f} ms, "
        f"{stats.throughput:.2f} dec/s → {md_path}",
        file=sys.stderr,
    )
    if stats.stop:
        print(
            "udp-eval: STOP — choice accuracy < 50% (coin-flip level). "
            f"Confusion at default scam cut: {stats.scam_confusion}. "
            "Report written; do not tune thresholds to hide this.",
            file=sys.stderr,
        )
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the ``udp-eval`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="udp-eval",
        description=(
            "Evaluate the RUNNING Laya model server: labeled-ticket accuracy, "
            "Brier calibration, concurrent latency, throughput."
        ),
    )
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help="tickets to generate")
    parser.add_argument(
        "--rate", type=float, default=DEFAULT_RATE, help="sustained requests/s; 0 = closed-loop"
    )
    parser.add_argument("--seed", type=int, default=7, help="generator seed")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="concurrent workers")
    parser.add_argument("--url", default="http://127.0.0.1:8082/v1/systemone", help="endpoint")
    parser.add_argument("--out-dir", type=Path, default=Path("docs"), help="report directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``udp-eval`` console script."""
    args = build_parser().parse_args(argv)
    config = EvalConfig(
        count=args.count,
        rate=args.rate,
        seed=args.seed,
        workers=args.workers,
        url=args.url,
        out_dir=args.out_dir,
    )
    try:
        return run(config)
    except httpx.HTTPError as exc:
        print(f"udp-eval: cannot reach {config.url}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
