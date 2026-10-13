"""Seeded, bounded synthetic app-user ticket generator.

Produces realistic ticket texts per category (complaint, scam_report, billing,
bug, access, feature_request) plus a small *ambiguous* subset — garbled or
mixed-label text the cascade fast path cannot classify confidently and will
flag for the model. Pacing, publishing, and the ``--dry-run`` display live here
too; the same parser backs the ``udp generate`` subcommand and the standalone
``udp-generate`` script.

Determinism: with the same ``--seed``, the seeded stream (event ids, texts,
categories, hints, user ids) is identical across runs; wall-clock timestamps
(``created_at`` / ``ingested_at``) are the only values that necessarily differ.
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
"""Default random seed; the same seed always yields the same ticket sequence."""

NOMINAL_RATE_EPS = 100.0
"""Nominal production rate in events per second (the S2 planning number)."""

DEFAULT_AMBIGUOUS_FRACTION = 0.05
"""Default flagged (ambiguous) fraction: ≤5% at nominal rate = ≤5 flagged/s."""

MODEL_CEILING_DEC_PER_S = 12.0
"""Conservative model decision ceiling (S0 measured ~15 dec/s; plan 10–12)."""

FLAG_BUDGET_HEADROOM = 0.5
"""Fraction of model ceiling usable by flagged events during a burst."""

BURST_DURATION_S = 60.0
"""Length of the demo scam-spike burst window in seconds."""

MAX_FLAGGED_PER_S_STEADY = NOMINAL_RATE_EPS * DEFAULT_AMBIGUOUS_FRACTION
"""Steady-state flagged ceiling: 100 ev/s × 5% = 5 flagged/s."""

MAX_FLAGGED_PER_BURST = int(MODEL_CEILING_DEC_PER_S * FLAG_BUDGET_HEADROOM * BURST_DURATION_S)
"""Demo burst bound: 0.5 × 12 dec/s × 60 s = 360 flagged events total."""

USER_POOL_SIZE = 500
"""Number of distinct synthetic app users (drives Kafka partition affinity)."""

MAX_BACKLOG_S = 60.0
"""Upper bound of the created-vs-ingested backlog jitter, in seconds."""

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
    "years": lambda rng, _faker: str(rng.randint(1, 9)),
    "n": lambda rng, _faker: str(rng.randint(2, 45)),
    "amount": lambda rng, _faker: f"{rng.uniform(4.5, 480.0):.2f}",
    "amount2": lambda rng, _faker: f"{rng.uniform(4.5, 480.0):.2f}",
    "date": lambda _rng, faker: faker.date(),
    "date2": lambda _rng, faker: faker.date(),
    "site": lambda rng, _faker: rng.choice(
        ("Telegram", "Discord", "eBay", "a marketplace", "social media")
    ),
    "platform": lambda rng, _faker: rng.choice(("Windows", "macOS", "Linux", "iOS", "Android")),
    "v": lambda rng, _faker: f"{rng.randint(2, 9)}.{rng.randint(0, 12)}.{rng.randint(0, 20)}",
    "code": lambda _rng, faker: faker.bothify(text="??-####").upper(),
    "app": lambda rng, _faker: rng.choice(("Notion", "Slack", "Linear")),
    "report": lambda rng, _faker: rng.choice(("usage", "revenue", "billing", "activity")),
}

#: Realistic ticket templates per category; each category ships 6 variants with
#: deliberately noisy phrasing (CAPS rants, short venting, typos).
TEMPLATES: dict[Category, tuple[str, ...]] = {
    "complaint": (
        "{name} here — the app has been freezing every time I open the dashboard "
        "since yesterday. This is unacceptable for a paid plan.",
        "Worst update so far. My whole team got logged out and sync is broken. "
        "Fix it or I'm cancelling, {company} owes me better than this.",
        "I've been a customer for {years} years and now nothing loads at all. "
        "Who do I talk to about this mess?",
        "THIS APP IS TRASH NOW!!! it eats my battery and drops every upload. do something!!",
        "support hasn't replied in {n} days, my ticket just sits there. not happy at all",
        "the new redesign is a downgrade, i can't find anything and my saved reports "
        "are gone. what happened??",
    ),
    "scam_report": (
        "I got an email claiming to be from {company} asking for my card details — "
        "pretty sure it's a phishing attempt, reporting it here.",
        "Someone is selling fake lifetime licenses for the app on {site}. "
        "You should take that down.",
        "Just got a call from 'support' asking to remote into my laptop to fix "
        "billing. Definitely a scam, wanted to warn you.",
        "a stranger on {site} sent me a link that looks exactly like your login page "
        "but the url is wrong. phishing for sure",
        "received an invoice for ${amount} that I never placed, looks like someone "
        "is using fake invoices to scam people",
        "hey, the promo code 'WIN-{code}' circulating on {site} is not yours, "
        "it installs malware. heads up",
    ),
    "billing": (
        "I was charged ${amount} twice this month for the same subscription. "
        "Please refund the duplicate.",
        "My invoice shows ${amount} but my plan is supposed to be ${amount2}/mo. "
        "Which one is right?",
        "can't update my card, the payment page keeps saying 'declined' but my bank "
        "says nothing was attempted",
        "why did my annual renewal go up from ${amount} to ${amount2} without any notice??",
        "refund request: I cancelled on {date} but you still billed me ${amount} on {date2}.",
        "My company needs a VAT invoice with {company} details, the billing portal "
        "won't accept our tax id.",
    ),
    "bug": (
        "App crashes with a white screen when I export the {report} report, "
        "console shows a null pointer.",
        "Since v{v}, uploads over {n}MB fail silently. No error message, nothing in logs.",
        "the search index is stale — new items don't show up unless I refresh 3-4 times",
        "dark mode makes all dropdown text unreadable (white on white) on {platform}.",
        "webhooks fire twice for the same event since this morning, we got duplicate "
        "rows in our DB",
        "the CSV export truncates column names after {n} characters — reproducible "
        "on {platform} and web",
    ),
    "access": (
        "Locked out of my account — the password reset email never arrives (checked spam twice).",
        "I can't enable 2FA, the QR code never loads on {platform}.",
        "my account was locked after the security review, I need it restored for work. "
        "this is {name} from {company}",
        "Says my session expired every 2 minutes even after re-login. Nothing on my end changed.",
        "please restore access for our contractor {name}, their SSO link bounces "
        "with 'user not found'",
        "the invite link for my teammate expired after {n} minutes — is that a setting or a bug?",
    ),
    "feature_request": (
        "Would love a dark-mode schedule (auto switch at sunset) like {app} has.",
        "Please add CSV export to the {report} view — we need it for our month-end close.",
        "Any chance of a native {platform} client? The web app is fine but native would be faster.",
        "It would help if I could tag tickets with custom labels and filter the board by them.",
        "Can we get an API endpoint for bulk user import? We onboard {n} people every quarter.",
        "Suggestion: per-project notification digests instead of one email per comment.",
    ),
}

#: Garbled / mixed-label texts the fast path cannot classify confidently —
#: exactly what the cascade flags for the model. ``category_hint`` is ``None``
#: for these tickets.
AMBIGUOUS_TEMPLATES: tuple[str, ...] = (
    "cant login?? charged twice i think idk it just says error 500???",
    "so my payment went thru but the app says unpaid and support chat is dead?? is this even legit",
    "keeps crashing since the update, also maybe got a phishing email from 'support', "
    "idk which app",
    "I WAS BILLED $0.00 TWICE?? or not billed?? the invoice page is blank. fix or refund whatever",
    "hi my account got locked after i changed my email, or phone? i forget. can someone check",
    "refund my subscription AND unlock my account, or actually just cancel everything idk",
    "asdkjh payment... login... broken??? pls",
    "why is my bill wrong and why am i locked out and why does it crash, pick one??",
)


@dataclass(frozen=True)
class BurstProfile:
    """Parameters of one load profile.

    A burst raises the event rate and skews the category mix toward
    ``scam_report`` for ``duration_s`` seconds, while the flagged (ambiguous)
    stream stays hard-capped at ``flagged_budget`` events for the whole burst —
    derived from the model ceiling with 50% headroom
    (``MAX_FLAGGED_PER_BURST = 0.5 × 12 dec/s × 60 s = 360``).

    Attributes:
        name: CLI profile name (``steady`` or ``demo``).
        duration_s: Burst window length in seconds; ``None`` disables bursting.
        rate_multiplier: Producer rate multiplier inside the burst window.
        scam_share: Fraction of burst events labeled ``scam_report``.
        flagged_budget: Hard cap on flagged events inside one burst; ``None``
            only when there is no burst.

    Raises:
        AssertionError: If burst parameters violate the model-capacity bounds.
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
"""Default profile: nominal rate, plain category mix, no burst."""

