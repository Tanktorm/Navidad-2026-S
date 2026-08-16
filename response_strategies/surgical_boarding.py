"""Desviar solo la carga que va a esperar mucho, y solo si el viaje es largo.

Por qué con umbral y no siempre
-------------------------------
La estrategia de llegada más temprana sobre el horario real está implementada y
funciona, pero al aplicarla a los 300,000 envíos pierde: cambia la decisión de
todos, incluidos los que el Default ya resolvía bien, y la métrica cobra por
cada período que deja de estar al nivel del baseline.

El guardián de conexiones demostró la alternativa: intervenir sobre el 1.3% de
los casos, dejar el resto intacto. Este módulo aplica esa misma disciplina al
**primer embarque**, que es el componente más grande de tiempo evitable.

Los dos filtros
---------------
``BOARDING_WAIT_THRESHOLD_DAYS``
    Solo se interviene si el primer servicio del camino del Default tarda más
    que esto en pasar. La espera media es de 3.2 días, pero S5, S6 y S7 tienen
    agujeros de dieciséis: el daño está en la cola de la distribución, no en el
    centro, y tratarla igual que al centro es lo que ha hundido cada intento.

``MIN_JOURNEY_DAYS``
    Solo se interviene si el viaje es largo. Sale de la aritmética de la
    métrica: un envío aporta al ATT en **cada** período que pasa sin
    completarse, con su edad de ese momento, y además con su duración entera al
    terminar. Integrando, su contribución va con el **cuadrado** de la duración.
    Un viaje de 40 días no daña el doble que uno de 20: daña unas cuatro veces
    más. El presupuesto de intervención debe gastarse ahí.

Con los dos filtros en cero el módulo se comporta como la estrategia de horario
completa, que ya sabemos que pierde. Subirlos es lo que lo convierte en
quirúrgico.
"""

from __future__ import annotations

import datetime as dt

from . import timetable_routing as tt
from .challenger_routing import _apply, _default_path, _graphs
from .strategy_params import PARAMS


DIAGNOSTICS = {
    "shipments_seen": 0,
    "default_no_path": 0,
    "gate_wait_ok": 0,
    "gate_journey_ok": 0,
    "candidates": 0,
    "switched": 0,
    "kept": 0,
    "saved_days": 0.0,
    "sum_first_wait_days": 0.0,
}


class _ChainEdge:
    __slots__ = ("service_route", "departure_segment_index", "arrival_segment_index")

    def __init__(self, route, departure, arrival):
        self.service_route = route
        self.departure_segment_index = departure
        self.arrival_segment_index = arrival


def _first_boarding_wait_hours(context, now_hours, default_path):
    """Cuándo pasa de verdad el primer servicio del camino del Default."""
    timetable = tt._build(context)
    first = default_path[0]
    table = timetable.tables.get(first.service_route)
    if table is None:
        return None
    stop = next(
        (s for s in table.stops
         if s.segment_index == first.departure_segment_index),
        None,
    )
    if stop is None:
        return None
    return table.next_departure(stop, now_hours) - now_hours


def assign(context, now, shipment):
    demand = shipment.demand
    origin_port, destination_port = demand.origin_port, demand.destination_port
    if origin_port is destination_port:
        return None

    DIAGNOSTICS["shipments_seen"] += 1
    graphs = _graphs(context, now)
    default_path = _default_path(context, graphs, origin_port, destination_port)
    if not default_path:
        DIAGNOSTICS["default_no_path"] += 1
        return None

    now_hours = tt._hours_since_origin(now)

    wait_hours = _first_boarding_wait_hours(context, now_hours, default_path)
    if wait_hours is None:
        return None
    DIAGNOSTICS["sum_first_wait_days"] += wait_hours / 24.0

    # Filtro 1: ¿espera lo suficiente como para que valga la pena mirar?
    if wait_hours < PARAMS["BOARDING_WAIT_THRESHOLD_DAYS"] * 24.0:
        DIAGNOSTICS["kept"] += 1
        return None
    DIAGNOSTICS["gate_wait_ok"] += 1

    # El camino de llegada más temprana sobre el horario real.
    chain, arrival_hours = tt.earliest_arrival_path(
        context, now, origin_port, destination_port
    )
    if not chain or arrival_hours is None:
        DIAGNOSTICS["kept"] += 1
        return None

    journey_days = (arrival_hours - now_hours) / 24.0

    # Filtro 2: ¿es un viaje largo, donde el daño crece con el cuadrado?
    if journey_days < PARAMS["MIN_JOURNEY_DAYS"]:
        DIAGNOSTICS["kept"] += 1
        return None
    DIAGNOSTICS["gate_journey_ok"] += 1
    DIAGNOSTICS["candidates"] += 1

    # ¿Cuándo llegaría siguiendo el camino del Default? Se estima con el mismo
    # horario para que la comparación sea homogénea.
    default_chain = [
        (edge.service_route, edge.departure_segment_index,
         edge.arrival_segment_index)
        for edge in default_path
    ]
    def _key(items):
        return tuple((id(r), d, a) for r, d, a in items)

    if _key(chain) == _key(default_chain):
        DIAGNOSTICS["kept"] += 1
        return None

    default_arrival = _estimate_arrival(context, now_hours, default_chain)
    if default_arrival is None:
        DIAGNOSTICS["kept"] += 1
        return None

    margin = PARAMS["BOARDING_MARGIN_HOURS"]
    if arrival_hours >= default_arrival - margin:
        DIAGNOSTICS["kept"] += 1
        return None

    DIAGNOSTICS["switched"] += 1
    DIAGNOSTICS["saved_days"] += (default_arrival - arrival_hours) / 24.0
    return _apply(
        shipment,
        [_ChainEdge(route, departure, arrival) for route, departure, arrival in chain],
    )


def _estimate_arrival(context, now_hours, chain):
    """Hora de llegada de una cadena concreta, leída del mismo horario."""
    timetable = tt._build(context)
    cursor = now_hours
    for route, departure, arrival in chain:
        table = timetable.tables.get(route)
        if table is None:
            return None
        stop = next(
            (s for s in table.stops if s.segment_index == departure), None
        )
        if stop is None:
            return None
        board = table.next_departure(stop, cursor)
        segments = sorted(route.segments, key=lambda s: s.sequence_index)
        count = len(segments)
        start = next(
            (i for i, s in enumerate(segments) if s.sequence_index == departure), None
        )
        end = next(
            (i for i, s in enumerate(segments) if s.sequence_index == arrival), None
        )
        if start is None or end is None:
            return None
        elapsed = 0.0
        index = start
        while True:
            segment = segments[index]
            _leg, _offset, nominal = table.legs_by_index[segment.sequence_index]
            leg = segment.associated_leg
            multiplier = float(getattr(leg, "sailing_time_multiplier", 1.0) or 1.0)
            elapsed += nominal * multiplier
            if index == end:
                break
            elapsed += PARAMS["PORT_CALL_HOURS"]
            index = (index + 1) % count
        cursor = board + elapsed + PARAMS["PORT_CALL_HOURS"]
    return cursor
