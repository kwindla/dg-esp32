"""Host-only baseline transaction tests; every command/build is mocked."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('baseline',Path(__file__).with_name('capture_s3_baselines.py'))
baseline=importlib.util.module_from_spec(spec);spec.loader.exec_module(baseline)


class BaselineTests(unittest.TestCase):
    def test_canonical_digest_ignores_comments_order_and_disabled_spelling(self):
        with tempfile.TemporaryDirectory() as tmp:
            left=Path(tmp)/'sdkconfig-left';right=Path(tmp)/'sdkconfig-right'
            left.write_text('CONFIG_FIXTURE_SECRET="synthetic-private-value"\nCONFIG_COUNT=3\n# CONFIG_FLAG is not set\n')
            right.write_text('# Changed generated heading/order\nCONFIG_FLAG=n\nCONFIG_COUNT=3\nCONFIG_FIXTURE_SECRET="synthetic-private-value"\n')
            digest=baseline.canonical_config_sha(left)
            self.assertEqual(digest,baseline.canonical_config_sha(right));self.assertEqual(len(digest),64)
            right.write_text('CONFIG_FLAG=n\nCONFIG_COUNT=4\nCONFIG_FIXTURE_SECRET="synthetic-private-value"\n')
            self.assertNotEqual(digest,baseline.canonical_config_sha(right))
            self.assertNotIn('synthetic-private-value',digest)

    def test_every_variant_removes_stale_generated_config_before_builder(self):
        for variant in baseline.VARIANTS:
            with self.subTest(variant=variant),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'build/accept').mkdir(parents=True);(root/'build-ws146').mkdir()
                configs={'normal':root/'sdkconfig','demo':root/'build/sdkconfig-demo',
                         'aec-normal':root/'build/accept/sdkconfig-aec-normal','aec-demo':root/'build/config-accept',
                         'waveshare_146':root/'build-ws146/sdkconfig'}
                config=configs[variant];config.write_text('CONFIG_STALE=y\n')
                local=root/'sdkconfig.local';local.write_text('untouched private fixture\n')
                def run(*args,**kw):self.assertFalse(config.exists(),'cached config reached builder')
                with patch.object(baseline,'ROOT',root),patch.object(baseline,'run',side_effect=run) as commands, \
                     patch.object(baseline,'snapshot',return_value={}),patch.dict(os.environ,{'TMPDIR':tmp}):
                    baseline.capture_variant(variant,root/'output',dict(PATH='/fixture/bin',IDF_PATH='/fixture/idf'))
                self.assertGreater(commands.call_count,0)
                self.assertEqual(local.read_text(),'untouched private fixture\n')

    def test_resume_refuses_legacy_cached_config_policy_before_any_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'dependencies.lock').write_text('pristine\n')
            output=root/'evidence/test';output.mkdir(parents=True)
            (output/'manifest.json').write_text(json.dumps(dict(commit='commit',dependencies_sha256=baseline.sha(root/'dependencies.lock'),variants={},complete=False)))
            with patch.object(baseline,'ROOT',root),patch.object(baseline,'EVIDENCE',root/'evidence'), \
                 patch.dict(os.environ,{'DG_BUILD_LOCK_HELD':'1'},clear=True), \
                 patch.object(baseline.subprocess,'run'),patch.object(baseline.subprocess,'check_output',return_value='commit\n'), \
                 patch.object(baseline,'capture_variant') as capture:
                with self.assertRaisesRegex(RuntimeError,'config policy mismatch'):baseline.main(['test','--resume'])
                capture.assert_not_called()

    def test_config_rename_is_restored_after_keydiff_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/'config-accept';config.write_text('synthetic fixture\n')
            with self.assertRaises(RuntimeError):
                with baseline.keydiff_name(config) as named:
                    self.assertTrue(named.name.startswith('sdkconfig'))
                    self.assertFalse(config.exists())
                    raise RuntimeError('tool failed')
            self.assertEqual(config.read_text(),'synthetic fixture\n')
            self.assertFalse((config.parent/'sdkconfig-baseline-aec-demo').exists())

    def test_missing_build_context_and_bad_labels_fail_before_commands(self):
        with patch.dict(os.environ,{},clear=True),patch.object(baseline.subprocess,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'build lock'): baseline.main(['valid-label'])
            run.assert_not_called()
        with contextlib.redirect_stderr(io.StringIO()),patch.object(baseline.subprocess,'run') as run:
            with self.assertRaises(SystemExit): baseline.main(['../bad'])
            run.assert_not_called()

    def matrix(self, fail=False, restore='ok'):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'dependencies.lock').write_text('pristine\n');order=[]
            build_error=RuntimeError('simulated build failed')
            def command(args,**kw):
                if args[:2]==['git','checkout']:
                    if restore=='error':raise OSError('simulated checkout failure')
                    if restore=='ok':(root/'dependencies.lock').write_text('pristine\n')
            def capture(variant,destination,env):
                self.assertEqual((root/'dependencies.lock').read_text(),'pristine\n')
                order.append(variant);(root/'dependencies.lock').write_text('changed graph\n')
                if fail: raise build_error
                image=destination/'dg-esp32.bin';image.write_bytes(variant.encode())
                return dict(files={'dg-esp32.bin':dict(bytes=image.stat().st_size,sha256=baseline.sha(image))})
            with patch.object(baseline,'ROOT',root),patch.object(baseline,'EVIDENCE',root/'evidence'), \
                 patch.dict(os.environ,{'DG_BUILD_LOCK_HELD':'1'},clear=True), \
                 patch.object(baseline.subprocess,'run',side_effect=command), \
                 patch.object(baseline.subprocess,'check_output',return_value='commit\n'), \
                 patch.object(baseline,'capture_variant',side_effect=capture), \
                 patch.object(baseline.signal,'signal'),contextlib.redirect_stdout(io.StringIO()):
                if fail or restore!='ok':
                    with self.assertRaises((RuntimeError,OSError)) as caught:baseline.main(['test'])
                    if fail:
                        self.assertIs(caught.exception,build_error)
                        if restore!='ok':self.assertIn('restoration failed',' '.join(caught.exception.__notes__))
                else: baseline.main(['test'])
            self.assertEqual((root/'dependencies.lock').read_text(),'pristine\n' if restore=='ok' else 'changed graph\n')
            state=json.loads((root/'evidence/test/manifest.json').read_text())
            if fail or restore!='ok':
                self.assertFalse(state['complete']);self.assertFalse(state['variants'])
                if fail:self.assertIn('simulated',state['error'])
                if restore!='ok':self.assertIn('restoration_error',state)
            else:
                self.assertTrue(state['complete']);self.assertEqual(order,list(baseline.VARIANTS))
                self.assertEqual(order[-1],'waveshare_146')
                for result in state['variants'].values():
                    self.assertEqual(result['dependencies']['before'],result['dependencies']['restored'])
            return state

    def test_matrix_order_and_lock_restoration_after_every_build(self): self.matrix()
    def test_failed_build_restores_lock_and_retains_failure(self): self.matrix(fail=True)

    def test_cleanup_mismatch_never_masks_original_build_exception(self):self.matrix(fail=True,restore='mismatch')
    def test_cleanup_command_failure_never_masks_original_build_exception(self):self.matrix(fail=True,restore='error')
    def test_cleanup_failure_after_success_does_not_publish_variant(self):self.matrix(restore='mismatch')

    def test_resume_replaces_link_left_before_interrupted_manifest_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'dependencies.lock').write_text('pristine\n');order=[]
            real_write=baseline.write;interrupt=True
            def write(path,data):
                nonlocal interrupt
                if path.name=='manifest.json' and data.get('variants') and interrupt:
                    interrupt=False;raise InterruptedError('after link, before manifest')
                real_write(path,data)
            def capture(variant,destination,env):
                order.append(variant);image=destination/'dg-esp32.bin';image.write_bytes(variant.encode())
                return dict(files={'dg-esp32.bin':dict(bytes=image.stat().st_size,sha256=baseline.sha(image))})
            with patch.object(baseline,'ROOT',root),patch.object(baseline,'EVIDENCE',root/'evidence'), \
                 patch.dict(os.environ,{'DG_BUILD_LOCK_HELD':'1'},clear=True), \
                 patch.object(baseline.subprocess,'run'),patch.object(baseline.subprocess,'check_output',return_value='commit\n'), \
                 patch.object(baseline,'capture_variant',side_effect=capture),patch.object(baseline,'write',side_effect=write), \
                 patch.object(baseline.signal,'signal'),contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(InterruptedError,'after link'):baseline.main(['test'])
                output=root/'evidence/test'
                self.assertEqual(json.loads((output/'manifest.json').read_text())['variants'],{})
                self.assertEqual((output/'normal').resolve().name,'normal-001')
                baseline.main(['test','--resume'])
                self.assertEqual((output/'normal').resolve().name,'normal-002')
                self.assertTrue((output/'attempts/normal-001/dg-esp32.bin').is_file())
                self.assertEqual(order,['normal',*baseline.VARIANTS])
                self.assertTrue(json.loads((output/'manifest.json').read_text())['complete'])
                baseline.main(['test','--resume'])
                self.assertEqual(order,['normal',*baseline.VARIANTS]) # Completed resume builds nothing.

    def test_variant_link_repair_is_atomic_and_refuses_real_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp);destination=output/'attempt-002';destination.mkdir()
            (output/'normal').symlink_to(output/'missing-attempt',target_is_directory=True)
            (output/'normal.link-tmp').symlink_to(output/'old-pending',target_is_directory=True)
            baseline.link_variant(output,'normal',destination)
            self.assertEqual((output/'normal').resolve(),destination)
            self.assertFalse((output/'normal.link-tmp').is_symlink())
            (output/'demo').mkdir()
            with self.assertRaisesRegex(RuntimeError,'not a link'):baseline.link_variant(output,'demo',destination)


if __name__=='__main__': unittest.main()
