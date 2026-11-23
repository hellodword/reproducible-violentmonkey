from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import ADDON_ID

ALLOWED_META_INF_FILES = frozenset(
    {
        "manifest.mf",
        "mozilla.rsa",
        "mozilla.sf",
        "cose.manifest",
        "cose.sig",
    }
)


class ArchiveError(RuntimeError):
    """Raised when an archive or artifact tree violates safety or identity rules."""


@dataclass(frozen=True)
class TreeDiff:
    missing: tuple[str, ...]
    extra: tuple[str, ...]
    modified: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not (self.missing or self.extra or self.modified)

    def summary(self, limit: int = 10) -> str:
        if self.matches:
            return "trees match byte-for-byte"
        parts: list[str] = []
        for label, items in (
            ("missing", self.missing),
            ("extra", self.extra),
            ("modified", self.modified),
        ):
            if not items:
                continue
            shown = ", ".join(items[:limit])
            if len(items) > limit:
                shown += f", ... (+{len(items) - limit} more)"
            parts.append(f"{label} ({len(items)}): {shown}")
        return "; ".join(parts)


def _validate_zip_entry(info: zipfile.ZipInfo) -> tuple[str, ...]:
    raw = info.filename
    if not raw or "\x00" in raw or "\\" in raw or raw.startswith("/"):
        raise ArchiveError(f"unsafe zip entry path: {raw!r}")

    parts = PurePosixPath(raw.rstrip("/")).parts
    if not parts or any(part in ("", ".", "..") or ":" in part for part in parts):
        raise ArchiveError(f"unsafe zip entry path: {raw!r}")

    unix_filetype = (info.external_attr >> 16) & 0o170000
    if unix_filetype == stat.S_IFLNK:
        raise ArchiveError(f"symlink entry not allowed in archive: {raw}")
    if unix_filetype not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise ArchiveError(f"unsupported file mode in archive: {raw}")

    return parts


def extract_unsigned_xpi(
    xpi_bytes: bytes,
    dest_dir: Path,
    *,
    expected_version: str,
    expected_addon_id: str = ADDON_ID,
) -> None:
    """Safely unpack an XPI archive, strip allowed Mozilla signature files, and verify identity."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    seen_files: set[str] = set()
    seen_folded: set[str] = set()

    try:
        archive = zipfile.ZipFile(io.BytesIO(xpi_bytes))
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"invalid zip archive: {exc}") from exc

    with archive:
        for info in archive.infolist():
            parts = _validate_zip_entry(info)
            if info.is_dir():
                continue

            rel = "/".join(parts)
            folded = rel.casefold()
            if rel in seen_files or folded in seen_folded:
                raise ArchiveError(f"duplicate or case-colliding archive path: {rel}")
            seen_files.add(rel)
            seen_folded.add(folded)

            if parts[0] == "META-INF":
                if len(parts) != 2 or parts[1] not in ALLOWED_META_INF_FILES:
                    raise ArchiveError(f"unexpected file inside META-INF: {rel}")
                continue

            target = dest_dir.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))

    manifest_path = dest_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveError("archive does not contain manifest.json")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveError(f"failed to parse manifest.json: {exc}") from exc

    manifest_version = manifest.get("version")
    if manifest_version != expected_version:
        raise ArchiveError(
            f"manifest version mismatch: expected {expected_version!r}, found {manifest_version!r}"
        )

    addon_id = (
        manifest.get("browser_specific_settings", {}).get("gecko", {}).get("id")
        or manifest.get("applications", {}).get("gecko", {}).get("id")
    )
    if addon_id != expected_addon_id:
        raise ArchiveError(
            f"manifest add-on ID mismatch: expected {expected_addon_id!r}, found {addon_id!r}"
        )


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _scan_tree(root: Path) -> dict[str, tuple[int, str]]:
    if not root.is_dir() or root.is_symlink():
        raise ArchiveError(f"artifact root is not a regular directory: {root}")

    records: dict[str, tuple[int, str]] = {}
    for current_root, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current_root)
        dirnames.sort()
        filenames.sort()

        for dirname in dirnames:
            dir_path = current_path / dirname
            st = dir_path.lstat()
            if stat.S_ISLNK(st.st_mode):
                rel = dir_path.relative_to(root).as_posix()
                raise ArchiveError(f"symlink directory not allowed in artifact tree: {rel}")

        for filename in filenames:
            file_path = current_path / filename
            rel = file_path.relative_to(root).as_posix()
            st = file_path.lstat()
            if stat.S_ISLNK(st.st_mode):
                raise ArchiveError(f"symlink file not allowed in artifact tree: {rel}")
            if not stat.S_ISREG(st.st_mode):
                raise ArchiveError(f"non-regular file not allowed in artifact tree: {rel}")
            records[rel] = _file_digest(file_path)

    return records


def compare_trees(expected_dir: Path, actual_dir: Path) -> TreeDiff:
    """Strictly compare every file path, byte length, and SHA-256 across two trees."""
    expected = _scan_tree(expected_dir)
    actual = _scan_tree(actual_dir)

    missing = tuple(sorted(expected.keys() - actual.keys()))
    extra = tuple(sorted(actual.keys() - expected.keys()))
    modified = tuple(
        sorted(
            path
            for path in (expected.keys() & actual.keys())
            if expected[path] != actual[path]
        )
    )
    return TreeDiff(missing=missing, extra=extra, modified=modified)
