from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Airline:
    code: str
    name: str


@dataclass
class Alliance:
    code: str
    name: str


@dataclass
class JsMetadata:
    airlines: list[Airline]
    alliances: list[Alliance]


@dataclass
class Airport:
    name: str
    code: str


@dataclass
class SimpleDatetime:
    date: tuple[int, int, int]
    time: tuple[int, int]
    # Google omits components it does not return. The values above fall back
    # to zero so legacy consumers keep working, while rule auditing needs to
    # tell "explicit 0/midnight" apart from "field was missing".
    time_known: bool = True
    date_known: bool = True


@dataclass
class SingleFlight:
    from_airport: Airport
    to_airport: Airport
    departure: SimpleDatetime
    arrival: SimpleDatetime

    duration: int
    """Unit: minutes; ``0`` when Google omitted the field (see ``duration_known``)."""

    plane_type: str

    duration_known: bool = True
    airline: str | None = None
    """Marketing carrier code for this single segment, when Google returns it."""


@dataclass
class CarbonEmission:
    typical_on_route: int
    """Unit: grams"""

    emission: int
    """Unit: grams"""


@dataclass
class Flights:
    type: str | Literal["multi"]
    price: int | None
    """Fare amount, or None when Google returns an unpriced itinerary."""
    airlines: list[str]
    flights: list[SingleFlight]
    carbon: CarbonEmission


RuleStatus = Literal["satisfied", "unknown", "violated"]
"""Outcome of checking one rule against one itinerary leg.

- ``satisfied``: every field the rule needs was present and the leg complies.
- ``violated``: every field the rule needs was present and the leg does not comply.
- ``unknown``: required times, durations or airport fields were missing, so the
  rule can neither pass nor fail. Missing data is never treated as a pass.
"""

RuleName = Literal[
    "max_stops",
    "earliest_departure_hour",
    "latest_departure_hour",
    "earliest_arrival_hour",
    "latest_arrival_hour",
    "max_duration_minutes",
    "connecting_airports",
    "min_layover_minutes",
    "max_layover_minutes",
    "airlines",
]

RuleSource = Literal["leg", "global"]
"""Where an effective constraint came from.

- ``leg``: declared explicitly on the leg's :class:`FlightQuery`.
- ``global``: the leg left the field unset and inherited ``create_query``'s
  whole-search value (only possible for ``max_stops``).
"""


@dataclass
class RuleResult:
    """One rule check on one itinerary leg."""

    rule: RuleName
    status: RuleStatus
    expected: object
    """The constraint value requested by the query."""

    actual: object
    """The observed value, or ``None`` when it could not be determined."""

    source: RuleSource = "leg"
    reason: str | None = None
    """Human-readable explanation, populated for ``unknown``/``violated``."""


@dataclass
class LegAudit:
    """Audit of a single queried leg within one itinerary.

    ``segments`` holds the parsed segments assigned to this leg by matching
    airport chains. ``matched`` is ``False`` when no compliant chain could be
    formed (missing airport codes or a broken chain); ``rules`` are still
    evaluated on whatever segments were assigned, but every time/field-based
    check degrades to ``unknown`` instead of silently passing.
    """

    leg_index: int
    from_airport: str | None
    to_airport: str | None
    segments: list[SingleFlight] = field(default_factory=list)
    matched: bool = False
    rules: list[RuleResult] = field(default_factory=list)


@dataclass
class ItineraryAudit:
    """Rule audit for one parsed :class:`Flights` itinerary.

    The itinerary is ``complete`` only when every queried leg was matched and
    no parsed segment was left unassigned. Unpriced and incomplete itineraries
    are audited and returned alongside successful ones, in the parser's order.
    """

    index: int
    flights: Flights
    legs: list[LegAudit]
    complete: bool

    @property
    def price(self) -> int | None:
        return self.flights.price

    @property
    def status(self) -> RuleStatus:
        """Roll-up of every leg rule, plus structural completeness.

        A definite violation dominates; otherwise any ``unknown`` rule or an
        incomplete structure makes the whole itinerary ``unknown``; only a
        complete itinerary whose rules all pass is ``satisfied``.
        """
        statuses = [rule.status for leg in self.legs for rule in leg.rules]
        if "violated" in statuses:
            return "violated"
        if not self.complete or "unknown" in statuses:
            return "unknown"
        return "satisfied"
