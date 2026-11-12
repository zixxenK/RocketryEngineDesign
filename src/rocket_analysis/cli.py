"""Command line entry points for the RP-1/LOX study workflow."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from rocket_analysis.service import (
    StudyService,
    abandon_adaptive_trials,
    create_initial_study,
    mark_candidate_stale,
    run_doctor,
)
from rocket_analysis.study import load_study_spec


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def _emit(value: Any) -> None:
    print(json.dumps(_jsonable(value), indent=2, default=str))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rocket-study", description="RP-1/LOX study workflow")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create a one-point study from an RPA4 CFG")
    init.add_argument("base_cfg", type=Path)
    init.add_argument("output", type=Path)

    doctor = commands.add_parser("doctor", help="inspect optional tools and local paths")
    doctor.add_argument("--shared-dir", type=Path)
    doctor.add_argument("--wsl-distro", default="Ubuntu-24.04")
    doctor.add_argument("--cantera-mechanism")

    for name in ("preview", "generate", "screen", "pack", "unpack", "import", "rank", "abandon", "status", "mark-stale"):
        command = commands.add_parser(name)
        command.add_argument("study", type=Path)
        if name in {"preview", "generate"}:
            command.add_argument("--count", type=int)
        elif name == "pack":
            command.add_argument("output", type=Path)
            command.add_argument("--candidate", action="append", dest="candidate_ids")
        elif name == "unpack":
            command.add_argument("bundle", type=Path)
            command.add_argument("--inbox", type=Path)
        elif name == "import":
            command.add_argument("--source", type=Path)
        elif name == "rank":
            command.add_argument("--spec", type=Path, help="JSON ranking specification")
            command.add_argument("--screened", action="store_true", help="rank local screening results")
        elif name == "abandon":
            command.add_argument("--trial", type=int, action="append", dest="trials")
        elif name == "mark-stale":
            command.add_argument("candidate_id")
            command.add_argument("--clear", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            _emit({"study": create_initial_study(args.base_cfg, args.output)})
            return 0
        if args.command == "doctor":
            _emit(run_doctor(shared_dir=args.shared_dir, wsl_distro=args.wsl_distro, cantera_mechanism=args.cantera_mechanism))
            return 0
        spec = load_study_spec(args.study)
        service = StudyService(args.study)
        try:
            if args.command == "preview":
                _emit(service.preview(args.count))
            elif args.command == "generate":
                _emit(service.generate(args.count))
            elif args.command == "screen":
                _emit(service.screen())
            elif args.command == "pack":
                _emit({"bundle": service.pack(args.output, set(args.candidate_ids) if args.candidate_ids else None)})
            elif args.command == "unpack":
                _emit({"inbox": service.unpack(args.bundle, args.inbox)})
            elif args.command == "import":
                _emit(service.import_results(args.source))
            elif args.command == "rank":
                ranking_spec = json.loads(args.spec.read_text(encoding="utf-8")) if args.spec else None
                _emit(service.rank(ranking_spec, screened=args.screened))
            elif args.command == "abandon":
                _emit({"abandoned_trials": service.abandon(args.trials)})
            elif args.command == "status":
                _emit(service.candidate_statuses())
            elif args.command == "mark-stale":
                mark_candidate_stale(spec, args.candidate_id, stale=not args.clear)
                _emit({"candidate_id": args.candidate_id, "stale": not args.clear})
            return 0
        finally:
            service.close()
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

