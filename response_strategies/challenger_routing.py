"""CHALLENGER: supervisión de la decisión del Default en la asignación inicial.

Este módulo implementa dos políticas que comparten toda la maquinaria:

``challenger``
    La estrategia E10 tal como está especificada en la guía del equipo. Para
    cada shipment reconstruye la decisión del Default; si esa ruta atraviesa
    una disrupción activa, busca una alternativa y la acepta solo si reduce el
    costo efectivo, no agrega más de un transbordo y no manda la carga a una
    salida claramente más presionada según QCR.

``rescue``
    Idéntica al Default en todo, salvo en un caso: cuando el Default se queda
    **sin ninguna ruta posible**. ``DefaultStrategy`` prohíbe los tramos
    congestionados en vez de cobrarlos, y cuando eso desconecta el destino
    devuelve ``False``: el shipment se queda en el puerto de origen y no
    reintenta hasta que termina la disrupción. Con ``New Jersey -> Cartagena``
    a x3 durante 30 días, la carga con destino Cartagena espera esas semanas
    aunque el tramo siga siendo navegable. Aquí, en ese caso y solo en ese, se
    enruta cobrando el tramo por su ``sailing_time_multiplier``.

Los puertos cerrados se siguen prohibiendo en ambas políticas: un muelle
cerrado no puede atender al buque, no es cuestión de precio.

Nada está escrito a mano para un puerto, una ruta o una fecha: el estado de
disrupción se lee de ``context.disruption_plans`` en el instante de decidir.
"""

from __future__ import annotations

from maritime_data_context import Booking

from .default_strategy import (
    _CandidateBookingEdge,
    _build_all_candidate_bookings,
    _find_shortest_booking_path,
    _get_active_disruption_plans,
    _get_avoid_port_names,
    _get_congested_legs,
    _leg_key,
)
from .strategy_params import PARAMS


# Contadores agregados; se vuelcan al summary de la corrida. La guía pide
# diagnósticos por contador y explícitamente prohíbe una línea de log por
# shipment, porque la E/S dominaría el tiempo de ejecución.
DIAGNOSTICS = {
    "shipments_seen": 0,
    "default_unaffected": 0,
    "default_affected": 0,
    "default_no_path": 0,
    "alternative_generated": 0,
    "alternative_not_found": 0,
    "rejected_transfers": 0,
    "rejected_qcr": 0,
    "rejected_effective_cost": 0,
    "alternative_selected": 0,
    "rescued": 0,
    "rescue_failed": 0,
}


class _Graphs:
    """Grafos de candidatos, reconstruidos solo cuando cambia la disrupción."""

    def __init__(self):
        self.key = None
        self.default_edges = None
        self.effective_edges = None
        self.avoid_port_names = None
        self.congested_legs = None
        self.congested_ids = frozenset()


_GRAPHS = {}


def _graphs(context, now) -> _Graphs:
    cached = _GRAPHS.get(id(context))
    if cached is None:
        cached = _Graphs()
        _GRAPHS[id(context)] = cached

    close_berth_plans, congested_leg_plans = _get_active_disruption_plans(context, now)
    avoid_port_names = _get_avoid_port_names(close_berth_plans)
    congested_legs = _get_congested_legs(congested_leg_plans)
    key = (
        tuple(sorted(avoid_port_names)),
        tuple(sorted(_leg_key(leg) for leg in congested_legs)),
        len(context.service_routes),
    )
    if key == cached.key:
        return cached

    cached.key = key
    cached.avoid_port_names = avoid_port_names
    cached.congested_legs = congested_legs
    cached.congested_ids = frozenset(id(leg) for leg in congested_legs)
    cached.default_edges = _build_all_candidate_bookings(
        context, avoid_port_names, congested_legs
    )
    cached.effective_edges = _build_effective_cost_edges(context, avoid_port_names)
    return cached


def _build_effective_cost_edges(context, avoid_port_names):
    """Candidatos que no prohíben tramos congestionados, sino que los cobran.

    Es ``_build_all_candidate_bookings`` con dos diferencias: no se filtra por
    ``congested_legs``, y la distancia acumulada de cada span es la distancia
    efectiva, ``distancia x sailing_time_multiplier`` vivo de cada leg. Así un
    tramo a x3 cuesta el triple en vez de desaparecer del grafo.
    """
    edges = []
    for service_route in context.service_routes:
        if service_route.source_service_route is not None:
            continue
        segments = sorted(
            service_route.segments, key=lambda segment: segment.sequence_index
        )
        segment_count = len(segments)
        for start_index in range(segment_count):
            departure_port = segments[start_index].associated_leg.departure_port
            if departure_port.name.casefold() in avoid_port_names:
                continue
            cumulative = 0.0
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
                intermediate_ports = [
                    segment.associated_leg.arrival_port
                    for segment in candidate_segments
                ]
                if any(
                    port.name.casefold() in avoid_port_names
                    for port in intermediate_ports
                ):
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


# ---------------------------------------------------------------------------
# medidas sobre un camino
# ---------------------------------------------------------------------------

def path_legs(path):
    """Legs realmente atravesados por un camino de spans."""
    legs = []
    for edge in path:
        segments = sorted(
            edge.service_route.segments, key=lambda segment: segment.sequence_index
        )
        count = len(segments)
        start = next(
            index
            for index, segment in enumerate(segments)
            if segment.sequence_index == edge.departure_segment_index
        )
        end = next(
            index
            for index, segment in enumerate(segments)
            if segment.sequence_index == edge.arrival_segment_index
        )
        cursor = start
        while True:
            legs.append(segments[cursor].associated_leg)
            if cursor == end:
                break
            cursor = (cursor + 1) % count
    return legs


