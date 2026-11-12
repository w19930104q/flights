"""Query-bound rule auditing for parsed itineraries.

The parser turns Google's response into flat segment lists. Multi-city
procurement, however, needs an answer per queried leg: *does this itinerary
actually satisfy each constraint we asked for, and where did the constraint
come from?*

This module re-groups an itinerary's segments into the query's legs by
following airport chains, then checks every rule the query declared. A rule
whose required times, durations or airport fields are missing resolves to
``unknown`` rather than passing: no field gap is ever audited as a pass.
Rules the query never declared are not reported at all.

Public entry point: :func:`audit_itineraries`.
"""

from datetime import datetime

from .model import (
    Flights,
    ItineraryAudit,
    LegAudit,
    RuleName,
    RuleResult,
    RuleSource,
    RuleStatus,
    SingleFlight,
)
from .querying import LegConstraints, Query

_ALLIANCE_TOKENS = frozenset({"SKYTEAM", "STAR_ALLIANCE", "ONEWORLD"})


def audit_itineraries(query: Query, flights: list[Flights]) -> list[ItineraryAudit]:
    """Audit every itinerary against the legs declared in ``query``.

    Itineraries are returned one-for-one with ``flights`` and in the same
    order, so successful, unpriced (``price is None``) and incomplete
    itineraries coexist on the same page without reordering or dropping
    successful entries.

    Args:
        query: The query the itineraries were fetched with. Its
            ``leg_constraints`` snapshot records the effective per-leg rules
            (and whether ``max_stops`` was inherited from the global value).
        flights: Parsed itineraries, e.g. a :class:`~fast_flights.parser.ResultList`.
    """
    constraints = query.leg_constraints or []
    audits: list[ItineraryAudit] = []

    for index, itinerary in enumerate(flights):
        legs, leftover = _group_segments(itinerary.flights, constraints)
        complete = not leftover and all(leg.matched for leg in legs)
        audits.append(
            ItineraryAudit(
                index=index,
                flights=itinerary,
                legs=legs,
                complete=complete,
            )
        )

    return audits


def _group_segments(
    segments: list[SingleFlight], constraints: list[LegConstraints]
) -> tuple[list[LegAudit], list[SingleFlight]]:
    """Assign segments to queried legs by matching airport chains.

    A leg consumes consecutive segments whose first origin and last
    destination match the queried airports. Segments after the final leg are
    returned as leftovers; either condition makes the itinerary incomplete.
    """
    legs: list[LegAudit] = []
    cursor = 0

    for leg_index, constraint in enumerate(constraints):
        assigned: list[SingleFlight] = []
        matched = False

        if cursor < len(segments):
            first = segments[cursor]
            if _airport_match(first.from_airport.code, constraint.from_airport):
                for offset in range(cursor, len(segments)):
                    last = segments[offset]
                    if not _airport_match(last.to_airport.code, constraint.to_airport):
                        continue

                    assigned = segments[cursor : offset + 1]
                    cursor = offset + 1
                    matched = True
                    break

        legs.append(
            LegAudit(
                leg_index=leg_index,
                from_airport=constraint.from_airport,
                to_airport=constraint.to_airport,
                segments=assigned,
                matched=matched,
                rules=_check_rules(constraint, assigned, matched),
            )
        )

    return legs, segments[cursor:]


def _airport_match(actual: str | None, expected: str | None) -> bool:
    # A missing airport code cannot confirm or refute the chain.
    if actual is None or expected is None:
        return False
    return actual.upper() == expected.upper()


def _check_rules(
    constraint: LegConstraints, segments: list[SingleFlight], matched: bool
) -> list[RuleResult]:
    if not segments:
        # Nothing observable to check against: declared rules stay unknown.
        return _unmatched_rules(constraint)

    rules: list[RuleResult] = []

    max_stops = _check_max_stops(constraint, segments, matched)
    if max_stops is not None:
        rules.append(max_stops)
    rules.extend(rule for rule in _check_time_rules(constraint, segments) if rule is not None)

    max_duration = _check_max_duration(constraint, segments)
    if max_duration is not None:
        rules.append(max_duration)

    connections = _check_connecting_airports(constraint, segments, matched)
    if connections is not None:
        rules.append(connections)
    rules.extend(rule for rule in _check_layovers(constraint, segments) if rule is not None)

    airlines = _check_airlines(constraint, segments)
    if airlines is not None:
        rules.append(airlines)

    return rules


