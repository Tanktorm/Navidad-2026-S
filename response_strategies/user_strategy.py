"""Contestant strategy: route shipments by expected time instead of distance.

The default strategy picks the path with the shortest **sailing distance**. That
ignores everything that actually makes a container late: how often a service
calls at the port, whether it has to change vessel on the way, and how much
cargo is already queued for the same departure. This strategy keeps the same
decision points but scores paths with a single cost expressed in hours::

    cost = sailing_hours + boarding_wait_hours + transfer_hours + queue_hours

* ``sailing_hours`` uses each leg's live ``sailing_time_multiplier``, so a
  congested leg is priced at what it really costs instead of being banned.
* ``boarding_wait_hours`` is ``WAIT_FRACTION`` of the service headway, and the
  headway is derived from the route's own cycle time and vessel count — a
  service with one vessel is correctly treated as an infrequent service.
* ``transfer_hours`` adds ``TRANSFER_BUFFER_HOURS`` every time the path changes
  service route, so transshipments stop looking free.
* ``queue_hours`` converts the TEU already waiting for that **port + route**
  departure into the number of extra headways they push this shipment back.

The shortest path runs over states ``(port, service_route)``: staying on the
same service costs nothing extra, changing services pays the transfer terms.

Nothing here is hardcoded to a port, a route or a date. Ports that are closed
and legs that are congested are read from ``context.disruption_plans`` and from
the live leg multipliers at the moment of the decision.

All tunables live in :mod:`response_strategies.strategy_params`.
"""

from __future__ import annotations

import datetime as dt
import heapq
import itertools
import math
from dataclasses import dataclass

from maritime_data_context import Booking

from . import challenger_routing
from .strategy_params import PARAMS, STRATEGY_ENABLED


# How often (in simulated hours) the port+route queue snapshot is rebuilt.
# The queue only needs to be approximately right; rebuilding it on every
# shipment would dominate the run time.
QUEUE_REFRESH_HOURS = 6.0


@dataclass
class _Edge:
    """One bookable ride: board ``service_route`` at ``departure_port`` and stay
    on it until ``arrival_port``."""

    service_route: object
    departure_port: object
    arrival_port: object
    departure_segment_index: int
    arrival_segment_index: int
    sailing_hours: float


@dataclass
class _RouteInfo:
    cycle_hours: float
    headway_hours: float
    capacity_teu: float


