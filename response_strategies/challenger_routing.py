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

import datetime as dt
import heapq
import itertools
import math

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
    # --- foresight ---
    "foresight_same_as_default": 0,
    "foresight_differs": 0,
    "foresight_avoided_future_window": 0,
    "foresight_used_expiring_window": 0,
    "foresight_no_path": 0,
}


# ---------------------------------------------------------------------------
# FORESIGHT: la red es lenta, la disrupción es corta, y el Default decide
# mirando solo el instante presente.
#
# DefaultStrategy prohíbe un tramo si está congestionado **ahora**. Pero un
# contenedor tarda días en llegar a ese tramo: los headways van de 2.7 a 15.6
# días y los ciclos de 8 a 78. Cuando la carga llega, la foto ya cambió. Eso
# produce dos errores simétricos:
#
#   * se desvía (o se vara) carga por una disrupción que habrá terminado antes
#     de que la carga llegue allí;
#   * se manda carga por un tramo libre hoy que estará congestionado, o hacia un
#     puerto que estará cerrado, justo cuando la carga llegue.
#
# FORESIGHT mantiene exactamente el criterio del Default —camino de menor
# distancia— y cambia solo *cuándo* se evalúa si un tramo o un puerto está
# disponible: en el momento estimado de llegada a ese tramo, no en el momento de
# decidir. Fuera de las ventanas de disrupción produce la misma decisión que el
# Default, tramo por tramo.
# ---------------------------------------------------------------------------


class _Span:
    """Un tramo bookeable con el detalle temporal de los legs que atraviesa."""

    __slots__ = (
        "service_route", "departure_port", "arrival_port",
        "departure_segment_index", "arrival_segment_index",
        "distance", "legs", "total_hours",
    )

    def __init__(self, service_route, departure_port, arrival_port,
                 departure_segment_index, arrival_segment_index,
                 distance, legs, total_hours):
        self.service_route = service_route
        self.departure_port = departure_port
        self.arrival_port = arrival_port
        self.departure_segment_index = departure_segment_index
        self.arrival_segment_index = arrival_segment_index
        self.distance = distance
        # legs = [(leg, horas_desde_el_inicio_del_span_hasta_entrar,
        #               horas_hasta_salir_del_leg)]
        self.legs = legs
        self.total_hours = total_hours


class _Topology:
    """Geometría y frecuencias de la red. No depende del estado de disrupción."""

    def __init__(self):
        self.key = None
        self.spans_by_port = {}
        self.headway_hours = {}


_TOPOLOGY = {}


def _topology(context) -> _Topology:
    cached = _TOPOLOGY.get(id(context))
    if cached is None:
        cached = _Topology()
        _TOPOLOGY[id(context)] = cached

    key = tuple(
        (id(route), len(route.segments), len(route.deployed_vessels))
        for route in context.service_routes
    )
    if key == cached.key:
        return cached

    port_call_hours = PARAMS["PORT_CALL_HOURS"]
    spans_by_port = {}
    headway_hours = {}

    for route in context.service_routes:
        segments = sorted(route.segments, key=lambda s: s.sequence_index)
        vessels = [v for v in route.deployed_vessels if v.vessel_class is not None]
        if not segments or not vessels:
            continue
        speed = sum(v.vessel_class.sailing_speed for v in vessels) / len(vessels)
        if speed <= 0:
            continue

        leg_hours = [
            segment.associated_leg.sailing_distance / speed for segment in segments
        ]
        cycle = sum(leg_hours) + port_call_hours * len(segments)
        headway_hours[id(route)] = cycle / len(vessels)

        count = len(segments)
        for start in range(count):
            departure_port = segments[start].associated_leg.departure_port
            distance = 0.0
            hours = 0.0
            legs = []
            for step in range(1, count):
                index = (start + step - 1) % count
                leg = segments[index].associated_leg
                if step > 1:
                    hours += port_call_hours
                entry_hours = hours
                hours += leg_hours[index]
                distance += leg.sailing_distance
                legs.append((leg, entry_hours, hours))
                arrival_port = leg.arrival_port
                if arrival_port is departure_port:
                    break
                spans_by_port.setdefault(departure_port, []).append(
                    _Span(route, departure_port, arrival_port,
                          start + 1, index + 1, distance, list(legs), hours)
                )

    cached.key = key
    cached.spans_by_port = spans_by_port
    cached.headway_hours = headway_hours
    return cached


