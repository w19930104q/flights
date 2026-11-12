# [AI CONTENT]
"""Regression tests for query-bound itinerary rule auditing.

The fixed JS fixture (``fixtures/multi_city_audit.json``) is hand-built with
``build_fixture.py`` and contains, across Google's best/other result groups:

* a complete priced compliant multi-city itinerary,
* a priced itinerary violating an explicit leg rule,
* an unpriced (no fare) itinerary,
* an incomplete itinerary with missing arrival/date/duration fields whose
  chain does not cover every queried leg,
* a compliant priced itinerary in the second group.

No live requests are made and no booking tokens are needed.
"""

import pathlib
import unittest

from fast_flights import FlightQuery, audit_itineraries, create_query
from fast_flights.parser import parse_js
from fast_flights.querying import LegConstraints

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load_flights():
    payload = (FIXTURES / "multi_city_audit.json").read_text()
    return parse_js(f"data:{payload},ignored")


def _multi_city_query():
    # Leg 1 declares its own max_stops: the global fallback must not overwrite it.
    return create_query(
        flights=[
            FlightQuery(
                date="2026-12-01",
                from_airport="TPE",
                to_airport="NRT",
                max_stops=0,
                earliest_departure_hour=8,
                max_duration_minutes=300,
                connecting_airports=["HND"],
                min_layover_minutes=30,
                max_layover_minutes=120,
                airlines=["JL", "NH"],
            ),
            FlightQuery(
                date="2026-12-01",
                from_airport="NRT",
                to_airport="ICN",
                earliest_departure_hour=8,
                latest_arrival_hour=20,
                max_duration_minutes=300,
                airlines=["JL", "NH"],
            ),
        ],
        trip="multi-city",
        max_stops=1,
    )


def _rules(audit, leg_index):
    return {rule.rule: rule for rule in audit.legs[leg_index].rules}


class MultiCityAuditFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.flights = _load_flights()
        cls.query = _multi_city_query()
        cls.audits = audit_itineraries(cls.query, cls.flights)

    def test_one_audit_per_itinerary_in_stable_order(self):
        self.assertEqual(len(self.audits), len(self.flights))
        self.assertEqual([a.index for a in self.audits], [0, 1, 2, 3, 4])
        # Same Flights objects, same order: priced, unpriced and incomplete.
        self.assertEqual([a.price for a in self.audits], [420, 510, None, 380, 460])
        for audit, itinerary in zip(self.audits, self.flights):
            self.assertIs(audit.flights, itinerary)

    def test_segments_are_grouped_by_queried_legs(self):
        first = self.audits[0]
        self.assertTrue(first.complete)
        self.assertEqual(len(first.legs), 2)
        self.assertEqual(
            [s.to_airport.code for s in first.legs[0].segments], ["NRT"]
        )
        self.assertEqual(
            [s.from_airport.code for s in first.legs[1].segments], ["NRT"]
        )
        self.assertEqual(first.legs[0].to_airport, "NRT")
        self.assertEqual(first.legs[1].to_airport, "ICN")

    def test_compliant_itinerary_passes_every_declared_rule(self):
        audit = self.audits[0]
        self.assertEqual(audit.status, "satisfied")
        for leg in audit.legs:
            self.assertTrue(leg.matched)
            self.assertTrue(leg.rules)
            self.assertEqual({r.status for r in leg.rules}, {"satisfied"})

    def test_explicit_leg_max_stops_takes_precedence_over_global(self):
        constraints: list[LegConstraints] = self.query.leg_constraints
        self.assertEqual(constraints[0].max_stops, 0)
        self.assertEqual(constraints[0].max_stops_source, "leg")
        self.assertEqual(constraints[1].max_stops, 1)
        self.assertEqual(constraints[1].max_stops_source, "global")

        # Itinerary 1 has one connection on leg 1: violates the explicit 0,
        # even though the whole-search fallback would have allowed 1.
        rules = _rules(self.audits[1], 0)
        self.assertEqual(rules["max_stops"].status, "violated")
        self.assertEqual(rules["max_stops"].expected, 0)
        self.assertEqual(rules["max_stops"].actual, 1)
        self.assertEqual(rules["max_stops"].source, "leg")
        self.assertEqual(self.audits[1].status, "violated")

    def test_constraint_conflict_is_reported_with_expected_and_actual(self):
        rules = _rules(self.audits[1], 1)
        latest_arrival = rules["latest_arrival_hour"]
        self.assertEqual(latest_arrival.status, "violated")
        self.assertEqual(latest_arrival.expected, 20)
        self.assertEqual(latest_arrival.actual, 22)
        self.assertIsNotNone(latest_arrival.reason)

    def test_unpriced_itinerary_is_kept_and_audited(self):
        audit = self.audits[2]
        self.assertIsNone(audit.price)
        self.assertTrue(audit.complete)
        self.assertEqual(audit.status, "satisfied")
        # Its neighbors survive on the same page in their original positions.
        self.assertEqual(self.audits[1].price, 510)
        self.assertEqual(self.audits[3].price, 380)

    def test_missing_fields_never_pass_and_break_completeness(self):
        audit = self.audits[3]
        self.assertFalse(audit.complete)

        leg_one = _rules(audit, 0)
        # Arrival time of the TPE-NRT segment is missing: arrival-based rules
        # cannot pass, and this query declares none, but duration still works.
        self.assertEqual(leg_one["max_duration_minutes"].status, "satisfied")
        self.assertEqual(leg_one["earliest_departure_hour"].status, "satisfied")

        # Leg 2 (NRT-ICN) cannot be matched: the itinerary ends at KIX, and
        # its duration field is missing. Every rule for that leg is unknown.
        leg_two = audit.legs[1]
        self.assertFalse(leg_two.matched)
        self.assertEqual(leg_two.segments, [])
        self.assertTrue(leg_two.rules)
        self.assertEqual({r.status for r in leg_two.rules}, {"unknown"})
        self.assertEqual(audit.status, "unknown")

    def test_alliance_tokens_resolve_to_unknown_not_pass(self):
        query = create_query(
            flights=[
                FlightQuery(
                    date="2026-12-01",
                    from_airport="TPE",
                    to_airport="NRT",
                    airlines=["ONEWORLD"],
                )
            ],
            trip="one-way",
        )
        audits = audit_itineraries(query, self.flights)
        rule = _rules(audits[0], 0)["airlines"]
        self.assertEqual(rule.status, "unknown")
        self.assertIsNotNone(rule.reason)

    def test_repeated_parsing_is_deterministic(self):
        # Parsing the same fixture twice must yield identical audit verdicts.
        parsed_again = _load_flights()
        again = audit_itineraries(self.query, parsed_again)

        self.assertEqual(
            [a.status for a in self.audits], [a.status for a in again]
        )
        for left, right in zip(self.audits, again):
            self.assertEqual(left.complete, right.complete)
            for left_leg, right_leg in zip(left.legs, right.legs):
                self.assertEqual(
                    [(r.rule, r.status, r.expected, r.actual, r.source)
                     for r in left_leg.rules],
                    [(r.rule, r.status, r.expected, r.actual, r.source)
                     for r in right_leg.rules],
                )

    def test_repeated_audit_on_the_same_objects_is_stable(self):
        first = audit_itineraries(self.query, self.flights)
        second = audit_itineraries(self.query, self.flights)
        self.assertEqual(
            [a.status for a in first], [a.status for a in second]
        )


if __name__ == "__main__":
    unittest.main()
# [/AI CONTENT]