class _Planner:
    """Builds and caches the time-weighted booking graph for one context."""

    def __init__(self, context):
        self._context = context
        self._graph_signature = None
        self._edges_by_port = {}
        self._route_info = {}
        self._queue_teu = {}
        self._queue_refreshed_at = None
        self._tracked_shipments = []

    # -- graph ------------------------------------------------------------

    def _leg_multiplier(self, leg, now):
        """Effective sailing multiplier, including disruptions about to start."""
        if not PARAMS["CONGESTION_AWARE"]:
            return 1.0

        multiplier = float(getattr(leg, "sailing_time_multiplier", 1.0) or 1.0)
        horizon_days = PARAMS["ANTICIPATION_DAYS"]
        if horizon_days <= 0:
            return multiplier

        horizon_end = now + dt.timedelta(days=horizon_days)
        for plan in self._context.disruption_plans:
            if plan.target_leg is not leg or plan.multiplier <= 1:
                continue
            start, end = _plan_window(plan)
            if start is None or start >= horizon_end or end <= now:
                continue
            multiplier = max(multiplier, float(plan.multiplier))
        return multiplier

    def _closed_port_names(self, now):
        """Ports whose berths are closed now, or close within the horizon."""
        if not PARAMS["AVOID_CLOSED_PORTS"]:
            return frozenset()

        horizon_end = now + dt.timedelta(days=max(0.0, PARAMS["ANTICIPATION_DAYS"]))
        closed = set()
        for plan in self._context.disruption_plans:
            if not plan.close_berth or plan.target_berth is None:
                continue
            start, end = _plan_window(plan)
            if start is None or end <= now or start > horizon_end:
                continue
            port = getattr(plan.target_berth, "port", None)
            if port is None:
                continue
            # A port with several berths keeps working while one is closed.
            berths = list(getattr(port, "berths", []) or [])
            closed_berths = sum(
                1
                for other in self._context.disruption_plans
                if other.close_berth
                and other.target_berth is not None
                and getattr(other.target_berth, "port", None) is port
                and _overlaps(_plan_window(other), (now, horizon_end))
            )
            if berths and closed_berths < len(berths):
                continue
            closed.add(port.name.casefold())
        return frozenset(closed)

    def _signature(self, now, closed_ports):
        """Cheap key that changes exactly when the graph would change."""
        return (
            closed_ports,
            tuple(
                (
                    id(route),
                    len(route.deployed_vessels),
                    len(route.segments),
                )
                for route in self._context.service_routes
            ),
            tuple(
                round(self._leg_multiplier(leg, now), 3)
                for leg in self._context.legs
            ),
        )

    def _ensure_graph(self, now):
        closed_ports = self._closed_port_names(now)
        signature = self._signature(now, closed_ports)
        if signature == self._graph_signature:
            return

        port_call_hours = PARAMS["PORT_CALL_HOURS"]
        edges_by_port = {}
        route_info = {}

        for route in self._context.service_routes:
            segments = sorted(
                route.segments, key=lambda segment: segment.sequence_index
            )
            vessels = [
                vessel
                for vessel in route.deployed_vessels
                if vessel.vessel_class is not None
            ]
            if not segments or not vessels:
                continue

            speed = sum(v.vessel_class.sailing_speed for v in vessels) / len(vessels)
            capacity = sum(v.vessel_class.teu_capacity for v in vessels) / len(vessels)
            if speed <= 0 or capacity <= 0:
                continue

            leg_hours = [
                (segment.associated_leg.sailing_distance / speed)
                * self._leg_multiplier(segment.associated_leg, now)
                for segment in segments
            ]
            cycle_hours = sum(leg_hours) + port_call_hours * len(segments)
            route_info[route] = _RouteInfo(
                cycle_hours=cycle_hours,
                headway_hours=cycle_hours / len(vessels),
                capacity_teu=capacity,
            )

            segment_count = len(segments)
            for start_index in range(segment_count):
                departure_port = segments[start_index].associated_leg.departure_port
                if departure_port.name.casefold() in closed_ports:
                    continue
                hours = 0.0
                for step in range(1, segment_count + 1):
                    segment_index = (start_index + step - 1) % segment_count
                    if step > 1:
                        hours += port_call_hours
                    hours += leg_hours[segment_index]
                    arrival_port = segments[segment_index].associated_leg.arrival_port
                    if arrival_port is departure_port:
                        break
                    if arrival_port.name.casefold() in closed_ports:
                        # The service cannot call there, so nothing further
                        # along this rotation is reachable either.
                        break
                    edges_by_port.setdefault(departure_port, []).append(
                        _Edge(
                            service_route=route,
                            departure_port=departure_port,
                            arrival_port=arrival_port,
                            departure_segment_index=start_index + 1,
                            arrival_segment_index=segment_index + 1,
                            sailing_hours=hours,
                        )
                    )

        self._edges_by_port = edges_by_port
        self._route_info = route_info
        self._graph_signature = signature

    # -- queue snapshot ---------------------------------------------------

    def _ensure_queue(self, now):
        if PARAMS["QUEUE_WEIGHT"] <= 0:
            self._queue_teu = {}
            return
        if (
            self._queue_refreshed_at is not None
            and (now - self._queue_refreshed_at).total_seconds()
            < QUEUE_REFRESH_HOURS * 3600.0
        ):
            return

        queue_teu = {}
        still_tracked = []
        for shipment in self._tracked_shipments:
            if shipment.completion_time is not None:
                continue
            still_tracked.append(shipment)
            if shipment.carrying_vessel is not None:
                # Already on board: it is not competing for a departure.
                continue
            try:
                booking = shipment.get_current_booking()
            except ValueError:
                continue
            route = booking.service_route
            if route is None:
                continue
            departure_port = _booking_departure_port(booking)
            if departure_port is None:
                continue
            key = (departure_port, route)
            queue_teu[key] = queue_teu.get(key, 0.0) + shipment.teu_size

        self._tracked_shipments = still_tracked
        self._queue_teu = queue_teu
        self._queue_refreshed_at = now

    def _queue_hours(self, port, route, info):
        weight = PARAMS["QUEUE_WEIGHT"]
        if weight <= 0:
            return 0.0
        waiting_teu = self._queue_teu.get((port, route), 0.0)
        if waiting_teu <= 0:
            return 0.0
        queue_cycles = waiting_teu / info.capacity_teu
        return weight * queue_cycles * info.headway_hours

    # -- shortest path ----------------------------------------------------

    def find_path(self, now, origin_port, destination_port):
        self._ensure_graph(now)
        self._ensure_queue(now)

        max_transfers = max(0, PARAMS["MAX_TRANSFERS"])
        wait_fraction = PARAMS["WAIT_FRACTION"]
        transfer_buffer = PARAMS["TRANSFER_BUFFER_HOURS"]

        counter = itertools.count()
        best = {(origin_port, None, 0): 0.0}
        previous = {}
        heap = [(0.0, next(counter), origin_port, None, 0)]

        while heap:
            cost, _, port, route, transfers = heapq.heappop(heap)
            state = (port, route, transfers)
            if cost > best.get(state, math.inf):
                continue
            if port is destination_port and route is not None:
                return _rebuild_path(previous, state)

            for edge in self._edges_by_port.get(port, ()):
                if edge.service_route is route:
                    continue
                next_transfers = transfers + (1 if route is not None else 0)
                if next_transfers > max_transfers:
                    continue
                info = self._route_info.get(edge.service_route)
                if info is None:
                    continue

                step_cost = edge.sailing_hours
                step_cost += wait_fraction * info.headway_hours
                step_cost += self._queue_hours(port, edge.service_route, info)
                if route is not None:
                    step_cost += transfer_buffer

                next_state = (edge.arrival_port, edge.service_route, next_transfers)
                next_cost = cost + step_cost
                if next_cost >= best.get(next_state, math.inf):
                    continue
                best[next_state] = next_cost
                previous[next_state] = (state, edge)
                heapq.heappush(
                    heap,
                    (
                        next_cost,
                        next(counter),
                        edge.arrival_port,
                        edge.service_route,
                        next_transfers,
                    ),
                )

        return None

    def track(self, shipment):
        self._tracked_shipments.append(shipment)


