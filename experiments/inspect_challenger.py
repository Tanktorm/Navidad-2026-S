"""Comprueba la premisa de CHALLENGER E10 sin correr la simulacion.

CHALLENGER E10 propone: reconstruir la decision del Default, y solo si esa ruta
esta expuesta a una disrupcion activa, buscar una alternativa. Este script mide
cuantas veces se cumple esa condicion.

La duda concreta: ``DefaultStrategy.assign_associated_bookings`` construye sus
candidatos con ``_build_all_candidate_bookings(context, avoid_port_names,
congested_legs)``, que **descarta** los tramos congestionados y los puertos
cerrados. Si el Default ya evita la disrupcion por construccion, el gate de
CHALLENGER nunca se abre y la estrategia es equivalente al Default.

El script tambien mide la oportunidad que si existe: como el Default *prohibe*
un tramo congestionado en vez de *cobrarlo*, puede estar tomando desvios mucho
mas caros que el propio tramo penalizado. Se compara, para cada par OD:

  * Default   = camino de menor distancia sobre el grafo filtrado (lo que hace hoy)
  * Efectivo  = camino de menor distancia efectiva sobre el grafo sin filtrar
                los tramos congestionados, con cada leg pesado por su
                ``sailing_time_multiplier`` actual (los puertos cerrados siguen
                prohibidos porque un muelle cerrado no puede atenderse)

Uso::

    python experiments/inspect_challenger.py --day 65
    python experiments/inspect_challenger.py --day 130 --limit 12
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
import simulation_model  # noqa: E402,F401  (debe importarse antes que response_strategies)
from config.simulation_config import WARM_UP_DAYS  # noqa: E402
from response_strategies.default_strategy import (  # noqa: E402
    _CandidateBookingEdge,
    _build_all_candidate_bookings,
    _find_shortest_booking_path,
    _get_active_disruption_plans,
    _get_avoid_port_names,
    _get_congested_legs,
)
from simulation_model.ordered_set import OrderedSet  # noqa: E402


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--day", type=float, default=65.0,
                        help="dia medido a evaluar (default: 65, dentro de la "
                             "primera ventana de disrupcion)")
    parser.add_argument("--limit", type=int, default=10,
                        help="cuantos pares OD detallar")
    return parser.parse_args(argv)


def apply_disruption_state(context, now):
    """Reproduce lo que hace DisruptionManager en el instante ``now``."""
    for leg in context.legs:
        leg.sailing_time_multiplier = 1.0
    for plan in context.disruption_plans:
        if plan.start_offset_days is None or plan.duration_days is None:
            continue
        start = dt.datetime.min + dt.timedelta(days=plan.start_offset_days)
        end = start + dt.timedelta(days=plan.duration_days)
        active = start <= now < end
        if plan.target_leg is not None:
            plan.target_leg.sailing_time_multiplier = plan.multiplier if active else 1.0
        if plan.target_berth is not None and plan.close_berth:
            plan.target_berth.is_available = not active


def build_effective_cost_candidates(context, avoid_port_names):
    """Candidatos sin prohibir tramos congestionados, pesados por su multiplicador.

    Es ``_build_all_candidate_bookings`` con dos cambios: no se filtra por
    ``congested_legs``, y la distancia acumulada de cada span es la **distancia
    efectiva** (distancia x multiplicador vivo). Los puertos cerrados se siguen
    prohibiendo: un muelle cerrado no puede atender al buque.
    """
    edges = []
    for service_route in context.service_routes:
        if service_route.source_service_route is not None:
            # Las rutas alternativas del Default dependen de su disruption_key;
            # aqui solo interesan las rutas base.
            continue
        segments = sorted(service_route.segments, key=lambda s: s.sequence_index)
        segment_count = len(segments)
        for start_index in range(segment_count):
            cumulative = 0.0
            departure_port = segments[start_index].associated_leg.departure_port
            if departure_port.name.casefold() in avoid_port_names:
                continue
            for step in range(1, segment_count):
                segment_index = (start_index + step - 1) % segment_count
                leg = segments[segment_index].associated_leg
                cumulative += leg.sailing_distance * leg.sailing_time_multiplier
                arrival_port = leg.arrival_port
                if arrival_port is departure_port:
                    continue
                candidate_segments = [
                    segments[(start_index + offset) % segment_count]
                    for offset in range(step)
                ]
                intermediate = [s.associated_leg.arrival_port for s in candidate_segments]
                if any(p.name.casefold() in avoid_port_names for p in intermediate):
                    continue
                edges.append(
                    _CandidateBookingEdge(
                        service_route,
                        departure_port,
                        arrival_port,
                        start_index + 1,
                        segment_index + 1,
                        cumulative,
                    )
                )
    return edges


def path_legs(path):
    """Legs realmente atravesados por un camino de spans."""
    legs = []
    for edge in path:
        segments = sorted(
            edge.service_route.segments, key=lambda s: s.sequence_index
        )
        count = len(segments)
        start = next(
            i for i, s in enumerate(segments)
            if s.sequence_index == edge.departure_segment_index
        )
        end = next(
            i for i, s in enumerate(segments)
            if s.sequence_index == edge.arrival_segment_index
        )
        cursor = start
        while True:
            legs.append(segments[cursor].associated_leg)
            if cursor == end:
                break
            cursor = (cursor + 1) % count
    return legs


def physical_and_effective(path):
    physical = effective = 0.0
    for leg in path_legs(path):
        physical += leg.sailing_distance
        effective += leg.sailing_distance * leg.sailing_time_multiplier
    return physical, effective


def route_chain(path):
    return " > ".join(edge.service_route.id for edge in path) if path else "(sin ruta)"


def main(argv=None) -> int:
    args = parse_arguments(argv)
    context = scenario_builders.create_with_disruption()
    # disruption_scenario.py programa las disrupciones en WARM_UP_DAYS + dia
    # medido, y el reloj del modelo arranca en datetime.min. Sin sumar el
    # calentamiento se evalua una red sin ninguna disrupcion activa.
    now = dt.datetime.min + dt.timedelta(days=WARM_UP_DAYS + args.day)
    apply_disruption_state(context, now)

    close_berth_plans, congested_leg_plans = _get_active_disruption_plans(context, now)
    avoid_port_names = _get_avoid_port_names(close_berth_plans)
    congested_legs = _get_congested_legs(congested_leg_plans)
    congested_set = set(id(leg) for leg in congested_legs)

    print()
    print("=" * 84)
    print(f"Estado de la red en el dia medido {args.day:g}")
    print("=" * 84)
    print(f"  Puertos cerrados      : {sorted(avoid_port_names) or 'ninguno'}")
    print("  Tramos congestionados :")
    for leg in congested_legs:
        print(f"      {leg.departure_port.name} -> {leg.arrival_port.name}"
              f"  x{leg.sailing_time_multiplier:g}  ({leg.sailing_distance:,.0f} nm)")
    if not congested_legs:
        print("      ninguno")

    default_edges = _build_all_candidate_bookings(
        context, avoid_port_names, congested_legs
    )
    effective_edges = build_effective_cost_candidates(context, avoid_port_names)

    gate_opens = 0
    pairs = 0
    unreachable_default = 0
    switches = []

    for demand in context.demands:
        origin, destination = demand.origin_port, demand.destination_port
        if origin is destination:
            continue
        pairs += 1

        default_path = _find_shortest_booking_path(
            context, origin, destination, default_edges
        )
        if not default_path:
            unreachable_default += 1
            continue

        # Gate de CHALLENGER: ¿el camino del Default toca la disrupcion?
        if any(id(leg) in congested_set for leg in path_legs(default_path)):
            gate_opens += 1

        effective_path = _find_shortest_booking_path(
            context, origin, destination, effective_edges
        )
        if not effective_path:
            continue

        default_physical, default_effective = physical_and_effective(default_path)
        alt_physical, alt_effective = physical_and_effective(effective_path)
        saving = default_effective - alt_effective
        if saving > 1e-6:
            switches.append({
                "origin": origin.name,
                "destination": destination.name,
                "default_chain": route_chain(default_path),
                "alt_chain": route_chain(effective_path),
                "default_effective": default_effective,
                "alt_effective": alt_effective,
                "default_physical": default_physical,
                "alt_physical": alt_physical,
                "saving": saving,
                "saving_pct": 100.0 * saving / default_effective,
                "transfers_default": len(default_path) - 1,
                "transfers_alt": len(effective_path) - 1,
                "uses_congested": any(
                    id(leg) in congested_set for leg in path_legs(effective_path)
                ),
            })

    print()
    print("=" * 84)
    print("1. ¿Se abre el gate de CHALLENGER E10?")
    print("=" * 84)
    print(f"  Pares origen-destino evaluados                    : {pairs}")
    print(f"  Sin camino posible para el Default                : {unreachable_default}")
    print(f"  Caminos del Default que TOCAN un tramo congestionado: {gate_opens}")
    print()
    if gate_opens == 0:
        print("  El gate no se abre en ningun par. DefaultStrategy construye sus")
        print("  candidatos con _build_all_candidate_bookings(context, avoid_ports,")
        print("  congested_legs), que descarta esos tramos: su camino no puede")
        print("  atravesar la disrupcion. CHALLENGER E10, tal como esta especificado,")
        print("  tomaria siempre la salida temprana del paso 7 y devolveria el Default.")
    else:
        print(f"  El gate se abre en {gate_opens} pares: hay margen para la version E10 tal cual.")

    print()
    print("=" * 84)
    print("2. La oportunidad que si existe: el Default PROHIBE en vez de COBRAR")
    print("=" * 84)
    print(f"  Pares donde cobrar el tramo congestionado sale mas barato: {len(switches)}")
    if switches:
        total_saving = sum(item["saving"] for item in switches)
        print(f"  Ahorro medio en distancia efectiva                       : "
              f"{sum(i['saving_pct'] for i in switches) / len(switches):.1f}%")
        print(f"  Ahorro total en distancia efectiva                       : "
              f"{total_saving:,.0f} unidades")
        extra = [i for i in switches if i["transfers_alt"] > i["transfers_default"] + 1]
        print(f"  De esos, rechazados por la regla de +1 transbordo        : {len(extra)}")
        print()
        print(f"  Los {min(args.limit, len(switches))} de mayor ahorro:")
        for item in sorted(switches, key=lambda i: -i["saving"])[: args.limit]:
            print(f"\n    {item['origin']} -> {item['destination']}"
                  f"   ahorro {item['saving_pct']:.0f}%")
            print(f"      default : {item['default_chain']:<14} "
                  f"efectiva {item['default_effective']:>9,.0f}  "
                  f"fisica {item['default_physical']:>8,.0f} nm  "
                  f"{item['transfers_default']} transbordos")
            print(f"      efectivo: {item['alt_chain']:<14} "
                  f"efectiva {item['alt_effective']:>9,.0f}  "
                  f"fisica {item['alt_physical']:>8,.0f} nm  "
                  f"{item['transfers_alt']} transbordos"
                  f"{'  (usa el tramo congestionado)' if item['uses_congested'] else ''}")
    else:
        print("  Ninguno: el desvio del Default nunca sale mas caro que el tramo")
        print("  penalizado. La palanca esta en otra parte.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
