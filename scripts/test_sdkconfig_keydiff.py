"""Synthetic config fixtures only; values must never appear on either output FD."""
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('keydiff', Path(__file__).with_name('sdkconfig_keydiff.py'))
keydiff = importlib.util.module_from_spec(spec); spec.loader.exec_module(keydiff)


class KeyDiffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.left = self.root/'sdkconfig-left'; self.right = self.root/'sdkconfig-right'
        self.secret = 'sk-fixture-secret-MUST-NOT-BE-PRINTED'
        self.left.write_text(f'CONFIG_DG_WIFI_PASSWORD="{self.secret}"\nCONFIG_OLD=y\n# CONFIG_DISABLED is not set\nCONFIG_EMPTY=\n')
        self.right.write_text('CONFIG_DG_WIFI_PASSWORD="another-hidden-value"\nCONFIG_NEW=y\nCONFIG_DISABLED=n\nCONFIG_EMPTY=\n')

    def invoke(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr): code = keydiff.main(args)
        self.assertNotIn(self.secret, stdout.getvalue()+stderr.getvalue())
        self.assertNotIn('another-hidden-value', stdout.getvalue()+stderr.getvalue())
        return code, stdout.getvalue(), stderr.getvalue()

    def test_only_key_names_and_statuses_for_differences(self):
        code, out, err = self.invoke([str(self.left), str(self.right)])
        self.assertEqual(code, 1); self.assertEqual(err, '')
        import json
        self.assertEqual(json.loads(out), dict(equal=['CONFIG_DISABLED','CONFIG_EMPTY'], differ=['CONFIG_DG_WIFI_PASSWORD'],
                                              added=['CONFIG_NEW'], removed=['CONFIG_OLD']))

    def test_self_diff_equal(self):
        self.assertEqual(self.invoke([str(self.left), str(self.left)])[0], 0)

    def test_expect_keys_accepts_disabled_and_empty_values(self):
        code, out, err = self.invoke([str(self.left), '--expect-keys', 'DG_WIFI_PASSWORD,DISABLED,EMPTY'])
        self.assertEqual(code, 0); self.assertIn('CONFIG_DISABLED', out)

    def test_expect_missing_key_and_two_path_target(self):
        code, out, err = self.invoke([str(self.left), str(self.right), '--expect-keys', 'NEW,OLD'])
        self.assertEqual(code, 1); self.assertIn('CONFIG_OLD', out)

    def test_invalid_name_directory_and_symlink_target_refused(self):
        private = self.root/'private.txt'; private.write_text(self.left.read_text())
        alias = self.root/'sdkconfig-alias'; alias.symlink_to(private)
        for path in (private, self.root, alias, self.root/'sdkconfig-missing'):
            with self.subTest(path=path): self.assertEqual(self.invoke([str(path), str(self.right)])[0], 2)

    def test_malformed_rows_do_not_leak(self):
        self.left.write_text(self.secret)
        self.assertEqual(self.invoke([str(self.left), str(self.right)])[0], 2)

    def test_legacy_duplicate_alias_uses_last_assignment(self):
        self.left.write_text(f'CONFIG_KEY="{self.secret}"\nCONFIG_KEY=y\n')
        self.right.write_text('CONFIG_KEY=y\n')
        self.assertEqual(self.invoke([str(self.left), str(self.right)])[0], 0)

    def test_invalid_expected_key_does_not_echo_input(self):
        self.assertEqual(self.invoke([str(self.left), '--expect-keys', self.secret])[0], 2)


if __name__ == '__main__': unittest.main()
