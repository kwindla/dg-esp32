"""No real bench/socket/serial/bot calls: rollback draft and cleanup fault tests."""
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
from types import SimpleNamespace

import pytest
import echoear_rollback as r


def rows():
    base=[dict(kind='aggregator-user-turn-stopped',text='Question',monotonic_ns=1),
          dict(kind='tts-transport-write',non_silent=True,drop=False,monotonic_ns=3),
          dict(kind='server-frame',frame='BotStoppedSpeakingFrame',monotonic_ns=6),
          dict(kind='aggregator-assistant-turn-stopped',text='Answer',monotonic_ns=7)]
    return [dict(x,pc_id='peer') for x in base]


def lines():
    return [(0,0,'MIC_GATE t=1 closed=0 pre=-40 post=-40'),
            (1,0,'MIC_GATE t=2 closed=1 pre=-20 post=-120'),
            (2,0,'MIC_GATE t=3 closed=0 pre=-60 post=-60')]


@pytest.mark.parametrize('talk',[False,True])
def test_complete_legacy_turn_has_independent_witnesses(talk):
    result=r.turn_witness(rows(),lines(),talk)
    assert result['passed'] and result['gate_reopened'] and result['pc_ids']==['peer']


@pytest.mark.parametrize('fault',['user','assistant','write','stop','closed','reopen','leak','interrupt','talkover-accepted'])
def test_missing_or_failed_half_duplex_witness_is_not_success(fault):
    events=rows();serial=lines()
    kinds={'user':'aggregator-user-turn-stopped','assistant':'aggregator-assistant-turn-stopped',
           'write':'tts-transport-write','stop':'server-frame'}
    if fault in kinds:events=[e for e in events if e['kind']!=kinds[fault]]
    if fault=='closed':serial=[serial[0],serial[2]]
    if fault=='reopen':serial=serial[:2]
    if fault=='leak':serial[1]=(1,0,'MIC_GATE t=2 closed=1 pre=-20 post=-80')
    if fault=='interrupt':events.append(dict(frame='InterruptionFrame',monotonic_ns=4))
    if fault=='talkover-accepted':events.append(dict(kind='aggregator-user-turn-stopped',text='Stop',monotonic_ns=4))
    assert not r.turn_witness(events,serial,True)['passed']


def test_pre_playback_turn_start_is_not_a_playback_interruption():
    events=rows()+[dict(frame='InterruptionFrame',monotonic_ns=2)]
    assert r.turn_witness(events,lines(),True)['passed']


def counters():
    return dict(failed_allocations=dict(total=0),console_output=dict(full=0,transport_drops=0),
        i2s_underflows=0,i2s_write_errors=0,i2s_write_timeouts=0,pcm_queue_drops=0)


@pytest.mark.parametrize('key',['i2s_underflows','i2s_write_errors','i2s_write_timeouts','pcm_queue_drops'])
def test_missing_increasing_or_reset_legacy_counters_fail(key):
    a=counters();assert r.counter_check(a,a)['passed']
    b=copy.deepcopy(a);b[key]=1;assert not r.counter_check(a,b)['passed']
    assert not r.counter_check(b,a)['passed']
    del b[key];assert not r.counter_check(a,b)['passed']


class Driver:
    def __init__(self,fail=None,cleanup_fail=False):self.calls=[];self.fail=fail;self.cleanup_fail=cleanup_fail
    def acquire(self):
        self.calls.append('acquire')
        if self.fail=='acquire':raise BlockingIOError('busy')
    def setup(self):
        self.calls.append('setup')
        if self.fail=='setup':raise RuntimeError('partial flash/boot/startup failed')
    def smoke(self):
        self.calls.append('smoke')
        if self.fail=='smoke':raise RuntimeError('incomplete conversation')
        if self.fail=='signal':raise KeyboardInterrupt()
        return dict(passed=True)
    def cleanup(self,attempt):
        self.calls.append('cleanup-'+str(attempt))
        if attempt==1 and self.cleanup_fail:return dict(errors=['restore failed'],restored_elf_sha256=None,owned_bot_stopped=True)
        return dict(errors=[],restored_elf_sha256=r.NORMAL_HASH,owned_bot_stopped=True)
    def wait_recovery(self):
        assert 'release' not in self.calls
        assert signal.getsignal(signal.SIGTERM)==signal.SIG_IGN
        self.calls.append('operator-recovery')
    def release(self):self.calls.append('release')


