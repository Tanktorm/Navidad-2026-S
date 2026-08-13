"""Inspect what the routing actually decides, without running a simulation.

Builds the scenario, then prints:

1. a table of every service route: vessels, cycle time, headway, capacity —
   this is what explains why an infrequent service is or is not chosen;
2. for each origin-destination pair, the path chosen by ``DefaultStrategy``
   (shortest distance) next to the path chosen by ``UserStrategy`` (shortest
   expected time), with the cost broken down into sailing / boarding wait /
   transfer / queue hours;
3. how many OD pairs each service route ends up carrying under each strategy.

Usage::

    python experiments/inspect_routing.py
    python experiments/inspect_routing.py --day 65      # inside the disruption
    python experiments/inspect_routing.py --od Shanghai:Rotterdam
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scenario_builders  # noqa: E402
import simulation_model  # noqa: E402,F401  (imported first: response_strategies
#                          and simulation_model import each other, and the
#                          package works only when simulation_model wins the race)
from config.simulation_config import WARM_UP_DAYS  # noqa: E402
from response_strategies import user_strategy as us  # noqa: E402
from response_strategies.default_strategy import (  # noqa: E402
    _build_all_candidate_bookings,
    _find_shortest_booking_path,
)
from response_strategies.strategy_params import PARAMS  # noqa: E402
from simulation_model.ordered_set import OrderedSet  # noqa: E402


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", choices=["disruption", "baseline"],
                        default="disruption")
    parser.add_argument("--day", type=float, default=0.0,
                        help="measured day to evaluate; disruption offsets are "
                             "relative to the start of measurement")
    parser.add_argument("--od", action="append", default=None,
                        help="restrict to 'Origin:Destination' (repeatable)")
    parser.add_argument("--limit", type=int, default=25,
                        help="how many OD pairs to print (default: 25)")
    return parser.parse_args(argv)


def build_context(scenario):
    if scenario == "baseline":
        return scenario_builders.create()
    return scenario_builders.create_with_disruption()


def evaluation_time(day):
    """Convert a measured day into the model's clock time.

    ``disruption_scenario.py`` schedules every plan at ``WARM_UP_DAYS + day``
    and the model clock starts at ``datetime.min``, so the warm-up has to be
    added or the evaluation lands on a network with no disruption active.
    """
    return dt.datetime.min + dt.timedelta(days=WARM_UP_DAYS + day)


def apply_leg_multipliers(context, now):
    """Reproduce what DisruptionManager does at time ``now``."""
    for leg in context.legs:
        leg.sailing_time_multiplier = 1.0
    for plan in context.disruption_plans:
        start, end = us._plan_window(plan)
        if start is None:
            continue
        active = start <= now < end
        if plan.target_leg is not None:
            plan.target_leg.sailing_time_multiplier = (
                plan.multiplier if active else 1.0
            )
        if plan.target_berth is not None and plan.close_berth:
            plan.target_berth.is_available = not active


def print_route_table(planner, context):
    print("=" * 88)
    print("Service routes at this instant")
    print("=" * 88)
    print(f"  {'Route':<10}{'Vessels':>8}{'Segments':>10}{'Cycle (d)':>12}"
          f"{'Headway (d)':>13}{'Capacity TEU':>14}")
    for route in context.service_routes:
        info = planner._route_info.get(route)
        if info is None:
            print(f"  {route.id:<10}{len(route.deployed_vessels):>8}"
                  f"{len(route.segments):>10}{'-':>12}{'-':>13}{'-':>14}"
                  "   (not bookable)")
            continue
        print(f"  {route.id:<10}{len(route.deployed_vessels):>8}"
              f"{len(route.segments):>10}{info.cycle_hours / 24:>12.2f}"
              f"{info.headway_hours / 24:>13.2f}{info.capacity_teu:>14,.0f}")
    print()
    print("  Headway is cycle time divided by deployed vessels: a one-vessel")
    print("  service only passes once per cycle, and the boarding wait charged")
    print(f"  by the cost function is {PARAMS['WAIT_FRACTION']:.2f} x headway.")
    print()


def describe_path(planner, path, origin_port):
    """Re-derive the cost breakdown for a chosen path."""
    parts = []
    total = 0.0
    port = origin_port
    previous_route = None
    for edge in path:
        info = planner._route_info[edge.service_route]
        sailing = edge.sailing_hours
        boarding = PARAMS["WAIT_FRACTION"] * info.headway_hours
        queue = planner._queue_hours(port, edge.service_route, info)
        transfer = PARAMS["TRANSFER_BUFFER_HOURS"] if previous_route else 0.0
        total += sailing + boarding + queue + transfer
        parts.append(
            f"    {edge.departure_port.name} -> {edge.arrival_port.name} "
            f"on {edge.service_route.id}: sail {sailing / 24:.2f}d + "
            f"wait {boarding / 24:.2f}d + transfer {transfer / 24:.2f}d + "
            f"queue {queue / 24:.2f}d"
        )
        port = edge.arrival_port
        previous_route = edge.service_route
    return total, parts


def route_chain(path):
    return " > ".join(edge.service_route.id for edge in path) if path else "(none)"


def port_chain(path, origin_port):
    if not path:
        return "(none)"
    names = [origin_port.name] + [edge.arrival_port.name for edge in path]
    return " > ".join(names)


def main(argv=None) -> int:
    args = parse_arguments(argv)
    context = build_context(args.scenario)
    now = evaluation_time(args.day)
    apply_leg_multipliers(context, now)

    planner = us._planner(context)
    planner._ensure_graph(now)
    planner._ensure_queue(now)

    print()
    print(f"Scenario: {args.scenario}   evaluated at measured day {args.day:g}")
    print(f"Parameters: {PARAMS}")
    print()
    print_route_table(planner, context)

    closed = planner._closed_port_names(now)
    congested = [
        f"{leg.departure_port.name}->{leg.arrival_port.name} x{leg.sailing_time_multiplier:g}"
        for leg in context.legs
        if getattr(leg, "sailing_time_multiplier", 1.0) > 1.0
    ]
    print(f"  Closed ports seen by the strategy : {sorted(closed) or 'none'}")
    print(f"  Congested legs seen by the strategy: {congested or 'none'}")
    print()

    default_edges = _build_all_candidate_bookings(context, OrderedSet(), OrderedSet())

    wanted = None
    if args.od:
        wanted = {tuple(item.split(":", 1)) for item in args.od}

    print("=" * 88)
    print("Chosen paths: default (shortest distance) vs user (shortest time)")
    print("=" * 88)

    default_usage = {}
    user_usage = {}
    differing = 0
    shown = 0

    for demand in context.demands:
        origin = demand.origin_port
        destination = demand.destination_port
        if origin is destination:
            continue
        if wanted and (origin.name, destination.name) not in wanted:
            continue

        default_path = _find_shortest_booking_path(
            context, origin, destination, default_edges
        )
        user_path = planner.find_path(now, origin, destination)

        for edge in default_path or []:
            default_usage[edge.service_route.id] = (
                default_usage.get(edge.service_route.id, 0) + 1
            )
        for edge in user_path or []:
            user_usage[edge.service_route.id] = (
                user_usage.get(edge.service_route.id, 0) + 1
            )

        same = route_chain(default_path) == route_chain(user_path)
        if not same:
            differing += 1
        if shown >= args.limit:
            continue
        shown += 1

        print(f"\n{origin.name} -> {destination.name}"
              f"{'' if not same else '   (same choice)'}")
        print(f"  default : {route_chain(default_path)}"
              f"   [{port_chain(default_path, origin)}]")
        print(f"  user    : {route_chain(user_path)}"
              f"   [{port_chain(user_path, origin)}]")
        if user_path:
            total, parts = describe_path(planner, user_path, origin)
            print(f"  expected total: {total / 24:.2f} days")
            for line in parts:
                print(line)

    print()
    print("=" * 88)
    print("How many OD pairs use each route")
    print("=" * 88)
    all_routes = sorted(set(default_usage) | set(user_usage))
    print(f"  {'Route':<12}{'default':>10}{'user':>10}")
    for route_id in all_routes:
        print(f"  {route_id:<12}{default_usage.get(route_id, 0):>10}"
              f"{user_usage.get(route_id, 0):>10}")
    print()
    print(f"  OD pairs where the two strategies disagree: {differing}")
    print(f"  (warm-up is {WARM_UP_DAYS} days; day 0 here is the first measured day)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
