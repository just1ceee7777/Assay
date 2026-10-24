"""Run a lab fault scenario and record when each phase happened.

Usage:
  python lab/scenario_runner.py redis-down             # run it against the lab
  python lab/scenario_runner.py redis-down --dry-run   # print the plan only

A scenario is a YAML file in lab/scenarios/. The run record (phase timestamps plus the
scenario's ground truth) is written to lab/runs/ and is what evaluation compares
Assay's answer against.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

LAB_DIR = Path(__file__).resolve().parent
SCENARIOS_DIR = LAB_DIR / "scenarios"
RUNS_DIR = LAB_DIR / "runs"
COMPOSE_FILE = LAB_DIR / "docker-compose.yml"

# Scenario action -> docker compose subcommand. Extend here, not in scenario files.
ACTIONS: dict[str, str] = {
    "stop_service": "stop",
    "start_service": "start",
    "restart_service": "restart",
    "pause_service": "pause",
    "unpause_service": "unpause",
}


class ScenarioError(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    action: str
    service: str

    def describe(self) -> str:
        return f"{self.action} {self.service}"


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    warmup_seconds: float
    fault: Step
    duration_seconds: float
    recovery: Step
    cooldown_seconds: float
    ground_truth: dict[str, Any] = field(default_factory=dict)


def _require(data: dict[str, Any], key: str, kind: type | tuple[type, ...]) -> Any:
    if key not in data:
        raise ScenarioError(f"missing field: {key}")
    value = data[key]
    if not isinstance(value, kind) or isinstance(value, bool):
        raise ScenarioError(f"field {key} has wrong type: {type(value).__name__}")
    return value


def _step(data: dict[str, Any], key: str) -> Step:
    raw = _require(data, key, dict)
    action = _require(raw, "action", str)
    if action not in ACTIONS:
        raise ScenarioError(f"{key}.action {action!r} is not one of {sorted(ACTIONS)}")
    return Step(action=action, service=_require(raw, "service", str))


def _seconds(data: dict[str, Any], key: str) -> float:
    value = float(_require(data, key, (int, float)))
    if value < 0:
        raise ScenarioError(f"{key} must not be negative")
    return value


def parse_scenario(data: Any) -> Scenario:
    if not isinstance(data, dict):
        raise ScenarioError("scenario must be a mapping")
    ground_truth = _require(data, "ground_truth", dict)
    for key in ("root_cause_type", "component"):
        _require(ground_truth, key, str)
    return Scenario(
        id=_require(data, "id", str),
        title=_require(data, "title", str),
        warmup_seconds=_seconds(data, "warmup_seconds"),
        fault=_step(data, "fault"),
        duration_seconds=_seconds(data, "duration_seconds"),
        recovery=_step(data, "recovery"),
        cooldown_seconds=_seconds(data, "cooldown_seconds"),
        ground_truth=ground_truth,
    )


def load_scenario(name_or_path: str) -> Scenario:
    path = Path(name_or_path)
    if not path.suffix:
        path = SCENARIOS_DIR / f"{name_or_path}.yaml"
    if not path.is_file():
        raise ScenarioError(f"scenario not found: {path}")
    scenario = parse_scenario(yaml.safe_load(path.read_text(encoding="utf-8")))
    if path.stem != scenario.id:
        raise ScenarioError(f"file name {path.stem!r} does not match id {scenario.id!r}")
    return scenario


class Executor(Protocol):
    def run(self, step: Step) -> None: ...


class ComposeExecutor:
    def __init__(self, compose_file: Path = COMPOSE_FILE) -> None:
        self.compose_file = compose_file

    def run(self, step: Step) -> None:
        command = [
            "docker",
            "compose",
            "-f",
            str(self.compose_file),
            ACTIONS[step.action],
            step.service,
        ]
        subprocess.run(command, check=True)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def run_scenario(
    scenario: Scenario,
    executor: Executor,
    out_dir: Path = RUNS_DIR,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], str] = _now,
    log: Callable[[str], None] = print,
) -> Path:
    """Execute the scenario phases and write the run record. Returns its path."""
    events: list[dict[str, str]] = []

    def mark(event: str) -> None:
        events.append({"event": event, "at": clock()})
        log(f"[{events[-1]['at']}] {event}")

    mark("warmup_started")
    sleep(scenario.warmup_seconds)

    mark("fault_injected")
    try:
        executor.run(scenario.fault)
        sleep(scenario.duration_seconds)
    finally:
        # Always try to recover, even if the run is interrupted mid-fault.
        executor.run(scenario.recovery)
        mark("fault_recovered")

    sleep(scenario.cooldown_seconds)
    mark("cooldown_finished")

    out_dir.mkdir(parents=True, exist_ok=True)
    started = events[0]["at"].replace(":", "").replace("-", "")
    path = out_dir / f"{scenario.id}-{started[:15]}.json"
    record = {
        "scenario": scenario.id,
        "fault": asdict(scenario.fault),
        "recovery": asdict(scenario.recovery),
        "ground_truth": scenario.ground_truth,
        "events": events,
    }
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"run record written to {path}")
    return path


def describe_plan(scenario: Scenario) -> str:
    return "\n".join(
        [
            f"scenario: {scenario.id} - {scenario.title}",
            f"  1. warm up for {scenario.warmup_seconds:g}s",
            f"  2. {scenario.fault.describe()}, keep for {scenario.duration_seconds:g}s",
            f"  3. {scenario.recovery.describe()}",
            f"  4. cool down for {scenario.cooldown_seconds:g}s",
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", help="scenario id or path to a scenario YAML file")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    args = parser.parse_args(argv)

    try:
        scenario = load_scenario(args.scenario)
    except ScenarioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(describe_plan(scenario))
    if args.dry_run:
        return 0
    run_scenario(scenario, ComposeExecutor())
    return 0


if __name__ == "__main__":
    sys.exit(main())
