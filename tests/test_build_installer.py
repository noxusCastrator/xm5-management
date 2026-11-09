"""Exact installer inventories and rejected inputs; fixtures never execute."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_installer as builder


class InstallerBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xm5-installer-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.payload = self.root / "portable 输入"
        self.payload.mkdir()
        for name in builder.REQUIRED | {"distribution.json", "xm5lib/example.py"}:
            path = self.payload / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic payload, never executed\n")
        self.manifest()

    def manifest(self):
        self.rows = {p.relative_to(self.payload).as_posix(): {"size": p.stat().st_size, "sha256": builder.sha(p)}
                     for p in self.payload.rglob("*") if p.is_file() and p.name != "BUILD-MANIFEST.json"}
        self.write_manifest()

    def write_manifest(self, status="approved"):
        (self.payload / "BUILD-MANIFEST.json").write_text(json.dumps({
            "schema": "xm5.portable-build.v2", "version": "0.2.2", "public_runtime_source_review": status,
            "files": self.rows}) + "\n", encoding="utf-8")

    def test_converts_only_new_staging_and_owns_exact_files(self):
        before = {p.relative_to(self.payload).as_posix(): p.read_bytes() for p in self.payload.rglob("*") if p.is_file()}
        app, include, manifest = builder.prepare(self.payload, self.root / "stage")
        self.assertEqual(before, {p.relative_to(self.payload).as_posix(): p.read_bytes() for p in self.payload.rglob("*") if p.is_file()})
        self.assertEqual(json.loads((app / "distribution.json").read_text())["mode"], "installed")
        self.assertEqual((app / ".xm5-studio-installation").read_text().strip(), builder.IDENTITY)
        rows = dict(line.split("\t", 1)[::-1] for line in (app / "OWNED-FILES.sha256").read_text().splitlines())
        actual = {p.relative_to(app).as_posix() for p in app.rglob("*") if p.is_file()}
        self.assertEqual(set(rows) | {"OWNED-FILES.sha256"}, actual)
        for name, digest in rows.items():
            self.assertEqual(builder.sha(app / name), digest)
        self.assertEqual(len(include.read_text().splitlines()), len(actual))
        self.assertNotIn("*", include.read_text())
        self.assertEqual(manifest["portable_manifest_sha256"], hashlib.sha256(before["BUILD-MANIFEST.json"]).hexdigest())

    def test_changed_file_blocks_before_staging(self):
        (self.payload / "xm5lib/example.py").write_bytes(b"unexpected edit")
        with self.assertRaisesRegex(ValueError, "Payload mismatch"):
            builder.prepare(self.payload, self.root / "stage")
        self.assertFalse((self.root / "stage").exists())

    def test_extra_files_are_not_silently_included(self):
        (self.payload / "personal.txt").write_text("fixture", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside its reviewed manifest"):
            builder.verified_payload(self.payload)

    def test_review_is_required_unless_explicit_local_fixture(self):
        self.write_manifest("pending")
        with self.assertRaisesRegex(ValueError, "approved"):
            builder.verified_payload(self.payload)
        builder.verified_payload(self.payload, allow_unreviewed=True)

    def test_manifest_cannot_include_profiles(self):
        path = self.payload / "data/private.sqlite3"
        path.parent.mkdir()
        path.write_bytes(b"synthetic database")
        self.manifest()
        with self.assertRaisesRegex(ValueError, "Private or generated"):
            builder.verified_payload(self.payload)

    def test_manifest_paths_are_windows_safe(self):
        for name in ("../escape", "C:/escape", "/absolute", "vendor//file", "file.", "NUL.txt", "one\\two", "two:stream"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                builder.relative(name)

    def test_case_collisions_refuse(self):
        self.rows["XM5LIB/example.py"] = self.rows["xm5lib/example.py"]
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "Case-colliding"):
            builder.verified_payload(self.payload)

    def test_input_and_stage_must_be_separate_and_new(self):
        for destination in (self.payload, self.payload / "stage", self.root):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                builder.prepare(self.payload, destination)


if __name__ == "__main__":
    unittest.main()
