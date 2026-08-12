"""Simulation-based optimization of the strategy parameters with Optuna.

This is the machine-learning part of the project: a Tree-structured Parzen
Estimator builds a probabilistic model of "parameters -> ATT" from the trials
run so far and uses it to decide what to try next. It learns from its own
experiments instead of sweeping a grid.

Two rules keep the result honest:

* **Fixed seeds during the search.** Every trial is scored on the same seeds,
  so a difference between trials is a difference in the parameters and not in
  the luck of the draw.
* **Unseen seeds for validation.** The best trials are re-run on seeds the
  optimizer never saw. What survives that is the number worth reporting.

The score is deliberately not the plain mean::

    score = mean(ATT over seeds) + robustness * stdev(ATT over seeds)

so a configuration that is excellent on one seed and poor on another loses to
a configuration that is merely good on all of them. That is what makes the
result more likely to hold up on a disruption scenario nobody has seen yet.

Usage
-----
    # search (writes/updates a SQLite study so several machines can share it)
    python experiments/ml_optimizer.py --trials 60 --seeds 2026 2027 2028

    # cheaper search first: shorter horizon, fewer seeds
    python experiments/ml_optimizer.py --trials 40 --days 180 --seeds 2026 2027

    # validate the best trials on seeds the optimizer never saw
    python experiments/ml_optimizer.py --validate 3 --validation-seeds 2030 2031 2032 2033 2034

    # what mattered
    python experiments/ml_optimizer.py --report
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_SIM = PROJECT_ROOT / "experiments" / "run_sim.py"
STUDY_DIRECTORY = PROJECT_ROOT / "Output" / "optuna"
PARAMS_DIRECTORY = STUDY_DIRECTORY / "params"


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--study", default="wsc2026",
                        help="study name inside the SQLite storage")
    parser.add_argument("--storage", default=None,
                        help="Optuna storage URL "
                             "(default: sqlite:///Output/optuna/<study>.db)")
    parser.add_argument("--trials", type=int, default=0,
                        help="how many trials this worker should run")
    parser.add_argument("--seeds", type=int, nargs="+", default=[2026, 2027, 2028],
                        help="seeds used to score every trial (kept fixed)")
    parser.add_argument("--validation-seeds", type=int, nargs="+",
                        default=[2030, 2031, 2032, 2033, 2034],
                        help="seeds used only for validation")
    parser.add_argument("--validate", type=int, default=0,
                        help="validate the N best trials on the validation seeds")
    parser.add_argument("--report", action="store_true",
                        help="print the best trials and the parameter importances")
    parser.add_argument("--days", type=int, default=None,
                        help="measured days per run (default: full config value)")
    parser.add_argument("--warmup", type=int, default=None,
                        help="warm-up days per run (default: full config value)")
    parser.add_argument("--scenario", choices=["disruption", "baseline"],
                        default="disruption")
    parser.add_argument("--robustness", type=float, default=0.5,
                        help="weight of the between-seed spread in the score "
                             "(0 = plain mean, higher = prefer stable)")
    parser.add_argument("--workers", type=int, default=1,
                        help="how many seeds of a trial to run at once")
    parser.add_argument("--sampler", choices=["tpe", "cmaes", "random"], default="tpe")
    parser.add_argument("--no-prune", action="store_true",
                        help="disable the median pruner")
    parser.add_argument("--freeze", type=Path, default=None,
                        help="write the best parameters to this JSON file")
    return parser.parse_args(argv)


# --------------------------------------------------------------------------
# search space
# --------------------------------------------------------------------------

def suggest_parameters(trial) -> dict:
    """Everything the strategy reads, including the structural choices.

    Tuning only numbers finds the best version of one design. Offering the
    design decisions as categorical parameters lets the optimizer compare
    designs, which is the only way past a badly shaped cost function.
    """
    parameters = {
        "TRANSFER_BUFFER_HOURS": trial.suggest_float("TRANSFER_BUFFER_HOURS", 0.0, 48.0),
        "WAIT_FRACTION": trial.suggest_float("WAIT_FRACTION", 0.25, 1.0),
        "QUEUE_WEIGHT": trial.suggest_float("QUEUE_WEIGHT", 0.0, 3.0),
        "PORT_CALL_HOURS": trial.suggest_float("PORT_CALL_HOURS", 0.0, 36.0),
        "MAX_TRANSFERS": trial.suggest_int("MAX_TRANSFERS", 1, 3),
        "ANTICIPATION_DAYS": trial.suggest_float("ANTICIPATION_DAYS", 0.0, 15.0),
        "CONGESTION_AWARE": trial.suggest_categorical("CONGESTION_AWARE", [True, False]),
        "ALTERNATIVE_ROUTES": trial.suggest_categorical(
            "ALTERNATIVE_ROUTES", ["default", "off"]
        ),
        "REROUTE_IN_TRANSIT": trial.suggest_categorical(
            "REROUTE_IN_TRANSIT", ["default", "off"]
        ),
    }
    return parameters


# The strategy's shipped defaults: worth evaluating before anything else so the
# optimizer does not spend trials rediscovering a sensible starting point.
SEED_TRIALS = [
    {
        "TRANSFER_BUFFER_HOURS": 24.0,
        "WAIT_FRACTION": 0.5,
        "QUEUE_WEIGHT": 1.0,
        "PORT_CALL_HOURS": 12.0,
        "MAX_TRANSFERS": 2,
        "ANTICIPATION_DAYS": 0.0,
        "CONGESTION_AWARE": True,
        "ALTERNATIVE_ROUTES": "default",
        "REROUTE_IN_TRANSIT": "default",
    },
    {
        # No transfer penalty, no queue term: closest thing to "distance only"
        # inside this parameterisation. A useful control.
        "TRANSFER_BUFFER_HOURS": 0.0,
        "WAIT_FRACTION": 0.25,
        "QUEUE_WEIGHT": 0.0,
        "PORT_CALL_HOURS": 0.0,
        "MAX_TRANSFERS": 3,
        "ANTICIPATION_DAYS": 0.0,
        "CONGESTION_AWARE": True,
        "ALTERNATIVE_ROUTES": "default",
        "REROUTE_IN_TRANSIT": "default",
    },
    {
        # Strongly connection-averse, anticipating disruptions.
        "TRANSFER_BUFFER_HOURS": 40.0,
        "WAIT_FRACTION": 0.75,
        "QUEUE_WEIGHT": 2.0,
        "PORT_CALL_HOURS": 18.0,
        "MAX_TRANSFERS": 1,
        "ANTICIPATION_DAYS": 10.0,
        "CONGESTION_AWARE": True,
        "ALTERNATIVE_ROUTES": "default",
        "REROUTE_IN_TRANSIT": "default",
    },
]


# --------------------------------------------------------------------------
# running the simulation
# --------------------------------------------------------------------------

def write_parameters(parameters: dict, name: str) -> Path:
    PARAMS_DIRECTORY.mkdir(parents=True, exist_ok=True)
    path = PARAMS_DIRECTORY / f"{name}.json"
    path.write_text(json.dumps(parameters, indent=2), encoding="utf-8")
    return path


def run_seed(params_path: Path, seed: int, args, tag: str) -> float:
    """Run one simulation and return its overall mean ATT in days."""
    summary_path = STUDY_DIRECTORY / "summaries" / f"{tag}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(RUN_SIM),
        "--scenario", args.scenario,
        "--seed", str(seed),
        "--params", str(params_path),
        "--tag", tag,
        "--json", str(summary_path),
        "--no-csv",
        "--quiet",
    ]
    if args.days is not None:
        command += ["--days", str(args.days)]
    if args.warmup is not None:
        command += ["--warmup", str(args.warmup)]

    completed = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        # Own process group: see the note in run_batch.py — otherwise a stray
        # console Ctrl+C kills the simulations of an unattended study.
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        ),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"run_sim failed for seed {seed}:\n{completed.stderr[-2000:]}"
        )
    return json.loads(summary_path.read_text(encoding="utf-8"))["overall_mean_att"]


def evaluate(parameters: dict, seeds, args, name: str, trial=None) -> dict:
    params_path = write_parameters(parameters, name)
    values = []
    for index, seed in enumerate(seeds):
        value = run_seed(params_path, seed, args, f"{name}_seed{seed}")
        values.append(value)
        if trial is not None:
            # Report progress so a clearly bad configuration can be pruned
            # before all of its seeds have been paid for.
            trial.report(statistics.mean(values), index)
            import optuna

            if trial.should_prune():
                raise optuna.TrialPruned()
    mean = statistics.mean(values)
    spread = statistics.stdev(values) if len(values) > 1 else 0.0
    return {
        "values": values,
        "mean": mean,
        "std": spread,
        "worst": max(values),
        "score": mean + args.robustness * spread,
    }


# --------------------------------------------------------------------------
# study
# --------------------------------------------------------------------------

def storage_url(args) -> str:
    if args.storage:
        return args.storage
    STUDY_DIRECTORY.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(STUDY_DIRECTORY / (args.study + '.db')).as_posix()}"


def load_study(args, create: bool = True):
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    sampler = {
        "tpe": optuna.samplers.TPESampler(seed=17),
        "cmaes": optuna.samplers.CmaEsSampler(seed=17),
        "random": optuna.samplers.RandomSampler(seed=17),
    }[args.sampler]
    pruner = (
        optuna.pruners.NopPruner()
        if args.no_prune
        else optuna.pruners.MedianPruner(n_startup_trials=8, n_warmup_steps=1)
    )
    if create:
        return optuna.create_study(
            study_name=args.study,
            storage=storage_url(args),
            direction="minimize",
            sampler=sampler,
            pruner=pruner,
            load_if_exists=True,
        )
    return optuna.load_study(
        study_name=args.study, storage=storage_url(args), sampler=sampler
    )


def search(args) -> None:
    study = load_study(args)

    already = {
        tuple(sorted(trial.params.items()))
        for trial in study.trials
        if trial.params
    }
    for parameters in SEED_TRIALS:
        if tuple(sorted(parameters.items())) not in already:
            study.enqueue_trial(parameters)

    def objective(trial):
        parameters = suggest_parameters(trial)
        result = evaluate(
            parameters, args.seeds, args, f"trial{trial.number:04d}", trial=trial
        )
        trial.set_user_attr("att_values", result["values"])
        trial.set_user_attr("att_mean", result["mean"])
        trial.set_user_attr("att_std", result["std"])
        trial.set_user_attr("att_worst", result["worst"])
        print(
            f"  trial {trial.number:>4}: score {result['score']:.3f} "
            f"(mean {result['mean']:.3f} +- {result['std']:.3f})  {parameters}",
            flush=True,
        )
        return result["score"]

    started = time.perf_counter()
    study.optimize(objective, n_trials=args.trials, catch=(RuntimeError,))
    print(f"\n{args.trials} trials in {(time.perf_counter() - started) / 60:.1f} min")
    report(args, study)


def report(args, study=None) -> None:
    import optuna

    study = study or load_study(args, create=False)
    completed = [
        trial
        for trial in study.trials
        if trial.state == optuna.trial.TrialState.COMPLETE
    ]
    if not completed:
        print("No completed trials yet.")
        return

    completed.sort(key=lambda trial: trial.value)
    print()
    print("=" * 78)
    print(f"Best trials of study '{study.study_name}' ({len(completed)} completed)")
    print("=" * 78)
    for trial in completed[:10]:
        print(f"  #{trial.number:<5} score {trial.value:.3f}  "
              f"mean {trial.user_attrs.get('att_mean', float('nan')):.3f}  "
              f"std {trial.user_attrs.get('att_std', float('nan')):.3f}")
        print(f"         {trial.params}")

    if len(completed) >= 8:
        try:
            importances = optuna.importance.get_param_importances(study)
        except (ValueError, RuntimeError) as error:
            print(f"\n  (importances unavailable: {error})")
        else:
            print()
            print("  Which parameters actually matter:")
            for name, weight in importances.items():
                bar = "#" * int(round(40 * weight))
                print(f"    {name:<24}{weight:6.1%}  {bar}")
            print("  Widen the range of the top ones; pin the bottom ones and "
                  "re-run to search a smaller space.")

    if args.freeze:
        best = completed[0]
        args.freeze.parent.mkdir(parents=True, exist_ok=True)
        args.freeze.write_text(json.dumps(best.params, indent=2), encoding="utf-8")
        print(f"\n  Best parameters written to {args.freeze}")


def validate(args) -> None:
    import optuna

    study = load_study(args, create=False)
    completed = [
        trial
        for trial in study.trials
        if trial.state == optuna.trial.TrialState.COMPLETE
    ]
    if not completed:
        print("No completed trials to validate.")
        return
    completed.sort(key=lambda trial: trial.value)

    print()
    print("=" * 78)
    print(f"Validation on unseen seeds {args.validation_seeds}")
    print("=" * 78)

    results = []
    for trial in completed[: args.validate]:
        result = evaluate(
            trial.params,
            args.validation_seeds,
            args,
            f"validate_trial{trial.number:04d}",
        )
        results.append((trial, result))
        print(f"  trial #{trial.number}: search score {trial.value:.3f} -> "
              f"validation mean {result['mean']:.3f} "
              f"(std {result['std']:.3f}, worst {result['worst']:.3f})")

    if not results:
        return
    winner, winning_result = min(results, key=lambda item: item[1]["mean"])
    print()
    print(f"  Winner on unseen seeds: trial #{winner.number} at "
          f"{winning_result['mean']:.3f} days")
    print(f"  {winner.params}")

    output = {
        "validation_seeds": args.validation_seeds,
        "results": [
            {"trial": trial.number, "params": trial.params, **result}
            for trial, result in results
        ],
        "winner": {"trial": winner.number, "params": winner.params, **winning_result},
    }
    path = STUDY_DIRECTORY / f"validation_{args.study}.json"
    path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"  written to {path}")

    if args.freeze:
        args.freeze.parent.mkdir(parents=True, exist_ok=True)
        args.freeze.write_text(json.dumps(winner.params, indent=2), encoding="utf-8")
        print(f"  Winning parameters written to {args.freeze}")


def main(argv=None) -> int:
    args = parse_arguments(argv)
    if args.trials:
        search(args)
    if args.validate:
        validate(args)
    if args.report and not args.trials:
        report(args)
    if not args.trials and not args.validate and not args.report:
        print("Nothing to do: pass --trials, --validate or --report.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
