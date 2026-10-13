"""Seeded, bounded synthetic app-user ticket generator.

Produces realistic ticket texts per category plus a small *ambiguous* subset —
garbled or mixed-label text the fast path cannot classify and will flag for
the model. Pacing, publishing, and the ``--dry-run`` display live here too;
the same parser backs the ``udp generate`` subcommand and the standalone
``udp-generate`` script. With the same ``--seed`` the stream (event ids,
texts, categories, hints, user ids) is identical across runs; wall-clock
timestamps necessarily differ.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from confluent_kafka import KafkaError, Message, Producer
from faker import Faker

from udp.schemas import CATEGORIES, Category, Ticket

DEFAULT_SEED = 42
NOMINAL_RATE_EPS = 100.0
DEFAULT_AMBIGUOUS_FRACTION = 0.05  # ≤5 flagged/s at the nominal rate

# Burst budget (S0 measured ~12-15 dec/s on this GPU): demo bursts cap flagged
# events at 0.5 × 12 dec/s × 60 s = 360 so the model path never oversubscribes.
MODEL_CEILING_DEC_PER_S = 12.0
FLAG_BUDGET_HEADROOM = 0.5
BURST_DURATION_S = 60.0
MAX_FLAGGED_PER_S_STEADY = NOMINAL_RATE_EPS * DEFAULT_AMBIGUOUS_FRACTION
MAX_FLAGGED_PER_BURST = int(MODEL_CEILING_DEC_PER_S * FLAG_BUDGET_HEADROOM * BURST_DURATION_S)

USER_POOL_SIZE = 500
MAX_BACKLOG_S = 60.0

#: Per-category event weights for the steady profile (must cover every category).
_STEADY_WEIGHTS: Mapping[Category, float] = {
    "complaint": 0.25,
    "billing": 0.20,
    "bug": 0.18,
    "feature_request": 0.15,
    "access": 0.12,
    "scam_report": 0.10,
}

#: Placeholder providers shared by all category templates.
_PLACEHOLDERS: dict[str, Callable[[random.Random, Faker], str]] = {
    "name": lambda _rng, faker: faker.first_name(),
    "company": lambda _rng, faker: faker.company(),
    "n": lambda rng, _faker: str(rng.randint(2, 45)),
    "amount": lambda rng, _faker: f"{rng.uniform(4.5, 480.0):.2f}",
    "amount2": lambda rng, _faker: f"{rng.uniform(4.5, 480.0):.2f}",
    "site": lambda rng, _faker: rng.choice(
        ("Telegram", "Discord", "eBay", "a marketplace", "social media")
    ),
    "platform": lambda rng, _faker: rng.choice(("Windows", "macOS", "Linux", "iOS", "Android")),
    "v": lambda rng, _faker: f"{rng.randint(2, 9)}.{rng.randint(0, 12)}.{rng.randint(0, 20)}",
    "app": lambda rng, _faker: rng.choice(("Notion", "Slack", "Linear")),
    "report": lambda rng, _faker: rng.choice(("usage", "revenue", "billing", "activity")),
}

#: Ticket templates per category (deliberately noisy phrasing; keywords line
#: up with the decide.yaml rule table so the fast path classifies them).
TEMPLATES: dict[Category, tuple[str, ...]] = {
    "complaint": (
        "{name} here — the app has been freezing every time I open the dashboard "
        "since yesterday. This is unacceptable for a paid plan.",
        "support hasn't replied in {n} days, my ticket just sits there. not happy at all",
    ),
    "scam_report": (
        "I got an email claiming to be from {company} asking for my card details — "
        "pretty sure it's a phishing attempt, reporting it here.",
        "Someone is selling fake lifetime licenses for the app on {site}. "
        "You should take that down.",
    ),
    "billing": (
        "I was charged ${amount} twice this month for the same subscription. "
        "Please refund the duplicate.",
        "My invoice shows ${amount} but my plan is supposed to be ${amount2}/mo. "
        "Which one is right?",
    ),
    "bug": (
        "App crashes with a white screen when I export the {report} report, "
        "console shows a null pointer.",
        "Since v{v}, uploads over {n}MB fail silently. No error message, nothing in logs.",
    ),
    "access": (
        "Locked out of my account — the password reset email never arrives (checked spam twice).",
        "I can't enable 2FA, the QR code never loads on {platform}.",
    ),
    "feature_request": (
        "Would love a dark-mode schedule (auto switch at sunset) like {app} has.",
        "Please add CSV export to the {report} view — we need it for our month-end close.",
    ),
}

#: Garbled / mixed-label texts the fast path cannot classify confidently —
#: exactly what the cascade flags for the model (``category_hint`` is None).
#: Every entry must dodge the rule-table keywords, else the fast path eats it
#: and the ~5% flagged stream collapses (measured S8: 95/5 fast/model split).
AMBIGUOUS_TEMPLATES: tuple[str, ...] = (
    "so my payment went thru but the app says unpaid and support chat is dead?? is this even legit",
    "asdkjh paymnt... app... brokn??? pls",
)


@dataclass(frozen=True)
class BurstProfile:
    """Parameters of one load profile.

    A burst raises the event rate and skews the category mix toward
    ``scam_report`` for ``duration_s`` seconds, while the flagged (ambiguous)
    stream stays hard-capped at ``flagged_budget`` events for the whole burst
    (derived from the model ceiling with 50% headroom).
    """

    name: str
    duration_s: float | None = None
    rate_multiplier: float = 1.0
    scam_share: float = 0.0
    flagged_budget: int | None = None

    def __post_init__(self) -> None:
        """Assert the profile stays inside the model's decision capacity."""
        if self.duration_s is None:
            assert self.flagged_budget is None, "flagged_budget requires a burst window"
            assert 0.0 <= self.scam_share < 1.0
            return
        assert self.duration_s > 0, "burst duration must be positive"
        assert self.rate_multiplier >= 1.0, "bursts never slow the stream down"
        assert self.flagged_budget is not None, "a burst must declare a flagged budget"
        assert 0.0 <= self.scam_share < 1.0, "scam_share must be in [0, 1)"
        capacity = int(self.duration_s * MODEL_CEILING_DEC_PER_S * FLAG_BUDGET_HEADROOM)
        assert self.flagged_budget <= capacity, (
            f"flagged_budget {self.flagged_budget} exceeds burst capacity {capacity}"
        )


