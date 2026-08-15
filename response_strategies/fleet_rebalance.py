"""Balanceo de líneas: prestar un buque a la línea con el peor agujero de horario.

Qué problema resuelve
---------------------
Este módulo **no** reduce el tiempo de navegación: la distancia y la velocidad
son dato y ninguna decisión de estrategia las cambia. Lo que reduce es la
**espera por frecuencia**, que en esta red vale unos 4.5 días de los ~24 de
tiempo puerta a puerta.

El simulador libera los buques de cada línea **uno cada 7 días** desde t=0
(``vessel_awaiting_instructions.py``), anclados al ``StartDayOfWeek`` del CSV de
entrada, y nunca vuelven al pool. Con ``n`` buques sobre un ciclo de ``C`` días
las salidas quedan en las fases ``0, 7, ... 7(n-1)`` y sobra un hueco de
``C - 7(n-1)``. Una línea está balanceada cuando ``C/7 = n``.

Hoy la flota está mal repartida: S3 tiene casi dos buques que no producen
frecuencia —el simulador no libera más de uno por semana— mientras S7 arrastra
un único hueco de 15.6 días.

Cómo se elige el traslado
-------------------------
No por ``ciclo / buques``. Ese número supone huecos iguales y aquí no lo son.
La espera media de una llegada aleatoria a un horario con huecos desiguales es,
por teoría de renovación::

    espera = suma(g_i^2) / (2 * C)

Con huecos iguales se reduce a ``C/2n``, pero con los huecos reales de esta red
da valores muy distintos: S3 espera 2.35 d y no 1.34; S7 espera 7.80 d.

Para cada par (donante, receptor) que comparta puerto se calcula el cambio neto
en TEU-días de espera al año, ponderando por la carga que embarca en cada línea.
Se elige el mejor, y solo si el neto es negativo por encima de un umbral.

Cómo se ejecuta
---------------
El validador del enunciado permite trasladar buques de rutas preexistentes a
rutas **nuevas** creadas por la estrategia, que es justo lo que hace
``DefaultStrategy`` con sus alternativas de disrupción. Así que se duplica el
ciclo del receptor reutilizando sus mismos legs y se le presta el buque.

El modelo solo deja cambiar un buque cuando llega **vacío al primer puerto** de
la ruta nueva. Por eso se reservan todos los buques elegibles del donante y se
mueve el primero que coincida; los demás se liberan. Y por eso el receptor se
prefiere entre los que empiezan su ciclo en el puerto compartido.
"""

from __future__ import annotations

import datetime as dt

from maritime_data_context import Segment, ServiceRoute

from .default_strategy import (
    _build_all_candidate_bookings,
    _find_shortest_booking_path,
    _try_switch_empty_vessel_to_pending_route,
)
from .strategy_params import PARAMS
from simulation_model.ordered_set import OrderedSet


RELEASE_DAYS = 7.0

DIAGNOSTICS = {
    "evaluated": False,
    "donor": None,
    "receiver": None,
    "exchange_port": None,
    "expected_saving_teu_days": 0.0,
    "expected_saving_att_days": 0.0,
    "route_created": False,
    "vessels_reserved": 0,
    "switch_attempts": 0,
    "empty_at_hook": 0,
    "switched": False,
    "switched_on_day": None,
    "switched_vessel_index": None,
}

_STATE = {}


# ---------------------------------------------------------------------------
# horario y espera
# ---------------------------------------------------------------------------

def _gaps(cycle_days, vessels):
    """Huecos reales entre pasadas, dados los buques liberados cada 7 días."""
    phases = sorted((RELEASE_DAYS * index) % cycle_days for index in range(vessels))
    gaps = []
    for index, phase in enumerate(phases):
        following = phases[(index + 1) % len(phases)]
        gap = (following - phase) % cycle_days
        gaps.append(gap if gap > 1e-9 else cycle_days)
    return gaps


def _expected_wait_days(cycle_days, vessels):
    """Espera media de una llegada aleatoria: suma(g^2) / (2C)."""
    if vessels < 1 or cycle_days <= 0:
        return float("inf")
    gaps = _gaps(cycle_days, vessels)
    return sum(gap * gap for gap in gaps) / (2.0 * cycle_days)


def _line_facts(context):
    port_call_days = PARAMS["PORT_CALL_HOURS"] / 24.0
    facts = {}
    for route in context.service_routes:
        if route.source_service_route is not None:
            continue
        segments = sorted(route.segments, key=lambda s: s.sequence_index)
        vessels = [v for v in route.deployed_vessels if v.vessel_class is not None]
        if not segments or not vessels:
            continue
        speed = sum(v.vessel_class.sailing_speed for v in vessels) / len(vessels)
        if speed <= 0:
            continue
        cycle = sum(
            s.associated_leg.sailing_distance / speed / 24.0 for s in segments
        ) + port_call_days * len(segments)
        facts[route] = {
            "cycle": cycle,
            "vessels": len(vessels),
            "segments": segments,
            "ports": [s.associated_leg.departure_port for s in segments],
            "start_port": segments[0].associated_leg.departure_port,
        }
    return facts


def _teu_by_line(context, facts):
    """Carga anual que embarca en cada línea, ruteando como el Default."""
    edges = _build_all_candidate_bookings(context, OrderedSet(), OrderedSet())
    carried = {route: 0.0 for route in facts}
    for demand in context.demands:
        origin, destination = demand.origin_port, demand.destination_port
        if origin is destination or demand.annual_teus <= 0:
            continue
        path = _find_shortest_booking_path(context, origin, destination, edges)
        if not path:
            continue
        for edge in path:
            if edge.service_route in carried:
                carried[edge.service_route] += demand.annual_teus
    return carried


# ---------------------------------------------------------------------------
# el plan
# ---------------------------------------------------------------------------

