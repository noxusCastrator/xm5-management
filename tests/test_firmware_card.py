"""Firmware copying is tested only inside owned temporary directories."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import io
import tarfile
import unittest
from unittest.mock import patch

from xm5lib.core.firmware_card import FirmwareCardError, FirmwareCardService


class Volumes:
    def __init__(self, root):
        self.row = {'id': 'owned-test-volume', 'root': str(root), 'identity': 'original',
                    'label': 'Synthetic card', 'filesystem': 'exFAT', 'capacity': 512*1024*1024, 'free': 512*1024*1024}
    def list_targets(self):
        return [dict(self.row)]
    def inspect(self, target_id):
        if target_id != self.row['id']:
            raise FirmwareCardError('Unlisted volume')
        return dict(self.row)


class FirmwareCardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='xm5-card-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.app = self.root/'application'
        self.card = self.root/'card'
        self.card.mkdir()
        (self.app/'firmware').mkdir(parents=True)
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w:') as package:
            for name, data in [('firmware_v20.info', b'synthetic metadata'),
                               ('recovery-update/xImage', b'K' * 4096),
                               ('recovery-update/rootfs.squashfs', b'R' * 8192)]:
                member = tarfile.TarInfo(name); member.size = len(data)
                package.addfile(member, io.BytesIO(data))
        self.firmware = archive.getvalue()
        (self.app/'firmware/update.bin').write_bytes(self.firmware)
        manifest = {'schema': 'xm5.firmware-bundle.v1', 'file': 'update.bin', 'version': 'TEST',
                    'size': len(self.firmware), 'sha256': hashlib.sha256(self.firmware).hexdigest()}
        (self.app/'firmware/manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        self.volumes = Volumes(self.card)
        self.service = FirmwareCardService(self.app, self.volumes)
        (self.card/'keep-until-confirmed.txt').write_text('original', encoding='utf-8')

    def plan(self):
        return self.service.plan(self.volumes.row['id'])

    def test_preview_and_cancel_do_not_change_files(self):
        self.assertTrue(self.service.firmware_info()['available'])
        self.assertEqual(len(self.service.list_targets()), 1)
        plan = self.plan()
        self.assertFalse(plan['existing_update'])
        self.assertEqual((self.card/'keep-until-confirmed.txt').read_text(), 'original')

    def test_copy_preserves_nested_contents_without_extra_confirmation(self):
        nested = self.card/'music'/'album'
        nested.mkdir(parents=True)
        (nested/'track.flac').write_bytes(b'owned fixture')
        external = self.root/'external-original.flac'
        external.write_bytes(b'not on card')
        progress = []
        result = self.service.execute(self.plan()['token'], progress=progress.append)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual((nested/'track.flac').read_bytes(), b'owned fixture')
        self.assertEqual((self.card/'keep-until-confirmed.txt').read_text(), 'original')
        self.assertEqual((self.card/'update.bin').read_bytes(), self.firmware)
        self.assertEqual(external.read_bytes(), b'not on card')
        self.assertEqual(progress[-1]['phase'], 'complete')

    def test_changed_volume_refuses_before_any_deletion(self):
        plan = self.plan()
        self.volumes.row['identity'] = 'replacement'
        with self.assertRaisesRegex(FirmwareCardError, 'volume changed'):
            self.service.execute(plan['token'], confirmed=True)
        self.assertTrue((self.card/'keep-until-confirmed.txt').exists())

    def test_unrelated_changed_contents_do_not_invalidate_copy(self):
        plan = self.plan()
        (self.card/'new-file').write_bytes(b'new')
        self.service.execute(plan['token'])
        self.assertTrue((self.card/'keep-until-confirmed.txt').exists())
        self.assertTrue((self.card/'new-file').exists())

    def test_tampered_bundle_refuses_before_deletion(self):
        plan = self.plan()
        (self.app/'firmware/update.bin').write_bytes(b'bad')
        with self.assertRaisesRegex(FirmwareCardError, 'integrity'):
            self.service.execute(plan['token'], confirmed=True)
        self.assertTrue((self.card/'keep-until-confirmed.txt').exists())
        self.assertFalse(self.service.firmware_info()['available'])

    def test_system_profile_application_and_unsupported_filesystem_not_offered(self):
        self.volumes.row['root'] = str(self.root)
        self.assertEqual(self.service.list_targets(), [])
        self.volumes.row['root'] = str(Path.home())
        self.assertEqual(self.service.list_targets(), [])
        self.volumes.row['root'] = str(self.card)
        self.volumes.row['filesystem'] = 'NTFS'
        self.assertEqual(self.service.list_targets(), [])

    def test_confirmation_cannot_be_replayed(self):
        plan = self.plan()
        self.service.execute(plan['token'], confirmed=True)
        (self.card/'new-file').write_bytes(b'preserve')
        with self.assertRaisesRegex(FirmwareCardError, 'already used'):
            self.service.execute(plan['token'], confirmed=True)
        self.assertEqual((self.card/'new-file').read_bytes(), b'preserve')

    def test_backend_override_volume_is_protected(self):
        with patch.dict(os.environ, {'XM5LIB_BACKEND_HOME': str(self.card/'backend')}):
            self.assertEqual(self.service.list_targets(), [])
            with self.assertRaisesRegex(FirmwareCardError, 'running-application drive'):
                self.plan()

    def test_write_protected_volume_is_not_offered(self):
        self.volumes.row['read_only'] = True
        self.assertEqual(self.service.list_targets(), [])

    def test_resolved_home_location_is_protected(self):
        actual_home = self.card/'personal'
        actual_home.mkdir()
        alias = self.root/'home-alias'
        if os.name == 'nt':
            import subprocess
            result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(alias), str(actual_home)],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            alias.symlink_to(actual_home, target_is_directory=True)
        with patch.object(Path, 'home', return_value=alias):
            self.assertEqual(self.service.list_targets(), [])

    def test_unrelated_links_preserved_without_traversing(self):
        outside = self.root/'outside'
        outside.mkdir()
        original = outside/'original'
        original.write_bytes(b'preserve')
        link = self.card/'linked-directory'
        if os.name == 'nt':
            import subprocess
            result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(outside)],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            link.symlink_to(outside, target_is_directory=True)
        self.service.execute(self.plan()['token'], confirmed=True)
        self.assertEqual(original.read_bytes(), b'preserve')
        self.assertTrue(link.is_dir())
        self.assertEqual({path.name for path in self.card.iterdir()}, {'update.bin', 'linked-directory', 'keep-until-confirmed.txt'})

    def test_interrupted_copy_never_reports_success(self):
        def interrupt(event):
            if event['phase'] == 'copy':
                raise OSError('synthetic interruption')
        with self.assertRaisesRegex(FirmwareCardError, 'synthetic interruption'):
            self.service.execute(self.plan()['token'], confirmed=True, progress=interrupt)
        self.assertEqual({p.name for p in self.card.iterdir()}, {'keep-until-confirmed.txt'})

    def test_existing_firmware_requires_confirmation(self):
        target = self.card/'update.bin'
        target.write_bytes(b'old firmware')
        plan = self.plan()
        self.assertTrue(plan['existing_update'])
        with self.assertRaisesRegex(FirmwareCardError, 'confirmation'):
            self.service.execute(plan['token'])
        self.assertEqual(target.read_bytes(), b'old firmware')
        self.service.execute(plan['token'], confirmed=True)
        self.assertEqual(target.read_bytes(), self.firmware)

    def test_new_or_changed_firmware_after_plan_is_not_overwritten(self):
        for existing in (False, True):
            target = self.card/'update.bin'
            if existing:
                target.write_bytes(b'old firmware')
            else:
                target.unlink(missing_ok=True)
            plan = self.plan()
            target.write_bytes(b'user changed this')
            with self.assertRaisesRegex(FirmwareCardError, 'update.bin changed'):
                self.service.execute(plan['token'], confirmed=True)
            self.assertEqual(target.read_bytes(), b'user changed this')

    def test_copy_failure_preserves_previous_firmware_and_cleans_only_own_temp(self):
        target = self.card/'update.bin'
        target.write_bytes(b'old firmware')
        unknown = self.card/'.xm5-update-unrelated.tmp'
        unknown.write_bytes(b'not ours')
        def corrupt(event):
            if event['phase'] == 'verify':
                for path in self.card.glob('.xm5-update-*.tmp'):
                    if path != unknown:
                        path.write_bytes(b'broken')
        with self.assertRaisesRegex(FirmwareCardError, 'verification'):
            self.service.execute(self.plan()['token'], confirmed=True, progress=corrupt)
        self.assertEqual(target.read_bytes(), b'old firmware')
        self.assertEqual(unknown.read_bytes(), b'not ours')
        self.assertEqual(len(list(self.card.glob('.xm5-update-*.tmp'))), 1)

    def test_insufficient_free_space_preserves_existing_files(self):
        self.volumes.row['free'] = 0
        with self.assertRaisesRegex(FirmwareCardError, 'free space'):
            self.plan()
        self.assertEqual((self.card/'keep-until-confirmed.txt').read_text(), 'original')

    def test_directory_named_update_bin_is_refused(self):
        (self.card/'update.bin').mkdir()
        with self.assertRaisesRegex(FirmwareCardError, 'regular file'):
            self.plan()

    def test_copy_space_alone_is_not_enough_for_player_extraction(self):
        self.volumes.row['free'] = len(self.firmware) + 1024 * 1024
        with self.assertRaisesRegex(FirmwareCardError, 'free space'):
            self.plan()
        self.assertFalse((self.card/'update.bin').exists())

    def test_recovery_names_are_preserved_and_refused(self):
        for name in ('firmware_v20.info', 'recovery-update'):
            for directory in (False, True):
                with self.subTest(name=name, directory=directory):
                    path = self.card/name
                    path.mkdir() if directory else path.write_bytes(b'preserve me')
                    with self.assertRaisesRegex(FirmwareCardError, name):
                        self.plan()
                    self.assertTrue(path.exists())
                    path.rmdir() if directory else path.unlink()

    def test_conflict_appearing_after_plan_blocks_copy(self):
        plan = self.plan()
        (self.card/'recovery-update').mkdir()
        (self.card/'recovery-update/original').write_bytes(b'unrelated')
        with self.assertRaisesRegex(FirmwareCardError, 'recovery-update'):
            self.service.execute(plan['token'])
        self.assertFalse((self.card/'update.bin').exists())
        self.assertEqual((self.card/'recovery-update/original').read_bytes(), b'unrelated')

    def test_existing_update_space_is_released_only_after_publication(self):
        target = self.card/'update.bin'
        with target.open('wb') as old:
            old.truncate(112 * 1024 * 1024)
        self.volumes.row['free'] = len(self.firmware) + 1024 * 1024
        plan = self.plan()  # Old update will free enough space for unpacking.
        self.assertLessEqual(plan['required_free'], self.volumes.row['free'])
        self.service.execute(plan['token'], confirmed=True)
        self.assertEqual(target.read_bytes(), self.firmware)

    def test_last_minute_space_loss_preserves_old_update(self):
        target = self.card/'update.bin'; target.write_bytes(b'old')
        def fill(event):
            if event['phase'] == 'verify':
                self.volumes.row['free'] = 0
        with self.assertRaisesRegex(FirmwareCardError, 'free space'):
            self.service.execute(self.plan()['token'], confirmed=True, progress=fill)
        self.assertEqual(target.read_bytes(), b'old')

    def test_last_minute_conflict_preserves_old_update(self):
        target = self.card/'update.bin'; target.write_bytes(b'old')
        def conflict(event):
            if event['phase'] == 'verify':
                (self.card/'firmware_v20.info').write_bytes(b'user file')
        with self.assertRaisesRegex(FirmwareCardError, 'firmware_v20.info'):
            self.service.execute(self.plan()['token'], confirmed=True, progress=conflict)
        self.assertEqual(target.read_bytes(), b'old')

    def test_space_calculation_rounds_files_and_counts_previous_allocation(self):
        self.volumes.row['allocation_unit'] = 4096
        info = self.service.firmware_info()
        unit = 4096
        rounded = lambda n: ((n + unit - 1) // unit) * unit
        reserve = max(sum(rounded(n) for n in info['payload_sizes']) + unit,
                      100 * 1024 * 1024) + 8 * 1024 * 1024
        required = rounded(len(self.firmware)) + reserve
        self.volumes.row['free'] = required - 1
        with self.assertRaisesRegex(FirmwareCardError, 'free space'):
            self.plan()
        self.volumes.row['free'] = required
        plan = self.plan()
        self.assertEqual((plan['required_free'], plan['upgrade_reserve']), (required, reserve))

    def test_low_space_after_plan_refuses_before_copy(self):
        plan = self.plan()
        self.volumes.row['free'] = len(self.firmware) + 1024 * 1024
        with self.assertRaisesRegex(FirmwareCardError, 'free space'):
            self.service.execute(plan['token'])
        self.assertEqual({p.name for p in self.card.iterdir()}, {'keep-until-confirmed.txt'})

    def test_original_menu_free_space_floor_applies_to_small_payloads(self):
        self.volumes.row['free'] = len(self.firmware) + 80 * 1024 * 1024
        with self.assertRaisesRegex(FirmwareCardError, 'free space'):
            self.plan()


if __name__ == '__main__':
    unittest.main()