def effective_distance(path):
    return sum(
        leg.sailing_distance * leg.sailing_time_multiplier for leg in path_legs(path)
    )


def touches_disruption(path, congested_ids):
    return any(id(leg) in congested_ids for leg in path_legs(path))


def nominal_capacity_teu(service_route):
    capacities = [
        vessel.vessel_class.teu_capacity
        for vessel in service_route.deployed_vessels
        if vessel.vessel_class is not None
    ]
    if not capacities:
        return 0.0
    return sum(capacities) / len(capacities)


def queued_teu(port, service_route, departure_segment_index):
    """TEU que esperan físicamente esa salida concreta: puerto + ruta + segmento."""
    total = 0.0
    for shipment in getattr(port, "shipments_in_storage", ()) or ():
        try:
            booking = shipment.get_current_booking()
        except ValueError:
            continue
        if (
            booking.service_route is service_route
            and booking.departure_segment_index == departure_segment_index
        ):
            total += shipment.teu_size
    return total


def max_qcr(path):
    """Peor presión de todo el camino: la salida más cargada respecto a su capacidad."""
    worst = 0.0
    for edge in path:
        capacity = nominal_capacity_teu(edge.service_route)
        if capacity <= 0:
            continue
        waiting = queued_teu(
            edge.departure_port, edge.service_route, edge.departure_segment_index
        )
        worst = max(worst, waiting / capacity)
    return worst


# ---------------------------------------------------------------------------
# materialización
# ---------------------------------------------------------------------------

def clear_bookings(shipment):
    for booking in shipment.associated_bookings:
        service_route = booking.service_route
        if service_route is None:
            continue
        while booking in service_route.associated_bookings:
            service_route.associated_bookings.remove(booking)
    shipment.associated_bookings = []
    shipment.current_booking_index = None


def materialize_path(shipment, path):
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


def _apply(shipment, path):
    clear_bookings(shipment)
    materialize_path(shipment, path)
    return True


# ---------------------------------------------------------------------------
# políticas
# ---------------------------------------------------------------------------

def _default_path(context, graphs, origin_port, destination_port):
    if destination_port.name.casefold() in graphs.avoid_port_names:
        return None
    return _find_shortest_booking_path(
        context, origin_port, destination_port, graphs.default_edges
    )


def assign_challenger(context, now, shipment):
    """CHALLENGER E10: el Default gana salvo prueba en contrario."""
    demand = shipment.demand
    origin_port, destination_port = demand.origin_port, demand.destination_port
    if origin_port is destination_port:
        return None

    DIAGNOSTICS["shipments_seen"] += 1
    graphs = _graphs(context, now)

    default_path = _default_path(context, graphs, origin_port, destination_port)
    if not default_path:
        # Caso D de la guía: no hay Default que supervisar.
        DIAGNOSTICS["default_no_path"] += 1
        return None

    # Gate: ¿la ruta del Default está realmente expuesta?
    if not touches_disruption(default_path, graphs.congested_ids):
        DIAGNOSTICS["default_unaffected"] += 1
        return _apply(shipment, default_path)

    DIAGNOSTICS["default_affected"] += 1
    alternative = _find_shortest_booking_path(
        context, origin_port, destination_port, graphs.effective_edges
    )
    if not alternative:
        DIAGNOSTICS["alternative_not_found"] += 1
        return _apply(shipment, default_path)

    DIAGNOSTICS["alternative_generated"] += 1

    extra_transfers = (len(alternative) - 1) - (len(default_path) - 1)
    if extra_transfers > PARAMS["MAX_EXTRA_TRANSFERS"]:
        DIAGNOSTICS["rejected_transfers"] += 1
        return _apply(shipment, default_path)

    default_cost = effective_distance(default_path)
    alternative_cost = effective_distance(alternative)
    if alternative_cost >= default_cost - PARAMS["MIN_EFFECTIVE_SAVING"]:
        DIAGNOSTICS["rejected_effective_cost"] += 1
        return _apply(shipment, default_path)

    if max_qcr(alternative) > max_qcr(default_path) + PARAMS["QCR_TOLERANCE"]:
        DIAGNOSTICS["rejected_qcr"] += 1
        return _apply(shipment, default_path)

    DIAGNOSTICS["alternative_selected"] += 1
    return _apply(shipment, alternative)


def assign_rescue(context, now, shipment):
    """Default en todo, salvo cuando el Default se queda sin ninguna ruta."""
    demand = shipment.demand
    origin_port, destination_port = demand.origin_port, demand.destination_port
    if origin_port is destination_port:
        return None

    DIAGNOSTICS["shipments_seen"] += 1
    graphs = _graphs(context, now)

    default_path = _default_path(context, graphs, origin_port, destination_port)
    if default_path:
        DIAGNOSTICS["default_unaffected"] += 1
        return _apply(shipment, default_path)

    DIAGNOSTICS["default_no_path"] += 1
    if destination_port.name.casefold() in graphs.avoid_port_names:
        # El puerto de destino está cerrado: no hay precio que valga, el buque
        # no puede ser atendido. Se deja al Default, que hará esperar la carga.
        DIAGNOSTICS["rescue_failed"] += 1
        return None

    rescue = _find_shortest_booking_path(
        context, origin_port, destination_port, graphs.effective_edges
    )
    if not rescue:
        DIAGNOSTICS["rescue_failed"] += 1
        return None

    if (len(rescue) - 1) > PARAMS["MAX_TRANSFERS"]:
        DIAGNOSTICS["rejected_transfers"] += 1
        return None

    DIAGNOSTICS["rescued"] += 1
    return _apply(shipment, rescue)
