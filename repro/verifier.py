from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import (
    ADDON_SLUG,
    RELEASE_WORKFLOW_PATH,
    UPSTREAM_OWNER,
    UPSTREAM_REPO,
    UPSTREAM_REPOSITORY,
)
from .archive import ArchiveError, extract_unsigned_xpi, compare_trees
from .env_solver import EnvSolveError, discover_sync_keys, make_sentinel_env, solve_sync_env

HTTP_TIMEOUT_SECONDS = 60
USER_AGENT = "reproducible-violentmonkey/2.0"

PASSTHROUGH_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "LD_LIBRARY_PATH",
    "NIX_LD",
    "NIX_LD_LIBRARY_PATH",
    "SYSTEMROOT",
)


class VerificationError(RuntimeError):
    """Raised when release retrieval, building, or byte-level verification fails."""


@dataclass(frozen=True)
class AmoArtifact:
    version: str
    url: str | None
    sha256_sri: str
    xpi_bytes: bytes


@dataclass(frozen=True)
class NodeRuntime:
    version: str
    major: int
    bin_dir: Path
    npm_exe: Path


def sri_sha256(data: bytes) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _log(message: str) -> None:
    print(f"[repro] {message}", file=sys.stderr, flush=True)


def http_get_bytes(url: str, *, accept: str = "*/*") -> bytes:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
    }
    if url.startswith("https://api.github.com/"):
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "application/vnd.github+json"

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise VerificationError(f"HTTP {exc.code} fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise VerificationError(f"network error fetching {url}: {exc.reason}") from exc


def http_get_json(url: str) -> dict:
    raw = http_get_bytes(url, accept="application/json")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"invalid JSON from {url}: {exc}") from exc


def fetch_latest_amo_version() -> str:
    url = f"https://addons.mozilla.org/api/v5/addons/addon/{ADDON_SLUG}/versions/?page=1&page_size=10&lang=en-US"
    data = http_get_json(url)
    results = data.get("results") or []
    if not results or not results[0].get("version"):
        raise VerificationError("AMO API returned no versions")
    return str(results[0]["version"])


def fetch_amo_artifact(version: str) -> AmoArtifact:
    next_url: str | None = (
        f"https://addons.mozilla.org/api/v5/addons/addon/{ADDON_SLUG}/versions/"
        "?page=1&page_size=50&lang=en-US"
    )
    matched: dict | None = None
    pages = 0

    while next_url and pages < 20:
        pages += 1
        data = http_get_json(next_url)
        for item in data.get("results") or []:
            if item.get("version") == version:
                matched = item
                break
        if matched is not None:
            break
        next_url = data.get("next")

    if matched is None:
        raise VerificationError(f"version {version} not found on AMO")

    file_info = matched.get("file") or {}
    file_url = file_info.get("url")
    if not file_url:
        raise VerificationError(f"AMO entry for {version} has no download URL")

    xpi_bytes = http_get_bytes(file_url)
    declared_hash = str(file_info.get("hash") or "")
    if declared_hash.startswith("sha256:"):
        expected_hex = declared_hash.split(":", 1)[1].lower()
        actual_hex = hashlib.sha256(xpi_bytes).hexdigest()
        if actual_hex != expected_hex:
            raise VerificationError(
                f"downloaded AMO file SHA-256 mismatch: expected {expected_hex}, got {actual_hex}"
            )

    return AmoArtifact(
        version=version,
        url=file_url,
        sha256_sri=sri_sha256(xpi_bytes),
        xpi_bytes=xpi_bytes,
    )


def _parse_iso_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _resolve_tag_commit(tag: str) -> str:
    ref = http_get_json(
        f"https://api.github.com/repos/{UPSTREAM_REPOSITORY}/git/ref/tags/{tag}"
    )
    obj = ref["object"]
    while obj.get("type") == "tag":
        tag_data = http_get_json(obj["url"])
        obj = tag_data["object"]
    return str(obj["sha"])