def _unmatched_rules(constraint: LegConstraints) -> list[RuleResult]:
    """Rules for a leg whose segments could not be located in the itinerary."""
    rules: list[RuleResult] = []
    if constraint.max_stops is not None:
        rules.append(
            _result(
                "max_stops",
                "unknown",
                constraint.max_stops,
                None,
                source=constraint.max_stops_source or "leg",
                reason="leg chain could not be matched; stop count is undetermined",
            )
        )
    for rule_name, limit in (
        ("earliest_departure_hour", constraint.earliest_departure_hour),
        ("latest_departure_hour", constraint.latest_departure_hour),
        ("earliest_arrival_hour", constraint.earliest_arrival_hour),
        ("latest_arrival_hour", constraint.latest_arrival_hour),
        ("max_duration_minutes", constraint.max_duration_minutes),
        ("min_layover_minutes", constraint.min_layover_minutes),
        ("max_layover_minutes", constraint.max_layover_minutes),
    ):
        if limit is not None:
            rules.append(
                _result(
                    rule_name,  # type: ignore[arg-type]
                    "unknown",
                    limit,
                    None,
                    reason="leg chain could not be matched; value is undetermined",
                )
            )
    if constraint.connecting_airports:
        rules.append(
            _result(
                "connecting_airports",
                "unknown",
                constraint.connecting_airports,
                None,
                reason="leg chain could not be matched; connections are undetermined",
            )
        )
    if constraint.airlines:
        rules.append(
            _result(
                "airlines",
                "unknown",
                constraint.airlines,
                None,
                reason="leg chain could not be matched; carriers are undetermined",
            )
        )
    return rules


def _result(
    rule: RuleName,
    status: RuleStatus,
    expected: object,
    actual: object,
    *,
    source: RuleSource = "leg",
    reason: str | None = None,
) -> RuleResult:
    return RuleResult(
        rule=rule,
        status=status,
        expected=expected,
        actual=actual,
        source=source,
        reason=reason,
    )


def _check_max_stops(
    constraint: LegConstraints, segments: list[SingleFlight], matched: bool
) -> RuleResult | None:
    limit = constraint.max_stops
    if limit is None:
        return None
    source = constraint.max_stops_source or "leg"

    if not matched:
        return _result(
            "max_stops",
            "unknown",
            limit,
            None,
            source=source,
            reason="leg chain could not be matched; stop count is undetermined",
        )

    stops = len(segments) - 1
    status: RuleStatus = "satisfied" if stops <= limit else "violated"
    return _result(
        "max_stops",
        status,
        limit,
        stops,
        source=source,
        reason=None if status == "satisfied" else "itinerary has more stops than allowed",
    )


def _check_time_rules(
    constraint: LegConstraints, segments: list[SingleFlight]
) -> list[RuleResult | None]:
    first, last = segments[0], segments[-1]
    return [
        _hour_rule(
            "earliest_departure_hour",
            constraint.earliest_departure_hour,
            first,
            "departure",
            lambda hour, limit: hour >= limit,
            "departs before the earliest allowed hour",
        ),
        _hour_rule(
            "latest_departure_hour",
            constraint.latest_departure_hour,
            first,
            "departure",
            lambda hour, limit: hour <= limit,
            "departs after the latest allowed hour",
        ),
        _hour_rule(
            "earliest_arrival_hour",
            constraint.earliest_arrival_hour,
            last,
            "arrival",
            lambda hour, limit: hour >= limit,
            "arrives before the earliest allowed hour",
        ),
        _hour_rule(
            "latest_arrival_hour",
            constraint.latest_arrival_hour,
            last,
            "arrival",
            lambda hour, limit: hour <= limit,
            "arrives after the latest allowed hour",
        ),
    ]


def _hour_rule(
    rule: RuleName,
    limit: int | None,
    segment: SingleFlight,
    endpoint: str,
    complies,
    violation_reason: str,
) -> RuleResult | None:
    if limit is None:
        return None

    moment = segment.departure if endpoint == "departure" else segment.arrival
    if not moment.time_known:
        return _result(
            rule,
            "unknown",
            limit,
            None,
            reason=f"{endpoint} time is missing from the response",
        )

    hour = moment.time[0]
    status: RuleStatus = "satisfied" if complies(hour, limit) else "violated"
    return _result(
        rule,
        status,
        limit,
        hour,
        reason=None if status == "satisfied" else violation_reason,
    )


