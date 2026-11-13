import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import echoear_aec_scored as scored


class BindingTests(unittest.TestCase):
    def test_cleanup_failure_does_not_skip_other_owners(self):
        calls=[]
        def fail(label):
            calls.append(label)
            raise OSError(label+' unit-test failure')
        board=SimpleNamespace(display_monitor=SimpleNamespace(result=lambda:fail('monitor')),
                              command=lambda text,timeout:fail(text),close=lambda:fail('serial close'))
        bot=SimpleNamespace(stop=lambda:fail('bot stop'))
        result=dict(passed=True,cleanup_errors=[])
        with tempfile.TemporaryDirectory() as root,patch('echoear_aec_scored.command',side_effect=lambda *_:fail('capture stop')):
            scored.cleanup_owned(board,bot,True,True,Path(root),result)
        self.assertIn('audio 0',calls)
        self.assertIn('disconnect',calls)
        self.assertIn('serial close',calls)
        self.assertEqual(calls[-1],'bot stop')
        self.assertFalse(result['passed'])
        self.assertGreaterEqual(len(result['cleanup_errors']),6)

    def test_exact_consumed_protocol_is_bound_and_unreviewed_draft_cannot_run(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);source=root/'source.json';source.write_text(json.dumps(dict(test=True,scored_consumption_marker=str(root/'consumed.json')))+'\n')
            out=root/'prepared'
            with patch.object(scored,'EVIDENCE',root),patch.object(scored,'PROTOCOL',source),\
                    patch.object(scored,'source_inputs',return_value={str(source):scored.sha(source)}),redirect_stdout(io.StringIO()):
                scored.prepare(out)
            binding=json.loads((out/'binding.json').read_text())
            scored.verify_binding(binding)
            self.assertIn(str(out/'protocol.json'),binding['inputs'])
            args=argparse.Namespace(out=out,approval=None,grant_note=None,grant_until=None)
            with patch('step9a_echoear.verify',side_effect=AssertionError('Must not touch hardware')):
                with self.assertRaisesRegex(ValueError,'Reviewed exact consumed protocol'):
                    scored.run(args)
            (out/'protocol.json').write_text('{"test":false}\n')
            with self.assertRaisesRegex(ValueError,'Frozen input changed'):
                scored.verify_binding(binding)

    def test_approval_must_name_this_source_commit_and_explicit_verdict(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);source=root/'source.json';source.write_text('{}\n')
            approval=root/'not-approved.md';approval.write_text('Draft looks reasonable, pending source review.')
            with patch.object(scored,'EVIDENCE',root),patch.object(scored,'PROTOCOL',source),\
                    patch.object(scored,'source_inputs',return_value={str(source):scored.sha(source)}):
                with self.assertRaisesRegex(ValueError,'Explicit Astra approval'):
                    scored.prepare(root/'prepared',approval)


if __name__=='__main__':unittest.main()