STEADY = BurstProfile(name="steady")

DEMO = BurstProfile(
    name="demo",
    duration_s=BURST_DURATION_S,
    rate_multiplier=3.0,
    scam_share=0.6,
    flagged_budget=MAX_FLAGGED_PER_BURST,
)

PROFILES: Mapping[str, BurstProfile] = {"steady": STEADY, "demo": DEMO}

assert MAX_FLAGGED_PER_S_STEADY <= MODEL_CEILING_DEC_PER_S * FLAG_BUDGET_HEADROOM, (
    "steady profile must stay inside the model's flagged-event budget"
)


@dataclass(frozen=True)
class GeneratorConfig:
    """All knobs for one generation run."""

    seed: int = DEFAULT_SEED
    rate: float = NOMINAL_RATE_EPS
    profile: BurstProfile = STEADY
    count: int | None = None
    ambiguous_fraction: float = DEFAULT_AMBIGUOUS_FRACTION
    bootstrap: str = "localhost:9092"
    topic: str = "tickets"

    def __post_init__(self) -> None:
        """Assert the configuration is generatable at all."""
        assert self.rate > 0, "rate must be positive"
        assert self.count is None or self.count >= 0, "count must be non-negative"
        assert 0.0 <= self.ambiguous_fraction <= 1.0, "ambiguous_fraction must be in [0, 1]"


class _PlaceholderDict(dict[str, str]):
    """Template mapping that draws provider values lazily, once per key.

    Repeated placeholders inside one template (e.g. two ``{amount}`` mentions)
    resolve to the same value, keeping ticket texts internally consistent.
    """

    def __init__(self, rng: random.Random, faker: Faker) -> None:
        super().__init__()
        self._rng = rng
        self._faker = faker

    def __missing__(self, key: str) -> str:
        try:
            provider = _PLACEHOLDERS[key]
        except KeyError as exc:
            msg = f"unknown template placeholder: {key!r}"
            raise ValueError(msg) from exc
        self[key] = provider(self._rng, self._faker)
        return self[key]


