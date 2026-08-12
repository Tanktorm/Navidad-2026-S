"""Tunable parameters for :mod:`response_strategies.user_strategy`.

The values below are the ones that ship with the strategy. They can be
overridden without touching this file, which is what the optimizer does:

* ``WSC_STRATEGY_PARAMS`` — path to a JSON file with a subset of the keys.
* ``WSC_PARAM_<KEY>`` — a single value, e.g. ``WSC_PARAM_QUEUE_WEIGHT=1.5``.
* ``WSC_STRATEGY_ENABLED=0`` — turn the whole strategy off so the simulation
  falls back to ``DefaultStrategy`` (used to produce the reference run).

Parameters are read once, when the module is first imported.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


DEFAULTS = {
    # --- cost function, everything in hours -------------------------------
    # Extra time charged for changing service route at a transshipment port,
    # on top of the wait for the next departure. Covers discharge, yard time
    # and the risk of missing the connection.
    "TRANSFER_BUFFER_HOURS": 24.0,
    # Fraction of a route's headway a shipment is expected to wait before it
    # can board. 0.5 is the textbook "uniform arrival" assumption.
    "WAIT_FRACTION": 0.5,
    # Weight applied to the queue term (TEU already booked on the same
    # port+route departure ahead of this shipment).
    "QUEUE_WEIGHT": 1.0,
    # Time a vessel spends in a port call. Used for route cycle time (and so
    # for headway) and for intermediate stops inside one booking.
    "PORT_CALL_HOURS": 12.0,

    # --- shape of the search ----------------------------------------------
    # Maximum number of transshipments allowed in a path.
    "MAX_TRANSFERS": 2,
    # How far ahead the strategy reacts to a scheduled disruption. 0 means it
    # only reacts to disruptions that are active right now.
    "ANTICIPATION_DAYS": 0.0,
    # Use each leg's live sailing_time_multiplier so congested legs cost what
    # they really cost instead of being banned outright.
    "CONGESTION_AWARE": True,
    # Ban a port that is closed (or about to close) instead of pricing it.
    # A closed berth cannot be served at all, so this is normally True.
    "AVOID_CLOSED_PORTS": True,

    # --- which decision points the user strategy takes over ----------------
    # "default" leaves the decision to DefaultStrategy, "off" suppresses it.
    "ALTERNATIVE_ROUTES": "default",
    "REROUTE_IN_TRANSIT": "default",
}

_NUMERIC_KEYS = {
    "TRANSFER_BUFFER_HOURS",
    "WAIT_FRACTION",
    "QUEUE_WEIGHT",
    "PORT_CALL_HOURS",
    "ANTICIPATION_DAYS",
}
_INTEGER_KEYS = {"MAX_TRANSFERS"}
_BOOLEAN_KEYS = {"CONGESTION_AWARE", "AVOID_CLOSED_PORTS"}


def _coerce(key, value):
    if key in _NUMERIC_KEYS:
        return float(value)
    if key in _INTEGER_KEYS:
        return int(value)
    if key in _BOOLEAN_KEYS:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    return str(value)


def _load() -> dict:
    values = dict(DEFAULTS)

    params_file = os.environ.get("WSC_STRATEGY_PARAMS")
    if params_file:
        path = Path(params_file)
        if path.is_file():
            for key, value in json.loads(path.read_text(encoding="utf-8")).items():
                if key in values:
                    values[key] = _coerce(key, value)

    for key in list(values):
        override = os.environ.get(f"WSC_PARAM_{key}")
        if override is not None:
            values[key] = _coerce(key, override)

    return values


PARAMS = _load()

STRATEGY_ENABLED = os.environ.get("WSC_STRATEGY_ENABLED", "1").strip() not in {
    "0",
    "false",
    "False",
    "no",
}


def get(key):
    return PARAMS[key]