@pytest.mark.parametrize('fault',[None,'setup','smoke','signal'])
def test_restore_and_release_on_success_failure_and_signal(tmp_path,fault):
    d=Driver(fault);result=r.execute(d,tmp_path)
    assert result['passed']==(fault is None)
    assert result['lock_released'] and d.calls[-2:]==['cleanup-1','release']
    assert json.loads((tmp_path/'result.json').read_text())==result


def test_lock_contention_touches_no_board_bot_or_cleanup(tmp_path):
    d=Driver('acquire')
    with pytest.raises(BlockingIOError):r.execute(d,tmp_path)
    assert d.calls==['acquire'] and not (tmp_path/'result.json').exists()


def test_restore_failure_holds_lock_and_retry_never_rescues_smoke(tmp_path):
    d=Driver(cleanup_fail=True);result=r.execute(d,tmp_path)
    assert not result['passed'] and result['lock_released']
    assert d.calls[-4:]==['cleanup-1','operator-recovery','cleanup-2','release']
    assert len(result['cleanup_attempts'])==2


def test_cleanup_exception_also_retains_lock(tmp_path):
    d=Driver();base=d.cleanup
    def cleanup(attempt):
        if attempt==1:raise OSError('cleanup failure')
        return base(attempt)
    d.cleanup=cleanup;result=r.execute(d,tmp_path)
    assert 'operator-recovery' in d.calls and not result['passed'] and result['lock_released']


def test_stop_failure_does_not_skip_serial_or_restore(tmp_path,monkeypatch):
    live=r.Live({},tmp_path);live.touched=True;calls=[]
    live.command=lambda *_:None
    def stop_timeout(**kwargs):raise subprocess.TimeoutExpired('owned bot',20)
    live.bot=SimpleNamespace(pid=123,poll=lambda:0,wait=stop_timeout)
    live.board=SimpleNamespace(close=lambda:calls.append('serial-close'))
    def install(label):calls.append(label);live.board=SimpleNamespace(close=lambda:calls.append('restored-close'))
    live.install=install;monkeypatch.setattr(r,'port_free',lambda:True)
    result=live.cleanup(1)
    assert result['errors'] and result['restored_elf_sha256']==r.NORMAL_HASH
    assert not result['owned_bot_stopped'] and calls==['serial-close','restore-1','restored-close']


def test_failed_identity_check_never_flashes_unknown_board(tmp_path,monkeypatch):
    live=r.Live({},tmp_path);live.install=lambda *_:pytest.fail('must not flash unidentified board')
    monkeypatch.setattr(r,'port_free',lambda:True)
    result=live.cleanup(1)
    assert result['device_untouched'] and result['restored_elf_sha256'] is None and not result['errors']


def test_environment_overrides_and_clears_campaign_hooks(tmp_path,monkeypatch):
    for k in ('ECHOEAR_AEC_EVALUATION_DIR','RTP_EVIDENCE_DIR','DEMO_TTS_TRIM_LEADING','DG_BENCH_LOCK_FD'):
        monkeypatch.setenv(k,'bad')
    monkeypatch.setenv('DEEPGRAM_API_KEY','fixture-secret')
    env=r.child_environment(r.PUBLIC_ENV,tmp_path)
    assert env['ECHOEAR_AEC_FULL_DUPLEX']==env['ECHOEAR_LIVENESS_SECONDS']=='0'
    assert env['DEEPGRAM_API_KEY']=='fixture-secret' and env['DEMO_TTS_TRIM_LEADING']=='0'
    assert not any(k in env for k in ('ECHOEAR_AEC_EVALUATION_DIR','RTP_EVIDENCE_DIR','DG_BENCH_LOCK_FD'))


@pytest.mark.parametrize('marker',['absent','directory','symlink'])
def test_launch_refuses_without_regular_marker_before_cd_or_uv(tmp_path,monkeypatch,marker):
    declaration=tmp_path/'declaration.json';declaration.write_text('{}')
    go=tmp_path/'USER-GO'
    if marker=='directory':go.mkdir()
    if marker=='symlink':go.symlink_to(declaration)
    monkeypatch.setattr(r,'ROOT',tmp_path/'missing-worktree')
    script=tmp_path/'launch.sh';script.write_text(r.launch_script(declaration,r.sha(declaration),tmp_path/'out'))
    result=subprocess.run(['bash',str(script)],capture_output=True,text=True)
    assert result.returncode==2 and 'USER-GO required' in result.stderr and 'cd:' not in result.stderr


