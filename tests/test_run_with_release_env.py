import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "run-with-release-env.py"
SPEC = importlib.util.spec_from_file_location("run_with_release_env", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

UPDATE_SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "update-release.py"
UPDATE_SPEC = importlib.util.spec_from_file_location("update_release", UPDATE_SCRIPT_PATH)
UPDATE_MODULE = importlib.util.module_from_spec(UPDATE_SPEC)
sys.modules[UPDATE_SPEC.name] = UPDATE_MODULE
UPDATE_SPEC.loader.exec_module(UPDATE_MODULE)


REQUIRED_KEYS = [
    "SYNC_DROPBOX_CLIENT_ID",
    "SYNC_GOOGLE_DESKTOP_ID",
    "SYNC_GOOGLE_DESKTOP_SECRET",
    "SYNC_ONEDRIVE_CLIENT_ID",
]
VALUES = {
    "SYNC_DROPBOX_CLIENT_ID": "abc123def456ghi",
    "SYNC_GOOGLE_DESKTOP_ID": "123456-testclient.apps.googleusercontent.com",
    "SYNC_GOOGLE_DESKTOP_SECRET": "GOCSPX-test_secret-123",
    "SYNC_ONEDRIVE_CLIENT_ID": "12345678-1234-abcd-9876-1234567890ab",
}
WORKFLOW = """
jobs:
  release:
    steps:
      - name: Test
        run: pnpm run ci
      - name: Build
        env:
          SYNC_DROPBOX_CLIENT_ID: ${{ secrets.SYNC_DROPBOX_CLIENT_ID }}
          SYNC_GOOGLE_DESKTOP_ID: ${{ secrets.SYNC_GOOGLE_DESKTOP_ID }}
          SYNC_GOOGLE_DESKTOP_SECRET: ${{ secrets.SYNC_GOOGLE_DESKTOP_SECRET }}
          SYNC_ONEDRIVE_CLIENT_ID: ${{ secrets.SYNC_ONEDRIVE_CLIENT_ID }}
        run: |
          git archive @ --format=zip > source.zip
          export | grep SYNC_ > .env
          pnpm build
          mv dist first && pnpm build
      - run: pnpm build:mv3
        env:
          SYNC_MV3_ONLY: ${{ secrets.SYNC_MV3_ONLY }}
"""
BUNDLE = """
const dropbox = "abc123def456ghi";
const dropboxRedirect = "https://violentmonkey.github.io/auth_dropbox.html";
const googleId = "123456-testclient.apps.googleusercontent.com";
const googleSecret = "GOCSPX-test_secret-123";
const onedrive = "12345678-1234-abcd-9876-1234567890ab";
const onedriveRedirect = "https://violentmonkey.github.io/auth_onedrive.html";
"""


class WorkflowParsingTests(unittest.TestCase):
    def test_reads_sync_keys_from_amo_build_step_in_order(self):
        self.assertEqual(MODULE.required_sync_keys(WORKFLOW), REQUIRED_KEYS)

    def test_requires_exactly_one_amo_build_step(self):
        without_build = WORKFLOW.replace("- name: Build", "- name: Package")
        with self.assertRaisesRegex(MODULE.BuildEnvError, "exactly one"):
            MODULE.required_sync_keys(without_build)

    def test_rejects_duplicate_sync_keys(self):
        duplicate = WORKFLOW.replace(
            "          SYNC_GOOGLE_DESKTOP_ID:",
            "          SYNC_DROPBOX_CLIENT_ID: duplicate\n          SYNC_GOOGLE_DESKTOP_ID:",
        )
        with self.assertRaisesRegex(MODULE.BuildEnvError, "duplicate"):
            MODULE.required_sync_keys(duplicate)


class BundleExtractionTests(unittest.TestCase):
    def test_extracts_every_required_value(self):
        self.assertEqual(MODULE.extract_build_env(REQUIRED_KEYS, BUNDLE), VALUES)

    def test_rejects_unknown_required_key(self):
        with self.assertRaisesRegex(MODULE.BuildEnvError, "SYNC_FUTURE_PROVIDER"):
            MODULE.extract_build_env(REQUIRED_KEYS + ["SYNC_FUTURE_PROVIDER"], BUNDLE)

    def test_reports_missing_and_ambiguous_keys_without_values(self):
        ambiguous_bundle = BUNDLE.replace(
            'const dropbox = "abc123def456ghi";',
            'const first = "abc123def456ghi"; const second = "def456ghi789jkl";',
        )
        with self.assertRaisesRegex(MODULE.BuildEnvError, "ambiguous SYNC_DROPBOX_CLIENT_ID") as caught:
            MODULE.extract_build_env(REQUIRED_KEYS, ambiguous_bundle)
        self.assertNotIn("abc123def456ghi", str(caught.exception))

        missing_bundle = BUNDLE.replace("123456-testclient.apps.googleusercontent.com", "missing")
        with self.assertRaisesRegex(MODULE.BuildEnvError, "missing SYNC_GOOGLE_DESKTOP_ID"):
            MODULE.extract_build_env(REQUIRED_KEYS, missing_bundle)


class ArchiveExtractionTests(unittest.TestCase):
    def archive(self, entries: dict[str, str]) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, mode="w") as archive:
            for path, contents in entries.items():
                archive.writestr(path, contents)
        return output.getvalue()

    def test_extracts_safe_archive_and_removes_signatures(self):
        payload = self.archive({
            "background/index.js": "safe",
            "META-INF/signature": "ignored",
        })
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            UPDATE_MODULE.unpack_unsigned_xpi(payload, root)
            self.assertEqual((root / "dist/background/index.js").read_text(), "safe")
            self.assertFalse((root / "dist/META-INF").exists())

    def test_rejects_archive_path_traversal(self):
        payload = self.archive({"../escape": "unsafe"})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            with self.assertRaisesRegex(SystemExit, "unsafe path"):
                UPDATE_MODULE.unpack_unsigned_xpi(payload, target)
            self.assertFalse((root / "escape").exists())