class _Windows:
    """Ventanas de disrupción resueltas a tiempo absoluto, una sola vez."""

    def __init__(self, context):
        self.leg_windows = {}
        self.port_windows = {}
        for plan in context.disruption_plans:
            if plan.start_offset_days is None or plan.duration_days is None:
                continue
            start = dt.datetime.min + dt.timedelta(days=plan.start_offset_days)
            end = start + dt.timedelta(days=plan.duration_days)
            if plan.target_leg is not None and plan.multiplier > 1:
                self.leg_windows.setdefault(id(plan.target_leg), []).append(
                    (start, end, plan.multiplier)
                )
            if plan.target_berth is not None and plan.close_berth:
                port = getattr(plan.target_berth, "port", None)
                if port is not None:
                    self.port_windows.setdefault(port.name.casefold(), []).append(
                        (start, end)
                    )

    def leg_multiplier_at(self, leg, entry_time, exit_time):
        """Multiplicador que sufriría un buque que recorre el leg en esa franja."""
        worst = 1.0
        for start, end, multiplier in self.leg_windows.get(id(leg), ()):  # pocas
            if entry_time < end and exit_time > start:
                worst = max(worst, multiplier)
        return worst

    def port_closed_at(self, port, when):
        for start, end in self.port_windows.get(port.name.casefold(), ()):
            if start <= when < end:
                return True
        return False


_WINDOWS = {}


def _windows(context) -> _Windows:
    cached = _WINDOWS.get(id(context))
    if cached is None:
        cached = _Windows(context)
        _WINDOWS[id(context)] = cached
    return cached


def _foresight_path(context, now, origin_port, destination_port, price_congestion=False):
    """Camino de menor distancia, con la disponibilidad evaluada a la hora de llegada.

    Con ``price_congestion=False`` un tramo congestionado en su franja se prohíbe,
    igual que hace el Default, solo que en el momento correcto. Con ``True`` no se
    prohíbe: se cobra multiplicando su distancia, que es el rescate de la sección
    anterior llevado al dominio temporal.
    """
    topology = _topology(context)
    windows = _windows(context)
    wait_fraction = PARAMS["WAIT_FRACTION"]

    counter = itertools.count()
    best = {origin_port: 0.0}
    elapsed_at = {origin_port: 0.0}
    previous = {}
    heap = [(0.0, 0.0, next(counter), origin_port)]

    while heap:
        cost, elapsed, _, port = heapq.heappop(heap)
        if cost > best.get(port, math.inf):
            continue
        if port is destination_port:
            break

        for span in topology.spans_by_port.get(port, ()):
            headway = topology.headway_hours.get(id(span.service_route))
            if headway is None:
                continue
            boarding = wait_fraction * headway
            penalty = 1.0
            blocked = False
            for leg, entry_offset, exit_offset in span.legs:
                entry = now + dt.timedelta(hours=elapsed + boarding + entry_offset)
                exit_time = now + dt.timedelta(hours=elapsed + boarding + exit_offset)
                multiplier = windows.leg_multiplier_at(leg, entry, exit_time)
                if multiplier > 1.0:
                    if price_congestion:
                        penalty = max(penalty, multiplier)
                    else:
                        blocked = True
                        break
                if windows.port_closed_at(leg.arrival_port, exit_time):
                    blocked = True
                    break
            if blocked:
                continue

            next_cost = cost + span.distance * penalty
            next_elapsed = elapsed + boarding + span.total_hours
            if next_cost >= best.get(span.arrival_port, math.inf):
                continue
            best[span.arrival_port] = next_cost
            elapsed_at[span.arrival_port] = next_elapsed
            previous[span.arrival_port] = span
            heapq.heappush(
                heap, (next_cost, next_elapsed, next(counter), span.arrival_port)
            )

    if destination_port not in previous:
        return None
    path = []
    cursor = destination_port
    while cursor is not origin_port:
        span = previous.get(cursor)
        if span is None:
            return None
        path.append(span)
        cursor = span.departure_port
    path.reverse()
    return path


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