DEMO = BurstProfile(
    name="demo",
    duration_s=BURST_DURATION_S,
    rate_multiplier=3.0,
    scam_share=0.6,
    flagged_budget=MAX_FLAGGED_PER_BURST,
)
"""Demo profile: 60 s scam spike at 3× rate, flagged events hard-capped at 360."""

PROFILES: Mapping[str, BurstProfile] = {"steady": STEADY, "demo": DEMO}
"""Profiles selectable via ``--profile``."""

assert MAX_FLAGGED_PER_S_STEADY <= MODEL_CEILING_DEC_PER_S * FLAG_BUDGET_HEADROOM, (
    "steady profile must stay inside the model's flagged-event budget"
)


@dataclass(frozen=True)
class GeneratorConfig:
    """All knobs for one generation run.

    Attributes:
        seed: Random seed; same seed ⇒ same ticket sequence.
        rate: Nominal event rate in events per second (multiplied by the
            profile's ``rate_multiplier`` inside a burst window).
        profile: Burst profile (``STEADY`` by default, ``DEMO`` for the scam
            spike).
        count: Total events to emit; ``None`` streams until interrupted.
        ambiguous_fraction: Flagged (ambiguous) fraction in steady state; the
            default 0.05 keeps flagged load at ≤5/s at the nominal rate.
        bootstrap: Kafka bootstrap servers (``REDPANDA_BOOTSTRAP``).
        topic: Target Kafka topic.

    Raises:
        AssertionError: If numeric bounds are violated.
    """

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
    resolve to the same value, which keeps ticket texts internally consistent.
    """

    def __init__(self, rng: random.Random, faker: Faker) -> None:
        """Bind the shared randomness.

        Args:
            rng: Seeded source for numeric/choice placeholders.
            faker: Seeded faker for name/company/date placeholders.
        """
        super().__init__()
        self._rng = rng
        self._faker = faker

    def __missing__(self, key: str) -> str:
        """Draw a fresh value the first time ``key`` appears.

        Args:
            key: Placeholder name from the template.

        Returns:
            The substituted string value.

        Raises:
            ValueError: If the template uses an unknown placeholder.
        """
        try:
            provider = _PLACEHOLDERS[key]
        except KeyError as exc:
            msg = f"unknown template placeholder: {key!r}"
            raise ValueError(msg) from exc
        self[key] = provider(self._rng, self._faker)
        return self[key]


class TicketGenerator:
    """Deterministic synthetic ticket stream driven by one seed.

    The generator yields pure data — no pacing, no I/O — so tests can inspect
    the mix, determinism, and burst bounds without sleeping. Pacing and
    publishing live in :func:`run` / :class:`TicketPublisher`.

    Attributes:
        config: The run configuration (seed, rate, profile, fractions).
    """

    def __init__(self, config: GeneratorConfig) -> None:
        """Seed every randomness source from ``config.seed``.

        Args:
            config: Run configuration; only ``seed`` affects the sequence.
        """
        self.config = config
        self._rng = random.Random(config.seed)
        self._faker = Faker()
        self._faker.seed_instance(config.seed)
        self._user_pool = tuple(f"usr_{self._faker.user_name()}" for _ in range(USER_POOL_SIZE))
        self._emitted = 0
        self._flagged_in_burst = 0

    def burst_event_count(self) -> int | None:
        """Return how many events fall inside the profile's burst window.

        The window is ``rate × rate_multiplier × duration_s`` events; a count-
        limited run may end inside it.

        Returns:
            Event count of the burst window, or ``None`` for non-burst profiles.
        """
        profile = self.config.profile
        if profile.duration_s is None:
            return None
        return math.ceil(self.config.rate * profile.rate_multiplier * profile.duration_s)

    def events(self, count: int) -> Iterator[Ticket]:
        """Yield exactly ``count`` tickets from the seeded stream.

        Args:
            count: Number of tickets to generate.

        Yields:
            Valid :class:`Ticket` instances; consecutive calls continue the
            same seeded sequence.
        """
        for _ in range(count):
            yield self._next()

    def stream(self) -> Iterator[Ticket]:
        """Yield tickets forever (caller owns pacing and interruption).

        Yields:
            Valid :class:`Ticket` instances until the caller stops iterating.
        """
        while True:
            yield self._next()

    def _next(self) -> Ticket:
        """Build the next ticket from the seeded draws.

        Returns:
            A fully populated, valid :class:`Ticket`.
        """
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
        burst, the per-event probability is sized to the flagged budget, and a
        running counter hard-clamps the burst total to ``flagged_budget`` —
        the ≤360 bound is guaranteed, not merely expected.

        Args:
            in_burst: Whether this event falls inside the burst window.
            burst_events: Size of the burst window (``None`` when not bursting).

        Returns:
            ``True`` if the event is ambiguous (its ``category_hint`` will be
            ``None``).
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
        """Draw the nominal category for this event.

        Steady state uses the weighted mix; inside a burst, ``scam_share`` of
        events are forced to ``scam_report`` (the scam spike) and the rest come
        from the steady mix.

        Args:
            in_burst: Whether this event falls inside the burst window.

        Returns:
            The drawn category label.
        """
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
        """Render the ticket text from a template pool.

        Args:
            category: Nominal category (only used when not flagged).
            flagged: Whether the text comes from the ambiguous pool.

        Returns:
            Filled ticket text with realistic noise.

        Raises:
            AssertionError: If a category has fewer than 5 templates.
        """
        assert len(TEMPLATES[category]) >= 5, f"{category} needs ≥5 templates"
        assert len(AMBIGUOUS_TEMPLATES) >= 5, "ambiguous pool needs ≥5 templates"
        if flagged:
            template = AMBIGUOUS_TEMPLATES[self._rng.randrange(len(AMBIGUOUS_TEMPLATES))]
        else:
            pool = TEMPLATES[category]
            template = pool[self._rng.randrange(len(pool))]
        return template.format_map(_PlaceholderDict(self._rng, self._faker))


class TicketPublisher:
    """Thin confluent-kafka producer for the ``tickets`` topic.

    Publishes the wire payload (``WIRE_EXCLUDED_FIELDS`` stripped) keyed by
    ``user_id`` so tickets from one user share a partition. Delivery failures
    are reported on stderr; ``flush`` must be called before exit.
    """

    def __init__(self, bootstrap: str, topic: str = "tickets") -> None:
        """Create the producer.

        Args:
            bootstrap: Kafka bootstrap servers string.
            topic: Target topic (default ``tickets``).
        """
        self._topic = topic
        self._producer = Producer({"bootstrap.servers": bootstrap})

    @staticmethod
    def _on_delivery(err: KafkaError | None, msg: Message | None) -> None:
        """Report failed deliveries on stderr.

        Args:
            err: Delivery error, or ``None`` on success.
            msg: The delivered message (used for context on failure).
        """
        if err is not None:
            print(f"udp-generate: delivery failed: {err}", file=sys.stderr)

    def publish(self, ticket: Ticket) -> str:
        """Serialize and produce one ticket.

        Args:
            ticket: The validated ticket to publish.

        Returns:
            The wire JSON payload (generator-only fields stripped).
        """
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
        """Flush pending messages to the broker.

        Args:
            timeout: Maximum seconds to wait for delivery.

        Returns:
            Number of messages still undelivered after the timeout.
        """
        return self._producer.flush(timeout)


def run(config: GeneratorConfig, *, dry_run: bool = False) -> int:
    """Run the paced generation loop, publishing (or printing) tickets.

    Deadline-based pacing self-corrects against ``time.sleep`` granularity;
    ``Ctrl+C`` flushes the producer and exits cleanly. Progress goes to stderr
    so ``--dry-run`` stdout stays parseable as JSON lines.

    Args:
        config: Generation settings (seed, rate, profile, count).
        dry_run: Print full tickets (with ``category_hint``) to stdout and
            publish nothing.

    Returns:
        Process exit code (always ``0``; interruptions are graceful).
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
    profile = config.profile.name
    count = "∞" if config.count is None else str(config.count)
    print(
        f"udp-generate: {mode} {emitted}/{count} tickets (profile={profile}, "
        f"seed={config.seed}, rate={config.rate:g}/s, topic={config.topic})",
        file=sys.stderr,
    )
    return 0