_PLANNERS = {}


def _planner(context) -> _Planner:
    planner = _PLANNERS.get(id(context))
    if planner is None or planner._context is not context:
        planner = _Planner(context)
        _PLANNERS[id(context)] = planner
    return planner


def _plan_window(plan):
    if plan.start_offset_days is None or plan.duration_days is None:
        return None, None
    start = dt.datetime.min + dt.timedelta(days=plan.start_offset_days)
    return start, start + dt.timedelta(days=plan.duration_days)


def _overlaps(window, other):
    start, end = window
    if start is None:
        return False
    return start < other[1] and end > other[0]


def _booking_departure_port(booking):
    route = booking.service_route
    if route is None:
        return None
    for segment in route.segments:
        if segment.sequence_index == booking.departure_segment_index:
            return segment.associated_leg.departure_port
    return None


def _rebuild_path(previous, state):
    path = []
    while state in previous:
        parent, edge = previous[state]
        path.append(edge)
        state = parent
    path.reverse()
    return path


def _clear_bookings(shipment):
    for booking in shipment.associated_bookings:
        route = booking.service_route
        if route is None:
            continue
        while booking in route.associated_bookings:
            route.associated_bookings.remove(booking)
    shipment.associated_bookings = []
    shipment.current_booking_index = None


def _apply_path(shipment, path):
    for sequence_index, edge in enumerate(path, start=1):
        booking = Booking(
            sequence_index=sequence_index,
            shipment=shipment,
            service_route=edge.service_route,
            departure_segment_index=edge.departure_segment_index,
            arrival_segment_index=edge.arrival_segment_index,
        )
        shipment.associated_bookings.append(booking)
        edge.service_route.associated_bookings.append(booking)
    shipment.current_booking_index = 1


class UserStrategy:
    @staticmethod
    def select_vessel_for_berth(
        maritime_data_context,
        port,
        waiting_vessels,
        available_berths,
        current_time,
        waiting_since_by_vessel=None,
    ):
        """PortResponseStrategy — left to the default policy.

        This hook only fires when at least ``PORT_CONGESTION_MULTIPLIER`` times
        the number of berths are queued, which is rare in this network (the
        average berth queue is well under one vessel), so there is nothing to
        gain here.
        """
        return None

    @staticmethod
    def create_alternative_service_routes(context, now, vessel=None):
        """ShippingLineResponseStrategy.

        Returning ``None`` lets ``DefaultStrategy`` build its disruption
        alternatives; the routing below picks them up automatically because it
        considers every service route that has vessels deployed on it. Setting
        ``ALTERNATIVE_ROUTES`` to ``"off"`` suppresses them instead, leaving the
        context untouched.
        """
        if not STRATEGY_ENABLED:
            return None
        if PARAMS["ALTERNATIVE_ROUTES"] == "off":
            return True
        return None

    @staticmethod
    def assign_associated_bookings(context, now, shipment):
        """CargoOwnerResponseStrategy — the main decision point.

        Builds the booking chain that minimises expected total transport time.
        Returns ``None`` when no path is found so the default distance-based
        planner still gets its chance.
        """
        if not STRATEGY_ENABLED:
            return None

        mode = PARAMS["ROUTING_MODE"]
        if mode == "challenger":
            return challenger_routing.assign_challenger(context, now, shipment)
        if mode == "rescue":
            return challenger_routing.assign_rescue(context, now, shipment)
        if mode == "foresight":
            return challenger_routing.assign_foresight(context, now, shipment)

        demand = shipment.demand
        origin_port = demand.origin_port
        destination_port = demand.destination_port
        if origin_port is destination_port:
            return None

        planner = _planner(context)
        path = planner.find_path(now, origin_port, destination_port)
        if not path:
            return None

        _clear_bookings(shipment)
        _apply_path(shipment, path)
        planner.track(shipment)
        return True

    @staticmethod
    def adjust_bookings_before_cargo_handling(context, now, vessel):
        """CargoOwnerResponseStrategy — replanning in transit.

        Left to ``DefaultStrategy`` by default: in-transit rerouting is only
        worth adding once the initial routing is demonstrably better, otherwise
        the two effects cannot be told apart.
        """
        if not STRATEGY_ENABLED:
            return None
        if PARAMS["REROUTE_IN_TRANSIT"] == "off":
            return True
        return None
