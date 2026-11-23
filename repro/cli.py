from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .verifier import VerificationError, fetch_latest_amo_version, verify_release

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASES_DIR = REPO_ROOT / "releases"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m repro",
        description="Verify Violentmonkey AMO releases against upstream source builds.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    subparsers.add_parser(
        "latest",
        help="Print the latest Violentmonkey version published on AMO.",
    )

    verify_parser = subparsers.add_parser(
        "verify",
        help="Reproduce a concrete Violentmonkey version and compare byte-for-byte with AMO.",
    )
    verify_parser.add_argument(
        "version",
        help="Concrete Violentmonkey version to verify (e.g. 2.50.0).",
    )
    verify_parser.add_argument(
        "--write",
        action="store_true",
        help="Write verified metadata to releases/<version>.json.",
    )
    verify_parser.add_argument(
        "--xpi",
        type=Path,
        help="Verify a local .xpi file instead of downloading from AMO (cannot be combined with --write).",
    )
    verify_parser.add_argument(
        "--cache-dir",
        type=Path,
        help="Optional directory to reuse downloaded toolchains and dependency stores across runs.",
    )
    return parser


def write_release_metadata(record: dict, releases_dir: Path = RELEASES_DIR) -> Path:
    releases_dir.mkdir(parents=True, exist_ok=True)
    version = record["version"]
    target = releases_dir / f"{version}.json"
    tmp = releases_dir / f".{version}.json.tmp"
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "latest":
        try:
            print(fetch_latest_amo_version())
            return 0
        except VerificationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.version == "latest":
        parser.error("verify requires an explicit version; run `python3 -m repro latest` first")
    if args.xpi is not None and args.write:
        parser.error("--write cannot be combined with --xpi because official AMO URL metadata is required")
    if args.xpi is not None and not args.xpi.is_file():
        parser.error(f"XPI file does not exist: {args.xpi}")

    try:
        record = verify_release(
            args.version,
            xpi_path=args.xpi,
            cache_dir=args.cache_dir,
        )
    except VerificationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.write:
        out_path = write_release_metadata(record)
        print(f"[repro] wrote {out_path}", file=sys.stderr)

    print(json.dumps(record, indent=2))
    return 0