def discover_commit_candidates(version: str) -> list[str]:
    tag = f"v{version}"
    tag_commit = _resolve_tag_commit(tag)
    workflow_commits: list[str] = []

    try:
        release = http_get_json(
            f"https://api.github.com/repos/{UPSTREAM_REPOSITORY}/releases/tags/{tag}"
        )
    except VerificationError:
        release = None

    if release is not None:
        created_at = _parse_iso_utc(release["created_at"])
        anchor_str = release.get("published_at") or release["created_at"]
        anchor = _parse_iso_utc(anchor_str)
        start_day = (created_at - timedelta(days=1)).strftime("%Y-%m-%d")
        end_day = (anchor + timedelta(days=1)).strftime("%Y-%m-%d")

        runs_url = (
            f"https://api.github.com/repos/{UPSTREAM_REPOSITORY}/actions/workflows/release.yml/runs"
            f"?per_page=100&created={start_day}..{end_day}"
        )
        try:
            runs_data = http_get_json(runs_url)
        except VerificationError:
            runs_data = {}

        matched_runs = []
        window_start = created_at - timedelta(hours=12)
        window_end = anchor + timedelta(hours=2)
        for run in runs_data.get("workflow_runs") or []:
            if run.get("conclusion") != "success":
                continue
            if run.get("path") and run.get("path") != RELEASE_WORKFLOW_PATH:
                continue
            run_created = _parse_iso_utc(run["created_at"])
            if window_start <= run_created <= window_end:
                matched_runs.append(run)

        matched_runs.sort(
            key=lambda r: abs((_parse_iso_utc(r["updated_at"]) - anchor).total_seconds())
        )
        for run in matched_runs:
            sha = str(run.get("head_sha") or "")
            if sha and sha not in workflow_commits:
                workflow_commits.append(sha)

    candidates = [*workflow_commits]
    if tag_commit not in candidates:
        candidates.append(tag_commit)
    return candidates


def download_upstream_source(commit: str, cache_dir: Path) -> Path:
    src_cache = cache_dir / "sources" / commit
    if src_cache.is_dir():
        return src_cache

    url = f"https://github.com/{UPSTREAM_REPOSITORY}/archive/{commit}.tar.gz"
    raw = http_get_bytes(url)
    staging = cache_dir / "sources" / f".tmp-{commit}-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        archive.extractall(staging, filter="data")

    entries = [p for p in staging.iterdir() if p.is_dir()]
    if len(entries) != 1:
        shutil.rmtree(staging, ignore_errors=True)
        raise VerificationError(f"unexpected archive layout for commit {commit}")

    src_cache.parent.mkdir(parents=True, exist_ok=True)
    entries[0].rename(src_cache)
    shutil.rmtree(staging, ignore_errors=True)
    return src_cache


def parse_min_node_major(engines_node: str) -> int:
    matches = [int(m) for m in re.findall(r"(\d+)", engines_node)]
    return min(matches) if matches else 20


def _detect_system_node() -> NodeRuntime | None:
    node_path = shutil.which("node")
    npm_path = shutil.which("npm")
    if not node_path or not npm_path:
        return None
    try:
        out = subprocess.check_output([node_path, "--version"], text=True, timeout=10).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    ver = out.lstrip("v")
    try:
        major = int(ver.split(".", 1)[0])
    except ValueError:
        return None
    return NodeRuntime(
        version=ver,
        major=major,
        bin_dir=Path(node_path).parent,
        npm_exe=Path(npm_path),
    )


