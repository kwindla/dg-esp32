#!/usr/bin/env python3
"""Offline session-guard mutation checks; run only when host tests are permitted.

Copies the harness/tests into task-owned temp directories and runs only the
session-continuity regressions. Never edits the worktree or takes a bench lock.
A compilation/collection failure is not a killed mutation.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
MUTATIONS = (
    ('bot-session-end', "e.get('kind') == 'session-end' for e in events", 'False for e in events'),
    ('bot-cancel-frame', "('CancelFrame', 'EndFrame')", "('EndFrame',)"),
    ('bot-end-frame', "('CancelFrame', 'EndFrame')", "('CancelFrame',)"),
    ('bot-direction', 'def check_bot_session(events):\n    frames = downstream_frames(events)',
     "def check_bot_session(events):\n    frames = [e for e in events if e.get('kind') == 'frame']"),
    ('bot-require', "require(not ended, 'bot session ended during turn')", 'pass'),
    ('turn-checks', '"""Real bot recorder, not device VAD or a substring count of \'interrupt\'."""\n    frames = check_bot_session(events)',
     '"""Real bot recorder, not device VAD or a substring count of \'interrupt\'."""\n    frames = downstream_frames(events)'),
    ('turn-finished', 'def turn_finished(events, need):\n    frames = check_bot_session(events)',
     'def turn_finished(events, need):\n    frames = downstream_frames(events)'),
    ('serial-fail', r'\bSESSION FAIL\b', r'\bDISABLED_SESSION_FAIL\b'),
    ('serial-peer', r'\bPEER_DEF:\s*Peer disconnected\b', r'\bDISABLED_PEER\b'),
    ('serial-bye', r'\bRTCP_BYE\b', r'\bDISABLED_BYE\b'),
    ('serial-disconnected', 'Disconnected|Starting|Idle', 'Starting|Idle'),
    ('serial-starting', 'Disconnected|Starting|Idle', 'Disconnected|Idle'),
    ('serial-idle', 'Disconnected|Starting|Idle', 'Disconnected|Starting'),
    ('serial-generation', 'int(state[1]) != self.session_gen', 'int(state[1]) == self.session_gen'),
    ('serial-read-cursor', 'board.lines_since(self.serial_seen)', 'board.lines_since(board.cursor)'),
    ('serial-advance', 'self.serial_seen = cursor', 'pass'),
    ('ready-generation', r"self.session_gen = int(re.search(r'\bSTATE Ready gen=(\d+)', ready)[1])", 'self.session_gen = 0'),
    ('ready-cursor', 'self.serial_seen = ready_at', 'self.serial_seen = board.cursor'),
    ('begin-stopping-reset', 'self.stopping = False; self.session_gen = self.pc_id = None', 'self.session_gen = self.pc_id = None'),
    ('begin-recorder', "check_bot_session(events)\n        profiles =", 'pass\n        profiles ='),
    ('poll-recorder', 'check_bot_session(self.events()[1])', 'pass'),
    ('turn-before-playback', 'self.poll(board)  # Reject a lost session before another stimulus starts.', 'pass'),
    ('recording-poll', 'self.wait(board, recorded, max(0, recording_at+seconds+5-self.clock()),',
     'if False: self.wait(board, recorded, max(0, recording_at+seconds+5-self.clock()),'),
    ('intentional-stop', 'self.stopping = True  # Own hold/disconnect is exempt, including cleanup.', 'pass'),
    ('stopping-exemption', 'self.active and not self.stopping', 'self.active', 2),
)
FILTER = 'session_end or session_lost_serial or session_serial_cursor or session_begin or session_own_finish'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    source = (HERE/'board_smoke.py').read_text()
    temporary = Path(os.environ.get('TMPDIR', str(Path.home()/'.cache/gap3-tmp')))
    temporary.mkdir(parents=True, exist_ok=True)
    for name, before, after, *counts in [('control', None, None), *MUTATIONS]:
        candidate = source
        if before is not None:
            assert source.count(before) == (counts[0] if counts else 1), (name, 'mutation anchor drift')
            candidate = source.replace(before, after)
        compile(candidate, 'board_smoke.py', 'exec')
        with tempfile.TemporaryDirectory(prefix='smoke-session-mutation-', dir=temporary) as directory:
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
