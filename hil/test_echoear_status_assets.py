"""Off-bench fixture/provisioning regressions; every board request is mocked."""
import hashlib
from pathlib import Path
import struct
import unittest
from unittest.mock import Mock,patch
import echoear_status_assets as status

class StatusAssets(unittest.TestCase):
    def test_fixtures_match_content_hashes_geometry_and_firmware_manifest(self):
        header=(status.ROOT/'components/dg_display/include/dg_display/demo_assets.h').read_text()
        rows=status.status_assets();self.assertEqual(len(rows),5)
        for index,row in enumerate(rows):
            data=(status.ROOT/'hil/fixtures/echoear360'/(row['hash']+'.dgi')).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(),row['hash']);self.assertEqual(len(data),row['bytes'])
            self.assertEqual(struct.unpack_from('<HH',data,8),(row['width'],row['height']))
            self.assertIn('"%s",%d,%d,%d,%d,%d'%(row['hash'],row['bytes'],row['width'],row['height'],row['x'],row['y']),header)
            self.assertEqual((row['width'],row['height']),(360,360) if index<2 else (72,18))
        self.assertLess(sum(row['bytes'] for row in rows)+85248,491520)

    def test_provision_requires_persist_ack_for_every_asset(self):
        board=Mock()
        with patch.object(status,'request',side_effect=lambda b,k,d,i:{'id':i}) as post, \
             patch.object(status,'terminal',return_value={'error':'none','persisted':True}):
            self.assertEqual(len(status.provision_status(board,'http://fixture:7874')),5)
            self.assertEqual([c.args[1] for c in post.call_args_list],['load','persist']*5)
        with patch.object(status,'request',return_value={'id':'test'}), \
             patch.object(status,'terminal',return_value={'error':'none','persisted':False}):
            with self.assertRaisesRegex(RuntimeError,'not confirmed'):status.provision_status(board,'http://fixture:7874')

    def test_preload_has_no_url_and_missing_flash_asset_fails(self):
        with patch.object(status,'request',side_effect=lambda b,k,d,i:{'id':i}) as post, \
             patch.object(status,'terminal',return_value={'error':'none','persisted':True}):
            self.assertEqual(len(status.preload_status(Mock())),5)
            for call in post.call_args_list:
                self.assertEqual(call.args[1],'preload');self.assertEqual(set(call.args[2]),{'hash'})
        with patch.object(status,'request',return_value={'id':'test'}), \
             patch.object(status,'terminal',return_value={'error':'not-loaded'}):
            with self.assertRaisesRegex(RuntimeError,'not-loaded'):status.preload_status(Mock())

if __name__=='__main__':unittest.main()