def _check_max_duration(
    constraint: LegConstraints, segments: list[SingleFlight]
) -> RuleResult | None:
    limit = constraint.max_duration_minutes
    if limit is None:
        return None

    if any(not segment.duration_known for segment in segments):
        return _result(
            "max_duration_minutes",
            "unknown",
            limit,
            None,
            reason="one or more segment durations are missing",
        )

    total = sum(segment.duration for segment in segments)
    status: RuleStatus = "satisfied" if total <= limit else "violated"
    return _result(
        "max_duration_minutes",
        status,
        limit,
        total,
        reason=None if status == "satisfied" else "leg duration exceeds the limit",
    )


def _check_connecting_airports(
    constraint: LegConstraints, segments: list[SingleFlight], matched: bool
) -> RuleResult | None:
    required = constraint.connecting_airports
    if not required:
        return None

    if not matched:
        return _result(
            "connecting_airports",
            "unknown",
            required,
            None,
            reason="leg chain could not be matched; connections are undetermined",
        )

    if any(
        segment.from_airport.code is None for segment in segments[1:]
    ) or any(segment.to_airport.code is None for segment in segments[:-1]):
        return _result(
            "connecting_airports",
            "unknown",
            required,
            None,
            reason="a connecting airport code is missing from the response",
        )

    required_set = {code.upper() for code in required}
    actual = [segment.from_airport.code.upper() for segment in segments[1:]]
    if all(code in required_set for code in actual):
        return _result("connecting_airports", "satisfied", required, actual)
    return _result(
        "connecting_airports",
        "violated",
        required,
        actual,
        reason="itinerary connects through an airport outside the allowed list",
    )


def _check_layovers(
    constraint: LegConstraints, segments: list[SingleFlight]
) -> list[RuleResult | None]:
    return [
        _layover_rule(
            "min_layover_minutes",
            constraint.min_layover_minutes,
            segments,
            lambda layover, limit: layover >= limit,
            "a connection is shorter than the minimum layover",
        ),
        _layover_rule(
            "max_layover_minutes",
            constraint.max_layover_minutes,
            segments,
            lambda layover, limit: layover <= limit,
            "a connection exceeds the maximum layover",
        ),
    ]


def _layover_rule(
    rule: RuleName,
    limit: int | None,
    segments: list[SingleFlight],
    complies,
    reason: str,
) -> RuleResult | None:
    if limit is None:
        return None

    layovers: list[int] = []
    # zip(a, a[1:]) works on every supported Python (>=3.10) and matches the
    # codebase's existing iteration style.
    for previous, current in zip(segments, segments[1:]):  # noqa: RUF007
        arrived = _to_datetime(previous.arrival)
        departed = _to_datetime(current.departure)
        if arrived is None or departed is None:
            return _result(
                rule,
                "unknown",
                limit,
                None,
                reason="connection times are missing from the response",
            )
        layovers.append(int((departed - arrived).total_seconds() // 60))

    if not layovers:
        # A nonstop leg has no connections to constrain.
        return _result(rule, "satisfied", limit, [])

    if all(complies(layover, limit) for layover in layovers):
        return _result(rule, "satisfied", limit, layovers)
    return _result(rule, "violated", limit, layovers, reason=reason)


def _to_datetime(moment) -> datetime | None:
    if not moment.time_known or not moment.date_known:
        return None
    year, month, day = moment.date
    hour, minute = moment.time
    if not year or not month or not day:
        return None
    return datetime(year, month, day, hour, minute)


def _check_airlines(
    constraint: LegConstraints, segments: list[SingleFlight]
) -> RuleResult | None:
    wanted = constraint.airlines
    if not wanted:
        return None

    carriers: list[str] = []
    for segment in segments:
        if segment.airline is None:
            return _result(
                "airlines",
                "unknown",
                wanted,
                [segment.airline for segment in segments],
                reason="a segment's marketing carrier code is missing",
            )
        carriers.append(segment.airline)

    # Alliance tokens cannot be resolved without membership tables; report
    # them as unknown rather than guessing a pass or failure.
    if any(code.upper() in _ALLIANCE_TOKENS for code in wanted):
        return _result(
            "airlines",
            "unknown",
            wanted,
            carriers,
            reason="alliance membership cannot be resolved locally",
        )

    allowed = {code.upper() for code in wanted}
    actual = [carrier.upper() for carrier in carriers]
    if all(code in allowed for code in actual):
        return _result("airlines", "satisfied", wanted, actual)
    return _result(
        "airlines",
        "violated",
        wanted,
        actual,
        reason="a segment is operated by a carrier outside the allowed list",
    )
