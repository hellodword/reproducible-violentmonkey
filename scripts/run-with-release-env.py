#!/usr/bin/env python3
import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


SYNC_KEY_RE = re.compile(r"SYNC_[A-Z0-9_]+")
BUILD_STEP_RE = re.compile(r"^(?P<indent> *)-\s+name:\s*['\"]?Build['\"]?\s*(?:#.*)?$")
LIST_ITEM_RE = re.compile(r"^(?P<indent> *)-\s+")
PNPM_BUILD_RE = re.compile(r"^\s*pnpm\s+build\s*(?:#.*)?$", re.MULTILINE)


class BuildEnvError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExtractionRule:
    pattern: re.Pattern[str]
    marker: str | None = None
    before: int = 500
    after: int = 100

    def candidates(self, bundle: str) -> set[str]:
        if self.marker is None:
            return set(self.pattern.findall(bundle))

        candidates = set()
        for marker in re.finditer(re.escape(self.marker), bundle):
            start = max(0, marker.start() - self.before)
            end = min(len(bundle), marker.end() + self.after)
            candidates.update(self.pattern.findall(bundle[start:end]))
        return candidates


EXTRACTION_RULES = {
    "SYNC_DROPBOX_CLIENT_ID": ExtractionRule(
        re.compile(r"\b[a-z0-9]{15}\b"),
        marker="auth_dropbox.html",
    ),
    "SYNC_GOOGLE_DESKTOP_ID": ExtractionRule(
        re.compile(r"\b\d+-[a-z0-9]+\.apps\.googleusercontent\.com\b"),
    ),
    "SYNC_GOOGLE_DESKTOP_SECRET": ExtractionRule(
        re.compile(r"\bGOCSPX-[A-Za-z0-9_-]+\b"),
    ),
    "SYNC_ONEDRIVE_CLIENT_ID": ExtractionRule(
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        marker="auth_onedrive.html",
    ),
}


def _block_end(lines: list[str], start: int, indent: int) -> int:
    for index in range(start + 1, len(lines)):
        match = LIST_ITEM_RE.match(lines[index])
        if match and len(match.group("indent")) == indent:
            return index
    return len(lines)


def _indented_mapping(lines: list[str], name: str) -> list[str] | None:
    mapping_re = re.compile(rf"^(?P<indent> *){re.escape(name)}:\s*(?:#.*)?$")
    for index, line in enumerate(lines):
        match = mapping_re.match(line)
        if not match:
            continue
        indent = len(match.group("indent"))
        end = len(lines)
        for candidate in range(index + 1, len(lines)):
            stripped = lines[candidate].strip()
            if not stripped:
                continue
            candidate_indent = len(lines[candidate]) - len(lines[candidate].lstrip(" "))
            if candidate_indent <= indent:
                end = candidate
                break
        return lines[index + 1:end]
    return None


def required_sync_keys(workflow: str) -> list[str]:
    lines = workflow.splitlines()
    build_steps = []
    for index, line in enumerate(lines):
        match = BUILD_STEP_RE.match(line)
        if not match:
            continue
        indent = len(match.group("indent"))
        block = lines[index:_block_end(lines, index, indent)]
        if PNPM_BUILD_RE.search("\n".join(block)):
            build_steps.append(block)

    if len(build_steps) != 1:
        raise BuildEnvError("release workflow must contain exactly one AMO Build step")

    env_lines = _indented_mapping(build_steps[0], "env")
    if env_lines is None:
        raise BuildEnvError("AMO Build step has no env mapping")

    keys = []
    for line in env_lines:
        match = re.match(r"^\s*(SYNC_[A-Z0-9_]+):", line)
        if match:
            keys.append(match.group(1))

    if not keys:
        raise BuildEnvError("AMO Build step declares no SYNC_ environment keys")
    if len(keys) != len(set(keys)):
        raise BuildEnvError("AMO Build step declares duplicate SYNC_ environment keys")
    if any(not SYNC_KEY_RE.fullmatch(key) for key in keys):
        raise BuildEnvError("AMO Build step contains an invalid SYNC_ environment key")
    return keys


def extract_build_env(required_keys: list[str], bundle: str) -> dict[str, str]:
    unsupported = [key for key in required_keys if key not in EXTRACTION_RULES]
    if unsupported:
        raise BuildEnvError("unsupported required build environment keys: " + ", ".join(unsupported))

    values = {}
    missing = []
    ambiguous = []
    for key in required_keys:
        candidates = EXTRACTION_RULES[key].candidates(bundle)
        if not candidates:
            missing.append(key)
        elif len(candidates) > 1:
            ambiguous.append(key)
        else:
            values[key] = next(iter(candidates))

    problems = []
    if missing:
        problems.append("missing " + ", ".join(missing))
    if ambiguous:
        problems.append("ambiguous " + ", ".join(ambiguous))
    if problems:
        raise BuildEnvError(
            "failed to recover required build environment keys: " + "; ".join(problems)
        )
    return values


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a command with Violentmonkey release build variables recovered from the AMO bundle."
    )
    parser.add_argument("--workflow", required=True, type=Path, help="Pinned upstream release workflow.")
    parser.add_argument("--bundle", required=True, type=Path, help="Published AMO background bundle.")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to run after --.")
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        workflow = args.workflow.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        print("error: failed to read the pinned release workflow", file=sys.stderr)
        return 1
    try:
        bundle = args.bundle.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        print("error: failed to read the published AMO background bundle", file=sys.stderr)
        return 1

    try:
        values = extract_build_env(required_sync_keys(workflow), bundle)
    except BuildEnvError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    env = os.environ.copy()
    env.update(values)
    try:
        return subprocess.run(args.command, env=env, check=False).returncode
    except OSError as error:
        print(f"error: failed to start build command: {error.strerror}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
