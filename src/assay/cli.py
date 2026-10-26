"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from assay import __version__
from assay.alerts import last_firing
from assay.doctor import run_checks
from assay.domain import Incident, Trigger
from assay.http import SourceError
from assay.investigate import investigate, whole_seconds
from assay.report import build_report, to_json, to_markdown
from assay.sources import Loki, Prometheus

EXIT_INVALID_REPORT = 2
EXIT_NO_INCIDENT = 3


def _doctor(args: argparse.Namespace) -> int:
    results = run_checks(args.prometheus, args.loki, args.job)
    for result in results:
        mark = "ok  " if result.ok else "FAIL"
        print(f"[{mark}] {result.name}: {result.detail}")
    return 0 if all(r.ok for r in results) else 1


def _duration(text: str) -> timedelta:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if len(text) < 2 or text[-1] not in units or not text[:-1].isdigit():
        raise argparse.ArgumentTypeError(f"expected a duration like 90s, 5m or 2h, got {text!r}")
    return timedelta(seconds=int(text[:-1]) * units[text[-1]])


def _moment(text: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an ISO 8601 time, got {text!r}") from exc
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _incident(args: argparse.Namespace, prometheus: Prometheus, now: datetime) -> Incident | None:
    if args.alert:
        firing = last_firing(prometheus, args.alert, now, args.lookback)
        if firing is None:
            return None
        start = whole_seconds(firing.symptoms_since - args.before)
        end = whole_seconds(min(now, firing.last_seen + args.after))
        detail = {
            "query": firing.query,
            "first_seen": firing.first_seen.isoformat(),
            "last_seen": firing.last_seen.isoformat(),
            "pending_since": firing.pending_since.isoformat() if firing.pending_since else None,
        }
        incident_id = f"inc-{firing.first_seen:%Y%m%dT%H%M%SZ}"
        return Incident(
            incident_id,
            Trigger.ALERT,
            start,
            end,
            firing.labels,
            detail,
            symptoms_start=whole_seconds(firing.symptoms_since),
            symptoms_end=whole_seconds(firing.last_seen),
        )
    start, end = whole_seconds(args.start), whole_seconds(args.end)
    return Incident(f"inc-{start:%Y%m%dT%H%M%SZ}", Trigger.MANUAL, start, end)


def _investigate(args: argparse.Namespace) -> int:
    prometheus, loki = Prometheus(args.prometheus), Loki(args.loki)
    now = min(args.until, datetime.now(tz=UTC)) if args.until else datetime.now(tz=UTC)
    try:
        incident = _incident(args, prometheus, now)
    except SourceError as exc:
        print(f"cannot read the alert history: {exc}", file=sys.stderr)
        return 1
    if incident is None:
        print(f"alert {args.alert} did not fire in the last {args.lookback}", file=sys.stderr)
        return EXIT_NO_INCIDENT

    report = build_report(investigate(incident, prometheus, loki, now=now))
    if not report.valid:
        # Never publish a report with an unbacked claim.
        print("report rejected by the evidence validator:", file=sys.stderr)
        for error in report.errors:
            print(f"  {error}", file=sys.stderr)
        return EXIT_INVALID_REPORT

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{incident.id}.json").write_text(to_json(report), encoding="utf-8")
    markdown = to_markdown(report)
    (out / f"{incident.id}.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"report written to {out / incident.id}.{{json,md}}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="assay")
    parser.add_argument("--version", action="version", version=f"assay {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check that telemetry sources are reachable")
    doctor.add_argument("--prometheus", default="http://localhost:9090")
    doctor.add_argument("--loki", default="http://localhost:3100")
    doctor.add_argument("--job", default="shop", help="Prometheus job that must be up")
    doctor.set_defaults(handler=_doctor)

    inv = sub.add_parser("investigate", help="investigate an incident and write a report")
    source = inv.add_mutually_exclusive_group(required=True)
    source.add_argument("--alert", help="alert name; uses its latest firing episode")
    source.add_argument("--from", dest="start", type=_moment, help="window start (ISO 8601)")
    inv.add_argument("--to", dest="end", type=_moment, help="window end, with --from")
    inv.add_argument("--lookback", type=_duration, default=timedelta(hours=6))
    inv.add_argument(
        "--until", type=_moment, help="with --alert: take the latest episode before this time"
    )
    inv.add_argument(
        "--before",
        type=_duration,
        default=timedelta(minutes=3),
        help="how far before the alert to look for the onset",
    )
    inv.add_argument("--after", type=_duration, default=timedelta(minutes=1))
    inv.add_argument("--prometheus", default="http://localhost:9090")
    inv.add_argument("--loki", default="http://localhost:3100")
    inv.add_argument("--out", default="reports", help="directory for the report files")
    inv.set_defaults(handler=_investigate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "investigate" and args.start and not args.end:
        parser.error("--from needs --to")
    handler = args.handler
    code: int = handler(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
