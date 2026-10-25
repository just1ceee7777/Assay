from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from scenario_runner import (
    SCENARIOS_DIR,
    ScenarioError,
    Step,
    install_sigterm_handler,
    load_scenario,
    main,
    parse_scenario,
    run_scenario,
)


def valid_data() -> dict[str, Any]:
    return {
        "id": "x",
        "title": "x",
        "warmup_seconds": 1,
        "fault": {"action": "stop_service", "service": "redis"},
        "duration_seconds": 2,
        "recovery": {"action": "start_service", "service": "redis"},
        "cooldown_seconds": 3,
        "ground_truth": {"root_cause_type": "dependency_unavailable", "component": "redis"},
    }


class RecordingExecutor:
    def __init__(self, fail_on: str | None = None) -> None:
        self.steps: list[Step] = []
        self.fail_on = fail_on

    def run(self, step: Step) -> None:
        self.steps.append(step)
        if step.action == self.fail_on:
            raise RuntimeError("boom")


@pytest.mark.parametrize("path", sorted(SCENARIOS_DIR.glob("*.yaml")), ids=lambda p: p.stem)
def test_every_shipped_scenario_is_valid(path: Path) -> None:
    load_scenario(str(path))


def test_unknown_action_rejected() -> None:
    data = valid_data()
    data["fault"]["action"] = "rm_rf"
    with pytest.raises(ScenarioError, match="not one of"):
        parse_scenario(data)


def test_missing_ground_truth_component_rejected() -> None:
    data = valid_data()
    del data["ground_truth"]["component"]
    with pytest.raises(ScenarioError, match="component"):
        parse_scenario(data)


def test_negative_duration_rejected() -> None:
    data = valid_data()
    data["duration_seconds"] = -1
    with pytest.raises(ScenarioError, match="negative"):
        parse_scenario(data)


def test_run_records_phases_in_order(tmp_path: Path) -> None:
    scenario = parse_scenario(valid_data())
    executor = RecordingExecutor()
    slept: list[float] = []
    ticks = iter(f"2026-10-25T00:00:0{i}+00:00" for i in range(10))

    path = run_scenario(
        scenario,
        executor,
        out_dir=tmp_path,
        sleep=slept.append,
        clock=lambda: next(ticks),
        log=lambda _: None,
    )

    assert [s.action for s in executor.steps] == ["stop_service", "start_service"]
    assert slept == [1, 2, 3]
    record = json.loads(path.read_text())
    assert [e["event"] for e in record["events"]] == [
        "warmup_started",
        "fault_injected",
        "fault_recovered",
        "cooldown_finished",
    ]
    assert record["ground_truth"]["component"] == "redis"


def test_recovery_runs_even_if_fault_phase_fails(tmp_path: Path) -> None:
    scenario = parse_scenario(valid_data())
    executor = RecordingExecutor(fail_on="stop_service")
    with pytest.raises(RuntimeError):
        run_scenario(scenario, executor, out_dir=tmp_path, sleep=lambda _: None, log=lambda _: None)
    assert [s.action for s in executor.steps] == ["stop_service", "start_service"]


def test_dry_run_does_not_execute(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["redis-down", "--dry-run"]) == 0
    assert "stop_service redis" in capsys.readouterr().out


def test_unknown_scenario_exit_code() -> None:
    assert main(["no-such-scenario", "--dry-run"]) == 2


def test_sigterm_during_fault_still_recovers(tmp_path: Path) -> None:
    scenario = parse_scenario(valid_data())
    executor = RecordingExecutor()

    def sleep(_: float) -> None:
        # Somebody runs `kill <pid>` while the fault is active.
        if executor.steps and executor.steps[-1].action == "stop_service":
            os.kill(os.getpid(), signal.SIGTERM)

    previous = signal.getsignal(signal.SIGTERM)
    install_sigterm_handler()
    try:
        with pytest.raises(SystemExit) as exit_info:
            run_scenario(scenario, executor, out_dir=tmp_path, sleep=sleep, log=lambda _: None)
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert exit_info.value.code == 128 + signal.SIGTERM
    assert [s.action for s in executor.steps] == ["stop_service", "start_service"]
    # An interrupted run is not a valid sample: it must not leave a run record.
    assert not list(tmp_path.iterdir())
