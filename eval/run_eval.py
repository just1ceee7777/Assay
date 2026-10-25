"""Run a lab scenario N times, investigate each run and score Assay against the ground truth.

Usage:
  python eval/run_eval.py redis-down --runs 10

Evaluation is kept apart from the product: it never imports `assay`. It runs the CLI the
way a user would and reads its JSON report. The ground truth comes from the scenario run
record in lab/runs/, which Assay itself never reads.

Results are appended to eval/results/<scenario>.jsonl, one line per run, and the report
of every run is kept next to them.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "eval" / "results"
RECORD_LINE = re.compile(r"run record written to (\S+)")


def score_run(record: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Compare one report with the ground truth of the run it investigated."""
    truth = record["ground_truth"]
    expected = (truth["root_cause_type"], truth["component"])
    ranked = [(h["cause_type"], h["component"]) for h in report["hypotheses"]]
    events = {e["event"]: datetime.fromisoformat(e["at"]) for e in record["events"]}
    first_seen = report["incident"].get("trigger_detail", {}).get("first_seen")
    # The alert must belong to this run, not to an earlier one.
    alert_in_run = bool(
        first_seen
        and events["fault_injected"]
        <= datetime.fromisoformat(first_seen)
        <= events["cooldown_finished"]
    )
    rank = ranked.index(expected) + 1 if expected in ranked else None
    top = report["hypotheses"][0] if report["hypotheses"] else {}
    # Control scenarios name wrong causes the data hints at; they must not win.
    distractors = []
    for d in truth.get("distractors", []):
        key = (d["root_cause_type"], d["component"])
        hyp = next(
            (h for h in report["hypotheses"] if (h["cause_type"], h["component"]) == key), None
        )
        distractors.append(
            {
                "cause": list(key),
                "rank": ranked.index(key) + 1 if key in ranked else None,
                "status": hyp["status"] if hyp else None,
                "score": hyp.get("score") if hyp else None,
            }
        )
    return {
        "expected": list(expected),
        "top1": list(ranked[0]) if ranked else None,
        "top1_status": top.get("status"),
        "rank": rank,
        "correct_top1": rank == 1,
        "in_top3": rank is not None and rank <= 3,
        "conclusion": report["conclusion"],
        "report_valid": report["valid"],
        "claims": len(report["claims"]),
        "claims_with_evidence": sum(1 for c in report["claims"] if c["evidence_ids"]),
        "alert_in_run": alert_in_run,
        "distractors": distractors,
        "distractor_won": any(d["rank"] == 1 for d in distractors),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    claims = sum(r["claims"] for r in rows)
    return {
        "runs": n,
        "top1": sum(r["correct_top1"] for r in rows) / n if n else 0.0,
        "top3": sum(r["in_top3"] for r in rows) / n if n else 0.0,
        "claims_with_evidence": (sum(r["claims_with_evidence"] for r in rows) / claims)
        if claims
        else 1.0,
        "valid_reports": sum(r["report_valid"] for r in rows),
        "alert_in_run": sum(r["alert_in_run"] for r in rows),
        "distractor_won": sum(r.get("distractor_won", False) for r in rows),
        "distractor_supported": sum(
            any(d["status"] == "supported" for d in r.get("distractors", [])) for r in rows
        ),
        "mean_investigation_seconds": round(sum(r["investigation_seconds"] for r in rows) / n, 2)
        if n
        else 0.0,
    }


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)


def run_once(scenario: str, alert: str, out_dir: Path) -> dict[str, Any]:
    started = datetime.now(tz=UTC)
    lab = _run(["uv", "run", "python", "lab/scenario_runner.py", scenario])
    match = RECORD_LINE.search(lab.stdout)
    if lab.returncode != 0 or not match:
        raise RuntimeError(f"scenario failed:\n{lab.stdout}\n{lab.stderr}")
    record_path = Path(match.group(1))
    record = json.loads(record_path.read_text(encoding="utf-8"))

    report_dir = out_dir / record_path.stem
    t0 = time.monotonic()
    inv = _run(
        [
            "uv",
            "run",
            "assay",
            "investigate",
            "--alert",
            alert,
            "--lookback",
            "15m",
            "--out",
            str(report_dir),
        ]
    )
    elapsed = time.monotonic() - t0
    row: dict[str, Any] = {
        "run": record_path.stem,
        "started": started.isoformat(),
        "investigate_exit": inv.returncode,
        "investigation_seconds": round(elapsed, 2),
        "llm": "off",
        "llm_cost_usd": 0.0,
    }
    reports = sorted(report_dir.glob("*.json")) if report_dir.exists() else []
    if inv.returncode != 0 or not reports:
        row.update(
            error=inv.stderr.strip()[-500:],
            correct_top1=False,
            in_top3=False,
            report_valid=False,
            claims=0,
            claims_with_evidence=0,
            alert_in_run=False,
        )
        return row
    row.update(score_run(record, json.loads(reports[-1].read_text(encoding="utf-8"))))
    return row


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--alert", default="ShopHighErrorRate")
    parser.add_argument("--pause", type=float, default=60.0, help="seconds between runs")
    args = parser.parse_args(argv)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results = RESULTS_DIR / f"{args.scenario}.jsonl"
    out_dir = RESULTS_DIR / "reports"
    rows: list[dict[str, Any]] = []
    for i in range(args.runs):
        if i:
            time.sleep(args.pause)
        row = run_once(args.scenario, args.alert, out_dir)
        rows.append(row)
        with results.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        print(
            f"[{i + 1}/{args.runs}] {row['run']}: top1={row.get('top1')} "
            f"rank={row.get('rank')} valid={row['report_valid']}",
            flush=True,
        )

    summary = summarize(rows)
    print(json.dumps(summary, indent=2))
    (RESULTS_DIR / f"{args.scenario}-summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
