"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from assay import __version__
from assay.doctor import run_checks


def _doctor(args: argparse.Namespace) -> int:
    results = run_checks(args.prometheus, args.loki, args.job)
    for result in results:
        mark = "ok  " if result.ok else "FAIL"
        print(f"[{mark}] {result.name}: {result.detail}")
    return 0 if all(r.ok for r in results) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="assay")
    parser.add_argument("--version", action="version", version=f"assay {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check that telemetry sources are reachable")
    doctor.add_argument("--prometheus", default="http://localhost:9090")
    doctor.add_argument("--loki", default="http://localhost:3100")
    doctor.add_argument("--job", default="shop", help="Prometheus job that must be up")
    doctor.set_defaults(handler=_doctor)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = args.handler
    code: int = handler(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
