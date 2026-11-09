"""Exercise real tiny ZIP installs without downloading or running executables."""
import hashlib
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

from tools import setup_desktop as setup


class DesktopSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "source"
        self.app = self.repo / "desktop"
        self.app.mkdir(parents=True)
        self.originals = {
            "desktop/bootstrap.py": b"live source\n",
            "desktop/xm5lib/ui/assets/cover.png": b"live artwork\n",
            "desktop/config.json": b'{"profile": "keep"}\n',
            "desktop/data/library.sqlite": b"personal data placeholder\n",
        }
        for name, data in self.originals.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def archive(self, *, extra=None, edit_manifest=None, payload=None):
        files = {"vendor/python/python314._pth": b"python314.zip\n.\nLib\\site-packages\n..\\..\n",
                 "vendor/python/python.exe": b"tiny dependency fixture\n",
                 "vendor/python/Lib/__pycache__/fixture.cpython-314.pyc": b"disposable release cache\n"}
        manifest = {"schema": "xm5.bundled-runtime.v1", "files": {
            name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in files.items()}}
        if edit_manifest:
            edit_manifest(manifest)
        manifest_bytes = json.dumps(manifest).encode()
        files[setup.MANIFEST] = manifest_bytes
        if payload:
            payload(files)
        archive = self.repo / "fixture.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr(setup.ARCHIVE_ROOT + "/bootstrap.py", b"release source must not replace live source\n")
            output.writestr(setup.ARCHIVE_ROOT + "/data/library.sqlite", b"release data must not be extracted\n")
            for name, data in files.items():
                output.writestr(setup.ARCHIVE_ROOT + "/" + name, data)
            if extra:
                name, data = extra
                output.writestr(name, data)
        lock = setup.RuntimeLock(hashlib.sha256(archive.read_bytes()).hexdigest(),
                                 archive.stat().st_size, hashlib.sha256(manifest_bytes).hexdigest())
        # Malformed manifests must reach the production validator during install.
        try:
            installed_digest = hashlib.sha256(setup.normalized_manifest(manifest_bytes, lock)).hexdigest()
        except setup.SetupError:
            installed_digest = "0" * 64
        lock = setup.RuntimeLock(lock.archive_sha256, lock.archive_size, lock.manifest_sha256, installed_digest)
        return archive, lock

    def assert_originals(self):
        for name, data in self.originals.items():
            self.assertEqual((self.repo / name).read_bytes(), data, name)

    def assert_no_install(self):
        self.assertFalse((self.app / "vendor").exists())
        work = self.repo / ".local/dev-setup"
        self.assertEqual(list(work.iterdir()) if work.exists() else [], [])
        self.assert_originals()

    def test_install_only_vendor_and_repeat_without_changing_source_or_data(self):
        archive, lock = self.archive()
        setup.install_archive(self.repo, archive, lock)
        self.assertEqual(setup.verify_vendor(self.app, lock), 2)
        self.assertEqual((self.app / "vendor/python/python.exe").read_bytes(), b"tiny dependency fixture\n")
        self.assertEqual((self.app / "vendor/python/python314._pth").read_bytes(),
                         b"python314.zip\n.\nLib\\site-packages\n..\\..\n")
        self.assertFalse((self.app / "vendor/python/Lib/__pycache__").exists())
        installed = json.loads((self.app / setup.MANIFEST).read_bytes())
        self.assertEqual(len(installed["files"]), 2)
        stamp = (self.app / "vendor/python/python.exe").stat().st_mtime_ns
        self.assertIn("no files changed", setup.install_archive(self.repo, archive, lock))
        self.assertEqual((self.app / "vendor/python/python.exe").stat().st_mtime_ns, stamp)
        self.assert_originals()

    def test_generated_bytecode_extras_are_ignored_but_preserved(self):
        archive, lock = self.archive()
        setup.install_archive(self.repo, archive, lock)
        cache = self.app / "vendor/python/Lib/__pycache__/generated.cpython-314.pyc"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"regenerated cache")
        self.assertEqual(setup.verify_vendor(self.app, lock), 2)
        self.assertIn("no files changed", setup.install_archive(self.repo, archive, lock))
        self.assertEqual(cache.read_bytes(), b"regenerated cache")
        self.assert_originals()

    def test_bad_archive_hash_does_not_install(self):
        archive, lock = self.archive()
        bad = setup.RuntimeLock("0" * 64, lock.archive_size, lock.manifest_sha256)
        with self.assertRaisesRegex(setup.SetupError, "Archive size/SHA-256 mismatch"):
            setup.install_archive(self.repo, archive, bad)
        self.assert_no_install()

    def test_reject_unsafe_zip_names_before_extracting(self):
        for name in (setup.ARCHIVE_ROOT + "/vendor/../../escape", "/absolute",
                     setup.ARCHIVE_ROOT + "/vendor/C:/escape", setup.ARCHIVE_ROOT + "/vendor/CON.txt",
                     setup.ARCHIVE_ROOT + "/vendor/a\\escape", setup.ARCHIVE_ROOT + "/vendor/trailing. "):
            with self.subTest(name=name):
                archive, lock = self.archive(extra=(name, b"unsafe"))
                with self.assertRaises(setup.SetupError):
                    setup.install_archive(self.repo, archive, lock)
                self.assert_no_install()
        self.assertFalse((self.repo.parent / "escape").exists())

    def test_reject_symlink_and_reparse_zip_members(self):
        for attributes in ((stat.S_IFLNK | 0o777) << 16,
                           (stat.S_IFREG | 0o644) << 16 | setup.REPARSE_POINT):
            with self.subTest(attributes=attributes):
                member = zipfile.ZipInfo(setup.ARCHIVE_ROOT + "/vendor/link")
                member.create_system = 3
                member.external_attr = attributes
                archive, lock = self.archive(extra=(member, b"../../outside"))
                with self.assertRaisesRegex(setup.SetupError, "special, linked or encrypted"):
                    setup.install_archive(self.repo, archive, lock)
                self.assert_no_install()

    def test_reject_manifest_traversal_and_unlisted_file(self):
        archive, lock = self.archive(edit_manifest=lambda manifest: manifest["files"].update({
            "vendor/../escape": {"size": 0, "sha256": "0" * 64}}))
        with self.assertRaises(setup.SetupError):
            setup.install_archive(self.repo, archive, lock)
        self.assert_no_install()
        archive, lock = self.archive(payload=lambda files: files.update({"vendor/unlisted": b"unexpected"}))
        with self.assertRaisesRegex(setup.SetupError, "do not match the runtime manifest"):
            setup.install_archive(self.repo, archive, lock)
        self.assert_no_install()

    def test_bad_payload_hash_cleans_private_staging_and_leaves_no_vendor(self):
        archive, lock = self.archive(payload=lambda files: files.update({"vendor/python/python.exe": b"wrong dependency bytes!\n"}))
        with self.assertRaisesRegex(setup.SetupError, "size/SHA-256 mismatch"):
            setup.install_archive(self.repo, archive, lock)
        self.assert_no_install()

    def test_existing_changed_or_extra_vendor_is_refused_without_overwrite(self):
        archive, lock = self.archive()
        setup.install_archive(self.repo, archive, lock)
        executable = self.app / "vendor/python/python.exe"
        executable.write_bytes(b"a local change")
        with self.assertRaisesRegex(setup.SetupError, "size/SHA-256 mismatch"):
            setup.install_archive(self.repo, archive, lock)
        self.assertEqual(executable.read_bytes(), b"a local change")
        executable.write_bytes(b"tiny dependency fixture\n")
        extra = self.app / "vendor/custom.txt"
        extra.write_bytes(b"keep my file")
        with self.assertRaisesRegex(setup.SetupError, "missing or extra"):
            setup.install_archive(self.repo, archive, lock)
        self.assertEqual(extra.read_bytes(), b"keep my file")
        self.assert_originals()

    def test_existing_vendor_link_is_refused_without_touching_target(self):
        archive, lock = self.archive()
        outside = self.repo / "outside"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_bytes(b"keep")
        vendor = self.app / "vendor"
        if os.name == "nt":
            completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(vendor), str(outside)],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
            self.assertEqual(completed.returncode, 0, completed.stdout)
        else:
            os.symlink(outside, vendor, target_is_directory=True)
        with self.assertRaises(setup.SetupError):
            setup.install_archive(self.repo, archive, lock)
        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertTrue(os.path.lexists(vendor))
        self.assert_originals()

    def test_check_only_uses_independent_runtime_lock_without_network_or_writes(self):
        archive, lock = self.archive()
        setup.install_archive(self.repo, archive, lock)
        (self.repo / "release.json").write_text(json.dumps({
            "version": "0.2.1", "tag": "v0.2.1", "assets": {}, "development_runtime": {
                "asset_filename": setup.ARCHIVE_NAME, "size": lock.archive_size,
                "sha256": lock.archive_sha256, "tag": "v0.2.0", "url": setup.RELEASE_URL}}), encoding="utf-8")
        before = {path.relative_to(self.repo): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.repo.rglob("*") if path.is_file()}
        with (mock.patch.object(setup, "REPO", self.repo), mock.patch.object(setup, "APPROVED", lock),
              mock.patch.object(setup, "download_archive", side_effect=AssertionError("Unexpected network")),
              mock.patch.object(setup, "install_archive", side_effect=AssertionError("Unexpected install")),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(setup.main(["--check-only"]), 0)
            self.assertEqual(setup.main([]), 0)
        after = {path.relative_to(self.repo): (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in self.repo.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_check_only_reports_tampered_manifest_without_repairing_it(self):
        archive, lock = self.archive()
        setup.install_archive(self.repo, archive, lock)
        (self.repo / "release.json").write_text(json.dumps({"assets": {setup.ARCHIVE_NAME: {
            "size": lock.archive_size, "sha256": lock.archive_sha256}}}), encoding="utf-8")
        manifest = self.app / setup.MANIFEST
        manifest.write_bytes(b"changed manifest\n")
        errors = io.StringIO()
        with (mock.patch.object(setup, "REPO", self.repo), mock.patch.object(setup, "APPROVED", lock),
              mock.patch.object(setup, "download_archive", side_effect=AssertionError("Unexpected network")),
              contextlib.redirect_stderr(errors)):
            self.assertEqual(setup.main(["--check-only"]), 1)
        self.assertIn("Runtime manifest SHA-256 mismatch", errors.getvalue())
        self.assertEqual(manifest.read_bytes(), b"changed manifest\n")
        self.assert_originals()


if __name__ == "__main__":
    unittest.main()
