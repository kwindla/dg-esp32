"""Lifecycle regressions for the mute and AEC-reconnect adapters (Astra r4 items 1 and 3).

Real Live.cleanup / MuteLive.cleanup / execute() / protected_execute(); only hardware boundaries are faked:
the bench lock file (a temp path), flash/boot (echoear_aec_bench), console commands, alsactl and the port.
Signal tests run the real wrapper in a disposable subprocess with a fully fake driver.
"""
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import echoear_rollback as r

MUTE_PLAN = json.loads((r.ROOT/'hil/plans/echoear-mute-gate-v1.json').read_text())
AEC_PLAN = json.loads((r.ROOT/'hil/plans/echoear-aec-reconnect-v1.json').read_text())


class Board:
    def __init__(self):
        self.closed = False
    def close(self):
        self.closed = True


@pytest.fixture
def bench(tmp_path, monkeypatch):
    import echoear_aec_bench as bench
    calls = []
    monkeypatch.setattr(r, 'LOCK', tmp_path/'bench.lock')
    monkeypatch.setattr(r, 'port_free', lambda: True)
    monkeypatch.setattr(bench, 'verify_bundle', lambda image, h, g=None: calls.append(('verify', str(image), h)))
    monkeypatch.setattr(bench, 'flash', lambda image, h, out, label, g=None: calls.append(('flash', str(image), h, label)))
    monkeypatch.setattr(bench, 'boot', lambda out, label, h: calls.append(('boot', label, h)) or Board())
    def run(args, **kw):
        if args[0] == 'alsactl':
            calls.append(('alsactl', args[3], Path(args[2]).name))
            if args[3] == 'store':
                Path(args[2]).write_bytes(b'mixer\n')
        return subprocess.CompletedProcess(args, 0, b'', b'')
    monkeypatch.setattr(r.subprocess, 'run', run)
    return calls


def make(cls, plan, tmp_path):
    decl = dict(plan_sha256='p', public_env=plan['public_env'], fixtures={}, bot_commit='x')
    live = cls(decl, plan, tmp_path)
    live.command = lambda text, timeout=40: (0, 0)
    return live


@pytest.mark.parametrize('cls,plan', [(r.MuteLive, MUTE_PLAN), (r.AecReconnectLive, AEC_PLAN)])
def test_inherited_cleanup_restores_the_normal_image_never_the_candidate(tmp_path, bench, cls, plan):
    live = make(cls, plan, tmp_path)
    live.touched, live.board, live.mixer_saved = True, Board(), True
    (tmp_path/'mixer-before.state').write_bytes(b'mixer\n')
    result = live.cleanup(1)
    assert result['errors'] == [] and result['restored_elf_sha256'] == r.NORMAL_HASH
    flashes = [c for c in bench if c[0] == 'flash']
    assert flashes == [('flash', str(r.NORMAL), r.NORMAL_HASH, 'restore-1')]
    assert plan['image']['elf_sha256'] not in json.dumps(bench)          # the candidate is never reflashed
    assert ('boot', 'restore-1', r.NORMAL_HASH) in bench and result['mixer'] == dict(restored=True, byte_equal=True)


@pytest.mark.parametrize('cls,plan', [(r.MuteLive, MUTE_PLAN), (r.AecReconnectLive, AEC_PLAN)])
def test_execute_end_to_end_runs_through_cleanup_and_releases_the_lock(tmp_path, bench, monkeypatch, cls, plan):
    live = make(cls, plan, tmp_path)
    def setup():
        live.touched, live.board, live.mixer_saved = True, Board(), True
        (tmp_path/'mixer-before.state').write_bytes(b'mixer\n')
        return dict(fake=True)
    live.setup = setup
    live.smoke = lambda: (_ for _ in ()).throw(RuntimeError('gate INCOMPLETE'))
    result = r.execute(live, tmp_path)
    assert result['lock_released'] and not result['passed'] and result['error'].startswith('RuntimeError')
    assert result['cleanup_attempts'][-1]['restored_elf_sha256'] == r.NORMAL_HASH
    assert [c[3] for c in bench if c[0] == 'flash'] == ['restore-1']
    assert json.loads((tmp_path/'result.json').read_text())['lock_released'] is True


def test_candidate_install_is_separate_from_restore():
    for cls in (r.MuteLive, r.AecReconnectLive):
        assert cls.install is r.Live.install and callable(cls.install_candidate)


SCRIPT = textwrap.dedent('''
    import json, os, sys, time
    sys.path[:0] = [{hil!r}, {server!r}]
    import echoear_rollback as r
    out = r.Path(sys.argv[1]); mode = sys.argv[2]
    r.LOCK = out/'bench.lock'
    class Driver:
        def __init__(self): self.log = []
        def note(self, x):
            with (out/'log.txt').open('a') as f: f.write(x+'\\n')
        def acquire(self):
            self.lock = open(r.LOCK, 'a'); self.note('acquire')
        def setup(self):
            self.note('setup'); (out/'ready').write_text('1')
        def smoke(self):
            while True: time.sleep(.02)
        def cleanup(self, attempt):
            self.note('cleanup'); return dict(errors=[], restored_elf_sha256=r.NORMAL_HASH, owned_bot_stopped=True)
        def wait_recovery(self): pass
        def release(self): self.lock.close(); self.note('release')
    r.protected_execute(Driver(), out, 1 if mode == 'deadline' else 600)
''')


@pytest.mark.parametrize('mode', ['term', 'deadline'])
def test_protected_wrapper_unwinds_term_and_deadline_into_cleanup(tmp_path, mode):
    script = tmp_path/'driver.py'
    script.write_text(SCRIPT.format(hil=str(r.ROOT/'hil'), server=str(r.ROOT/'server')))
    out = tmp_path/'out'; out.mkdir()
    proc = subprocess.Popen([sys.executable, str(script), str(out), mode])
    deadline = time.monotonic()+20
    while not (out/'ready').exists():
        assert time.monotonic() < deadline and proc.poll() is None
        time.sleep(.02)
    if mode == 'term':
        proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=20) == 0
    result = json.loads((out/'result.json').read_text())
    assert (out/'log.txt').read_text().split() == ['acquire', 'setup', 'cleanup', 'release']
    assert result['lock_released'] and result['cleanup_attempts'][-1]['restored_elf_sha256'] == r.NORMAL_HASH
    assert ('signal' in result['error']) and (str(int(signal.SIGTERM if mode == 'term' else signal.SIGALRM)) in result['error'])
