"""Ruta crítica sobre el horario real: llegada más temprana, no distancia mínima.

Por qué esto es distinto de todo lo anterior
--------------------------------------------
Todas las estrategias probadas hasta ahora —incluida la del Default— eligen un
camino y luego *estiman* cuánto tardará, usando una frecuencia media
(``ciclo / buques`` o ``headway/2``). Esa frecuencia media **no existe en
ninguna salida de esta red**.

El simulador despliega la flota así (``vessel_awaiting_instructions.py``):

* todos los buques entran al pool en t=0 y **nunca vuelven**;
* el primer buque de cada ruta sale en la próxima ocurrencia de
  ``route.start_day_of_week`` (ancla semanal del CSV de entrada);
* los siguientes salen **uno cada 7 días exactos**.

Como el ciclo de cada ruta no es múltiplo de 7, las pasadas por un puerto no
quedan repartidas de forma uniforme: cada servicio es semanal con **un hueco
irregular** que absorbe el desajuste. Medido sobre esta red:

    S1  7.00 x10, 8.43        S5  7.00 x8, 16.16
    S2  2.76, 7.00            S6  7.00, 7.00, 16.09
    S3  1.03, 1.03, 5.97      S7  15.60
    S4  3.67, 7.00 x9

S5 y S6 tienen agujeros de dieciséis días. La carga que llega justo después de
esa salida espera el doble de lo que predice cualquier modelo de frecuencia
media, y S6 es la única ruta que sirve Cartagena.

Qué hace este módulo
--------------------
Reconstruye ese horario de forma exacta y resuelve un **camino de llegada más
temprana** sobre él: Dijkstra donde la etiqueta de cada puerto es *el instante
en que la carga puede estar allí*, no una distancia ni un costo compuesto. Es
la formulación de ruta crítica sobre red expandida en el tiempo, con la ventaja
de que aquí el horario es determinista y no hay que estimarlo observando dónde
están los buques —que además es imposible, porque ``Vessel`` no expone ningún
timestamp.

La exposición a disrupciones sale gratis: como se conoce la hora de paso por
cada tramo, basta comprobar si cae dentro de la ventana de un
``disruption_plan`` y aplicar su multiplicador, o descartar el tramo si el
puerto está cerrado a esa hora.

Límites conocidos
-----------------
El tiempo de manipulación en puerto depende de la carga y no se conoce de
antemano; se aproxima con ``PORT_CALL_HOURS``. La navegación lleva además un
±5% aleatorio. Ambos hacen que el horario derive con el horizonte, así que el
error crece con el número de tramos por delante. Por eso la ventaja es mayor en
las primeras conexiones, que es donde se decide.
"""

from __future__ import annotations

import datetime as dt
import heapq
import itertools
import math

from .challenger_routing import (
    _apply,
    _graphs,
    _windows,
    _default_path,
)
from .strategy_params import PARAMS


DIAGNOSTICS = {
    "shipments_seen": 0,
    "timetable_built": 0,
    "path_found": 0,
    "path_not_found": 0,
    "same_as_default": 0,
    "differs_from_default": 0,
    "sum_predicted_days": 0.0,
    "sum_first_wait_days": 0.0,
    "transfers_total": 0,
}

WEEK_HOURS = 7 * 24.0
RELEASE_HEADWAY_HOURS = WEEK_HOURS  # vessel_awaiting_instructions._headway


class _Stop:
    """Una parada bookeable de una ruta: el puerto y su desfase en el ciclo."""

    __slots__ = ("segment_index", "port", "offset_hours")

    def __init__(self, segment_index, port, offset_hours):
        self.segment_index = segment_index
        self.port = port
        self.offset_hours = offset_hours


class _RouteTable:
    """Horario de una ruta: fases de sus buques y desfases de sus paradas."""

    __slots__ = ("route", "cycle_hours", "stops", "phases", "legs_by_index")

    def __init__(self, route, cycle_hours, stops, phases, legs_by_index):
        self.route = route
        self.cycle_hours = cycle_hours
        self.stops = stops
        # Instante de salida del primer puerto para cada buque, en horas
        # absolutas desde datetime.min.
        self.phases = phases
        self.legs_by_index = legs_by_index

    def next_departure(self, stop, after_hours):
        """Primera pasada por ``stop`` en o después de ``after_hours``."""
        best = math.inf
        for phase in self.phases:
            base = phase + stop.offset_hours
            if base >= after_hours:
                candidate = base
            else:
                cycles = math.ceil((after_hours - base) / self.cycle_hours)
                candidate = base + cycles * self.cycle_hours
            if candidate < best:
                best = candidate
        return best


