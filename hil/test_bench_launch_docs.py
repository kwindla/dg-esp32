"""Validate documented shared-smoke bench environments without entering a bench."""
import os
from pathlib import Path
import re
import shlex
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = re.compile(r'hil/(?:board_smoke|p4_headless|p4_audio(?:_fix4)?)\.py')
REQUIRED = ('pyserial','pyelftools','idf-component-manager==3.1.2','esptool==5.5.0')


def bench_commands():
    names = subprocess.check_output(['rg','--files','hil','docs','spikes','-g','*.md'],cwd=ROOT,text=True).splitlines()
    for name in names:
        path = ROOT/name
        for block in re.findall(r'```(?:bash|sh)\n(.*?)```',path.read_text(),re.S):
            for line in re.sub(r'\\\n\s*',' ',block).splitlines():
                if 'hil/bench.py' not in line or not ENTRY.search(line): continue
                # Validate the child's environment, not an outer runner's.
                child = line.split('hil/bench.py',1)[1]
                tokens = shlex.split(child)
                if tokens[:2]!=['uv','run']: raise AssertionError(f'Explicit uv environment required: {path}')
                end = tokens.index('python')
                yield path.relative_to(ROOT), tokens[:end]

def validate(prefix):
    assert prefix[:2]==['uv','run']
    assert prefix[prefix.index('--project')+1]=='server'
    assert '--frozen' in prefix and '--offline' in prefix
    assert prefix[prefix.index('--group')+1]=='dev'
    packages = [prefix[i+1] for i,token in enumerate(prefix) if token=='--with']
    assert all(package in packages for package in REQUIRED)


def test_every_documented_shared_bench_environment_imports_the_preflight_modules():
    commands = list(bench_commands())
    assert {'hil/p4_audio.md','hil/p4_audio_fix4.md','hil/p4_headless.md','hil/board_smoke.md'}<=set(str(path) for path,_ in commands)
    environments = set()
    for path,prefix in commands:
        try: validate(prefix)
        except (AssertionError,ValueError): raise AssertionError(f'Broken bench environment in {path}: {prefix}')
        environments.add(tuple(prefix))
    for prefix in environments:
        # The real preflight imports every required module and checks tool
        # versions. It never opens serial, takes a bench lock or starts a bot.
        result = subprocess.run([*prefix,'python','hil/board_smoke.py','--preflight-tools'],cwd=ROOT,
            env=dict(os.environ,PYTHONPATH='hil:server',OPENBLAS_NUM_THREADS='1',PYTHONDONTWRITEBYTECODE='1'),
            capture_output=True,text=True,timeout=60)
        assert result.returncode==0, 'Documented environment failed dependency preflight: '+result.stderr
        assert 'no bench/device access' in result.stdout


def test_original_hil_environment_regression_is_rejected():
    prefix=['uv','run','--project','hil','--frozen','--offline','--group','dev']
    for package in REQUIRED: prefix+=['--with',package]
    with pytest.raises(AssertionError): validate(prefix)


def test_fix4_live_commands_are_wrapped_and_use_the_checked_environment():
    found = 0
    for name in ('hil/p4_audio.md', 'hil/p4_audio_fix4.md'):
        for block in re.findall(r'```(?:bash|sh)\n(.*?)```', (ROOT/name).read_text(), re.S):
            for line in re.sub(r'\\\n\s*', ' ', block).splitlines():
                if 'hil/p4_audio_fix4.py voice-smoke' not in line: continue
                assert 'hil/bench.py' in line, 'Live fix4 command missing bench wrapper: '+name
                tokens = shlex.split(line.split('hil/bench.py', 1)[1])
                validate(tokens[:tokens.index('python')]); found += 1
    assert found >= 3  # The original loop plus explicit S3/P4 examples.
