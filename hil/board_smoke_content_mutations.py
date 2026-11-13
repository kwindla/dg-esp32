#!/usr/bin/env python3
"""Offline calibration-content mutations; run only after host-test permission.

Each candidate lives in a separate temp directory. The unchanged control must
pass; mutants must fail assertions, not compilation or pytest collection.
No bench lock, live IO or worktree source changes occur here.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
FILTER = 'content_ or calibration_content or turn_must_finish_before_recording_tail'
MUTATIONS = (
    ('calibration-content-strict', '    def content(ok, finding, reason):\n        if calibration:',
     '    def content(ok, finding, reason):\n        if False:'),
    ('qualification-content-soft', '    def content(ok, finding, reason):\n        if calibration:',
     '    def content(ok, finding, reason):\n        if True:'),
    ('missing-paris-finding', "content(any('paris' in words(t) for t in assistants),", 'content(True,'),
    ('missing-rome-finding', "content(any('rome' in words(t) for t in assistants),", 'content(True,'),
    ('off-script-count-finding', 'content(any(completed_count(t) for t in assistants),', 'content(True,'),
    ('off-script-city-finding', "if calibration and (kind == 'france' or (kind == 'talkover' and aec)):", 'if False:'),
    ('france-transcript', "require('france' in words(users[0]),", 'require(True,'),
    ('count-transcript', "require('count' in words(users[0]) and bool(words(users[0]) & {'twenty', '20'}),", 'require(True,'),
    ('italy-transcript', "require(any('italy' in words(t) for t in users[1:]),", 'require(True,'),
    ('aec-interruption', 'require(len(starts) == 2 and len(during) == 1 and interrupts,', 'require(True,'),
    ('quiet-interruption', 'require(len(starts) == 1 and not during and not interrupts,', 'require(True,'),
    ('aec-off-italy', "require(not any('italy' in words(t) for t in users),", 'require(True,'),
    ('bot-start', "require(frames and any(e.get('frame') == 'BotStartedSpeakingFrame' for e in frames),", 'require(True,'),
    ('bot-stop', "require(any(e.get('frame') == 'BotStoppedSpeakingFrame' for e in frames),", 'require(True,'),
    ('turn-evidence', "require(users and assistants and starts, 'missing bot turn evidence')", 'pass'),
    ('bot-active-witness', "require(all(type(e.get('bot_active')) is bool for e in frames if e.get('frame') in ('UserStartedSpeakingFrame', 'InterruptionFrame')),", 'require(True,'),
    ('pipeline-error', "require(not any(e.get('frame') in ('ErrorFrame', 'FatalErrorFrame') for e in frames),", 'require(True,'),
    ('session-end', "require(not ended, 'bot session ended during turn')", 'pass'),
    ('device-session-fail', r'\bSESSION FAIL\b', r'\bDISABLED_SESSION_FAIL\b'),
    ('live-content-mode', "turn_checks(events, kind, self.manifest['aec_enabled'], calibration=calibration)",
     "turn_checks(events, kind, self.manifest['aec_enabled'], calibration=False)"),
    ('result-findings', "for finding in turn.get('bot_content_findings', [])", 'for finding in []'),
    ('qualification-timing-soft', '            if calibration:\n                # Reach the unchanged recording boundary',
     '            if True:\n                # Reach the unchanged recording boundary'),
    ('recording-duration', "seconds = 15 if kind == 'france' else 30", "seconds = 20 if kind == 'france' else 40"),
    ('in-window-user-turn', "require(users, 'missing bot turn evidence within recording window')", 'pass'),
    ('in-window-bot-start', "require(any(e['frame'] == 'BotStartedSpeakingFrame' and e['monotonic_ns'] > users[-1]['monotonic_ns'] for e in speech),", 'require(True,'),
    ('clean-stop-wait', "self.wait(board, finished, WAIT_SAFETY_SECONDS, label='bot reply completion safety cap')", 'pass'),
    ('safety-cap', 'WAIT_SAFETY_SECONDS = 60', 'WAIT_SAFETY_SECONDS = 120'),
    ('overlong-finding', '                if speaking_at_end:\n', '                if False:\n'),
    ('turn-timing-findings', "if calibration: verdict['bot_content_findings'].extend(timing_findings)", 'pass'),
    ('late-settle-detection', 'settle_after_recording = self.clock() > finish_at', 'settle_after_recording = False'),
    ('drain-settle', '                settle_after_recording = True\n', '                settle_after_recording = False\n'),
    ('late-settle-wait', 'if settle_after_recording: self.pause(board, 2)', 'pass'),
    ('late-settle-duration', 'if settle_after_recording: self.pause(board, 2)', 'if settle_after_recording: self.pause(board, 1)'),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    source = (HERE/'board_smoke.py').read_text()
    temporary = Path(os.environ.get('TMPDIR', str(Path.home()/'.cache/gap3-tmp')))
    temporary.mkdir(parents=True, exist_ok=True)
    for name, before, after in [('control', None, None), *MUTATIONS]:
        candidate = source
        if before is not None:
            assert source.count(before) == 1, (name, 'mutation anchor drift')
            candidate = source.replace(before, after)
        compile(candidate, 'board_smoke.py', 'exec')
        with tempfile.TemporaryDirectory(prefix='smoke-content-mutation-', dir=temporary) as directory:
            work = Path(directory); hil = work/'hil'; hil.mkdir()
            (hil/'board_smoke.py').write_text(candidate)
            shutil.copy2(HERE/'test_board_smoke.py', hil/'test_board_smoke.py')
            (hil/'fixtures').symlink_to(HERE/'fixtures', target_is_directory=True)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', OPENBLAS_NUM_THREADS='1',
                       PYTHONPATH=os.pathsep.join((str(hil), str(HERE), str(HERE.parent/'server'))))
            command = [sys.executable, '-m', 'pytest', '-q', '--import-mode=importlib',
                       '--basetemp', str(work/'pytest'), str(hil/'test_board_smoke.py'), '-k', FILTER]
            result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True)
            (args.out/(name+'.log')).write_text(result.stdout+result.stderr)
            assert result.returncode == (0 if name == 'control' else 1), (name, result.returncode)
            print(name+(': PASS' if name == 'control' else ': KILLED'), flush=True)


if __name__ == '__main__':
    main()
