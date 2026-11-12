import typing
from base64 import b64encode
from dataclasses import dataclass, field
from datetime import datetime as Datetime
from typing import Literal

from typing_extensions import override

from .pb.flights_pb2 import (
    Airport,
    Baggage,
    Emissions,
    FlightData,
    Info,
    Passenger,
    Seat,
    Trip,
)
from .types import Currency, Language, SeatType, TripType


@dataclass
class LegConstraints:
    """Audit snapshot of the filters that apply to one queried flight leg.

    This mirrors the per-leg fields of :class:`FlightQuery` after the global
    ``create_query(..., max_stops=...)`` fallback has been resolved, so rule
    checks can always show where an effective value came from. The protobuf
    request is still serialized from ``FlightData``; this snapshot is metadata
    for local rule auditing and never leaves the process.
    """

    date: str
    from_airport: str
    to_airport: str
    max_stops: int | None = None
    max_stops_source: Literal["leg", "global"] | None = None
    airlines: list[str] | None = None
    earliest_departure_hour: int | None = None
    latest_departure_hour: int | None = None
    earliest_arrival_hour: int | None = None
    latest_arrival_hour: int | None = None
    max_duration_minutes: int | None = None
    connecting_airports: list[str] | None = None
    min_layover_minutes: int | None = None
    max_layover_minutes: int | None = None
    less_emissions_only: bool = False


@dataclass
class Query:
    """A query containing `?tfs` data.

    Essentially, it's a container for user-specified queries.
    """

    flight_data: list[FlightData]
    seat: Seat
    trip: Trip
    passengers: list[Passenger]
    language: str
    currency: str
    max_price: int | None = None
    carry_on_bags: int = 0
    checked_bags: int = 0
    hide_separate_and_self_transfer: bool = False
    exclude_basic_economy: bool = False
    leg_constraints: list[LegConstraints] = field(default_factory=list)
    """Resolved per-leg filters, kept for rule auditing (never serialized)."""

    def pb(self) -> Info:
        """(internal) Protobuf data. (`Info`)"""
        baggage = (
            Baggage(
                carry_on_bags=self.carry_on_bags,
                checked_bags=self.checked_bags,
            )
            if self.carry_on_bags or self.checked_bags
            else None
        )
        return Info(
            data=self.flight_data,
            seat=self.seat,
            trip=self.trip,
            passengers=self.passengers,
            max_price=self.max_price,
            baggage=baggage,
            hide_separate_and_self_transfer=(
                True if self.hide_separate_and_self_transfer else None
            ),
            exclude_basic_economy=True if self.exclude_basic_economy else None,
        )

    def to_bytes(self) -> bytes:
        """Convert this query to bytes."""
        return self.pb().SerializeToString()

    def to_str(self) -> str:
        """Convert this query to a string."""
        return b64encode(self.to_bytes()).decode("utf-8")

    def url(self) -> str:
        """Get the URL for this query.

        This is generally used for debugging purposes.
        """
        return (
            "https://www.google.com/travel/flights/search?tfs="
            + self.to_str()
            + "&tfu=EgQIABABIgA"
            + "&hl="
            + self.language
            + "&curr="
            + self.currency
        )

    def params(self) -> dict[str, str]:
        """Create `params` in dictionary form."""
        return {
            "tfs": self.to_str(),
            "hl": self.language,
            "tfu": "EgQIABABIgA",  # show all flights and prices condition
            "curr": self.currency,
        }

    def get_trip_type(self) -> TripType:
        data = REVERSE_TRIP_LOOKUP[self.trip]
        if data is None:
            raise TypeError("malformed trip type: 0")

        return typing.cast(TripType, data)

    def get_seat_type(self) -> SeatType:
        data = REVERSE_SEAT_LOOKUP[self.seat]
        if data is None:
            raise TypeError("malformed seat type: 0")

        return typing.cast(SeatType, data)

    @override
    def __repr__(self) -> str:
        return "Query(...)"


@dataclass
class FlightQuery:
    date: str | Datetime
    from_airport: str
    to_airport: str
    max_stops: int | None = None
    airlines: list[str] | None = None
    earliest_departure_hour: int | None = None
    latest_departure_hour: int | None = None
    earliest_arrival_hour: int | None = None
    latest_arrival_hour: int | None = None
    max_duration_minutes: int | None = None
    connecting_airports: list[str] | None = None
    min_layover_minutes: int | None = None
    max_layover_minutes: int | None = None
    less_emissions_only: bool = False

    def pb(self) -> FlightData:
        if isinstance(self.date, str):
            date = self.date
        else:
            date = self.date.strftime("%Y-%m-%d")

        return FlightData(
            date=date,
            from_airport=Airport(airport=self.from_airport),
            to_airport=Airport(airport=self.to_airport),
            max_stops=self.max_stops,
            airlines=self.airlines,
            earliest_departure_hour=self.earliest_departure_hour,
            latest_departure_hour=self.latest_departure_hour,
            earliest_arrival_hour=self.earliest_arrival_hour,
            latest_arrival_hour=self.latest_arrival_hour,
            max_duration_minutes=self.max_duration_minutes,
            connecting_airports=self.connecting_airports,
            min_layover_minutes=self.min_layover_minutes,
            max_layover_minutes=self.max_layover_minutes,
            emissions=([Emissions.LESS_EMISSIONS] if self.less_emissions_only else []),
        )

    def with_max_stops(self, m: int | None = None) -> "FlightQuery":
        # Explicit per-leg limits win: a global `max_stops` only fills in
        # legs that did not declare their own value, it never overwrites one.
        if m is not None and self.max_stops is None:
            self.max_stops = m

        return self


