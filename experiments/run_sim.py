"""Batch runner for the WSC 2026 simulation.

Runs one simulation without launching the dashboard and writes a machine
readable summary, so several runs (seeds, parameter sets) can be compared or
driven by an optimizer.

This file does not modify the simulator. It only calls the same public API that
``main.py`` uses: ``scenario_builders``, ``Model`` and the CSV writers.

Examples
--------
    python experiments/run_sim.py --scenario disruption --seed 2026
    python experiments/run_sim.py --scenario baseline --seed 2026 --tag base
    python experiments/run_sim.py --strategy off --seed 2026   # DefaultStrategy only
    python experiments/run_sim.py --params tuned.json --days 180 --quiet

The reported ``overall_mean_att`` is the same number the simulator writes as
``OverallMean`` in ``ATT_By_Statistics_Interval.csv``: the mean of the
per-period TEU-weighted average transport times, in days.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", choices=["disruption", "baseline"],
                        default="disruption",
                        help="which scenario builder to use (default: disruption)")
    parser.add_argument("--seed", type=int, default=2026,
                        help="random seed passed to Model (default: 2026)")
    parser.add_argument("--days", type=int, default=None,
                        help="measured days (default: config SIMULATION_DAYS)")
    parser.add_argument("--warmup", type=int, default=None,
                        help="warm-up days (default: config WARM_UP_DAYS)")
    parser.add_argument("--interval", type=int, default=None,
                        help="statistics interval in days (default: config value)")
    parser.add_argument("--strategy", choices=["on", "off"], default="on",
                        help="'off' disables UserStrategy so DefaultStrategy runs alone")
    parser.add_argument("--params", type=Path, default=None,
                        help="JSON file with UserStrategy parameter overrides")
    parser.add_argument("--out", type=Path, default=None,
                        help="directory for the CSV output (default: Output/runs/<tag>)")
    parser.add_argument("--tag", default=None,
                        help="name of the run; defaults to scenario_seed")
    parser.add_argument("--json", dest="json_path", type=Path, default=None,
                        help="where to write the summary JSON (default: <out>/summary.json)")
    parser.add_argument("--no-csv", action="store_true",
                        help="skip writing the full CSV set (faster for optimization)")
    parser.add_argument("--quiet", action="store_true",
                        help="only print the final summary line")
    return parser.parse_args(argv)


def configure_environment(args) -> None:
    """Set the knobs the strategy reads, before the model modules are imported."""
    os.environ["WSC_STRATEGY_ENABLED"] = "0" if args.strategy == "off" else "1"
    if args.params is not None:
        os.environ["WSC_STRATEGY_PARAMS"] = str(Path(args.params).resolve())


def run(args) -> dict:
    configure_environment(args)

    from config.simulation_config import (
        SIMULATION_DAYS,
        STATISTICS_INTERVAL_DAYS,
        WARM_UP_DAYS,
    )
    import scenario_builders
    from simulation_model import Model
    from simulation_output_csv_writer import write_all, write_att_by_period

    warm_up_days = args.warmup if args.warmup is not None else WARM_UP_DAYS
    simulation_days = args.days if args.days is not None else SIMULATION_DAYS
    interval_days = (
        args.interval if args.interval is not None else STATISTICS_INTERVAL_DAYS
    )

    tag = args.tag or f"{args.scenario}_seed{args.seed}"
    output_directory = args.out or (PROJECT_ROOT / "Output" / "runs" / tag)
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)

    if args.scenario == "baseline":
        context = scenario_builders.create()
    else:
        context = scenario_builders.create_with_disruption()

    started_at = time.perf_counter()
    sim = Model(context, seed=args.seed)
    sim.warmup(period=dt.timedelta(days=warm_up_days))
    warm_up_seconds = time.perf_counter() - started_at

    measurement_start_time = sim.clock_time
    att_period_rows = []
    period_start_day = 1
    period_start_time = sim.clock_time

    for day in range(1, simulation_days + 1):
        sim.run(duration=dt.timedelta(days=1))
        if day % interval_days != 0 and day != simulation_days:
            continue

        average_transport_time_days = (
            sim.get_teu_weighted_average_transport_time_hours(
                period_start_time, sim.clock_time
            )
            / 24.0
        )
        att_period_rows.append((period_start_day, day, average_transport_time_days))
        if not args.quiet:
            print(
                f"[{tag}] day {day:>4}/{simulation_days}: "
                f"ATT {average_transport_time_days:6.2f} d  "
                f"({time.perf_counter() - started_at:6.1f}s)",
                flush=True,
            )
        period_start_day = day + 1
        period_start_time = sim.clock_time

    wall_seconds = time.perf_counter() - started_at

    period_values = [row[2] for row in att_period_rows]
    overall_mean = sum(period_values) / len(period_values) if period_values else 0.0
    period_std = (
        math.sqrt(
            sum((value - overall_mean) ** 2 for value in period_values)
            / (len(period_values) - 1)
        )
        if len(period_values) > 1
        else 0.0
    )

    summary = {
        "tag": tag,
        "scenario": args.scenario,
        "seed": args.seed,
        "warm_up_days": warm_up_days,
        "simulation_days": simulation_days,
        "interval_days": interval_days,
        "strategy": args.strategy,
        "params_file": str(args.params) if args.params else None,
        "overall_mean_att": overall_mean,
        "period_std_att": period_std,
        "min_period_att": min(period_values) if period_values else None,
        "max_period_att": max(period_values) if period_values else None,
        # ATT computed over the whole measured window in one shot; it is a
        # different (backlog-weighted) view than the mean of the periods.
        "window_att": sim.get_teu_weighted_average_transport_time_hours(
            measurement_start_time, sim.clock_time
        ) / 24.0,
        "periods": att_period_rows,
        "wall_seconds": wall_seconds,
        "warm_up_seconds": warm_up_seconds,
    }

    if not args.no_csv:
        write_all(sim, output_directory)
        write_att_by_period(output_directory, att_period_rows)
        summary.update(read_secondary_kpis(output_directory))

    json_path = args.json_path or (output_directory / "summary.json")
    Path(json_path).parent.mkdir(parents=True, exist_ok=True)
    Path(json_path).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    summary["summary_path"] = str(json_path)
    return summary


def _to_number(text):
    text = str(text).strip().replace(",", "").rstrip("%")
    if text in {"", "-"}:
        return float("nan")
    try:
        return float(text)
    except ValueError:
        return float("nan")


def read_secondary_kpis(output_directory: Path) -> dict:
    """Pull the waiting-TEU and route-utilization totals out of the written CSVs."""
    kpis = {}

    waiting_path = output_directory / "Port_Waiting_Statistics.csv"
    if waiting_path.is_file():
        with waiting_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        total_row = next((row for row in rows if row.get("Port") == "TOTAL"), None)
        source = [total_row] if total_row else rows
        kpis["origin_waiting_teu"] = sum(
            _to_number(row.get("Origin Waiting TEU", 0)) for row in source
        )
        kpis["transshipment_waiting_teu"] = sum(
            _to_number(row.get("Transshipment Waiting TEU", 0)) for row in source
        )
        kpis["total_waiting_teu"] = sum(
            _to_number(row.get("Total Waiting TEU", 0)) for row in source
        )
        kpis["waiting_teu_by_port"] = {
            row["Port"]: _to_number(row.get("Total Waiting TEU", 0))
            for row in rows
            if row.get("Port") != "TOTAL"
        }

    utilization_path = output_directory / "Service_Route_Utilization.csv"
    if utilization_path.is_file():
        with utilization_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        kpis["route_utilization"] = {
            row["Route"]: _to_number(row.get("Utilization", 0))
            for row in rows
            if row.get("Route") != "TOTAL"
        }
        kpis["route_carried_teu"] = {
            row["Route"]: _to_number(row.get("Avg Carried TEU", 0))
            for row in rows
            if row.get("Route") != "TOTAL"
        }

    completed_path = output_directory / "Cumulative_Completed_TEU_By_OD.csv"
    if completed_path.is_file():
        with completed_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            next(reader, None)
            total = 0.0
            for row in reader:
                for cell in row[1:]:
                    value = _to_number(cell)
                    if not math.isnan(value):
                        total += value
        kpis["completed_teu"] = total

    return kpis


def main(argv=None) -> int:
    args = parse_arguments(argv)
    summary = run(args)
    print(
        f"[{summary['tag']}] ATT={summary['overall_mean_att']:.2f} d "
        f"(period sd {summary['period_std_att']:.2f}) "
        f"waiting={summary.get('total_waiting_teu', float('nan')):,.0f} TEU "
        f"in {summary['wall_seconds']:.0f}s -> {summary['summary_path']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
