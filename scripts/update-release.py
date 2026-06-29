#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path


PROJECT = "violentmonkey"
OWNER = "violentmonkey"
REPO = "violentmonkey"
ADDON = "violentmonkey"
RELEASE_WORKFLOW = ".github/workflows/release.yml"
BUILD_ENV_KEYS = [
    "SYNC_DROPBOX_CLIENT_ID",
    "SYNC_GOOGLE_DESKTOP_ID",
    "SYNC_GOOGLE_DESKTOP_SECRET",
    "SYNC_ONEDRIVE_CLIENT_ID",
    "SYNC_ONEDRIVE_CLIENT_SECRET",
]

ROOT = Path(os.environ.get("REPRODUCIBLE_VIOLENTMONKEY_ROOT", Path.cwd())).resolve()
RELEASES_ROOT = ROOT / "releases" / PROJECT
NIXPKGS_PATH = os.environ.get("REPRODUCIBLE_VIOLENTMONKEY_NIXPKGS")
SYSTEM = os.environ.get("REPRODUCIBLE_VIOLENTMONKEY_SYSTEM", "x86_64-linux")


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "reproducible-violentmonkey-updater"})
    with urllib.request.urlopen(request) as response:
        return response.read()


def fetch_json(url: str) -> dict:
    return json.loads(fetch_bytes(url).decode("utf-8"))


