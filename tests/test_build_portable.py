"""Portable assembly regressions using tiny local fixtures, never downloads."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


class PortableBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xm5-portable-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.script = self.repo / "tools/build_portable.py"
        self.script.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "tools/build_portable.py", self.script)
        (self.repo / "release.json").write_text('{"version":"9.8.7-fixture"}\n', encoding="utf-8")
        self.source = self.repo / "desktop"
        for name in ("bootstrap.py", "XM5Library.cmd", "xm5lib/ui/assets/NOTICE.md", "xm5lib/core/bundled_runtime.py"):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture source\n")
        (self.source / "derived.pyo").write_bytes(b"source cache is not source\n")
        self.runtime = self.root / "runtime"
        self.immutable = {
            "vendor/python/python.exe": b"fixture interpreter, not executed\n",
            "vendor/mpv/mpv.exe": b"fixture mpv, not executed\n",
            "vendor/ffmpeg/ffmpeg.exe": b"fixture ffmpeg, not executed\n",
            "vendor/python/Lib/site-packages/example/__init__.py": b"value = 1\n",
        }
        self.bytecode = {
            "vendor/python/Lib/__pycache__/example.cpython-314.pyc": b"original derived cache\n",
            "vendor/python/Lib/legacy.pyc": b"legacy derived cache\n",
            "vendor/python/Lib/legacy.pyo": b"optimized derived cache\n",
        }
        payload = self.immutable | self.bytecode
        for name, data in payload.items():
            path = self.runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.runtime_manifest = {
            "schema": "xm5.bundled-runtime.v1", "platform": "Windows x64",
            "inputs": [{"description": "retain \u539f\u59cb metadata"}],
            "files": {name: {"size": len(data), "sha256": digest(data)} for name, data in payload.items()},
        }
        self.write_runtime_manifest()
        self.source_archive = self.root / "media-source.zip"
        self.source_archive.write_bytes(b"fixture corresponding-source archive\n")
        self.review = {
            "schema": "xm5.runtime-source-review.v1", "status": "approved",
            "binaries": {name: digest(self.immutable[name]) for name in
                         ("vendor/mpv/mpv.exe", "vendor/ffmpeg/ffmpeg.exe")},
            "source_artifacts": [{"file": self.source_archive.name, "sha256": digest(self.source_archive.read_bytes())}],
        }
        self.review_path = self.root / "RUNTIME-SOURCE-REVIEW.json"
        self.write_review()

    def write_runtime_manifest(self):
        (self.runtime / "vendor/runtime-manifest.json").write_text(
            json.dumps(self.runtime_manifest, indent=2) + "\n", encoding="utf-8")

    def write_review(self):
        self.review_path.write_text(json.dumps(self.review) + "\n", encoding="utf-8")

    def build(self, output_name, *, runtime=None, public=False, archive=False):
        output = self.root / output_name
        command = [sys.executable, "-B", str(self.script), "--source", str(self.source),
                   "--runtime-dir", str(runtime or self.runtime), "--output", str(output)]
        if public:
            command += ["--public", "--source-review", str(self.review_path)]
        if archive:
            command += ["--zip", str(self.root / (output_name + ".zip"))]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        return output, result

    def test_changed_or_missing_bytecode_does_not_block_repeat_packaging(self):
        (self.runtime / next(iter(self.bytecode))).write_bytes(b"regenerated differently\n")
        (self.runtime / "vendor/python/Lib/legacy.pyo").unlink()
        first, result = self.build("first", archive=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        normalized = self.runtime_manifest | {"files": {
            name: self.runtime_manifest["files"][name] for name in self.immutable}}
        canonical = (json.dumps(normalized, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        self.assertEqual((first / "vendor/runtime-manifest.json").read_bytes(), canonical)
        manifest = json.loads((first / "BUILD-MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "9.8.7-fixture")
        self.assertEqual(manifest["files"]["vendor/runtime-manifest.json"],
                         {"size": len(canonical), "sha256": digest(canonical)})
        self.assertFalse(any("__pycache__" in name or name.endswith((".pyc", ".pyo"))
                             for name in manifest["files"]))
        for name, data in self.immutable.items():
            self.assertEqual((first / name).read_bytes(), data)
        with zipfile.ZipFile(self.root / "first.zip") as archive:
            self.assertFalse(any("__pycache__" in name or name.endswith((".pyc", ".pyo"))
                                 for name in archive.namelist()))
        second, repeated = self.build("second", runtime=first)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual((second / "vendor/runtime-manifest.json").read_bytes(), canonical)

    def test_modified_immutable_binary_still_blocks_packaging(self):
        binary = self.runtime / "vendor/mpv/mpv.exe"
        binary.write_bytes(b"X" * binary.stat().st_size)
        output, result = self.build("modified")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Runtime mismatch: vendor/mpv/mpv.exe", result.stderr)
        self.assertFalse(output.exists())

    def test_missing_immutable_input_still_blocks_packaging(self):
        (self.runtime / "vendor/python/python.exe").unlink()
        output, result = self.build("missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_profile_settings_backups_locks_and_discovery_are_excluded(self):
        private = {"config.json": b"fixture settings\n",
                   "config.json.bak": b"fixture previous settings\n",
                   "config.json.lock": b"fixture process lock\n",
                   "config.json.random.tmp": b"fixture interrupted settings secret\n",
                   "config.json.bak.random.tmp": b"fixture interrupted backup secret\n",
                   ".env": b"FIXTURE_SECRET=not-for-publication\n",
                   ".env.local": b"FIXTURE_LOCAL_SECRET=not-for-publication\n",
                   ".ENV.PROFILE": b"FIXTURE_CASE_VARIANT=not-for-publication\n",
                   ".local/development-input.txt": b"fixture local development input\n",
                   "runtime/connection.json": b"fixture session discovery\n"}
        for name, data in private.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        code = self.source / "xm5lib/feature.py"
        code.write_bytes(b"value = 'public source'\n")
        ordinary_temp = self.source / "ordinary.tmp"
        ordinary_temp.write_bytes(b"ordinary source fixture\n")
        output, result = self.build("private-excluded", archive=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((output / "BUILD-MANIFEST.json").read_text(encoding="utf-8"))
        for name in private:
            self.assertNotIn(name, manifest["files"])
            self.assertFalse((output / name).exists())
            self.assertEqual((self.source / name).read_bytes(), private[name])
        self.assertEqual((output / "xm5lib/feature.py").read_bytes(), code.read_bytes())
        self.assertEqual((output / "ordinary.tmp").read_bytes(), ordinary_temp.read_bytes())
        with zipfile.ZipFile(self.root / "private-excluded.zip") as archive:
            names = {name.removeprefix(output.name + "/") for name in archive.namelist()}
            self.assertTrue(set(private).isdisjoint(names))
            self.assertIn("xm5lib/feature.py", names)

    def test_public_review_keeps_exact_media_and_source_archive_pins(self):
        output, accepted = self.build("reviewed", public=True)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        manifest = json.loads((output / "BUILD-MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["public_runtime_source_review"], "approved")
        self.assertEqual(manifest["source_review_sha256"], digest(self.review_path.read_bytes()))
        for name in self.review["binaries"]:
            self.assertEqual(manifest["files"][name]["sha256"], self.review["binaries"][name])
        self.review["binaries"]["vendor/mpv/mpv.exe"] = "0" * 64
        self.write_review()
        rejected_output, rejected = self.build("bad-media-pin", public=True)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("Review does not cover exact binary", rejected.stderr)
        self.assertFalse(rejected_output.exists())
        self.review["binaries"]["vendor/mpv/mpv.exe"] = digest(self.immutable["vendor/mpv/mpv.exe"])
        self.write_review()
        self.source_archive.write_bytes(b"changed corresponding source\n")
        rejected_output, rejected = self.build("bad-source-pin", public=True)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("Corresponding-source archive mismatch", rejected.stderr)
        self.assertFalse(rejected_output.exists())

    def test_unsafe_bytecode_manifest_paths_are_rejected_before_omission(self):
        for number, name in enumerate(("vendor/__pycache__/../../escape.pyc",
                                       "vendor//__pycache__/escape.pyc",
                                       "vendor/__pycache__/C:/escape.pyc")):
            with self.subTest(path=name):
                self.runtime_manifest["files"][name] = {"size": 0, "sha256": digest(b"")}
                self.write_runtime_manifest()
                output, result = self.build("unsafe-" + str(number))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Unsafe runtime manifest path", result.stderr)
                self.assertFalse(output.exists())
                del self.runtime_manifest["files"][name]


if __name__ == "__main__":
    unittest.main()
