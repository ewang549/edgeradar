"""The pluggable source-adapter interface.

This is the central abstraction of EdgeRadar. Every platform (Kalshi, Manifold,
Metaculus, The Odds API, weather) is wrapped by a subclass of `SourceAdapter`.
The pipeline downstream only knows about this interface and the `MarketQuote`
contract — so onboarding a new platform never touches the rest of the system.

Design intent (kept as a stub in Phase 0; implemented in Phase 1):

    fetch()        -> raw payloads from the platform (respecting rate limits)
    normalize()    -> convert raw payloads into MarketQuote records
    run()          -> fetch + normalize, with a --dry-run path that reads saved
                      sample responses instead of hitting the network (so you can
                      develop without burning API quota)

Each concrete adapter also documents its price→implied-probability formula
(American odds, decimal odds, Kalshi cents, Polymarket shares, ...).
"""

from __future__ import annotations

import abc
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from edgeradar.models import MarketQuote, RawRecord


class SourceAdapter(abc.ABC):
    """Abstract base class for all data-source adapters.

    Subclasses must set `source` (the platform slug) and implement `fetch` and
    `normalize`. `run` wires them together and is the single entrypoint the
    ingestion layer calls.
    """

    #: Platform slug, e.g. "kalshi". Used in the natural key and lake paths.
    source: str = ""

    def __init__(
        self, *, dry_run: bool = False, sample_dir: str | Path = "sample_responses"
    ) -> None:
        if not self.source:
            raise NotImplementedError("Concrete adapters must set a non-empty `source` slug.")
        self.dry_run = dry_run
        self.sample_dir = Path(sample_dir) / self.source

    # --- to be implemented by Phase 1 -------------------------------------

    @abc.abstractmethod
    def fetch(self) -> Iterable[RawRecord]:
        """Return raw records from the platform.

        In `--dry-run` mode this should read from `self.sample_dir` instead of
        making network calls. Implementations must respect platform rate limits
        and Terms of Service, and use only official/public endpoints.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def normalize(self, raw: Iterable[RawRecord]) -> Iterable[MarketQuote]:
        """Convert raw payloads into normalized `MarketQuote` records.

        Must document and apply the platform's price→implied-probability formula
        and (where two-sided) remove the vig so probabilities are comparable.
        """
        raise NotImplementedError

    # --- shared orchestration ---------------------------------------------

    def run(self) -> list[MarketQuote]:
        """Fetch then normalize. Single entrypoint used by the ingestion layer."""
        raw = list(self.fetch())
        return list(self.normalize(raw))

    @staticmethod
    def now_utc() -> datetime:
        """Timezone-aware current time in UTC (all snapshots use this)."""
        return datetime.now(tz=timezone.utc)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} source={self.source!r} dry_run={self.dry_run}>"
