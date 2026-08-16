"""No bajar la carga a una conexión mala mientras se pueda seguir a bordo.

El hueco que nadie había tocado
-------------------------------
``shipment_waiting_for_loading_at_transshipment_port.py`` **no tiene ningún
punto de decisión de estrategia**. Cuando un contenedor se descarga en un hub
para transbordar, su cadena de reservas queda congelada: ninguna de las cuatro
interfaces oficiales vuelve a verlo hasta que aparece el servicio que tiene
reservado. Si ese servicio tarda quince días, espera quince días.

Por tanto el **último instante** en que se puede evitar una conexión mala es
mientras la carga todavía va a bordo del buque anterior, y eso ocurre en
``adjust_bookings_before_cargo_handling``, que el modelo llama justo antes de
manipular carga en cada escala.

Qué hace
--------
Para cada contenedor a bordo que **está a punto de bajarse aquí** para
transbordar, se compara:

* **quedarse con el plan**: esperar en este puerto a que pase el servicio
  siguiente, según el horario real;
* **seguir a bordo**: continuar en este mismo buque hasta una escala posterior
  de su rotación y conectar allí.

Si seguir a bordo entrega antes por un margen claro, se alarga la reserva actual
y se reconstruye el resto del camino desde ese puerto. Si no, no se toca nada.

Por qué es compatible con la métrica
------------------------------------
La puntuación oficial suma ``(1 - baseline/escenario)`` por período, y un
período al nivel del baseline aporta cero. Medido en este repositorio, cada
estrategia que reoptimiza la decisión de **todos** los envíos pierde períodos de
baseline y puntúa peor, aunque mejore la media. Este módulo no toca la
asignación inicial de nadie: solo interviene sobre contenedores que están a
punto de quedarse esperando una conexión larga, que son pocos y están
identificados uno a uno.
"""

from __future__ import annotations

import datetime as dt

from maritime_data_context import Booking

from . import timetable_routing as tt
from .challenger_routing import _windows
from .strategy_params import PARAMS


DIAGNOSTICS = {
    "calls": 0,
    "transfers_inspected": 0,
    "connection_wait_days": 0.0,
    "candidates_evaluated": 0,
    "extended": 0,
    "saved_days": 0.0,
    "kept": 0,
}


def _ordered(route):
    return sorted(route.segments, key=lambda s: s.sequence_index)


def _final_destination(shipment):
    return shipment.demand.destination_port


def _next_booking(shipment, booking):
    following = [
        b for b in shipment.associated_bookings
        if b.sequence_index > booking.sequence_index
    ]
    return min(following, key=lambda b: b.sequence_index) if following else None


def _departure_port_of(booking):
    if booking.service_route is None:
        return None
    for segment in booking.service_route.segments:
        if segment.sequence_index == booking.departure_segment_index:
            return segment.associated_leg.departure_port
    return None


def _plan_arrival_hours(context, now_hours, port, destination):
    """Cuándo llegaría la carga al destino saliendo de ``port`` en ``now_hours``."""
    now = dt.datetime.min + dt.timedelta(hours=now_hours)
    chain, arrival = tt.earliest_arrival_path(context, now, port, destination)
    if not chain:
        return None, None
    return chain, arrival


def adjust(context, now, vessel):
    """Devuelve None siempre: se deja que DefaultStrategy haga además lo suyo."""
    if vessel is None or not vessel.carried_shipments:
        return None

    current_segment = vessel.current_segment
    if current_segment is None or current_segment.associated_leg is None:
        return None
    port = current_segment.associated_leg.arrival_port
    if port is None:
        return None

    DIAGNOSTICS["calls"] += 1
    route = vessel.assigned_service_route
    if route is None:
        return None

    timetable = tt._build(context)
    windows = _windows(context)
    table = timetable.tables.get(route)
    if table is None:
        return None

    now_hours = tt._hours_since_origin(now)
    margin_hours = PARAMS["CONNECTION_MARGIN_HOURS"]

    for shipment in list(vessel.carried_shipments):
        try:
            booking = shipment.get_current_booking()
        except ValueError:
            continue
        if booking.service_route is not route:
            continue
        # ¿se baja aquí?
        if booking.arrival_segment_index != current_segment.sequence_index:
            continue
        following = _next_booking(shipment, booking)
        if following is None:
            continue  # llega a destino, no es transbordo

        DIAGNOSTICS["transfers_inspected"] += 1
        destination = _final_destination(shipment)

        # (a) quedarse: esperar aquí la conexión reservada y seguir el plan
        keep_chain, keep_arrival = _plan_arrival_hours(
            context, now_hours, port, destination
        )
        if keep_arrival is None:
            continue
        DIAGNOSTICS["connection_wait_days"] += max(
            0.0, (keep_arrival - now_hours) / 24.0
        )

        # (b) seguir a bordo hasta una escala posterior de esta misma rotación
        segments = _ordered(route)
        count = len(segments)
        start = next(
            (i for i, s in enumerate(segments)
             if s.sequence_index == current_segment.sequence_index),
            None,
        )
        if start is None:
            continue

        best = None
        elapsed = 0.0
        for step in range(1, count):
            index = (start + step) % count
            segment = segments[index]
            leg = segment.associated_leg
            _leg, _o, nominal = table.legs_by_index[segment.sequence_index]
            entry = dt.datetime.min + dt.timedelta(hours=now_hours + elapsed)
            multiplier = windows.leg_multiplier_at(
                leg, entry, entry + dt.timedelta(hours=nominal)
            )
            elapsed += PARAMS["PORT_CALL_HOURS"] + nominal * multiplier
            stop_port = leg.arrival_port
            if stop_port is port:
                break
            arrive_hours = now_hours + elapsed
            if windows.port_closed_at(
                stop_port, dt.datetime.min + dt.timedelta(hours=arrive_hours)
            ):
                break

            DIAGNOSTICS["candidates_evaluated"] += 1
            if stop_port is destination:
                total = arrive_hours
                chain = []
            else:
                chain, total = _plan_arrival_hours(
                    context, arrive_hours, stop_port, destination
                )
                if total is None:
                    continue
            if best is None or total < best[0]:
                best = (total, segment.sequence_index, chain)

        if best is None or best[0] >= keep_arrival - margin_hours:
            DIAGNOSTICS["kept"] += 1
            continue

        # Alargar la reserva actual hasta esa escala y rehacer el resto.
        total, arrival_index, chain = best
        DIAGNOSTICS["extended"] += 1
        DIAGNOSTICS["saved_days"] += (keep_arrival - total) / 24.0

        for old in list(shipment.associated_bookings):
            if old.sequence_index <= booking.sequence_index:
                continue
            if old.service_route is not None:
                while old in old.service_route.associated_bookings:
                    old.service_route.associated_bookings.remove(old)
            shipment.associated_bookings.remove(old)

        booking.arrival_segment_index = arrival_index
        sequence = booking.sequence_index + 1
        for next_route, departure, arrival in chain:
            new_booking = Booking(
                sequence_index=sequence,
                shipment=shipment,
                service_route=next_route,
                departure_segment_index=departure,
                arrival_segment_index=arrival,
            )
            shipment.associated_bookings.append(new_booking)
            next_route.associated_bookings.append(new_booking)
            sequence += 1

    return None