def build_generate_parser(add_help: bool = True) -> argparse.ArgumentParser:
    """Build the ``generate`` argument parser.

    Shared by the ``udp generate`` subcommand (which needs ``add_help=False``
    because argparse parents would duplicate ``-h``) and the standalone
    ``udp-generate`` entry point.

    Args:
        add_help: Whether to register the automatic ``-h/--help`` action.

    Returns:
        The argument parser with seed/rate/profile/count/dry-run options.
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
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="random seed; same seed ⇒ same ticket sequence (default: %(default)s)",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=NOMINAL_RATE_EPS,
        help="nominal events per second; bursts multiply this (default: %(default)s)",
    )
    parser.add_argument(
        "--profile",
        choices=tuple(PROFILES),
        default=STEADY.name,
        help="load profile; demo adds a bounded 60s scam spike (default: %(default)s)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        metavar="N",
        help="total events to emit; omit to stream until Ctrl+C",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print full tickets (with category_hint) to stdout, publish nothing",
    )
    return parser


def run_from_namespace(args: argparse.Namespace) -> int:
    """Run the generator from a parsed CLI namespace.

    Single source of truth for both entry points: ``udp generate`` forwards its
    subparser namespace here, and ``udp-generate`` parses its own arguments
    into the same shape.

    Args:
        args: Namespace carrying seed/rate/profile/count/dry_run attributes.

    Returns:
        Process exit code.
    """
    config = GeneratorConfig(
        seed=args.seed,
        rate=args.rate,
        profile=PROFILES[args.profile],
        count=args.count,
    )
    return run(config, dry_run=args.dry_run)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``udp-generate`` console script.

    Args:
        argv: CLI arguments (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    args = build_generate_parser().parse_args(argv)
    return run_from_namespace(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