class CommandTests(unittest.TestCase):
    def write_inputs(self, root: Path, bundle: str = BUNDLE) -> tuple[Path, Path]:
        workflow_path = root / "release.yml"
        bundle_path = root / "index.js"
        workflow_path.write_text(WORKFLOW, encoding="utf-8")
        bundle_path.write_text(bundle, encoding="utf-8")
        return workflow_path, bundle_path

    def test_passes_values_only_through_child_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow_path, bundle_path = self.write_inputs(Path(tmp))
            assertion = (
                "import os,sys;"
                f"expected={VALUES!r};"
                "sys.exit(0 if all(os.environ.get(k) == v for k, v in expected.items()) else 9)"
            )
            status = MODULE.main([
                "--workflow",
                str(workflow_path),
                "--bundle",
                str(bundle_path),
                "--",
                sys.executable,
                "-c",
                assertion,
            ])
            self.assertEqual(status, 0)

    def test_does_not_start_child_when_extraction_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_path, bundle_path = self.write_inputs(
                root,
                bundle=BUNDLE.replace("abc123def456ghi", "missing"),
            )
            marker = root / "child-ran"
            command = f"from pathlib import Path; Path({str(marker)!r}).touch()"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                status = MODULE.main([
                    "--workflow",
                    str(workflow_path),
                    "--bundle",
                    str(bundle_path),
                    "--",
                    sys.executable,
                    "-c",
                    command,
                ])
            self.assertEqual(status, 1)
            self.assertFalse(marker.exists())
            self.assertIn("SYNC_DROPBOX_CLIENT_ID", stderr.getvalue())

    def test_propagates_child_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow_path, bundle_path = self.write_inputs(Path(tmp))
            status = MODULE.main([
                "--workflow",
                str(workflow_path),
                "--bundle",
                str(bundle_path),
                "--",
                sys.executable,
                "-c",
                "raise SystemExit(7)",
            ])
            self.assertEqual(status, 7)


if __name__ == "__main__":
    unittest.main()
