---
title: "Rule auditing"
---

# Rule auditing

Google Flights returns flat segment lists. For multi-city procurement you
usually need an answer **per queried leg**: does this itinerary actually
satisfy every constraint, and did the constraint come from the leg itself or
from a whole-search default? `audit_itineraries()` re-groups each itinerary's
segments into your query's legs and checks every rule you declared.

```python
from fast_flights import (
    FlightQuery,
    create_query,
    get_flights,
    audit_itineraries,
)

query = create_query(
    flights=[
        FlightQuery(date="2026-12-01", from_airport="TPE", to_airport="NRT",
                    max_stops=0, earliest_departure_hour=8),
        FlightQuery(date="2026-12-05", from_airport="NRT", to_airport="ICN",
                    latest_arrival_hour=20),
    ],
    trip="multi-city",
    max_stops=1,  # global fallback only
)

flights = get_flights(query)
audits = audit_itineraries(query, flights)

for audit in audits:
    print(audit.index, audit.price, audit.status, audit.complete)
    for leg in audit.legs:
        for rule in leg.rules:
            print(" ", leg.leg_index, rule.rule, rule.status,
                  rule.expected, "->", rule.actual, f"({rule.source})")
```

## Explicit leg limits win

`create_query(..., max_stops=...)` is a **fallback**. It is applied only to
legs whose own `max_stops` is unset; an explicit per-leg value is never
overwritten. Each `Query` carries a `leg_constraints` snapshot recording the
effective value and its origin:

- `source="leg"` — declared on the `FlightQuery`;
- `source="global"` — the leg left it unset and inherited the whole-search
  value.

The snapshot is audit metadata. The serialized `?tfs` bytes, `to_bytes()`,
`to_str()`, `url()` and `params()` are unchanged.

## Rule statuses

Every reported rule resolves to one of three statuses:

| Status | Meaning |
|---|---|
| `satisfied` | All fields the rule needs were present and the leg complies. |
| `violated` | All fields the rule needs were present and the leg does not comply. |
| `unknown` | Required times, durations or airport codes were missing, or the leg could not be matched to the itinerary. A missing field is **never** treated as a pass. |

Rules the query never declares are not reported. The itinerary-level
`status` rolls up every leg rule: a definite `violated` dominates; otherwise
any `unknown` rule or an incomplete structure makes it `unknown`; only a
complete itinerary whose rules all pass is `satisfied`.

## Grouping and completeness

Segments are assigned to legs in order by following airport chains
(`from_airport` → `to_airport`). An itinerary is `complete` only when every
queried leg was matched and no segment was left unassigned. Unpriced
itineraries (`price is None`) and incomplete ones are returned alongside
successful results, one-for-one with the parser output and in the same
order — successful entries are never dropped or reordered.

## Audited rules

Per-leg rules from `FlightQuery`:

- `max_stops` (with `leg`/`global` source),
- `earliest_departure_hour`, `latest_departure_hour`,
- `earliest_arrival_hour`, `latest_arrival_hour`,
- `max_duration_minutes`,
- `connecting_airports`,
- `min_layover_minutes`, `max_layover_minutes`,
- `airlines`.

Airline alliance tokens (`SKYTEAM`, `STAR_ALLIANCE`, `ONEWORLD`) resolve to
`unknown` because membership cannot be resolved locally; plain IATA carrier
codes are checked directly. `less_emissions_only` is not audited: the
response carries no threshold to compare the filter against.
