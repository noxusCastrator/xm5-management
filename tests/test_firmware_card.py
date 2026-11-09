"""Destructive preparation is tested only inside owned temporary directories."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from xm5lib.core.firmware_card import FirmwareCardError, FirmwareCardService


class Volumes:
    def __init__(self, root):
        self.row = {'id': 'owned-test-volume', 'root': str(root), 'identity': 'original',
                    'label': 'Synthetic card', 'filesystem': 'exFAT', 'capacity': 64*1024*1024, 'free': 100}
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
        self.firmware = b'synthetic firmware, never for a real device'
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
        self.assertEqual(plan['files_count'], 1)
        with self.assertRaisesRegex(FirmwareCardError, 'confirmation'):
            self.service.execute(plan['token'])
        self.assertEqual((self.card/'keep-until-confirmed.txt').read_text(), 'original')

    def test_confirmed_operation_clears_nested_contents_and_only_copies_firmware(self):
        nested = self.card/'music'/'album'
        nested.mkdir(parents=True)
        (nested/'track.flac').write_bytes(b'owned fixture')
        external = self.root/'external-original.flac'
        external.write_bytes(b'not on card')
        progress = []
        result = self.service.execute(self.plan()['token'], confirmed=True, progress=progress.append)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual([path.name for path in self.card.iterdir()], ['update.bin'])
        self.assertEqual((self.card/'update.bin').read_bytes(), self.firmware)
        self.assertEqual(external.read_bytes(), b'not on card')
        self.assertEqual(progress[-1]['phase'], 'complete')

    def test_changed_volume_refuses_before_any_deletion(self):
        plan = self.plan()
        self.volumes.row['identity'] = 'replacement'
        with self.assertRaisesRegex(FirmwareCardError, 'volume changed'):
            self.service.execute(plan['token'], confirmed=True)
        self.assertTrue((self.card/'keep-until-confirmed.txt').exists())

    def test_changed_contents_require_new_confirmation(self):
        plan = self.plan()
        (self.card/'new-file').write_bytes(b'new')
        with self.assertRaisesRegex(FirmwareCardError, 'contents changed'):
            self.service.execute(plan['token'], confirmed=True)
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

    def test_links_removed_without_traversing_outside_target(self):
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
        self.assertEqual({path.name for path in self.card.iterdir()}, {'update.bin'})

    def test_interrupted_copy_never_reports_success(self):
        def interrupt(event):
            if event['phase'] == 'copying':
                raise OSError('synthetic interruption')
        with self.assertRaisesRegex(FirmwareCardError, 'partially cleared'):
            self.service.execute(self.plan()['token'], confirmed=True, progress=interrupt)


if __name__ == '__main__':
    unittest.main()