def sri_from_bytes(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")


def sri_from_hex(value: str) -> str:
    return "sha256-" + base64.b64encode(bytes.fromhex(value)).decode("ascii")


def run_json(args: list[str]) -> dict:
    output = subprocess.check_output(args, cwd=ROOT, text=True)
    return json.loads(output)


def run_text(args: list[str]) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def read_tracked_text(path: Path) -> str | None:
    try:
        relpath = path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return None
    proc = subprocess.run(
        ["git", "show", f"HEAD:{relpath}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def prefetch_hash(url: str, *, unpack: bool = False) -> str:
    args = ["nix", "store", "prefetch-file", "--json"]
    if unpack:
        args.append("--unpack")
    args.append(url)
    return run_json(args)["hash"]


def hash_path(path: Path) -> str:
    return run_text(["nix", "hash", "path", "--sri", str(path)])


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def find_amo_version(version: str) -> dict:
    data = fetch_json(f"https://addons.mozilla.org/api/v5/addons/addon/{ADDON}/versions/?page=1&lang=en-US")
    if version == "latest":
        return data["results"][0]
    for item in data["results"]:
        if item["version"] == version:
            return item
    raise SystemExit(f"AMO version not found on first page: {version}")


def resolve_tag_sha(tag: str) -> str:
    ref = fetch_json(f"https://api.github.com/repos/{OWNER}/{REPO}/git/ref/tags/{tag}")
    obj = ref["object"]
    if obj["type"] == "tag":
        tag_obj = fetch_json(obj["url"])
        return tag_obj["object"]["sha"]
    return obj["sha"]


def find_release_asset(release: dict, version: str) -> dict | None:
    expected = f"Violentmonkey-webext-v{version}.zip"
    for asset in release.get("assets", []):
        if asset.get("name") == expected:
            return asset
    return None


def find_release_run(release: dict, asset: dict | None) -> dict | None:
    cutoff = parse_time((asset or release).get("updated_at") or release["published_at"]) + timedelta(minutes=10)
    start = parse_time(release["created_at"]) - timedelta(hours=2)
    created = release["created_at"][:10]
    url = (
        f"https://api.github.com/repos/{OWNER}/{REPO}/actions/workflows/release.yml/runs"
        f"?per_page=100&created={created}"
    )
    runs = fetch_json(url).get("workflow_runs", [])
    candidates = []
    for run in runs:
        if run.get("conclusion") != "success":
            continue
        if run.get("path") != RELEASE_WORKFLOW:
            continue
        updated = parse_time(run["updated_at"])
        created_at = parse_time(run["created_at"])
        if start <= created_at <= cutoff and updated <= cutoff:
            candidates.append(run)
    if not candidates:
        return None
    return max(candidates, key=lambda run: parse_time(run["updated_at"]))


def package_json_for_rev(rev: str) -> dict:
    url = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{rev}/package.json"
    return json.loads(fetch_bytes(url).decode("utf-8"))


def pnpm_info(package_manager: str) -> tuple[str, str]:
    match = re.match(r"pnpm@([^+]+)", package_manager)
    if not match:
        raise SystemExit(f"unsupported packageManager: {package_manager}")
    version = match.group(1)
    meta = fetch_json(f"https://registry.npmjs.org/pnpm/{version}")
    tarball = meta["dist"]["tarball"]
    return version, prefetch_hash(tarball)


def prefetch_pnpm_deps(
    *,
    fetcher_version: int,
    pnpm_hash: str,
    pnpm_version: str,
    rev: str,
    source_hash: str,
    version: str,
) -> str:
    args = [
        "nix",
        "build",
        "--no-link",
        "--file",
        str(ROOT / "nix" / "prefetch-pnpm-deps.nix"),
        "--argstr",
        "pnpmSha256",
        pnpm_hash,
        "--argstr",
        "pnpmVersion",
        pnpm_version,
        "--argstr",
        "rev",
        rev,
        "--argstr",
        "sourceSha256",
        source_hash,
        "--argstr",
        "system",
        SYSTEM,
        "--argstr",
        "version",
        version,
        "--arg",
        "fetcherVersion",
        str(fetcher_version),
    ]
    if NIXPKGS_PATH:
        args.extend(["--arg", "nixpkgsPath", NIXPKGS_PATH])

    proc = subprocess.run(
        args,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    output = proc.stdout
    hashes = re.findall(r"got:\s+(sha256-[A-Za-z0-9+/=]+)", output)
    if hashes:
        return hashes[-1]
    if proc.returncode == 0:
        raise SystemExit("failed to prefetch pnpm deps hash: build unexpectedly succeeded with an empty hash")
    raise SystemExit("failed to prefetch pnpm deps hash:\n" + output)


def parse_env(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        text = read_tracked_text(path)
    if text is None:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def write_env(path: Path, values: dict[str, str]) -> None:
    lines = [f'{key}="{values.get(key, "")}"\n' for key in BUILD_ENV_KEYS]
    path.write_text("".join(lines), encoding="utf-8")


def update_env_from_bundle(values: dict[str, str], background_js: str) -> dict[str, str]:
    updated = dict(values)
    patterns = {
        "SYNC_GOOGLE_DESKTOP_ID": r"\b\d+-[a-z0-9]+\.apps\.googleusercontent\.com\b",
        "SYNC_GOOGLE_DESKTOP_SECRET": r"\bGOCSPX-[A-Za-z0-9_-]+\b",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, background_js)
        if match:
            updated[key] = match.group(0)

    auth_idx = background_js.find("auth_onedrive.html")
    if auth_idx >= 0:
        window = background_js[max(0, auth_idx - 500):auth_idx + 100]
        match = re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", window)
        if match:
            updated["SYNC_ONEDRIVE_CLIENT_ID"] = match.group(0)

    return updated


def unpack_unsigned_xpi(xpi_data: bytes, target: Path) -> str:
    xpi_path = target / "addon.xpi"
    xpi_path.write_bytes(xpi_data)
    dist = target / "dist"
    with zipfile.ZipFile(xpi_path) as archive:
        archive.extractall(dist)
    shutil.rmtree(dist / "META-INF", ignore_errors=True)
    return (dist / "background" / "index.js").read_text(encoding="utf-8")


def load_release(version: str) -> dict:
    path = RELEASES_ROOT / version / "release.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    tracked = read_tracked_text(path)
    if tracked is not None:
        return json.loads(tracked)
    return {
        "project": PROJECT,
        "version": version,
        "amo": {"strip": ["META-INF"]},
        "upstream": {},
        "toolchain": {
            "node": {},
            "packageManager": {},
            "nixpkgs": {},
        },
        "dependencies": {
            "lockFile": "pnpm-lock.yaml",
            "pnpmDepsSha256": None,
            "fetcherVersion": 4,
            "sharpPolicy": "use-upstream-prebuilt-img-packages",
        },
        "buildEnv": {
            "file": "build.env",
            "provenance": "build-env.provenance.json",
            "requiredKeys": BUILD_ENV_KEYS,
        },
        "comparison": {
            "mode": "recursive-file-byte-comparison",
            "root": "dist",
            "ignore": ["META-INF"],
            "manifestAlgorithm": "sha256 over sorted relative file paths",
        },
    }


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Update recorded Violentmonkey release metadata.")
    parser.add_argument("version", nargs="?", help="Concrete AMO version to update.")
    parser.add_argument(
        "--print-latest-version",
        action="store_true",
        help="Print the latest AMO version and exit without writing metadata.",
    )
    args = parser.parse_args()

    if args.print_latest_version:
        print(find_amo_version("latest")["version"])
        return
    if not args.version:
        parser.error("version is required")
    if args.version == "latest":
        parser.error("update requires an explicit version; use `nix run .#latest` to inspect the latest version")

    amo_version = find_amo_version(args.version)
    version = amo_version["version"]
    tag = f"v{version}"
    release_dir = RELEASES_ROOT / version
    release_dir.mkdir(parents=True, exist_ok=True)

    file_url = amo_version["file"]["url"]
    xpi_data = fetch_bytes(file_url)
    fetchzip_url = f"{file_url}#violentmonkey-{version}.xpi.zip"

    github_release = fetch_json(f"https://api.github.com/repos/{OWNER}/{REPO}/releases/tags/{tag}")
    release_asset = find_release_asset(github_release, version)
    release_run = find_release_run(github_release, release_asset)
    tag_rev = resolve_tag_sha(tag)
    rev = release_run["head_sha"] if release_run else tag_rev
    source_hash = prefetch_hash(f"https://github.com/{OWNER}/{REPO}/archive/{rev}.tar.gz", unpack=True)

    package_json = package_json_for_rev(rev)
    declared_package_manager = package_json.get("packageManager")
    if not declared_package_manager:
        raise SystemExit(
            f"unsupported pre-pnpm release {version}: package.json has no packageManager; "
            "only pnpm releases are supported"
        )
    pnpm_version, pnpm_hash = pnpm_info(declared_package_manager)

    with tempfile.TemporaryDirectory(prefix="violentmonkey-update-") as tmp:
        tmp_path = Path(tmp)
        background_js = unpack_unsigned_xpi(xpi_data, tmp_path)
        unsigned_tree_hash = hash_path(tmp_path / "dist")

    env_path = release_dir / "build.env"
    env_values = update_env_from_bundle(parse_env(env_path), background_js)
    write_env(env_path, env_values)

    provenance = release_dir / "build-env.provenance.json"
    if not provenance.exists():
        tracked_provenance = read_tracked_text(provenance)
        if tracked_provenance is not None:
            provenance.write_text(tracked_provenance, encoding="utf-8")
        else:
            write_json(provenance, {
                "source": "published AMO and GitHub release bundle",
                "sourceFormat": "published background/index.js literals",
                "reason": "Violentmonkey release workflow injects SYNC_* values into the build environment, and writes them to .env only for the release source zip.",
                "publicness": "These values are treated as public release inputs because they are recoverable from the published extension.",
                "requiredKeys": BUILD_ENV_KEYS,
            })

    data = load_release(version)
    data["project"] = PROJECT
    data["version"] = version
    data["amo"].update({
        "addon": ADDON,
        "fileUrl": file_url,
        "xpiSha256": sri_from_bytes(xpi_data),
        "fetchzipSha256": prefetch_hash(fetchzip_url, unpack=True),
        "unsignedTreeSha256": unsigned_tree_hash,
        "strip": ["META-INF"],
    })
    upstream = data["upstream"]
    upstream.update({
        "owner": OWNER,
        "repo": REPO,
        "tag": tag,
        "tagRev": tag_rev,
        "rev": rev,
        "sourceSha256": source_hash,
        "releaseWorkflow": RELEASE_WORKFLOW,
    })
    if release_run:
        workflow_run = {
            "id": release_run["id"],
            "event": release_run["event"],
            "headBranch": release_run["head_branch"],
            "headSha": release_run["head_sha"],
            "createdAt": release_run["created_at"],
            "completedAt": release_run["updated_at"],
        }
        if release_asset and release_asset.get("digest", "").startswith("sha256:"):
            workflow_run["releaseAssetSha256"] = sri_from_hex(release_asset["digest"].split(":", 1)[1])
        upstream["releaseWorkflowRun"] = workflow_run

    node = data["toolchain"]["node"]
    node["declared"] = package_json.get("engines", {}).get("node", "")
    node.setdefault("resolvedVersion", "24.16.0")
    node.setdefault("resolutionSource", "nixpkgs nodejs_24")

    package_manager = data["toolchain"]["packageManager"]
    package_manager.update({
        "name": "pnpm",
        "declared": declared_package_manager,
        "nixPackage": "pnpm_10 override",
        "nixVersion": pnpm_version,
        "tarballSha256": pnpm_hash,
    })

    dependencies = data["dependencies"]
    dependencies.setdefault("lockFile", "pnpm-lock.yaml")
    dependencies.setdefault("fetcherVersion", 4)
    dependencies.setdefault("sharpPolicy", "use-upstream-prebuilt-img-packages")
    if not dependencies.get("pnpmDepsSha256"):
        dependencies["pnpmDepsSha256"] = prefetch_pnpm_deps(
            fetcher_version=dependencies["fetcherVersion"],
            pnpm_hash=pnpm_hash,
            pnpm_version=pnpm_version,
            rev=rev,
            source_hash=source_hash,
            version=version,
        )

    data["buildEnv"] = {
        "file": "build.env",
        "provenance": "build-env.provenance.json",
        "requiredKeys": BUILD_ENV_KEYS,
    }

    write_json(release_dir / "release.json", data)
    print(f"updated {PROJECT} {version}")


if __name__ == "__main__":
    main()