@dataclass
class Passengers:
    adults: int = 0
    children: int = 0
    infants_in_seat: int = 0
    infants_on_lap: int = 0

    def __post_init__(self):
        assert (
            sum((self.adults, self.children, self.infants_in_seat, self.infants_on_lap))
            <= 9
        ), "Too many passengers (> 9)"
        assert self.infants_on_lap <= self.adults, (
            "Must have at least one adult per infant on lap"
        )

    def pb(self) -> list[Passenger]:
        return [
            *(Passenger.ADULT for _ in range(self.adults)),
            *(Passenger.CHILD for _ in range(self.children)),
            *(Passenger.INFANT_IN_SEAT for _ in range(self.infants_in_seat)),
            *(Passenger.INFANT_ON_LAP for _ in range(self.infants_on_lap)),
        ]


SEAT_LOOKUP = {
    "economy": Seat.ECONOMY,
    "premium-economy": Seat.PREMIUM_ECONOMY,
    "business": Seat.BUSINESS,
    "first": Seat.FIRST,
}
REVERSE_SEAT_LOOKUP = [None, "economy", "premium-economy", "business", "first"]

TRIP_LOOKUP = {
    "round-trip": Trip.ROUND_TRIP,
    "one-way": Trip.ONE_WAY,
    "multi-city": Trip.MULTI_CITY,
}
REVERSE_TRIP_LOOKUP = [
    None,
    "round-trip",
    "one-way",
    "multi-city",
]


def _date_str(date: str | Datetime) -> str:
    return date if isinstance(date, str) else date.strftime("%Y-%m-%d")


def create_query(
    *,
    flights: list[FlightQuery],
    seat: SeatType = "economy",
    trip: TripType = "one-way",
    passengers: Passengers | None = None,
    language: str | Literal[""] | Language = "",
    currency: str | Literal[""] | Currency = "",
    max_stops: int | None = None,
    max_price: int | None = None,
    carry_on_bags: int = 0,
    checked_bags: int = 0,
    hide_separate_and_self_transfer: bool = False,
    exclude_basic_economy: bool = False,
) -> Query:
    """Create a query.

    Args:
        flights: The flight queries.
        seat: Desired seat type.
        trip: Trip type.
        passengers: Passengers.
        language: Set the language. Use `""` (blank str) to let Google decide.
        currency: Set the currency. Use `""` (blank str) to let Google decide.
        max_stops (optional): Set the maximum stops for every flight query
            whose own ``max_stops`` is unset. Explicit per-leg values are kept
            and take precedence over this global fallback.
        max_price: Maximum price in the selected currency.
        carry_on_bags: Carry-on bags whose estimated fees should be included.
        checked_bags: Checked bags whose estimated fees should be included.
        hide_separate_and_self_transfer: Hide separate-ticket and self-transfer
            itineraries.
        exclude_basic_economy: Exclude basic economy fares.
    """
    leg_constraints: list[LegConstraints] = []
    flight_data: list[FlightData] = []

    for flight in flights:
        # Record the rule's origin before resolving the global fallback, so
        # audits can distinguish an explicit leg limit from an inherited one.
        stops_source = (
            "leg"
            if flight.max_stops is not None
            else ("global" if max_stops is not None else None)
        )
        leg_constraints.append(
            LegConstraints(
                date=_date_str(flight.date),
                from_airport=flight.from_airport,
                to_airport=flight.to_airport,
                max_stops=flight.max_stops if flight.max_stops is not None else max_stops,
                max_stops_source=stops_source,
                airlines=list(flight.airlines) if flight.airlines is not None else None,
                earliest_departure_hour=flight.earliest_departure_hour,
                latest_departure_hour=flight.latest_departure_hour,
                earliest_arrival_hour=flight.earliest_arrival_hour,
                latest_arrival_hour=flight.latest_arrival_hour,
                max_duration_minutes=flight.max_duration_minutes,
                connecting_airports=(
                    list(flight.connecting_airports)
                    if flight.connecting_airports is not None
                    else None
                ),
                min_layover_minutes=flight.min_layover_minutes,
                max_layover_minutes=flight.max_layover_minutes,
                less_emissions_only=flight.less_emissions_only,
            )
        )
        flight_data.append(flight.with_max_stops(max_stops).pb())

    return Query(
        flight_data=flight_data,
        seat=SEAT_LOOKUP[seat],
        trip=TRIP_LOOKUP[trip],
        passengers=(passengers or Passengers(adults=1)).pb(),
        language=language,
        currency=currency,
        max_price=max_price,
        carry_on_bags=carry_on_bags,
        checked_bags=checked_bags,
        hide_separate_and_self_transfer=hide_separate_and_self_transfer,
        exclude_basic_economy=exclude_basic_economy,
        leg_constraints=leg_constraints,
    )
