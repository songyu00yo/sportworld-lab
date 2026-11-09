"""Offline checks for local setup, verified patching, and archive extraction."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import bootstrap, patch_gnss, setup_android


class BootstrapTests(unittest.TestCase):
    def test_initialization_preserves_existing_user_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            examples = root / "examples"
            examples.mkdir()
            for filename in bootstrap.SEEDS:
                (examples / filename).write_text('{"example": true}')
            lab = bootstrap.initialize(root)
            route = lab / "routes/neusoft-track-route.json"
            route.write_text('{"user": true}')
            bootstrap.initialize(root)
            self.assertEqual(route.read_text(), '{"user": true}')
            self.assertEqual((lab / "reference/user-elevation-profile.json").read_text(), '{"example": true}')
            self.assertEqual((lab / "private").stat().st_mode & 0o777, 0o700)
            self.assertTrue((root / "private/runs").is_dir())
            self.assertEqual((root / "private/runs").stat().st_mode & 0o777, 0o700)
            self.assertIn(str(lab / "android/avd"), (lab / "android-env.sh").read_text())
            self.assertIn("scripts/start_android_tablet.sh", (lab / "start_android_tablet.sh").read_text())

    def test_missing_seed_does_not_leave_empty_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(FileNotFoundError):
                bootstrap.initialize(root)
            self.assertFalse((root / ".lab/routes/neusoft-track-route.json").exists())


class GnssPatchTests(unittest.TestCase):
    def fixture(self, root):
        data = bytearray(patch_gnss.SIZE)
        data[patch_gnss.OFFSET] = 0x0C
        original = root / "original"
        original.write_bytes(data)
        modified = bytearray(data)
        modified[patch_gnss.OFFSET] = 0x8C
        return original, bytes(data), bytes(modified)

    def test_known_patch_is_repeatable_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, original, modified = self.fixture(root)
            source.chmod(0o444)
            destination = root / "result"
            with patch.object(patch_gnss, "ORIGINAL_SHA256", hashlib.sha256(original).hexdigest()), \
                    patch.object(patch_gnss, "PATCHED_SHA256", hashlib.sha256(modified).hexdigest()):
                patch_gnss.patch(source, destination)
                timestamp = destination.stat().st_mtime_ns
                patch_gnss.patch(source, destination)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(destination.read_bytes(), modified)
            self.assertEqual(destination.stat().st_mtime_ns, timestamp)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o755)

    def test_unknown_original_rejected_without_touching_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "unknown"
            source.write_bytes(b"unknown")
            destination = root / "result"
            destination.write_bytes(b"keep")
            with self.assertRaises(ValueError):
                patch_gnss.patch(source, destination)
            self.assertEqual(destination.read_bytes(), b"keep")

    def test_output_cannot_be_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _, _ = self.fixture(root)
            with self.assertRaises(ValueError):
                patch_gnss.patch(source, source)

    def test_failed_replace_leaves_existing_output_and_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, original, modified = self.fixture(root)
            destination = root / "result"
            destination.write_bytes(b"keep")
            with patch.object(patch_gnss, "ORIGINAL_SHA256", hashlib.sha256(original).hexdigest()), \
                    patch.object(patch_gnss, "PATCHED_SHA256", hashlib.sha256(modified).hexdigest()), \
                    patch.object(patch_gnss.os, "replace", side_effect=OSError("simulated failure")):
                with self.assertRaises(OSError):
                    patch_gnss.patch(source, destination)
            self.assertEqual(destination.read_bytes(), b"keep")
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["original", "result"])

    def test_wrong_result_checksum_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, original, _ = self.fixture(root)
            with patch.object(patch_gnss, "ORIGINAL_SHA256", hashlib.sha256(original).hexdigest()):
                with self.assertRaises(ValueError):
                    patch_gnss.patch(source, root / "result")
            self.assertFalse((root / "result").exists())


class AndroidArchiveTests(unittest.TestCase):
    def test_extract_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "sdk.zip"
            with zipfile.ZipFile(archive, "w") as stream:
                stream.writestr("sdk/bin/tool", b"content")
            target = root / "installed"
            setup_android.extract(archive, target, "sdk", "fixture-checksum")
            setup_android.extract(archive, target, "sdk", "fixture-checksum")
            self.assertEqual((target / "bin/tool").read_bytes(), b"content")

    def test_path_traversal_rejected_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "sdk.zip"
            with zipfile.ZipFile(archive, "w") as stream:
                stream.writestr("sdk/bin/tool", b"content")
                stream.writestr("sdk/../../outside", b"escape")
            target = root / "installed"
            with self.assertRaises(ValueError):
                setup_android.extract(archive, target, "sdk", "fixture-checksum")
            self.assertFalse(target.exists())
            self.assertEqual(list(root.iterdir()), [archive])


if __name__ == "__main__":
    unittest.main()
