"""Horario aprendido de la observación, no derivado de supuestos.

El problema del horario calculado
---------------------------------
Se puede reconstruir el calendario de la red a partir del despliegue de flota:
los buques salen uno cada 7 días desde t=0, anclados al ``StartDayOfWeek``, y
después circulan. Con la geometría de los tramos y la velocidad sale, en teoría,
cuándo pasa cada buque por cada parada.

En la práctica no sale. El tiempo de escala depende de la carga que se maneja
—``TEU / (grúas × 45)``— y no se conoce de antemano. Un error de unas horas por
escala, quince escalas por ciclo y 140 días de calentamiento bastan para que la
fase calculada no tenga nada que ver con la real. Medido: el horario derivado
predecía esperas medias de 12.9 días cuando el ATT total es de 20.4, lo que es
imposible. Esa deriva es la razón por la que la estrategia de llegada más
temprana no ganó.

La solución: no calcularlo, verlo
---------------------------------
El modelo llama a la estrategia **para cada buque en cada escala**. Eso es un
flujo en vivo de posiciones reales con su hora exacta, y lo estábamos tirando.
Este módulo lo anota y reconstruye el horario por observación:

* para cada ``(ruta, parada)`` guarda las últimas pasadas observadas;
* estima el ciclo como la mediana del tiempo que tarda el patrón en repetirse,
  que con ``n`` buques es la diferencia entre una pasada y la n-ésima anterior;
* predice la próxima pasada proyectando las observaciones recientes hacia
  delante en múltiplos de ese ciclo.

No hay modelo que pueda desviarse: si el tiempo de escala cambia, las
observaciones cambian con él. Durante el calentamiento el módulo solo mira y no
decide nada; cuando tiene suficientes pasadas de una parada, empieza a
responder. Esa espera es deliberada — es preferible no intervenir a intervenir
con un calendario inventado.
"""

from __future__ import annotations

import datetime as dt
import statistics


# Pasadas que se guardan por parada. Basta con cubrir algo más de una vuelta
# completa del patrón para poder estimar el ciclo.
HISTORY = 16

# Pasadas mínimas antes de fiarse de la estimación.
MIN_OBSERVATIONS = 4


_STATE = {}


DIAGNOSTICS = {
    "recorded": 0,
    "stops_tracked": 0,
    "queries": 0,
    "answered": 0,
    "not_enough_data": 0,
}


def _state(context):
    state = _STATE.get(id(context))
    if state is None:
        state = {"passages": {}, "cycle": {}}
        _STATE[id(context)] = state
    return state


def _hours(when):
    return (when - dt.datetime.min).total_seconds() / 3600.0


def record(context, now, vessel) -> None:
    """Anota que este buque está en esta parada, ahora."""
    if vessel is None:
        return
    route = vessel.assigned_service_route
    segment = vessel.current_segment
    if route is None or segment is None:
        return

    state = _state(context)
    key = (id(route), segment.sequence_index)
    history = state["passages"].setdefault(key, [])
    hours = _hours(now)

    # El mismo buque puede pasar por el hook varias veces en una escala.
    if history and abs(history[-1] - hours) < 1e-6:
        return

    history.append(hours)
    if len(history) > HISTORY:
        del history[0]
    DIAGNOSTICS["recorded"] += 1
    DIAGNOSTICS["stops_tracked"] = len(state["passages"])

    vessels = max(1, len(route.deployed_vessels))
    if len(history) > vessels:
        gaps = [
            history[i] - history[i - vessels]
            for i in range(vessels, len(history))
            if history[i] > history[i - vessels]
        ]
        if gaps:
            state["cycle"][key] = statistics.median(gaps)


def next_departure(context, route, segment_index, after_hours):
    """Próxima pasada observada por esa parada, o None si aún no se sabe."""
    DIAGNOSTICS["queries"] += 1
    state = _state(context)
    key = (id(route), segment_index)
    history = state["passages"].get(key)
    cycle = state["cycle"].get(key)

    if not history or len(history) < MIN_OBSERVATIONS or not cycle or cycle <= 0:
        DIAGNOSTICS["not_enough_data"] += 1
        return None

    vessels = max(1, len(route.deployed_vessels))
    best = None
    for passage in history[-vessels:] or history:
        if passage >= after_hours:
            candidate = passage
        else:
            steps = int((after_hours - passage) / cycle) + 1
            candidate = passage + steps * cycle
            # Puede haberse pasado por un ciclo entero; ajustar hacia atrás.
            while candidate - cycle >= after_hours:
                candidate -= cycle
        if best is None or candidate < best:
            best = candidate

    if best is not None:
        DIAGNOSTICS["answered"] += 1
    return best


def coverage(context) -> float:
    """Fracción de paradas para las que ya hay una estimación fiable."""
    state = _state(context)
    total = len(state["passages"]) or 1
    ready = sum(
        1
        for key, history in state["passages"].items()
        if len(history) >= MIN_OBSERVATIONS and state["cycle"].get(key)
    )
    return ready / total
