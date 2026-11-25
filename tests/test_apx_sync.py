import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import apx_sync as apx  # noqa: E402


def entry(path: str, size: int = 10, sha: str = "a" * 64) -> dict:
    return {
        "relative_path": path,
        "target_type": "locallow",
        "target_path": path,
        "sha256": sha,
        "size": size,
        "description": "",
    }


def manifest_payload(**overrides) -> bytes:
    payload = {
        "format_version": "2.0",
        "platform": "CN_STEAM",
        "version": "3.3.0",
        "game_version": "3.3.0",
        "generated_at": "2026-11-18T16:40:20+08:00",
        "total_patch_files": 2,
        "total_patch_size": 20,
        "total_restore_files": 1,
        "total_restore_size": 10,
        "files": [entry("dependence/a.bundle"), entry("runtime_dependencies.json")],
        "restore_files": [entry("restore/com.unity.addressables/x/__data")],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class SafePathTests(unittest.TestCase):
    def test_accepts_nested_relative_path(self):
        self.assertEqual(
            apx.safe_relative_path("dependence\\apx_modern_assist_ui.bundle"),
            "dependence/apx_modern_assist_ui.bundle",
        )

    def test_strips_dot_segments(self):
        self.assertEqual(apx.safe_relative_path("./a/./b"), "a/b")

    def test_rejects_traversal(self):
        with self.assertRaises(apx.SyncError):
            apx.safe_relative_path("../evil")

    def test_rejects_absolute_and_drive(self):
        for raw in ("/etc/passwd", "C:\\Windows\\system32", "d:/x"):
            with self.subTest(raw=raw), self.assertRaises(apx.SyncError):
                apx.safe_relative_path(raw)

    def test_rejects_empty_and_control(self):
        for raw in ("", "   ", "a/\x00b"):
            with self.subTest(raw=raw), self.assertRaises(apx.SyncError):
                apx.safe_relative_path(raw)


class ManifestTests(unittest.TestCase):
    def test_parses_valid_manifest(self):
        manifest = apx.parse_manifest(manifest_payload(), "CN_STEAM")
        self.assertEqual(manifest.version, "3.3.0")
        self.assertEqual(len(manifest.files), 2)
        self.assertEqual(manifest.patch_bytes, 20)

    def test_rejects_platform_mismatch(self):
        with self.assertRaises(apx.SyncError):
            apx.parse_manifest(manifest_payload(platform="CN_TAPTAP"), "CN_STEAM")

    def test_rejects_bad_sha(self):
        payload = manifest_payload(files=[entry("a.bin", sha="xyz")])
        with self.assertRaises(apx.SyncError):
            apx.parse_manifest(payload, "CN_STEAM")

    def test_rejects_count_mismatch(self):
        with self.assertRaises(apx.SyncError):
            apx.parse_manifest(manifest_payload(total_patch_files=9), "CN_STEAM")

    def test_rejects_duplicate_paths(self):
        payload = manifest_payload(
            files=[entry("a.bin"), entry("a.bin")],
            total_patch_files=2,
            total_patch_size=20,
        )
        with self.assertRaises(apx.SyncError):
            apx.parse_manifest(payload, "CN_STEAM")

    def test_rejects_missing_files(self):
        with self.assertRaises(apx.SyncError):
            apx.parse_manifest(manifest_payload(files=[]), "CN_STEAM")


class SnapshotTests(unittest.TestCase):
    def test_snapshot_is_order_independent(self):
        a = apx.parse_manifest(manifest_payload(), "CN_STEAM")
        b = apx.parse_manifest(manifest_payload(platform="CN_TAPTAP"), "CN_TAPTAP")
        self.assertEqual(
            apx.snapshot_of({"CN_STEAM": a, "CN_TAPTAP": b}),
            apx.snapshot_of({"CN_TAPTAP": b, "CN_STEAM": a}),
        )

    def test_snapshot_changes_with_content(self):
        a = apx.parse_manifest(manifest_payload(), "CN_STEAM")
        b = apx.parse_manifest(manifest_payload(version="3.3.1"), "CN_STEAM")
        self.assertNotEqual(apx.snapshot_of({"CN_STEAM": a}), apx.snapshot_of({"CN_STEAM": b}))


class OptionalMetadataTests(unittest.TestCase):
    def test_detects_runtime_dependencies(self):
        parsed = apx.parse_manifest(manifest_payload(), "CN_STEAM")
        flags = {item.relative_path: apx.is_optional_metadata(item) for item in parsed.files}
        self.assertTrue(flags["runtime_dependencies.json"])
        self.assertFalse(flags["dependence/a.bundle"])

    def test_semantic_validation(self):
        apx.validate_runtime_dependencies(b'{"platform":"CN_STEAM"}', "CN_STEAM")
        with self.assertRaises(apx.SyncError):
            apx.validate_runtime_dependencies(b'{"platform":"CN_TAPTAP"}', "CN_STEAM")
        with self.assertRaises(apx.SyncError):
            apx.validate_runtime_dependencies(b"not json", "CN_STEAM")


class PackagingTests(unittest.TestCase):
    def test_zip_layout(self):
        manifest = apx.parse_manifest(manifest_payload(), "CN_STEAM")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mirror"
            (root / "CN_STEAM").mkdir(parents=True, exist_ok=True)
            (root / "CN_STEAM" / "manifest.json").write_bytes(manifest.raw)
            for item in (*manifest.files, *manifest.restore_files):
                path = root / "CN_STEAM" / item.relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x" * item.size)
            destination = Path(tmp) / "out.zip"
            apx.zip_platform(root, manifest, destination)

            with zipfile.ZipFile(destination) as archive:
                names = set(archive.namelist())
            self.assertIn("CN_STEAM/manifest.json", names)
            self.assertIn("CN_STEAM/dependence/a.bundle", names)
            self.assertIn("CN_STEAM/restore/com.unity.addressables/x/__data", names)
            self.assertTrue(all(not name.startswith("/") for name in names))

    def test_sanitize_name(self):
        self.assertEqual(apx.sanitize_name("3.3.0"), "3.3.0")
        self.assertEqual(apx.sanitize_name("3.3.0 beta/1"), "3.3.0-beta-1")
        self.assertEqual(apx.sanitize_name("///"), "unknown")


if __name__ == "__main__":
    unittest.main()
