"""Balanceo de líneas y ruta crítica sobre el horario real de la red.

Todo lo que hay aquí sale del horario que viene en ``Input/``:

* ``service_routes.csv`` -> ``StartDayOfWeek``, que ``input_summary.csv``
  describe como *"initial schedule offsets"*: el ancla semanal de cada línea.
* ``route_plan.csv``     -> cuántos buques lleva cada línea.
* ``route_segments.csv`` -> la secuencia de tramos y sus distancias.
* ``vessel_classes.csv`` -> velocidad de cada clase.

Y de cómo el simulador despliega la flota
(``simulation_model/vessel_awaiting_instructions.py``): todos los buques entran
al pool en t=0, el primero de cada línea sale en la próxima ocurrencia de su
ancla semanal, y los siguientes **uno cada 7 días exactos**. Después circulan y
no vuelven al pool.

De ahí sale la ecuación de balanceo
----------------------------------
Con ``n`` buques liberados cada 7 días sobre un ciclo de ``C`` días, las salidas
quedan en las fases ``0, 7, 14, ... 7(n-1)`` dentro del ciclo. El hueco
irregular que queda es::

    hueco = C - 7 x (n - 1)

Una línea está **balanceada** cuando ``C / 7 = n``: entonces las salidas quedan
exactamente semanales y no hay agujero. Si ``n`` es menor, aparece un hueco
largo. Si ``n`` es mayor, sobran buques que no añaden frecuencia, porque el
simulador no libera más de uno cada 7 días.

Uso::

    python analysis/line_balance.py
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

INPUT_DIR = PROJECT_ROOT / "Input"
RELEASE_DAYS = 7.0


def read_csv(name):
    with (INPUT_DIR / name).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_schedule(port_call_hours):
    """Reconstruye el horario de cada línea desde los CSV de entrada."""
    speeds = {
        row["VesselClassName"]: float(row["SailingSpeedKnots"])
        for row in read_csv("vessel_classes.csv")
    }
    capacities = {
        row["VesselClassName"]: float(row["TeuCapacity"])
        for row in read_csv("vessel_classes.csv")
    }
    plan = {
        row["RouteId"]: (row["VesselClassName"], int(row["VesselCount"]))
        for row in read_csv("route_plan.csv")
    }
    anchors = {
        row["RouteId"]: float(row["StartDayOfWeek"])
        for row in read_csv("service_routes.csv")
    }
    names = {row["RouteId"]: row["RouteName"] for row in read_csv("service_routes.csv")}

    segments = {}
    for row in read_csv("route_segments.csv"):
        segments.setdefault(row["RouteId"], []).append(
            (
                int(row["Sequence"]),
                row["FromPort"],
                row["ToPort"],
                float(row["SailingDistanceNm"]),
            )
        )

    lines = {}
    for route_id, legs in segments.items():
        legs.sort()
        vessel_class, vessels = plan.get(route_id, (None, 0))
        speed = speeds.get(vessel_class, 0.0)
        if speed <= 0 or vessels <= 0:
            continue
        sail_days = [distance / speed / 24.0 for _, _, _, distance in legs]
        cycle = sum(sail_days) + (port_call_hours / 24.0) * len(legs)
        lines[route_id] = {
            "name": names.get(route_id, route_id),
            "class": vessel_class,
            "capacity": capacities.get(vessel_class, 0.0),
            "vessels": vessels,
            "anchor": anchors.get(route_id, 0.0),
            "legs": legs,
            "sail_days": sail_days,
            "cycle": cycle,
            "ports": [leg[1] for leg in legs],
        }
    return lines


def gaps_for(cycle, vessels):
    """Huecos reales entre pasadas, dados los buques liberados cada 7 días."""
    phases = sorted((RELEASE_DAYS * index) % cycle for index in range(vessels))
    gaps = []
    for index, phase in enumerate(phases):
        nxt = phases[(index + 1) % len(phases)]
        gap = (nxt - phase) % cycle
        gaps.append(gap if gap > 1e-9 else cycle)
    return gaps


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port-call-hours", type=float, default=12.0,
                        help="tiempo por escala usado para el ciclo (default 12)")
    args = parser.parse_args(argv)

    lines = load_schedule(args.port_call_hours)

    print()
    print("=" * 92)
    print("1. BALANCEO DE LÍNEAS: buques desplegados frente a los que pide el ciclo")
    print("=" * 92)
    print(f"  {'Línea':<6}{'Clase':<15}{'Buques':>7}{'Ciclo (d)':>11}"
          f"{'Necesita C/7':>14}{'Balance':>10}{'Hueco (d)':>11}")
    surplus_total = 0.0
    rows = []
    for route_id, line in sorted(lines.items()):
        needed = line["cycle"] / RELEASE_DAYS
        balance = line["vessels"] - needed
        gaps = gaps_for(line["cycle"], line["vessels"])
        worst = max(gaps)
        rows.append((route_id, line, needed, balance, worst))
        surplus_total += balance
        flag = "sobra" if balance > 0.35 else ("FALTA" if balance < -0.35 else "ok")
        print(f"  {route_id:<6}{line['class']:<15}{line['vessels']:>7}"
              f"{line['cycle']:>11.2f}{needed:>14.2f}{balance:>+9.2f} {flag:<6}"
              f"{worst:>10.2f}")
    print(f"\n  Flota total desplegada: {sum(l['vessels'] for l in lines.values())} buques")
    print(f"  Flota que pediría un servicio semanal limpio en todas las líneas: "
          f"{sum(l['cycle'] / RELEASE_DAYS for l in lines.values()):.1f}")
    print(f"  Diferencia global: {surplus_total:+.2f} buques")
    print()
    print("  Lectura: el total es casi exacto, pero está mal repartido. Un buque")
    print("  de más en una línea con ciclo corto no añade frecuencia, porque el")
    print("  simulador no libera más de uno cada 7 días; ese buque solo hace cola.")

    print()
    print("=" * 92)
    print("2. PROPUESTA DE REBALANCEO")
    print("=" * 92)
    donors = sorted([r for r in rows if r[3] > 0.35], key=lambda r: -r[3])
    receivers = sorted([r for r in rows if r[3] < -0.35], key=lambda r: r[3])
    if not donors or not receivers:
        print("  La flota ya está balanceada.")
    else:
        print(f"  {'Donante':<8}{'sobra':>8}   {'Receptor':<10}{'falta':>8}"
              f"{'Hueco actual':>15}{'Hueco tras recibir':>20}{'Puerto común':>18}")
        for donor_id, donor, _n, donor_balance, _g in donors:
            for recv_id, recv, _n2, recv_balance, recv_gap in receivers:
                shared = set(donor["ports"]) & set(recv["ports"])
                if not shared:
                    continue
                new_gap = max(gaps_for(recv["cycle"], recv["vessels"] + 1))
                print(f"  {donor_id:<8}{donor_balance:>+8.2f}   {recv_id:<10}"
                      f"{recv_balance:>+8.2f}{recv_gap:>15.2f}{new_gap:>20.2f}"
                      f"{sorted(shared)[0]:>18}")

    print()
    print("=" * 92)
    print("3. RUTA CRÍTICA DE CADA LÍNEA: qué tramos mandan en el ciclo")
    print("=" * 92)
    for route_id, line in sorted(lines.items()):
        legs = line["legs"]
        sail = line["sail_days"]
        total = line["cycle"]
        order = sorted(range(len(legs)), key=lambda i: -sail[i])[:2]
        detail = "; ".join(
            f"{legs[i][1]}->{legs[i][2]} {sail[i]:.2f} d ({100 * sail[i] / total:.0f}%)"
            for i in order
        )
        port_share = 100 * (args.port_call_hours / 24.0) * len(legs) / total
        print(f"  {route_id:<5} ciclo {total:>6.2f} d | escalas {port_share:>4.0f}% | "
              f"crítico: {detail}")
    print()
    print("  Los tramos críticos no se pueden acortar (la geometría es dato), pero")
    print("  determinan el ciclo, y el ciclo determina cuántos buques pide la línea.")
    print("  Por eso el balanceo es la única palanca real sobre la frecuencia.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