class _Timetable:
    __slots__ = ("key", "tables", "stops_by_port")

    def __init__(self):
        self.key = None
        self.tables = {}
        self.stops_by_port = {}


_TIMETABLES = {}


def _hours_since_origin(when):
    return (when - dt.datetime.min).total_seconds() / 3600.0


def _first_release_hours(route):
    """Primera salida de la ruta: próxima ocurrencia del ancla semanal tras t=0.

    Reproduce ``VesselAwaitingInstructions._compute_initial_delay``, que mide
    el día de la semana con ``datetime.min`` como origen del reloj.
    """
    start_day = float(getattr(route, "start_day_of_week", 0.0) or 0.0)
    origin = dt.datetime.min
    current_day = origin.weekday() + (
        origin.hour * 3600 + origin.minute * 60 + origin.second
    ) / 86400.0
    delay_days = start_day - current_day
    if delay_days < 0:
        delay_days += 7.0
    return max(0.0, delay_days) * 24.0


def _build(context) -> _Timetable:
    cached = _TIMETABLES.get(id(context))
    if cached is None:
        cached = _Timetable()
        _TIMETABLES[id(context)] = cached

    key = tuple(
        (id(route), len(route.segments), len(route.deployed_vessels))
        for route in context.service_routes
    )
    if key == cached.key:
        return cached

    port_call_hours = PARAMS["PORT_CALL_HOURS"]
    tables = {}
    stops_by_port = {}

    for route in context.service_routes:
        segments = sorted(route.segments, key=lambda s: s.sequence_index)
        vessels = [v for v in route.deployed_vessels if v.vessel_class is not None]
        if not segments or not vessels:
            continue
        speed = sum(v.vessel_class.sailing_speed for v in vessels) / len(vessels)
        if speed <= 0:
            continue

        # Desfase de cada parada respecto a la salida del primer puerto.
        stops = []
        legs_by_index = {}
        offset = 0.0
        for position, segment in enumerate(segments):
            leg = segment.associated_leg
            stops.append(_Stop(segment.sequence_index, leg.departure_port, offset))
            legs_by_index[segment.sequence_index] = (
                leg,
                offset,
                leg.sailing_distance / speed,
            )
            offset += leg.sailing_distance / speed
            if position < len(segments) - 1:
                offset += port_call_hours
        cycle_hours = offset + port_call_hours

        first = _first_release_hours(route)
        phases = [first + RELEASE_HEADWAY_HOURS * index for index in range(len(vessels))]

        table = _RouteTable(route, cycle_hours, stops, phases, legs_by_index)
        tables[route] = table
        for stop in stops:
            stops_by_port.setdefault(stop.port, []).append((table, stop))

    cached.key = key
    cached.tables = tables
    cached.stops_by_port = stops_by_port
    DIAGNOSTICS["timetable_built"] += 1
    return cached


def _ride(table, start_stop, windows, board_hours, avoid_port_names):
    """Recorre la ruta desde ``start_stop`` devolviendo cada llegada posible.

    Produce ``(segmento_llegada, puerto, hora_de_llegada)`` para cada parada
    siguiente, aplicando el multiplicador de disrupción vigente en el momento
    real de recorrer cada tramo. Corta si un puerto está cerrado a esa hora.
    """
    segments = sorted(table.route.segments, key=lambda s: s.sequence_index)
    count = len(segments)
    start = next(
        (i for i, s in enumerate(segments) if s.sequence_index == start_stop.segment_index),
        None,
    )
    if start is None:
        return

    now_hours = board_hours
    for step in range(count):
        index = (start + step) % count
        segment = segments[index]
        leg = segment.associated_leg
        _leg, _offset, nominal = table.legs_by_index[segment.sequence_index]

        entry = dt.datetime.min + dt.timedelta(hours=now_hours)
        multiplier = windows.leg_multiplier_at(
            leg, entry, entry + dt.timedelta(hours=nominal)
        )
        now_hours += nominal * multiplier
        arrival = dt.datetime.min + dt.timedelta(hours=now_hours)

        port = leg.arrival_port
        if port.name.casefold() in avoid_port_names:
            return
        if windows.port_closed_at(port, arrival):
            return
        if port is start_stop.port:
            return

        yield segment.sequence_index, port, now_hours
        now_hours += PARAMS["PORT_CALL_HOURS"]


