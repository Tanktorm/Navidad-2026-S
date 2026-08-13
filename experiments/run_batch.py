"""Run several simulations in parallel and summarise them in one table.

Each run is an independent ``run_sim.py`` process, so the runs do not share
state and can use all available cores.

Examples
--------
    # reference: DefaultStrategy alone, three seeds of the disruption scenario
    python experiments/run_batch.py --scenario disruption --seeds 2026 2027 2028 \
        --strategy off --label default

    # same seeds with the user strategy and a tuned parameter file
    python experiments/run_batch.py --seeds 2026 2027 2028 --params tuned.json \
        --label tuned

The batch prints the mean ATT across seeds and the standard deviation between
them. That standard deviation is the noise floor: a change smaller than it
cannot be told apart from luck.
"""

from __future__ import annotations

import argparse
import json
import signal
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_SIM = PROJECT_ROOT / "experiments" / "run_sim.py"

# On Windows every process attached to the same console receives the console
# control events. A batch left running unattended (a scheduled task, a detached
# shell) then loses its children to a stray Ctrl+C, which shows up as exit code
# 0xC000013A. Giving each run its own process group keeps that from happening.
CREATION_FLAGS = (
    subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
)


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", choices=["disruption", "baseline"],
                        default="disruption")
    parser.add_argument("--seeds", type=int, nargs="+", default=[2026, 2027, 2028])
    parser.add_argument("--strategy", choices=["on", "off"], default="on")
    parser.add_argument("--params", type=Path, default=None)
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--warmup", type=int, default=None)
    parser.add_argument("--label", default="run",
                        help="prefix for the run tags and the batch summary")
    parser.add_argument("--workers", type=int, default=4,
                        help="how many simulations to run at once (default: 4)")
    parser.add_argument("--out", type=Path, default=None,
                        help="where to write batch_<label>.json "
                             "(default: Output/runs)")
    parser.add_argument("--no-csv", action="store_true",
                        help="skip the per-run CSV set")
    parser.add_argument("--detached", action="store_true",
                        help="ignore Ctrl+C in the driver. Use it when the batch "
                             "runs unattended (scheduled task, detached shell); "
                             "stop it with taskkill.")
    parser.add_argument("--quiet", action="store_true",
                        help="hide the per-period progress of each run. Without "
                             "it the runs stream their progress, which is the "
                             "only way to see how far a long batch has got.")
    return parser.parse_args(argv)


def build_commands(args):
    commands = []
    for seed in args.seeds:
        tag = f"{args.label}_{args.scenario}_seed{seed}"
        command = [
            sys.executable,
            str(RUN_SIM),
            "--scenario", args.scenario,
            "--seed", str(seed),
            "--tag", tag,
            "--strategy", args.strategy,
        ]
        if args.quiet:
            command.append("--quiet")
        if args.params:
            command += ["--params", str(Path(args.params).resolve())]
        if args.days is not None:
            command += ["--days", str(args.days)]
        if args.warmup is not None:
            command += ["--warmup", str(args.warmup)]
        if args.no_csv:
            command.append("--no-csv")
        commands.append((tag, command))
    return commands


def run_one(item):
    tag, command = item
    started = time.perf_counter()
    # stdout is inherited so the progress of a long run is visible live; only
    # stderr is captured, and only to be able to report a failure.
    completed = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        stderr=subprocess.PIPE,
        text=True,
        creationflags=CREATION_FLAGS,
    )
    elapsed = time.perf_counter() - started
    if completed.returncode != 0:
        print(f"[{tag}] FAILED after {elapsed:.0f}s "
              f"(exit code {completed.returncode})", flush=True)
        print(completed.stderr[-2000:], flush=True)
        return {"tag": tag, "failed": True, "stderr": completed.stderr[-2000:]}

    summary_path = PROJECT_ROOT / "Output" / "runs" / tag / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    print(
        f"[{tag}] ATT={summary['overall_mean_att']:.3f} d  in {elapsed / 60:.1f} min",
        flush=True,
    )
    return summary


def main(argv=None) -> int:
    args = parse_arguments(argv)
    if args.detached:
        # The children already have their own process group; this protects the
        # driver itself, which otherwise dies to the same stray console Ctrl+C
        # and takes the batch report with it. Stop it with taskkill instead.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    commands = build_commands(args)
    print(f"Running {len(commands)} simulations, {args.workers} at a time.", flush=True)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        summaries = list(pool.map(run_one, commands))
    total_minutes = (time.perf_counter() - started) / 60

    good = [item for item in summaries if not item.get("failed")]
    values = [item["overall_mean_att"] for item in good]

    print()
    print("=" * 72)
    print(f"Batch '{args.label}'  scenario={args.scenario}  strategy={args.strategy}")
    if args.params:
        print(f"  params: {args.params}")
    print("=" * 72)
    print(f"  {'Run':<40}{'ATT (d)':>10}{'Waiting TEU':>14}")
    for item in good:
        print(f"  {item['tag']:<40}{item['overall_mean_att']:>10.3f}"
              f"{item.get('total_waiting_teu', float('nan')):>14,.0f}")

    result = {
        "label": args.label,
        "scenario": args.scenario,
        "strategy": args.strategy,
        "params_file": str(args.params) if args.params else None,
        "seeds": args.seeds,
        "runs": good,
        "failed": [item for item in summaries if item.get("failed")],
        "total_minutes": total_minutes,
    }

    if values:
        mean = statistics.mean(values)
        spread = statistics.stdev(values) if len(values) > 1 else 0.0
        result["mean_att"] = mean
        result["std_att_between_seeds"] = spread
        result["worst_att"] = max(values)
        print()
        print(f"  mean ATT across seeds  : {mean:.3f} days")
        print(f"  std dev between seeds  : {spread:.3f} days  <- noise floor")
        print(f"  worst seed             : {max(values):.3f} days")
    print(f"  wall clock             : {total_minutes:.1f} min")

    out_directory = args.out or (PROJECT_ROOT / "Output" / "runs")
    out_directory.mkdir(parents=True, exist_ok=True)
    out_path = out_directory / f"batch_{args.label}.json"
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"  written to             : {out_path}")
    return 0 if values else 1


if __name__ == "__main__":
    raise SystemExit(main())
