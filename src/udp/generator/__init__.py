"""Synthetic workload generation for the realtime pipeline.

Exposes the seeded ticket generator and its CLI pieces so ``udp generate`` and
the standalone ``udp-generate`` script share one implementation.
"""

from udp.generator.tickets import (
    DEFAULT_AMBIGUOUS_FRACTION,
    DEFAULT_SEED,
    NOMINAL_RATE_EPS,
    PROFILES,
    BurstProfile,
    GeneratorConfig,
    TicketGenerator,
    TicketPublisher,
    main,
    run,
    run_from_namespace,
)

__all__ = [
    "DEFAULT_AMBIGUOUS_FRACTION",
    "DEFAULT_SEED",
    "NOMINAL_RATE_EPS",
    "BurstProfile",
    "GeneratorConfig",
    "PROFILES",
    "TicketGenerator",
    "TicketPublisher",
    "main",
    "run",
    "run_from_namespace",
]
