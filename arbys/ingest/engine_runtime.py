"""Engine runtime — glues quotes → detectors → opportunity stream.

Keeps an in-memory index from outcome_id → event_groups so a quote update only
re-runs detection for the affected groups.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from decimal import Decimal

from ..shared.arb_engine import (
    ArbOpportunity,
    detect_complementary_set,
    detect_cross_venue_two_leg,
)
from ..shared.fees import FeeModelRegistry
from ..shared.quotebook import QuoteBook
from ..shared.types import EventGroup, Quote

log = logging.getLogger(__name__)

OpportunityHandler = Callable[[ArbOpportunity], None]

DEFAULT_TARGET_PAYOFF = Decimal("100")


class EngineRuntime:
    def __init__(
        self,
        *,
        quotebook: QuoteBook,
        fees: FeeModelRegistry,
        on_opportunity: OpportunityHandler | None = None,
        target_payoff: Decimal = DEFAULT_TARGET_PAYOFF,
    ) -> None:
        self._book = quotebook
        self._fees = fees
        self._on_opp = on_opportunity or (lambda _o: None)
        self._target_payoff = target_payoff
        self._groups: dict[str, EventGroup] = {}
        self._outcome_to_groups: dict[str, set[str]] = defaultdict(set)

    def register_group(self, group: EventGroup) -> None:
        self._groups[group.id] = group
        for leg in group.legs:
            self._outcome_to_groups[leg.outcome_id].add(group.id)

    def unregister_group(self, group_id: str) -> None:
        group = self._groups.pop(group_id, None)
        if group is None:
            return
        for leg in group.legs:
            self._outcome_to_groups[leg.outcome_id].discard(group_id)

    def on_quote(self, quote: Quote) -> None:
        affected = list(self._outcome_to_groups.get(quote.outcome_id, ()))
        for gid in affected:
            self._evaluate(gid)

    def _evaluate(self, group_id: str) -> None:
        group = self._groups.get(group_id)
        if group is None:
            return
        quotes = {leg.outcome_id: self._book.get(leg.outcome_id) for leg in group.legs}
        quotes = {oid: q for oid, q in quotes.items() if q is not None}

        cross = detect_cross_venue_two_leg(
            group, quotes, self._fees, target_payoff=self._target_payoff
        )
        if cross is not None:
            self._on_opp(cross)

        # Complementary set only makes sense within a single venue.
        by_venue: dict[str, list] = defaultdict(list)
        for leg in group.legs:
            by_venue[leg.venue_id].append(leg)
        for venue_id, legs in by_venue.items():
            if len(legs) < 2:
                continue
            comp = detect_complementary_set(
                f"{group_id}:{venue_id}",
                legs,
                quotes,
                self._fees,
                target_payoff=self._target_payoff,
            )
            if comp is not None:
                self._on_opp(comp)
