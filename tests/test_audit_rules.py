# [AI CONTENT]
"""Focused unit tests for individual rule checks in the audit engine.

These build minimal parsed objects directly, so each rule's
satisfied/unknown/violated boundary can be asserted without a full payload.
"""

import unittest

from fast_flights import FlightQuery, audit_itineraries, create_query
from fast_flights.model import (
    Airport,
    CarbonEmission,
    Flights,
    SimpleDatetime,
    SingleFlight,
)


def moment(hour, minute, *, time_known=True, date_known=True):
    return SimpleDatetime(
        date=(2026, 12, 1),
        time=(hour, minute),
        time_known=time_known,
        date_known=date_known,
    )


def segment(origin, destination, departure, arrival, *, duration=100,
            duration_known=True, airline="JL"):
    return SingleFlight(
        from_airport=Airport(code=origin, name=f"{origin} Airport"),
        to_airport=Airport(code=destination, name=f"{destination} Airport"),
        departure=departure,
        arrival=arrival,
        duration=duration,
        duration_known=duration_known,
        plane_type="Test aircraft",
        airline=airline,
    )


def itinerary(*segments, price=100, typ="multi"):
    return Flights(
        type=typ,
        price=price,
        airlines=["Test Airline"],
        flights=list(segments),
        carbon=CarbonEmission(typical_on_route=200000, emission=180000),
    )


def query_with(constraints):
    query = create_query(
        flights=[
            FlightQuery(
                date="2026-12-01",
                from_airport="AAA",
                to_airport="BBB",
            )
        ]
    )
    query.leg_constraints = constraints
    return query


def rule_map(audit, leg=0):
    return {rule.rule: rule for rule in audit.legs[leg].rules}


class HourRuleTests(unittest.TestCase):
    def _query(self, **filters):
        query = create_query(
            flights=[
                FlightQuery(
                    date="2026-12-01", from_airport="AAA", to_airport="BBB", **filters
                )
            ]
        )
        return query

    def test_departure_window_satisfied_on_boundary_and_violated_outside(self):
        flights = [
            itinerary(segment("AAA", "BBB", moment(7, 0), moment(9, 0))),
            itinerary(segment("AAA", "BBB", moment(6, 59), moment(9, 0))),
        ]
        query = self._query(earliest_departure_hour=7, latest_departure_hour=18)
        audits = audit_itineraries(query, flights)

        first = rule_map(audits[0])
        self.assertEqual(first["earliest_departure_hour"].status, "satisfied")
        self.assertEqual(first["latest_departure_hour"].status, "satisfied")

        second = rule_map(audits[1])
        self.assertEqual(second["earliest_departure_hour"].status, "violated")
        self.assertEqual(second["earliest_departure_hour"].actual, 6)

    def test_missing_departure_time_is_unknown_not_satisfied(self):
        flights = [
            itinerary(
                segment(
                    "AAA", "BBB", moment(0, 0, time_known=False), moment(9, 0)
                )
            )
        ]
        query = self._query(earliest_departure_hour=7)
        audit = audit_itineraries(query, flights)[0]

        rule = rule_map(audit)["earliest_departure_hour"]
        self.assertEqual(rule.status, "unknown")
        self.assertIsNone(rule.actual)
        self.assertIn("missing", rule.reason)
        self.assertEqual(audit.status, "unknown")

    def test_missing_arrival_time_keeps_departure_rule_decisive(self):
        flights = [
            itinerary(
                segment(
                    "AAA",
                    "BBB",
                    moment(8, 0),
                    moment(0, 0, time_known=False),
                )
            )
        ]
        query = self._query(
            earliest_departure_hour=7, latest_arrival_hour=20
        )
        rules = rule_map(audit_itineraries(query, flights)[0])
        self.assertEqual(rules["earliest_departure_hour"].status, "satisfied")
        self.assertEqual(rules["latest_arrival_hour"].status, "unknown")