def earliest_arrival_path(context, now, origin_port, destination_port):
    """Dijkstra de llegada más temprana sobre el horario.

    La etiqueta de cada puerto es el instante en que la carga puede estar allí.
    Devuelve la lista de tramos a reservar, o None.
    """
    timetable = _build(context)
    graphs = _graphs(context, now)
    windows = _windows(context)
    avoid = graphs.avoid_port_names
    max_transfers = max(0, PARAMS["MAX_TRANSFERS"])

    start_hours = _hours_since_origin(now)
    counter = itertools.count()
    best = {origin_port: start_hours}
    previous = {}
    heap = [(start_hours, 0, next(counter), origin_port, None)]

    while heap:
        arrive_hours, transfers, _, port, arrived_on = heapq.heappop(heap)
        if arrive_hours > best.get(port, math.inf):
            continue
        if port is destination_port:
            break

        for table, stop in timetable.stops_by_port.get(port, ()):
            if table.route is arrived_on:
                continue
            next_transfers = transfers + (1 if arrived_on is not None else 0)
            if next_transfers > max_transfers:
                continue

            board = table.next_departure(stop, arrive_hours)
            if not math.isfinite(board):
                continue

            for arrival_index, arrival_port, arrival_hours in _ride(
                table, stop, windows, board, avoid
            ):
                if arrival_hours >= best.get(arrival_port, math.inf):
                    continue
                best[arrival_port] = arrival_hours
                previous[arrival_port] = (
                    port, table.route, stop.segment_index, arrival_index, arrive_hours
                )
                heapq.heappush(
                    heap,
                    (arrival_hours, next_transfers, next(counter),
                     arrival_port, table.route),
                )

    if destination_port not in previous:
        return None, None

    chain = []
    cursor = destination_port
    guard = 0
    while cursor is not origin_port and guard < 64:
        guard += 1
        entry = previous.get(cursor)
        if entry is None:
            return None, None
        from_port, route, departure_index, arrival_index, _ = entry
        chain.append((route, departure_index, arrival_index))
        cursor = from_port
    chain.reverse()
    return chain, best.get(destination_port)


class _ChainEdge:
    """Adaptador mínimo para reutilizar la materialización existente."""

    __slots__ = ("service_route", "departure_segment_index", "arrival_segment_index")

    def __init__(self, route, departure_index, arrival_index):
        self.service_route = route
        self.departure_segment_index = departure_index
        self.arrival_segment_index = arrival_index


def assign_timetable(context, now, shipment):
    demand = shipment.demand
    origin_port, destination_port = demand.origin_port, demand.destination_port
    if origin_port is destination_port:
        return None

    DIAGNOSTICS["shipments_seen"] += 1
    chain, arrival_hours = earliest_arrival_path(
        context, now, origin_port, destination_port
    )
    if not chain:
        DIAGNOSTICS["path_not_found"] += 1
        return None

    DIAGNOSTICS["path_found"] += 1
    DIAGNOSTICS["transfers_total"] += len(chain) - 1
    if arrival_hours is not None:
        DIAGNOSTICS["sum_predicted_days"] += (
            arrival_hours - _hours_since_origin(now)
        ) / 24.0

    graphs = _graphs(context, now)
    default_path = _default_path(context, graphs, origin_port, destination_port)
    if default_path is not None:
        same = len(default_path) == len(chain) and all(
            edge.service_route is route
            and edge.departure_segment_index == departure
            and edge.arrival_segment_index == arrival
            for edge, (route, departure, arrival) in zip(default_path, chain)
        )
        DIAGNOSTICS["same_as_default" if same else "differs_from_default"] += 1

    return _apply(
        shipment,
        [_ChainEdge(route, departure, arrival) for route, departure, arrival in chain],
    )