def test_runner_cannot_bypass_marker(tmp_path,monkeypatch):
    declaration=tmp_path/'declaration.json';declaration.write_text('{}')
    monkeypatch.setattr(r,'image_record',lambda:pytest.fail('marker must precede further validation'))
    with pytest.raises(ValueError,match='USER-GO'):r.validate(declaration,r.sha(declaration),True)
    with pytest.raises(ValueError,match='hash mismatch'):r.validate(declaration,'bad',True)


def test_policy_matches_old_firmware_liveness_compatibility():
    from demo_liveness import cadence,Liveness
    # Decision 141 removed the bot's duplex override. This frozen 9936da45 plan still sets it, so a live
    # rollback-01 stops at bot start (demo_bot refuses the variable) instead of running another policy.
    assert r.PUBLIC_ENV['ECHOEAR_AEC_FULL_DUPLEX']=='0'
    watchdog=Liveness(None,None,period=cadence(r.PUBLIC_ENV['ECHOEAR_LIVENESS_SECONDS']))
    watchdog.start();assert watchdog.tasks==[]


def test_decision_141_refuses_fresh_rollback_and_mute_runs_before_validation(monkeypatch):
    # Astra decision-141 review #2: refused before any declaration validation, port check or bench access.
    for name in ('validate','validate_mute','mute_plan'):
        monkeypatch.setattr(r,name,lambda *a,**k:pytest.fail('refusal must come first'))
    for call in (lambda:r.prepare('x'),lambda:r.prepare_mute('x'),lambda:r.run('x','y'),lambda:r.run_mute('x','y')):
        with pytest.raises(RuntimeError,match='pre-decision-141'):call()


def test_prepare_and_validate_only_host_files(tmp_path,monkeypatch):
    monkeypatch.setattr(r,'refuse_pre_141',lambda:None)   # the pre-141 prepare logic itself stays tested
    root=tmp_path/'repo';(root/'hil').mkdir(parents=True);(root/'server').mkdir();(root/'docs').mkdir()
    for name in ('hil/echoear_rollback.py','server/demo_bot.py','server/uv.lock','server/pyproject.toml','docs/echoear-rollback-01.md'):
        (root/name).write_text('fixture')
    monkeypatch.setattr(r,'ROOT',root);monkeypatch.setattr(r,'BASE',tmp_path/'evidence')
    monkeypatch.setattr(r,'head',lambda:'a'*40);monkeypatch.setattr(r,'image_record',lambda:dict(elf_sha256=r.NORMAL_HASH))
    original=r.subprocess.check_output
    monkeypatch.setattr(r.subprocess,'check_output',lambda args,**kw: '' if args[:2]==['git','status'] else original(args,**kw))
    bundle=tmp_path/'bundle';r.prepare(bundle);p=r.validate(bundle/'declaration.json')
    assert p['status']=='draft-not-authorized' and p['final_image']==p['image']
    assert not (bundle/'USER-GO').exists() and not (r.BASE/'rollback-01').exists()
    (root/'server/uv.lock').write_text('changed')
    with pytest.raises(ValueError,match='uv.lock'):r.validate(bundle/'declaration.json')


@pytest.mark.parametrize('event',[dict(kind='gap'),dict(kind='pipeline-error'),dict(frame='FatalErrorFrame')])
def test_recorder_gap_or_pipeline_failure_cannot_pass(event):
    assert not r.turn_witness(rows()+[event],lines())['passed']


def test_synthetic_go_launches_only_pinned_runner_command(tmp_path,monkeypatch):
    root=tmp_path/'root';(root/'server').mkdir(parents=True);monkeypatch.setattr(r,'ROOT',root)
    declaration=tmp_path/'declaration.json';declaration.write_text('{}')
    (tmp_path/'USER-GO').write_text('synthetic host test only')
    bin=tmp_path/'bin';bin.mkdir();log=tmp_path/'argv'
    fake=bin/'uv';fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CALL_LOG"\n');fake.chmod(0o755)
    script=tmp_path/'launch.sh';script.write_text(r.launch_script(declaration,r.sha(declaration),tmp_path/'out'))
    completed=subprocess.run(['bash',str(script)],env=dict(os.environ,PATH=str(bin)+':'+os.environ['PATH'],CALL_LOG=str(log)),capture_output=True,text=True)
    assert completed.returncode==0,completed.stderr
    args=log.read_text().splitlines()
    assert args[-5:]==['../hil/echoear_rollback.py','run',str(declaration),'--declaration-sha256',r.sha(declaration)]
    assert '--frozen' in args and '--offline' in args and not (tmp_path/'out').exists()


