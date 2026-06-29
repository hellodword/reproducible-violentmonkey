#!/usr/bin/env python3
import argparse
import hashlib
from pathlib import Path


def iter_files(root: Path):
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        yield path


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write a deterministic sha256 manifest for every file in a tree."
    )
    parser.add_argument("root", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")

    lines = []
    for path in iter_files(root):
        relpath = path.relative_to(root).as_posix()
        lines.append(f"{file_hash(path)}  {relpath}\n")

    if args.output:
        args.output.write_text("".join(lines), encoding="utf-8")
    else:
        print("".join(lines), end="")


if __name__ == "__main__":
    main()