class DurationRuleTests(unittest.TestCase):
    def test_total_segment_duration_is_checked(self):
        flights = [
            itinerary(
                segment("AAA", "CCC", moment(7, 0), moment(8, 0), duration=100),
                segment("CCC", "BBB", moment(9, 0), moment(10, 0), duration=100),
            )
        ]
        query = create_query(
            flights=[
                FlightQuery(
                    date="2026-12-01",
                    from_airport="AAA",
                    to_airport="BBB",
                    max_duration_minutes=200,
                )
            ]
        )
        audit = audit_itineraries(query, flights)[0]
        rule = rule_map(audit)["max_duration_minutes"]
        self.assertEqual(rule.status, "satisfied")
        self.assertEqual(rule.actual, 200)

    def test_duration_limit_violation(self):
        flights = [itinerary(segment("AAA", "BBB", moment(7, 0), moment(9, 0), duration=301))]
        query = create_query(
            flights=[
                FlightQuery(
                    date="2026-12-01",
                    from_airport="AAA",
                    to_airport="BBB",
                    max_duration_minutes=300,
                )
            ]
        )
        rule = rule_map(audit_itineraries(query, flights)[0])["max_duration_minutes"]
        self.assertEqual(rule.status, "violated")
        self.assertEqual(rule.actual, 301)

    def test_missing_duration_is_unknown(self):
        flights = [
            itinerary(
                segment(
                    "AAA",
                    "BBB",
                    moment(7, 0),
                    moment(9, 0),
                    duration=0,
                    duration_known=False,
                )
            )
        ]
        query = create_query(
            flights=[
                FlightQuery(
                    date="2026-12-01",
                    from_airport="AAA",
                    to_airport="BBB",
                    max_duration_minutes=300,
                )
            ]
        )
        rule = rule_map(audit_itineraries(query, flights)[0])["max_duration_minutes"]
        self.assertEqual(rule.status, "unknown")


class ConnectingAirportRuleTests(unittest.TestCase):
    def test_allowed_connection_passes_other_connection_violates(self):
        via_ccc = itinerary(
            segment("AAA", "CCC", moment(7, 0), moment(8, 0)),
            segment("CCC", "BBB", moment(9, 0), moment(10, 0)),
        )
        via_ddd = itinerary(
            segment("AAA", "DDD", moment(7, 0), moment(8, 0)),
            segment("DDD", "BBB", moment(9, 0), moment(10, 0)),
        )
        query = create_query(
            flights=[
                FlightQuery(
                    date="2026-12-01",
                    from_airport="AAA",
                    to_airport="BBB",
                    connecting_airports=["CCC"],
                )
            ]
        )
        audits = audit_itineraries(query, [via_ccc, via_ddd])
        self.assertEqual(rule_map(audits[0])["connecting_airports"].status, "satisfied")
        violated = rule_map(audits[1])["connecting_airports"]
        self.assertEqual(violated.status, "violated")
        self.assertEqual(violated.actual, ["DDD"])

    def test_missing_connection_code_is_unknown(self):
        # The chain endpoints (AAA->BBB) still match, but the intermediate
        # connecting airport's code is absent from the response.
        broken = itinerary(
            segment("AAA", "CCC", moment(7, 0), moment(8, 0)),
            segment("CCC", "BBB", moment(9, 0), moment(10, 0)),
        )
        broken.flights[0].to_airport = Airport(code=None, name=None)
        broken.flights[1].from_airport = Airport(code=None, name=None)

        query = create_query(
            flights=[
                FlightQuery(
                    date="2026-12-01",
                    from_airport="AAA",
                    to_airport="BBB",
                    connecting_airports=["CCC"],
                )
            ]
        )
        audit = audit_itineraries(query, [broken])[0]
        self.assertTrue(audit.complete)
        rule = rule_map(audit)["connecting_airports"]
        self.assertEqual(rule.status, "unknown")


