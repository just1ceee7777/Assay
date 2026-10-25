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
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

LAB_DIR = Path(__file__).resolve().parent
SCENARIOS_DIR = LAB_DIR / "scenarios"
RUNS_DIR = LAB_DIR / "runs"
COMPOSE_FILE = LAB_DIR / "docker-compose.yml"

# Scenario action -> docker compose arguments. Extend here, not in scenario files.
ACTIONS: dict[str, list[str]] = {
    "stop_service": ["stop"],
    "start_service": ["start"],
    "restart_service": ["restart"],
    "pause_service": ["pause"],
    "unpause_service": ["unpause"],
    # A release: recreate the container with a new APP_VERSION, leaving dependencies alone.
    "deploy_service": ["up", "-d", "--no-deps", "--force-recreate"],
}


class ScenarioError(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    action: str
    service: str
    version: str | None = None

    def describe(self) -> str:
        suffix = f" version {self.version}" if self.version else ""
        return f"{self.action} {self.service}{suffix}"


@dataclass(frozen=True)
class TimedStep:
    """A step during warm-up that is not the fault, e.g. an unrelated release."""

    after_seconds: float
    step: Step


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
    events: tuple[TimedStep, ...] = ()


def _require(data: dict[str, Any], key: str, kind: type | tuple[type, ...]) -> Any:
    if key not in data:
        raise ScenarioError(f"missing field: {key}")
    value = data[key]
    if not isinstance(value, kind) or isinstance(value, bool):
        raise ScenarioError(f"field {key} has wrong type: {type(value).__name__}")
    return value


def _parse_step(raw: dict[str, Any], where: str) -> Step:
    action = _require(raw, "action", str)
    if action not in ACTIONS:
        raise ScenarioError(f"{where}.action {action!r} is not one of {sorted(ACTIONS)}")
    version = raw.get("version")
    if action == "deploy_service" and not isinstance(version, str):
        raise ScenarioError(f"{where}: deploy_service needs a version")
    return Step(action=action, service=_require(raw, "service", str), version=version)


def _step(data: dict[str, Any], key: str) -> Step:
    return _parse_step(_require(data, key, dict), key)


def _events(data: dict[str, Any], warmup: float) -> tuple[TimedStep, ...]:
    raw = data.get("events", [])
    if not isinstance(raw, list):
        raise ScenarioError("events must be a list")
    events = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ScenarioError(f"events[{i}] must be a mapping")
        after = _seconds(item, "after_seconds")
        if after > warmup:
            raise ScenarioError(f"events[{i}] happens after the warm-up ends")
        events.append(TimedStep(after, _parse_step(item, f"events[{i}]")))
    return tuple(sorted(events, key=lambda e: e.after_seconds))


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
    warmup = _seconds(data, "warmup_seconds")
    return Scenario(
        id=_require(data, "id", str),
        title=_require(data, "title", str),
        warmup_seconds=warmup,
        fault=_step(data, "fault"),
        duration_seconds=_seconds(data, "duration_seconds"),
        recovery=_step(data, "recovery"),
        cooldown_seconds=_seconds(data, "cooldown_seconds"),
        ground_truth=ground_truth,
        events=_events(data, warmup),
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
        command = ["docker", "compose", "-f", str(self.compose_file), *ACTIONS[step.action]]
        env = None
        if step.version:
            # docker-compose.yml passes APP_VERSION into the service's environment.
            env = {**os.environ, "APP_VERSION": step.version}
        subprocess.run([*command, step.service], check=True, env=env)


def _exit_on_sigterm(signum: int, frame: object) -> None:
    raise SystemExit(128 + signum)


def install_sigterm_handler() -> None:
    """Turn SIGTERM into SystemExit so `finally` blocks run and the fault gets undone.

    By default Python dies on SIGTERM without unwinding the stack, and SIGTERM is what
    `kill`, `docker stop`, systemd and closing a session send. SIGKILL cannot be caught;
    after one, `make lab-up` starts whatever the fault left stopped.
    """
    signal.signal(signal.SIGTERM, _exit_on_sigterm)


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
    stamp = events[0]["at"].replace(":", "").replace("-", "")[:13]
    elapsed = 0.0
    for event in scenario.events:
        sleep(event.after_seconds - elapsed)
        elapsed = event.after_seconds
        step = event.step
        if step.version:
            # Every run must ship a version that was never running before.
            step = replace(step, version=f"{step.version}+{stamp}")
        executor.run(step)
        mark(f"event:{step.describe()}")
    sleep(scenario.warmup_seconds - elapsed)

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
        "events_planned": [asdict(e) for e in scenario.events],
        "ground_truth": scenario.ground_truth,
        "events": events,
    }
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"run record written to {path}")
    return path


def describe_plan(scenario: Scenario) -> str:
    during = "".join(
        f"\n     at {e.after_seconds:g}s: {e.step.describe()}" for e in scenario.events
    )
    return "\n".join(
        [
            f"scenario: {scenario.id} - {scenario.title}",
            f"  1. warm up for {scenario.warmup_seconds:g}s{during}",
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
    install_sigterm_handler()
    run_scenario(scenario, ComposeExecutor())
    return 0


if __name__ == "__main__":
    sys.exit(main())