def _node_platform_tag() -> str:
    sys_name = platform.system().lower()
    machine = platform.machine().lower()
    os_part = {"linux": "linux", "darwin": "darwin"}.get(sys_name)
    arch_part = {
        "x86_64": "x64",
        "amd64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine)
    if not os_part or not arch_part:
        raise VerificationError(f"unsupported platform for portable Node download: {sys_name}/{machine}")
    return f"{os_part}-{arch_part}"


def _provision_official_node(major: int, cache_dir: Path) -> NodeRuntime | None:
    plat = _node_platform_tag()
    shasums_url = f"https://nodejs.org/dist/latest-v{major}.x/SHASUMS256.txt"
    try:
        shasums_text = http_get_bytes(shasums_url, accept="text/plain").decode("utf-8")
    except VerificationError:
        return None

    pattern = re.compile(
        rf"^(?P<sha>[0-9a-f]{{64}})\s+(?P<filename>node-v(?P<ver>{major}\.\d+\.\d+)-{re.escape(plat)}\.tar\.(?:xz|gz))$",
        re.MULTILINE,
    )
    match = pattern.search(shasums_text)
    if match is None:
        return None

    expected_sha = match.group("sha")
    filename = match.group("filename")
    version = match.group("ver")

    install_dir = cache_dir / "node" / f"v{version}-{plat}"
    bin_dir = install_dir / "bin"
    if (bin_dir / "node").is_file() and (bin_dir / "npm").is_file():
        return NodeRuntime(version=version, major=major, bin_dir=bin_dir, npm_exe=bin_dir / "npm")

    _log(f"downloading official Node.js v{version} ({plat})")
    archive_bytes = http_get_bytes(f"https://nodejs.org/dist/latest-v{major}.x/{filename}")
    actual_sha = hashlib.sha256(archive_bytes).hexdigest()
    if actual_sha != expected_sha:
        raise VerificationError(
            f"Node.js tarball SHA-256 mismatch for {filename}: expected {expected_sha}, got {actual_sha}"
        )

    staging = cache_dir / "node" / f".tmp-v{version}-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    mode = "r:xz" if filename.endswith(".xz") else "r:gz"
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode=mode) as archive:
        archive.extractall(staging, filter="data")

    extracted = [p for p in staging.iterdir() if p.is_dir()]
    if len(extracted) != 1:
        shutil.rmtree(staging, ignore_errors=True)
        raise VerificationError(f"unexpected Node.js archive layout for {filename}")

    install_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(install_dir, ignore_errors=True)
    extracted[0].rename(install_dir)
    shutil.rmtree(staging, ignore_errors=True)
    return NodeRuntime(version=version, major=major, bin_dir=bin_dir, npm_exe=bin_dir / "npm")


def iter_node_candidates(min_major: int, cache_dir: Path):
    tried_majors: set[int] = set()
    sys_node = _detect_system_node()
    if sys_node is not None and sys_node.major >= min_major:
        tried_majors.add(sys_node.major)
        yield sys_node

    for candidate_major in (min_major, min_major + 2, min_major + 4):
        if candidate_major in tried_majors:
            continue
        tried_majors.add(candidate_major)
        runtime = _provision_official_node(candidate_major, cache_dir)
        if runtime is not None:
            yield runtime


def parse_package_manager(package_json: dict, source_dir: Path) -> tuple[str, str | None, str]:
    declared = str(package_json.get("packageManager") or "").strip()
    if declared:
        spec = declared.split("+", 1)[0]
        if "@" not in spec:
            raise VerificationError(f"invalid packageManager field: {declared!r}")
        name, version = spec.rsplit("@", 1)
        return name, version, f"{name}@{version}"

    if (source_dir / "pnpm-lock.yaml").is_file():
        return "pnpm", None, "pnpm"
    if (source_dir / "yarn.lock").is_file():
        return "yarn", None, "yarn"
    return "npm", None, "npm"