def normalized_bookings(path):
    """Fusiona tramos consecutivos del mismo servicio en una sola reserva.

    El shortest path puede devolver dos tramos seguidos de la misma ruta de
    servicio: el buque pasa de largo, pero como son dos bookings distintos la
    carga se descarga y se vuelve a cargar en el puerto intermedio. Es un
    transbordo que no existe. Fusionarlos lo elimina.

    Devuelve una lista de ``(service_route, departure_index, arrival_index)``.
    """
    merged = []
    for edge in path:
        route = edge.service_route
        if merged:
            last_route, last_departure, last_arrival = merged[-1]
            if last_route is route:
                count = len(route.segments)
                if count and edge.departure_segment_index == (last_arrival % count) + 1:
                    merged[-1] = (route, last_departure, edge.arrival_segment_index)
                    continue
        merged.append(
            (route, edge.departure_segment_index, edge.arrival_segment_index)
        )
    return merged


def materialize_path(shipment, path):
    if PARAMS["NORMALIZE_PATH"]:
        bookings = normalized_bookings(path)
    else:
        bookings = [
            (edge.service_route, edge.departure_segment_index,
             edge.arrival_segment_index)
            for edge in path
        ]

    for sequence_index, (route, departure, arrival) in enumerate(bookings, start=1):
        booking = Booking(
            sequence_index=sequence_index,
            shipment=shipment,
            service_route=route,
            departure_segment_index=departure,
            arrival_segment_index=arrival,
        )
        shipment.associated_bookings.append(booking)
        route.associated_bookings.append(booking)
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


def _chain(path):
    return tuple(
        (id(span.service_route), span.departure_segment_index,
         span.arrival_segment_index)
        for span in path
    )


def assign_foresight(context, now, shipment):
    """FORESIGHT: el criterio del Default, evaluado en el momento correcto.

    Se conserva la función objetivo del Default —distancia navegada— porque es
    lo que ha demostrado funcionar: no compra transbordos y no persigue esperas
    difíciles de estimar. Lo único que cambia es que la disponibilidad de cada
    tramo y de cada puerto se evalúa en la hora estimada de llegada de la carga
    a ese punto, no en la hora de tomar la decisión.

    Si ni siquiera así hay camino, se cobra la congestión en vez de prohibirla
    (el rescate), de modo que ninguna carga se queda esperando a que termine la
    disrupción.
    """
    demand = shipment.demand
    origin_port, destination_port = demand.origin_port, demand.destination_port
    if origin_port is destination_port:
        return None

    DIAGNOSTICS["shipments_seen"] += 1
    graphs = _graphs(context, now)
    default_path = _default_path(context, graphs, origin_port, destination_port)

    path = _foresight_path(context, now, origin_port, destination_port)
    if path is None:
        path = _foresight_path(
            context, now, origin_port, destination_port, price_congestion=True
        )
        if path is None:
            DIAGNOSTICS["foresight_no_path"] += 1
            return None
        DIAGNOSTICS["rescued"] += 1

    if default_path is None:
        DIAGNOSTICS["foresight_differs"] += 1
        DIAGNOSTICS["foresight_used_expiring_window"] += 1
    elif _chain(path) == _chain(default_path):
        DIAGNOSTICS["foresight_same_as_default"] += 1
    else:
        DIAGNOSTICS["foresight_differs"] += 1
        # ¿Se desvió de algo que hoy está limpio (ventana futura) o aprovechó
        # algo que hoy está sucio pero estará libre al llegar?
        if effective_distance_of_spans(path) > 0 and touches_disruption(
            default_path, graphs.congested_ids
        ):
            DIAGNOSTICS["foresight_used_expiring_window"] += 1
        else:
            DIAGNOSTICS["foresight_avoided_future_window"] += 1

    return _apply(shipment, path)


def effective_distance_of_spans(path):
    return sum(
        leg.sailing_distance * leg.sailing_time_multiplier
        for span in path
        for leg, _entry, _exit in span.legs
    )