class _Plan:
    __slots__ = ("donor", "receiver", "exchange_port", "saving_teu_days",
                 "saving_att_days", "route", "done")

    def __init__(self, donor, receiver, exchange_port, saving_teu_days,
                 saving_att_days):
        self.donor = donor
        self.receiver = receiver
        self.exchange_port = exchange_port
        self.saving_teu_days = saving_teu_days
        self.saving_att_days = saving_att_days
        self.route = None
        self.done = False


def _choose_plan(context):
    facts = _line_facts(context)
    if len(facts) < 2:
        return None
    carried = _teu_by_line(context, facts)
    total_demand = sum(d.annual_teus for d in context.demands) or 1.0

    best = None
    for donor, donor_facts in facts.items():
        if donor_facts["vessels"] < 2:
            continue
        donor_cost = (
            _expected_wait_days(donor_facts["cycle"], donor_facts["vessels"] - 1)
            - _expected_wait_days(donor_facts["cycle"], donor_facts["vessels"])
        ) * carried[donor]

        for receiver, receiver_facts in facts.items():
            if receiver is donor:
                continue
            shared = set(donor_facts["ports"]) & set(receiver_facts["ports"])
            if not shared:
                continue
            receiver_gain = (
                _expected_wait_days(receiver_facts["cycle"], receiver_facts["vessels"] + 1)
                - _expected_wait_days(receiver_facts["cycle"], receiver_facts["vessels"])
            ) * carried[receiver]

            net = donor_cost + receiver_gain
            if net >= -PARAMS["FLEET_MIN_SAVING_TEU_DAYS"]:
                continue

            # Puerto de intercambio: el propio inicio de ciclo del receptor si
            # el donante pasa por ahí. Es donde más probable es que el buque
            # llegue vacío, y evita tener que rotar el ciclo duplicado.
            start = receiver_facts["start_port"]
            if start in shared:
                exchange = start
            else:
                # Si no, el puerto compartido que el donante visita más veces.
                exchange = max(
                    shared, key=lambda port: donor_facts["ports"].count(port)
                )

            if best is None or net < best.saving_teu_days:
                best = _Plan(donor, receiver, exchange, net, -net / total_demand)
    return best


# ---------------------------------------------------------------------------
# ejecución
# ---------------------------------------------------------------------------

def _duplicate_cycle(context, source, start_port):
    """Duplica el ciclo del receptor, rotado para empezar en ``start_port``."""
    segments = sorted(source.segments, key=lambda s: s.sequence_index)
    if not segments:
        return None

    offset = next(
        (
            index
            for index, segment in enumerate(segments)
            if segment.associated_leg.departure_port is start_port
        ),
        0,
    )
    if offset:
        segments = segments[offset:] + segments[:offset]

    existing = {route.id.casefold() for route in context.service_routes}
    index = 1
    while f"{source.id}-BAL-{index}".casefold() in existing:
        index += 1

    route = ServiceRoute(
        id=f"{source.id}-BAL-{index}",
        name=f"{source.name} Frequency Balance",
        start_day_of_week=source.start_day_of_week,
    )
    for sequence_index, segment in enumerate(segments, start=1):
        leg = segment.associated_leg
        new_segment = Segment(sequence_index, leg, route)
        route.segments.append(new_segment)
        leg.segments.append(new_segment)
        context.partial_service_routes.append(new_segment)

    context.service_routes.append(route)
    return route


def _reserve_all(plan):
    reserved = 0
    for vessel in sorted(plan.donor.deployed_vessels, key=lambda v: v.index):
        if vessel.assigned_service_route is not plan.donor:
            continue
        if vessel.pending_assigned_service_route is not None:
            continue
        vessel.pending_assigned_service_route = plan.route
        reserved += 1
    return reserved


def _release_others(plan, switched):
    for vessel in list(plan.donor.deployed_vessels):
        if vessel is not switched and vessel.pending_assigned_service_route is plan.route:
            vessel.pending_assigned_service_route = None


def rebalance(context, now, vessel=None) -> None:
    if PARAMS["FLEET_REBALANCE"] != "on":
        return

    state = _STATE.setdefault(id(context), {"plan": None, "evaluated": False})

    if not state["evaluated"]:
        state["evaluated"] = True
        plan = _choose_plan(context)
        state["plan"] = plan
        DIAGNOSTICS["evaluated"] = True
        if plan is not None:
            DIAGNOSTICS["donor"] = plan.donor.id
            DIAGNOSTICS["receiver"] = plan.receiver.id
            DIAGNOSTICS["exchange_port"] = plan.exchange_port.name
            DIAGNOSTICS["expected_saving_teu_days"] = -plan.saving_teu_days
            DIAGNOSTICS["expected_saving_att_days"] = plan.saving_att_days

    plan = state["plan"]
    if plan is None or plan.done:
        return

    if plan.route is None:
        plan.route = _duplicate_cycle(context, plan.receiver, plan.exchange_port)
        DIAGNOSTICS["route_created"] = plan.route is not None
        if plan.route is None:
            plan.done = True
            return
        DIAGNOSTICS["vessels_reserved"] = _reserve_all(plan)

    if vessel is not None and vessel.pending_assigned_service_route is plan.route:
        DIAGNOSTICS["switch_attempts"] += 1
        if not vessel.carried_shipments:
            DIAGNOSTICS["empty_at_hook"] += 1
        if _try_switch_empty_vessel_to_pending_route(vessel):
            DIAGNOSTICS["switched"] = True
            DIAGNOSTICS["switched_vessel_index"] = vessel.index
            DIAGNOSTICS["switched_on_day"] = round(
                (now - dt.datetime.min).total_seconds() / 86400.0, 2
            )
            _release_others(plan, vessel)
            plan.done = True
