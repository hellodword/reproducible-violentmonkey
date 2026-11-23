from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from repro import ADDON_ID
from repro.archive import ArchiveError, compare_trees, extract_unsigned_xpi
from repro.cli import main as cli_main
from repro.env_solver import EnvSolveError, discover_sync_keys, make_sentinel_env, solve_sync_env

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_xpi(
    entries: dict[str, str | bytes],
    *,
    version: str = "2.50.0",
    addon_id: str = ADDON_ID,
    include_manifest: bool = True,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if include_manifest and "manifest.json" not in entries:
            manifest = {
                "manifest_version": 2,
                "name": "Violentmonkey",
                "version": version,
                "browser_specific_settings": {"gecko": {"id": addon_id}},
            }
            zf.writestr("manifest.json", json.dumps(manifest))
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


class ArchiveTests(unittest.TestCase):
    def test_extracts_unsigned_xpi_and_strips_allowed_signatures(self) -> None:
        xpi = _make_xpi(
            {
                "background/index.js": "console.log('ok');",
                "META-INF/mozilla.rsa": b"sig",
                "META-INF/manifest.mf": "manifest",
                "META-INF/cose.sig": b"cose",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out"
            extract_unsigned_xpi(xpi, dest, expected_version="2.50.0")
            self.assertEqual((dest / "background/index.js").read_text(), "console.log('ok');")
            self.assertFalse((dest / "META-INF").exists())

    def test_rejects_path_traversal_and_unexpected_meta_inf_files(self) -> None:
        traversal_xpi = _make_xpi({"../escape.js": "bad"})
        unexpected_meta = _make_xpi({"META-INF/injected.js": "bad"})
        wrong_id = _make_xpi({"index.js": "ok"}, addon_id="other@example.org")

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self.assertRaisesRegex(ArchiveError, "unsafe zip entry"):
                extract_unsigned_xpi(traversal_xpi, base / "a", expected_version="2.50.0")
            self.assertFalse((base / "escape.js").exists())

            with self.assertRaisesRegex(ArchiveError, "unexpected file inside META-INF"):
                extract_unsigned_xpi(unexpected_meta, base / "b", expected_version="2.50.0")

            with self.assertRaisesRegex(ArchiveError, "add-on ID mismatch"):
                extract_unsigned_xpi(wrong_id, base / "c", expected_version="2.50.0")

    def test_compare_trees_detects_missing_extra_modified_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            left = root / "left"
            right = root / "right"
            (left / "sub").mkdir(parents=True)
            (right / "sub").mkdir(parents=True)
            (left / "sub/app.js").write_text("const a = 1;", encoding="utf-8")
            (right / "sub/app.js").write_text("const a = 1;", encoding="utf-8")

            self.assertTrue(compare_trees(left, right).matches)

            (right / "sub/app.js").write_text("const a = 2;", encoding="utf-8")
            (right / "extra.txt").write_text("x", encoding="utf-8")
            (left / "only-left.txt").write_text("y", encoding="utf-8")
            diff = compare_trees(left, right)
            self.assertFalse(diff.matches)
            self.assertEqual(diff.missing, ("only-left.txt",))
            self.assertEqual(diff.extra, ("extra.txt",))
            self.assertEqual(diff.modified, ("sub/app.js",))

            (right / "link.js").symlink_to(right / "extra.txt")
            with self.assertRaisesRegex(ArchiveError, "symlink"):
                compare_trees(left, right)


class EnvSolverTests(unittest.TestCase):
    def test_recovers_sync_values_across_mangled_identifiers_and_adjacent_keys(self) -> None:
        keys = [
            "SYNC_DROPBOX_CLIENT_ID",
            "SYNC_GOOGLE_DESKTOP_ID",
            "SYNC_GOOGLE_DESKTOP_SECRET",
            "SYNC_ONEDRIVE_CLIENT_ID",
            "SYNC_UNUSED_MV3",
        ]
        sentinels = make_sentinel_env(keys)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            (src / ".github/workflows").mkdir(parents=True)
            (src / ".github/workflows/release.yml").write_text(
                "\n".join(f"  {k}: ${{{{ secrets.{k} }}}}" for k in keys),
                encoding="utf-8",
            )
            self.assertEqual(discover_sync_keys(src), sorted(keys))

            sentinel_dist = root / "sentinel"
            amo_dist = root / "amo"
            (sentinel_dist / "background").mkdir(parents=True)
            (amo_dist / "background").mkdir(parents=True)

            # Include preceding strings containing commas/equals and local identifier drift (e/t/r -> n/a/c)
            (sentinel_dist / "background/index.js").write_text(
                'console.info("[sync] auto-sync disabled, check=later");'
                f'const e={{client_id:"{sentinels["SYNC_DROPBOX_CLIENT_ID"]}",redirect_uri:t+"auth_dropbox.html"}};'
                f'const r={{client_id:"{sentinels["SYNC_GOOGLE_DESKTOP_ID"]}",'
                f'client_secret:"{sentinels["SYNC_GOOGLE_DESKTOP_SECRET"]}",'
                f'redirect_uri:"http://127.0.0.1:45678/"}};'
                f'const o={{client_id:"{sentinels["SYNC_ONEDRIVE_CLIENT_ID"]}",redirect_uri:t+"auth_onedrive.html"}};',
                encoding="utf-8",
            )
            (amo_dist / "background/index.js").write_text(
                'console.info("[sync] auto-sync disabled, check=later");'
                'const n={client_id:"dbx_real_id_12345",redirect_uri:a+"auth_dropbox.html"};'
                "const c={client_id:'123-google.apps.googleusercontent.com',"
                'client_secret:"GOCSPX-real_secret",'
                'redirect_uri:"http://127.0.0.1:45678/"};'
                'const s={client_id:"11111111-2222-3333-4444-555555555555",redirect_uri:a+"auth_onedrive.html"};',
                encoding="utf-8",
            )

            recovered = solve_sync_env(keys, sentinel_dist, amo_dist)
            self.assertEqual(
                recovered,
                {
                    "SYNC_DROPBOX_CLIENT_ID": "dbx_real_id_12345",
                    "SYNC_GOOGLE_DESKTOP_ID": "123-google.apps.googleusercontent.com",
                    "SYNC_GOOGLE_DESKTOP_SECRET": "GOCSPX-real_secret",
                    "SYNC_ONEDRIVE_CLIENT_ID": "11111111-2222-3333-4444-555555555555",
                },
            )

    def test_redacts_candidate_values_when_alignment_is_ambiguous(self) -> None:
        keys = ["SYNC_DROPBOX_CLIENT_ID"]
        sentinels = make_sentinel_env(keys)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sentinel_dist = root / "sentinel"
            amo_dist = root / "amo"
            sentinel_dist.mkdir()
            amo_dist.mkdir()

            (sentinel_dist / "index.js").write_text(
                f'const a={{id:"{sentinels["SYNC_DROPBOX_CLIENT_ID"]}"}};',
                encoding="utf-8",
            )
            (amo_dist / "index.js").write_text(
                'const x={id:"secret_alpha"};const y={id:"secret_beta"};',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(EnvSolveError, "ambiguous.*SYNC_DROPBOX_CLIENT_ID") as ctx:
                solve_sync_env(keys, sentinel_dist, amo_dist)
            self.assertNotIn("secret_alpha", str(ctx.exception))
            self.assertNotIn("secret_beta", str(ctx.exception))


class CliAndFlakeContractTests(unittest.TestCase):
    def test_cli_rejects_invalid_flag_combinations_with_usage_exit_code(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as latest_err:
                cli_main(["verify", "latest"])
            self.assertEqual(latest_err.exception.code, 2)

            with tempfile.NamedTemporaryFile(suffix=".xpi") as tmp_xpi:
                with self.assertRaises(SystemExit) as write_err:
                    cli_main(["verify", "2.50.0", "--xpi", tmp_xpi.name, "--write"])
                self.assertEqual(write_err.exception.code, 2)

    @unittest.skipUnless(shutil.which("nix"), "nix executable not available")
    def test_flake_lib_exports_latest_recorded_release(self) -> None:
        releases_dir = REPO_ROOT / "releases"
        json_files = sorted(releases_dir.glob("*.json"))
        if not json_files:
            self.skipTest("releases/*.json not populated yet")

        records = [json.loads(p.read_text(encoding="utf-8")) for p in json_files]
        highest = max(records, key=lambda r: [int(x) for x in r["version"].split(".")])

        out = subprocess.check_output(
            ["nix", "eval", "--no-write-lock-file", "--json", f"{REPO_ROOT}#lib"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        flake_lib = json.loads(out)
        self.assertEqual(flake_lib["latestVersion"], highest["version"])
        self.assertEqual(
            flake_lib["violentmonkeyXpi"],
            {
                "version": highest["version"],
                "url": highest["amo"]["url"],
                "sha256": highest["amo"]["sha256"],
            },
        )
        self.assertEqual(
            flake_lib["violentmonkeyBuildFirefoxXpiAddonArgs"]["addonId"],
            ADDON_ID,
        )


if __name__ == "__main__":
    unittest.main()