def test_stop_pattern_matches_only_declared_python_child():
    import re
    pattern=r.stop_pattern('/f/rollback-01/declaration.json','abc123')
    argv='python ../hil/echoear_rollback.py run /f/rollback-01/declaration.json --declaration-sha256 abc123'
    assert re.fullmatch(pattern,argv)
    assert not re.search(pattern,'uv run '+argv)
    assert not re.search(pattern,argv.replace('abc123','def456'))


def test_stop_before_first_write_is_not_completed_playback():
    events=rows();events[2]['monotonic_ns']=2
    assert not r.turn_witness(events,lines())['passed']


RETAINED_SERIAL=Path('/home/khkramer/src/evidence/echoear/B/item2-final-master-9bc2d42/a6_echo_stress/attempt-1/serial.log')
RETAINED_SERIAL_SHA256='253b51ed9182f5b9a68a7b06fdf1f3981ac8573d6041a6dcaa5a4266c670c495'


def test_retained_9936da45_raw_perf_and_gate_lines_are_supported():
    # Raw old-image evidence, not a fabricated newer PERF or snapshot helper's
    # optional AUDIO augmentation. Server events are synthetic interface fixtures;
    # this regression claims no historical conversation/rollback acceptance.
    assert r.sha(RETAINED_SERIAL)==RETAINED_SERIAL_SHA256
    serial=RETAINED_SERIAL.read_text().splitlines()
    from echoear_aec_bench import HISTORICAL_NORMAL_9936DA45_HASH   # gap 3: retained 9bc2d42 evidence
    assert any('elf_sha256='+HISTORICAL_NORMAL_9936DA45_HASH in line for line in serial)
    perfs=[(i,json.loads(line[5:])) for i,line in enumerate(serial) if line.startswith('PERF ')]
    assert len(perfs)==5
    a,b=perfs[:2]
    assert (a[0]+1,b[0]+1)==(2584,3085) and 'audio' not in a[1] and 'audio' not in b[1]
    result=r.counter_check(a[1],b[1])
    assert result['passed'] and set(result['values'])==set(r.POLICY['counter_paths'])
    assert tuple(r.POLICY['counter_paths'])==r.COUNTER_PATHS
    assert len(r.COUNTER_PATHS)==7 and not any(k.startswith('audio.') for k in r.COUNTER_PATHS)
    # The actual later queue-drop increment remains a failure, not a schema waiver.
    bad=r.counter_check(b[1],perfs[-1][1])
    assert not bad['passed'] and bad['values']['pcm_queue_drops']==[0,1]
    lines=[(i,0,line) for i,line in enumerate(serial) if a[0]<=i<=b[0]]
    witness=r.turn_witness(rows(),lines)
    assert witness['passed'] and witness['gate_closed_samples']==177 and witness['gate_reopened']
    assert not r.turn_witness([],lines)['passed'] # gate logs alone never prove a conversation


@pytest.mark.parametrize('path',r.COUNTER_PATHS)
@pytest.mark.parametrize('bad',[None,-1,True,1,'0'])
def test_each_declared_legacy_counter_fails_for_missing_invalid_or_changed_value(path,bad):
    a=counters();b=copy.deepcopy(a);target=b;keys=path.split('.')
    for key in keys[:-1]:target=target[key]
    if bad is None:del target[keys[-1]]
    else:target[keys[-1]]=bad
    assert not r.counter_check(a,b)['passed']


@pytest.mark.parametrize('hour,minute,allowed',[(2,29,True),(2,30,False),(2,40,False),(3,0,False),(4,9,False),(4,10,True)])
def test_start_window_has_reviewed_cleanup_margin(hour,minute,allowed):
    if allowed:r.check_start_window(hour,minute)
    else:
        with pytest.raises(ValueError,match='02:30'):r.check_start_window(hour,minute)