class TicketGenerator:
    """Deterministic synthetic ticket stream driven by one seed.

    Yields pure data — no pacing, no I/O — so tests can inspect the mix,
    determinism, and burst bounds without sleeping.
    """

    def __init__(self, config: GeneratorConfig) -> None:
        """Seed every randomness source from ``config.seed``."""
        self.config = config
        self._rng = random.Random(config.seed)
        self._faker = Faker()
        self._faker.seed_instance(config.seed)
        self._user_pool = tuple(f"usr_{self._faker.user_name()}" for _ in range(USER_POOL_SIZE))
        self._emitted = 0
        self._flagged_in_burst = 0

    def burst_event_count(self) -> int | None:
        """Events inside the profile's burst window (``None`` = non-burst profile)."""
        profile = self.config.profile
        if profile.duration_s is None:
            return None
        return math.ceil(self.config.rate * profile.rate_multiplier * profile.duration_s)

    def events(self, count: int) -> Iterator[Ticket]:
        """Yield exactly ``count`` tickets; consecutive calls continue the sequence."""
        for _ in range(count):
            yield self._next()

    def stream(self) -> Iterator[Ticket]:
        """Yield tickets forever (caller owns pacing and interruption)."""
        while True:
            yield self._next()

    def _next(self) -> Ticket:
        """Build the next ticket from the seeded draws."""
        index = self._emitted
        burst_events = self.burst_event_count()
        in_burst = burst_events is not None and index < burst_events
        flagged = self._draw_flagged(in_burst, burst_events)
        category = self._draw_category(in_burst)
        text = self._draw_text(category, flagged)
        now = datetime.now(UTC)
        ticket = Ticket(
            event_id=uuid.UUID(int=self._rng.getrandbits(128), version=4).hex,
            text=text,
            category_hint=None if flagged else category,
            user_id=self._rng.choice(self._user_pool),
            created_at=now - timedelta(seconds=self._rng.uniform(0.0, MAX_BACKLOG_S)),
            ingested_at=now,
        )
        self._emitted += 1
        return ticket

    def _draw_flagged(self, in_burst: bool, burst_events: int | None) -> bool:
        """Decide whether this event is flagged (ambiguous → routed to the model).

        Steady state flags with probability ``ambiguous_fraction``. Inside a
        burst the per-event probability is sized to the flagged budget and a
        running counter hard-clamps the burst total to ``flagged_budget`` —
        the ≤360 bound is guaranteed, not merely expected.
        """
        profile = self.config.profile
        if in_burst and burst_events is not None and profile.flagged_budget is not None:
            if self._flagged_in_burst >= profile.flagged_budget:
                return False
            if self._rng.random() < profile.flagged_budget / burst_events:
                self._flagged_in_burst += 1
                return True
            return False
        return self._rng.random() < self.config.ambiguous_fraction

    def _draw_category(self, in_burst: bool) -> Category:
        """Draw the nominal category: steady mix, or ``scam_share`` forced scam in-burst."""
        profile = self.config.profile
        if in_burst and self._rng.random() < profile.scam_share:
            return "scam_report"
        roll = self._rng.random()
        cumulative = 0.0
        for category, weight in _STEADY_WEIGHTS.items():
            cumulative += weight
            if roll < cumulative:
                return category
        return CATEGORIES[-1]

    def _draw_text(self, category: Category, flagged: bool) -> str:
        """Render the ticket text from the category (or ambiguous) template pool."""
        pool = AMBIGUOUS_TEMPLATES if flagged else TEMPLATES[category]
        template = pool[self._rng.randrange(len(pool))]
        return template.format_map(_PlaceholderDict(self._rng, self._faker))