def _clean_base_env(extra_bin_dirs: list[Path], home_dir: Path) -> dict[str, str]:
    base_path = os.environ.get("PATH", "/usr/bin:/bin")
    joined_path = os.pathsep.join([*(str(p) for p in extra_bin_dirs), base_path])
    env: dict[str, str] = {
        "PATH": joined_path,
        "HOME": str(home_dir),
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    for key in PASSTHROUGH_ENV_KEYS:
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env


def ensure_package_manager(
    pm_name: str,
    pm_version: str | None,
    node: NodeRuntime,
    cache_dir: Path,
) -> Path:
    if pm_name == "npm" and pm_version is None:
        return node.bin_dir

    spec = f"{pm_name}@{pm_version}" if pm_version else pm_name
    slug = f"{pm_name}-{pm_version or 'latest'}"
    pm_dir = cache_dir / "pm" / slug
    pm_bin_dir = pm_dir / "node_modules" / ".bin"
    if (pm_bin_dir / pm_name).exists():
        return pm_bin_dir

    _log(f"installing {spec}")
    staging = cache_dir / "pm" / f".tmp-{slug}-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    npm_home = cache_dir / "npm-home"
    npm_home.mkdir(parents=True, exist_ok=True)

    env = _clean_base_env([node.bin_dir, node.npm_exe.parent], npm_home)
    cmd = [
        str(node.npm_exe),
        "install",
        "--prefix",
        str(staging),
        "--no-save",
        "--no-audit",
        "--no-fund",
        "--loglevel=error",
        spec,
    ]
    proc = subprocess.run(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not (staging / "node_modules" / ".bin" / pm_name).exists():
        shutil.rmtree(staging, ignore_errors=True)
        raise VerificationError(f"failed to install {spec}:\n{proc.stdout}")

    pm_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(pm_dir, ignore_errors=True)
    staging.rename(pm_dir)
    return pm_bin_dir


def _run_checked(cmd: list[str], *, cwd: Path, env: dict[str, str], step: str) -> None:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        tail = "\n".join(proc.stdout.splitlines()[-40:])
        raise VerificationError(f"{step} failed (exit {proc.returncode}):\n{tail}")


def _install_command(pm_name: str, pm_exe: Path, store_dir: Path) -> list[str]:
    if pm_name == "pnpm":
        return [
            str(pm_exe),
            "install",
            "--frozen-lockfile",
            f"--store-dir={store_dir}",
            "--package-import-method=copy",
        ]
    if pm_name == "yarn":
        return [str(pm_exe), "install", "--frozen-lockfile"]
    return [str(pm_exe), "ci"]


def _verify_single_candidate(
    *,
    source_template: Path,
    amo_dist: Path,
    node: NodeRuntime,
    cache_dir: Path,
    work_dir: Path,
) -> tuple[str, dict[str, str]]:
    package_json = json.loads((source_template / "package.json").read_text(encoding="utf-8"))
    pm_name, pm_version, pm_label = parse_package_manager(package_json, source_template)
    pm_bin_dir = ensure_package_manager(pm_name, pm_version, node, cache_dir)
    pm_exe = pm_bin_dir / pm_name

    build_src = work_dir / "src"
    shutil.copytree(source_template, build_src)
    home_dir = work_dir / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    store_dir = cache_dir / "pnpm-store"
    store_dir.mkdir(parents=True, exist_ok=True)

    base_env = _clean_base_env([pm_bin_dir, node.bin_dir], home_dir)
    sync_keys = discover_sync_keys(build_src)

    _run_checked(
        _install_command(pm_name, pm_exe, store_dir),
        cwd=build_src,
        env=base_env,
        step=f"{pm_label} install",
    )

    # Pass 1: Sentinel build & structural alignment
    sentinel_env = {**base_env, **make_sentinel_env(sync_keys)}
    _run_checked(
        [str(pm_exe), "build"],
        cwd=build_src,
        env=sentinel_env,
        step="sentinel build",
    )
    sentinel_dist = build_src / "dist"
    if not sentinel_dist.is_dir():
        raise VerificationError("sentinel build did not produce a dist/ directory")

    recovered_sync = solve_sync_env(sync_keys, sentinel_dist, amo_dist)
    shutil.rmtree(sentinel_dist)

    # Pass 2: Pristine build with only recovered SYNC_* variables in memory
    clean_env = {**base_env, **recovered_sync}
    _run_checked(
        [str(pm_exe), "build"],
        cwd=build_src,
        env=clean_env,
        step="reproduction build",
    )
    built_dist = build_src / "dist"
    if not built_dist.is_dir():
        raise VerificationError("reproduction build did not produce a dist/ directory")

    diff = compare_trees(amo_dist, built_dist)
    if not diff.matches:
        raise VerificationError(f"artifact mismatch ({diff.summary()})")

    return pm_label, recovered_sync


def verify_release(
    version: str,
    *,
    xpi_path: Path | None = None,
    amo_url_override: str | None = None,
    cache_dir: Path | None = None,
) -> dict:
    if xpi_path is not None:
        xpi_bytes = xpi_path.resolve().read_bytes()
        artifact = AmoArtifact(
            version=version,
            url=amo_url_override,
            sha256_sri=sri_sha256(xpi_bytes),
            xpi_bytes=xpi_bytes,
        )
    else:
        _log(f"fetching AMO metadata and XPI for {version}")
        artifact = fetch_amo_artifact(version)

    owns_cache = cache_dir is None
    active_cache = (
        Path(tempfile.mkdtemp(prefix="repro-vm-cache-"))
        if owns_cache
        else cache_dir.resolve()
    )
    assert active_cache is not None
    active_cache.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix=f"repro-vm-{version}-") as tmp:
            tmp_root = Path(tmp)
            amo_dist = tmp_root / "amo-dist"
            try:
                extract_unsigned_xpi(
                    artifact.xpi_bytes,
                    amo_dist,
                    expected_version=version,
                )
            except ArchiveError as exc:
                raise VerificationError(str(exc)) from exc

            commits = discover_commit_candidates(version)
            failures: list[str] = []

            for commit in commits:
                source_dir = download_upstream_source(commit, active_cache)
                pkg_json = json.loads((source_dir / "package.json").read_text(encoding="utf-8"))
                min_node_major = parse_min_node_major(
                    str(pkg_json.get("engines", {}).get("node") or "")
                )

                for attempt_idx, node in enumerate(
                    iter_node_candidates(min_node_major, active_cache)
                ):
                    label = f"commit={commit[:12]} node=v{node.version}"
                    _log(f"verifying {version} ({label})")
                    attempt_dir = tmp_root / f"attempt-{commit[:8]}-{attempt_idx}"
                    attempt_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        pm_label, _ = _verify_single_candidate(
                            source_template=source_dir,
                            amo_dist=amo_dist,
                            node=node,
                            cache_dir=active_cache,
                            work_dir=attempt_dir,
                        )
                    except (VerificationError, EnvSolveError, ArchiveError) as exc:
                        failures.append(f"[{label}] {exc}")
                        continue
                    finally:
                        shutil.rmtree(attempt_dir, ignore_errors=True)

                    _log(f"reproduced {version} byte-for-byte ({label}, {pm_label})")
                    amo_record: dict[str, str] = {"sha256": artifact.sha256_sri}
                    if artifact.url:
                        amo_record = {"url": artifact.url, "sha256": artifact.sha256_sri}
                    return {
                        "version": version,
                        "amo": amo_record,
                        "source": {
                            "repository": UPSTREAM_REPOSITORY,
                            "tag": f"v{version}",
                            "commit": commit,
                        },
                        "toolchain": {
                            "node": node.version,
                            "packageManager": pm_label,
                        },
                    }

            joined = "\n".join(failures) if failures else "no viable commit/toolchain candidates"
            raise VerificationError(f"failed to reproduce Violentmonkey {version}:\n{joined}")
    finally:
        if owns_cache:
            shutil.rmtree(active_cache, ignore_errors=True)