class LayoverRuleTests(unittest.TestCase):
    def _connecting_flights(self):
        return [
            itinerary(
                segment("AAA", "CCC", moment(7, 0), moment(8, 0), duration=60),
                segment("CCC", "BBB", moment(9, 30), moment(11, 0), duration=90),
            ),
            itinerary(
                segment("AAA", "CCC", moment(7, 0), moment(8, 0), duration=60),
                segment("CCC", "BBB", moment(8, 20), moment(9, 0), duration=40),
            ),
        ]

    def test_layover_bounds(self):
        query = create_query(
            flights=[
                FlightQuery(
                    date="2026-12-01",
                    from_airport="AAA",
                    to_airport="BBB",
                    min_layover_minutes=60,
                    max_layover_minutes=120,
                )
            ]
        )
        audits = audit_itineraries(query, self._connecting_flights())
        first = rule_map(audits[0])
        # 90-minute connection: within both bounds.
        self.assertEqual(first["min_layover_minutes"].actual, [90])
        self.assertEqual(first["min_layover_minutes"].status, "satisfied")
        self.assertEqual(first["max_layover_minutes"].status, "satisfied")

        second = rule_map(audits[1])
        # 20-minute connection: violates the minimum, passes the maximum.
        self.assertEqual(second["min_layover_minutes"].status, "violated")
        self.assertEqual(second["max_layover_minutes"].status, "satisfied")

    def test_nonstop_satisfies_layover_bounds(self):
        query = create_query(
            flights=[
                FlightQuery(
                    date="2026-12-01",
                    from_airport="AAA",
                    to_airport="BBB",
                    min_layover_minutes=60,
                )
            ]
        )
        flights = [itinerary(segment("AAA", "BBB", moment(7, 0), moment(9, 0)))]
        rule = rule_map(audit_itineraries(query, flights)[0])["min_layover_minutes"]
        self.assertEqual(rule.status, "satisfied")
        self.assertEqual(rule.actual, [])

    def test_missing_connection_times_are_unknown(self):
        flights = [
            itinerary(
                segment("AAA", "CCC", moment(7, 0), moment(8, 0)),
                segment(
                    "CCC",
                    "BBB",
                    moment(0, 0, time_known=False, date_known=False),
                    moment(11, 0),
                ),
            )
        ]
        query = create_query(
            flights=[
                FlightQuery(
                    date="2026-12-01",
                    from_airport="AAA",
                    to_airport="BBB",
                    min_layover_minutes=60,
                )
            ]
        )
        rule = rule_map(audit_itineraries(query, flights)[0])["min_layover_minutes"]
        self.assertEqual(rule.status, "unknown")


class AirlineRuleTests(unittest.TestCase):
    def test_carrier_code_filter(self):
        allowed = itinerary(
            segment("AAA", "BBB", moment(7, 0), moment(9, 0), airline="JL")
        )
        rejected = itinerary(
            segment("AAA", "BBB", moment(7, 0), moment(9, 0), airline="XX")
        )
        query = create_query(
            flights=[
                FlightQuery(
                    date="2026-12-01",
                    from_airport="AAA",
                    to_airport="BBB",
                    airlines=["JL"],
                )
            ]
        )
        audits = audit_itineraries(query, [allowed, rejected])
        self.assertEqual(rule_map(audits[0])["airlines"].status, "satisfied")
        self.assertEqual(rule_map(audits[1])["airlines"].status, "violated")

    def test_missing_carrier_code_is_unknown(self):
        flights = [
            itinerary(
                segment("AAA", "BBB", moment(7, 0), moment(9, 0), airline=None)
            )
        ]
        query = create_query(
            flights=[
                FlightQuery(
                    date="2026-12-01",
                    from_airport="AAA",
                    to_airport="BBB",
                    airlines=["JL"],
                )
            ]
        )
        self.assertEqual(
            rule_map(audit_itineraries(query, flights)[0])["airlines"].status,
            "unknown",
        )


class UnpricedAndOrderTests(unittest.TestCase):
    def test_unpriced_itinerary_keeps_position_and_audits(self):
        priced = itinerary(segment("AAA", "BBB", moment(7, 0), moment(9, 0)), price=100)
        unpriced = itinerary(segment("AAA", "BBB", moment(8, 0), moment(10, 0)), price=None)
        query = create_query(
            flights=[
                FlightQuery(
                    date="2026-12-01",
                    from_airport="AAA",
                    to_airport="BBB",
                    earliest_departure_hour=7,
                )
            ]
        )
        audits = audit_itineraries(query, [priced, unpriced])
        self.assertEqual([a.price for a in audits], [100, None])
        self.assertTrue(audits[0].complete and audits[1].complete)
        self.assertEqual([a.status for a in audits], ["satisfied", "satisfied"])

    def test_undeclared_rules_are_not_reported(self):
        flights = [itinerary(segment("AAA", "BBB", moment(7, 0), moment(9, 0)))]
        # No filters at all: no RuleResult rows, but the structure is still complete.
        query = create_query(
            flights=[
                FlightQuery(date="2026-12-01", from_airport="AAA", to_airport="BBB")
            ]
        )
        audit = audit_itineraries(query, flights)[0]
        self.assertEqual(audit.legs[0].rules, [])
        self.assertTrue(audit.complete)
        self.assertEqual(audit.status, "satisfied")


if __name__ == "__main__":
    unittest.main()
# [/AI CONTENT]
