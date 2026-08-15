"""Calcula la Cumulative Resilience Loss (CRL) de una o varias corridas.

El simulador escribe el ATT período a período del escenario y, por separado, el
del baseline sin disrupción. La métrica de ranking se construye comparando los
dos, período por período:

    ATT performance ratio  = Baseline ATT / Scenario ATT
    Period resilience loss = (1 - ratio) x días del período
    CRL                    = suma de las pérdidas de todos los períodos

Esto **no** es equivalente a minimizar el ATT medio, y la diferencia importa:

* La CRL es una suma de razones, no de diferencias. Un día ahorrado en un
  período donde el ATT ya está cerca del baseline vale más que el mismo día
  ahorrado en el peor pico. Derivando, la ganancia marginal de bajar el ATT de
  un período es ``baseline / escenario²``: crece cuando el escenario está
  cerca del baseline.
* Por eso la cola después del shock pesa tanto como el pico: son muchos
  períodos ligeramente degradados, y cada uno aporta.

Uso::

    python analysis/resilience_loss.py Output/runs/C1_challenger
    python analysis/resilience_loss.py Output/runs/*  --baseline Output/Baseline_ATT_By_Statistics_Interval.csv
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = PROJECT_ROOT / "Output" / "Baseline_ATT_By_Statistics_Interval.csv"


def read_periods(path: Path) -> dict[int, tuple[float, float, float]]:
    """{índice: (día inicio, día fin, ATT)}, ignorando las filas de resumen."""
    periods = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            index = (row.get("PeriodIndex") or "").strip()
            if not index:
                continue
            try:
                periods[int(index)] = (
                    float(row["StartDay"]),
                    float(row["EndDay"]),
                    float(row["AverageTransportTime"]),
                )
            except (KeyError, ValueError):
                continue
    return periods


def resilience_loss(scenario: dict, baseline: dict):
    """Devuelve (CRL, períodos usados, peor período, ATT medio del escenario)."""
    total = 0.0
    used = 0
    worst = None
    att_sum = 0.0
    for index, (start, end, scenario_att) in sorted(scenario.items()):
        if index not in baseline or scenario_att <= 0:
            continue
        baseline_att = baseline[index][2]
        days = max(1.0, end - start + 1.0)
        loss = (1.0 - baseline_att / scenario_att) * days
        total += loss
        att_sum += scenario_att
        used += 1
        if worst is None or loss > worst[1]:
            worst = (index, loss, start, end, scenario_att, baseline_att)
    mean_att = att_sum / used if used else 0.0
    return total, used, worst, mean_att


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", type=Path,
                        help="carpetas con ATT_By_Statistics_Interval.csv")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE,
                        help="CSV de ATT por período del escenario sin disrupción")
    args = parser.parse_args(argv)

    if not args.baseline.is_file():
        print(f"No encuentro el baseline: {args.baseline}")
        return 1
    baseline = read_periods(args.baseline)
    if not baseline:
        print("El baseline no tiene períodos legibles.")
        return 1

    rows = []
    for run in args.runs:
        path = run if run.is_file() else run / "ATT_By_Statistics_Interval.csv"
        if not path.is_file():
            continue
        scenario = read_periods(path)
        if not scenario:
            continue
        crl, used, worst, mean_att = resilience_loss(scenario, baseline)
        rows.append((path.parent.name, crl, mean_att, used, worst))

    if not rows:
        print("Ninguna corrida legible.")
        return 1

    rows.sort(key=lambda item: item[1])
    print()
    print(f"Baseline: {args.baseline}  ({len(baseline)} períodos)")
    print("=" * 78)
    print(f"  {'Corrida':<26}{'CRL':>10}{'ATT medio':>12}{'Períodos':>10}"
          f"{'Peor período':>20}")
    print("=" * 78)
    for name, crl, mean_att, used, worst in rows:
        peor = f"{worst[2]:.0f}-{worst[3]:.0f} ({worst[1]:+.2f})" if worst else "-"
        print(f"  {name:<26}{crl:>10.2f}{mean_att:>12.3f}{used:>10}{peor:>20}")
    print()
    print("  CRL más baja = mejor. Ordenado por CRL.")
    if len(rows) > 1:
        by_att = sorted(rows, key=lambda item: item[2])
        if by_att[0][0] != rows[0][0]:
            print(f"  OJO: por ATT medio ganaría '{by_att[0][0]}', "
                  f"pero por CRL gana '{rows[0][0]}'. Las dos métricas no "
                  f"coinciden en esta comparación.")
        else:
            print("  Las dos métricas coinciden en el ganador.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