class TicketPublisher:
    """Thin confluent-kafka producer for the ``tickets`` topic.

    Publishes the wire payload (``WIRE_EXCLUDED_FIELDS`` stripped) keyed by
    ``user_id``; delivery failures are reported on stderr; ``flush`` must be
    called before exit.
    """

    def __init__(self, bootstrap: str, topic: str = "tickets") -> None:
        self._topic = topic
        self._producer = Producer({"bootstrap.servers": bootstrap})

    @staticmethod
    def _on_delivery(err: KafkaError | None, msg: Message | None) -> None:
        """Report failed deliveries on stderr."""
        if err is not None:
            print(f"udp-generate: delivery failed: {err}", file=sys.stderr)

    def publish(self, ticket: Ticket) -> str:
        """Serialize and produce one ticket; returns the wire JSON payload."""
        payload = ticket.wire_json()
        self._producer.produce(
            self._topic,
            key=ticket.user_id.encode(),
            value=payload.encode(),
            on_delivery=self._on_delivery,
        )
        self._producer.poll(0)
        return payload

    def flush(self, timeout: float = 10.0) -> int:
        """Flush pending messages; returns the undelivered count after the timeout."""
        return self._producer.flush(timeout)


def run(config: GeneratorConfig, *, dry_run: bool = False) -> int:
    """Run the paced generation loop, publishing (or printing) tickets.

    Deadline-based pacing self-corrects against ``time.sleep`` granularity;
    ``Ctrl+C`` flushes the producer and exits cleanly. Progress goes to stderr
    so ``--dry-run`` stdout stays parseable as JSON lines.
    """
    generator = TicketGenerator(config)
    publisher: TicketPublisher | None = (
        None if dry_run else TicketPublisher(config.bootstrap, config.topic)
    )
    steady_interval = 1.0 / config.rate
    burst_interval = 1.0 / (config.rate * config.profile.rate_multiplier)
    burst_events = generator.burst_event_count()
    emitted = 0
    deadline = time.monotonic()
    events = generator.stream() if config.count is None else generator.events(config.count)
    try:
        for ticket in events:
            if publisher is None:
                print(ticket.model_dump_json(), flush=True)
            else:
                publisher.publish(ticket)
            emitted += 1
            in_burst = burst_events is not None and emitted <= burst_events
            deadline += burst_interval if in_burst else steady_interval
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
    except KeyboardInterrupt:
        pass
    finally:
        if publisher is not None:
            publisher.flush()
    mode = "printed" if publisher is None else "published"
    count = "∞" if config.count is None else str(config.count)
    print(
        f"udp-generate: {mode} {emitted}/{count} tickets (profile={config.profile.name}, "
        f"seed={config.seed}, rate={config.rate:g}/s, topic={config.topic})",
        file=sys.stderr,
    )
    return 0


def build_generate_parser(add_help: bool = True) -> argparse.ArgumentParser:
    """Build the ``generate`` argument parser.

    Shared by the ``udp generate`` subcommand (``add_help=False`` because
    argparse parents would duplicate ``-h``) and the standalone ``udp-generate``
    entry point.
    """
    parser = argparse.ArgumentParser(
        prog="udp-generate",
        add_help=add_help,
        description=(
            "Generate synthetic app-user tickets onto Kafka "
            "(--dry-run prints them to stdout instead)."
        ),
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help="same seed ⇒ same ticket sequence"
    )
    parser.add_argument(
        "--rate", type=float, default=NOMINAL_RATE_EPS, help="nominal events/s; bursts multiply"
    )
    parser.add_argument(
        "--profile",
        choices=tuple(PROFILES),
        default=STEADY.name,
        help="load profile; demo adds a bounded 60s scam spike",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        metavar="N",
        help="total events; omit = stream until Ctrl+C",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print full tickets (with category_hint), publish nothing",
    )
    return parser


def run_from_namespace(args: argparse.Namespace) -> int:
    """Run the generator from a parsed CLI namespace (shared by both entry points)."""
    config = GeneratorConfig(
        seed=args.seed,
        rate=args.rate,
        profile=PROFILES[args.profile],
        count=args.count,
    )
    return run(config, dry_run=args.dry_run)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``udp-generate`` console script."""
    args = build_generate_parser().parse_args(argv)
    return run_from_namespace(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
