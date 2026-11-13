"""Host-only F11 protocol, ledger and lifecycle controls; no bench or serial."""
import copy
from contextlib import contextmanager,nullcontext
from pathlib import Path
import tempfile
from types import SimpleNamespace
import json

import pytest
import p4_touch_verify as touch
from test_p4_parity_fix1 import copy_restore,put


def state(**changes):
    row=dict(initialized=True,enabled=True,phase=1,generation=1,pending=False,
        requests=0,request_rejected=0,phase_ignored=0,driver={k:0 for k in touch.DRIVER})
    row['driver']['reads']=5
    for key,value in changes.items():
        if key in touch.DRIVER:row['driver'][key]=value
        else:row[key]=value
    return row


class ClockIO:
    def __init__(self,out,board='echoear',go_at=2,events=()):
        self.out=Path(out);self.out.mkdir(parents=True,exist_ok=True);self.board=board
        self.go_at=go_at;self.events=events;self.now=0;self.messages=[];self.states=[];self.last_gen=1
        self.restarts=[];self.on_restart=None
        self.raw=self.out/'serial.log';self.raw.write_text('Host-only synthetic touch fixture\n')
    def clock(self):return self.now
    def sleep(self,seconds):
        assert 0<seconds<=touch.POLL
        self.now+=seconds
        if self.go_at is not None and self.now>=self.go_at and not (self.out/'GO').exists():
            (self.out/'GO').write_text('coordinator confirmed ready\n')
    def status(self):
        value=state()
        for at,row in self.events:
            if self.now>=at:value=copy.deepcopy(row)
        if self.board=='p4xc':value['driver']['reads']+=50*int(self.now)
        with self.raw.open('a') as stream:
            if value['generation']!=self.last_gen:stream.write('STATE Ready gen='+str(value['generation'])+'\n')
            stream.write('DEMO_TOUCH_STATUS '+json.dumps(value)+'\n')
        self.last_gen=value['generation'];self.states.append((self.now,copy.deepcopy(value)));return value
    def mark(self):return dict(cursor=len(self.states),byte_offset=self.raw.stat().st_size)
    def serial(self,mark):return self.raw.read_bytes()[mark['byte_offset']:].decode().splitlines()
    def emit(self,text,**kwargs):self.messages.append(text)
    def restart(self):
        self.restarts.append(self.now)
        if self.on_restart:self.on_restart()
    def observe(self):
        return touch.observe(self.out,self.board,self.status,self.mark,self.serial,
            restart=self.restart,clock=self.clock,sleep=self.sleep,emit=self.emit)


@pytest.mark.parametrize('board',touch.BOARDS)
def test_go_gate_rebases_premature_taps_and_full_window(board,tmp_path):
    early=state(taps=2,double_taps=1,requests=1,generation=2)
    io=ClockIO(tmp_path/'run',board,go_at=5,events=[(2,early)])
    result=io.observe()
    assert result['before']['driver']['double_taps']==1 and result['delta']['double_taps']==0
    assert result['touch_verified'] is False and result['retryable'] is True
    assert result['opened_at']==5 and result['elapsed_s']==600 and io.now==605
    assert result['contact_precheck']['detected'] is False
    rows=[json.loads(l) for l in (io.out/'status-timeline.jsonl').read_text().splitlines()]
    assert any(r['phase']=='waiting-for-go' and r['status']['driver']['double_taps']==1 for r in rows)
    assert not any(r['phase']=='window' and r['at']<5 for r in rows)


@pytest.mark.parametrize('board',touch.BOARDS)
def test_single_contact_precheck_then_first_real_double_stops_early(board,tmp_path):
    io=ClockIO(tmp_path/'run',board,events=[(3,state(taps=1)),
        (5,state(taps=3,double_taps=1,requests=1,generation=2)),
        (8,state(taps=5,double_taps=2,requests=2,generation=3))])
    result=io.observe()
    assert result['status']=='passed' and result['touch_verified'] and not result['retryable']
    assert result['elapsed_s']==3 and io.now==5 and not result['window_complete']
    assert result['contact_precheck']['standalone_single_observed'] and result['witness']['physical_double_taps']==1
    assert any('CONTACT PRE-CHECK' in m for m in io.messages)
    assert result['after']['driver']['double_taps']==1


def test_ready_marker_precedes_go_and_does_not_start_timer(tmp_path):
    io=ClockIO(tmp_path/'run',go_at=20)
    original_sleep=io.sleep
    def sleep(seconds):
        assert (io.out/'ready-for-go.json').is_file()
        if io.now<19:assert not (io.out/'window-opened.json').exists()
        original_sleep(seconds)
    io.sleep=sleep;result=io.observe()
    assert result['opened_at']==20 and io.now==620
    assert touch.load(io.out/'ready-for-go.json')['wait_seconds']==900


def test_no_go_times_out_unverified_without_retry(tmp_path):
    io=ClockIO(tmp_path/'run',go_at=None);result=io.observe()
    assert io.now==900 and result['status']=='go-timeout' and not result['retryable']
    assert not result['go_observed'] and not result['touch_verified']
    assert not (io.out/'window-opened.json').exists()


def test_early_go_file_is_refused(tmp_path):
    io=ClockIO(tmp_path/'run');(io.out/'GO').touch()
    with pytest.raises(ValueError,match='only after ready'):io.observe()


@pytest.mark.parametrize('changes',[dict(taps=1),dict(ignored=1),dict(rejected=1),
    dict(interrupts=1),dict(reads=6),dict(coalesced=1)])
def test_any_contact_or_s3_hardware_activity_blocks_absence_retry(changes,tmp_path):
    io=ClockIO(tmp_path/'run',events=[(3,state(**changes))]);result=io.observe()
    assert not result['touch_verified'] and not result['retryable']
    assert result['contact_precheck']['activity']


def test_p4_poll_reads_are_not_contact_evidence(tmp_path):
    io=ClockIO(tmp_path/'run','p4xc');result=io.observe()
    assert result['delta']['reads']>29000 and result['retryable'] and not result['contact_precheck']['activity']


@pytest.mark.parametrize('change',['injection','read-error','counter-reset','disabled'])
def test_invalid_window_never_becomes_a_zero_contact_retry(change,tmp_path):
    changes=dict(injected=1) if change=='injection' else dict(read_errors=1) if change=='read-error' else (
        dict(reads=0) if change=='counter-reset' else dict(enabled=False))
    io=ClockIO(tmp_path/'run',events=[(3,state(**changes))])
    with pytest.raises(ValueError):io.observe()


@pytest.mark.parametrize('change',['no-ready','no-request','rejected','extra-double'])
def test_existing_f11_witness_remains_strict(change,tmp_path):
    row=state(taps=2,double_taps=1,requests=1,generation=2)
    if change=='no-ready':row['generation']=1
    if change=='no-request':row.update(requests=0,generation=1)
    if change=='rejected':row['request_rejected']=1
    if change=='extra-double':row['driver']['double_taps']=2
    io=ClockIO(tmp_path/'run',events=[(3,row)]);result=io.observe()
    assert result['touch_verified'] is False and not result['retryable']


@pytest.fixture
def plan():
    touch.ROOT.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='host-test-',dir=touch.ROOT) as name:
        root=Path(name);path=root/'plan.json'
        identity=touch.PIN/'coordinator-run-1/smoke-echoear-normal/identity.json'
        touch.write_new(path,touch.create_plan(root,identity));yield path,root


def retained(plan_file,root,board='p4xc',events=(),go_at=2,change=None,renew=False):
    out=root/board/'attempt-01';touch.register(plan_file,board,out)
    io=ClockIO(out,board,go_at=go_at,events=events)
    if renew:
        def restart():
            if len(io.restarts)>1:io.events=[*io.events,(io.now,state(generation=3))]
        io.on_restart=restart
    result=io.observe()
    restored=copy_restore(out,board,'demo');p=touch.load(plan_file)
    result.update(restored,kind='user-paced-f11-result',acquisition_passed=True,cleanup_errors=[],
        plan_file=str(plan_file),plan_sha256=touch.digest(plan_file),identity=p['product_identity'],
        elf_sha256=p['images'][board]['elf_sha256'],qualification=False)
    if change=='restore':result['restore']['clean']=False;put(out/'restore.json',result['restore'])
    if change=='error':result['error']='reader exited'
    if change=='short':result['closed_at']-=1;result['elapsed_s']-=1
    put(out/'touch.json',result);touch.finish(plan_file,out)
    return out,io,result


def test_own_plan_pins_sources_and_both_restore_images(plan):
    path,root=plan;p=touch.checked_plan(path)
    assert set(p['images'])==set(touch.BOARDS) and p['policy']['window_seconds']==600 and p['policy']['wait_seconds']==900
    assert p['restore_record_sha256']==touch.RESTORE_SHA256 and p['s3_restore']['elf_sha256'].startswith('55152f25')
    with pytest.raises(ValueError):touch.create_plan(touch.ROOT,p['product_identity_file'])
    value=touch.load(path);value['policy']['window_seconds']=45;put(path,value)
    with pytest.raises(ValueError,match='changed'):touch.checked_plan(path)


@pytest.mark.parametrize('board',touch.BOARDS)
def test_only_one_retry_after_sealed_full_zero_contact_window(plan,board):
    path,root=plan;out,_,_=retained(path,root,board)
    assert touch.retry_eligible(touch.load(path),out)
    second=out.parent/'attempt-02';touch.register(path,board,second);second.mkdir();put(second/'touch.json',dict(acquisition_passed=False,error='second failed'))
    touch.finish(path,second)
    with pytest.raises(ValueError,match='cap'):touch.register(path,board,out.parent/'attempt-03')
    with touch.journal(path) as (_,_,rows,registered,finished):
        assert len(registered)==2 and len(finished)==2 and len(rows)==4
    assert (out/'touch.json').exists() and (second/'touch.json').exists()


@pytest.mark.parametrize('change',['contact','go-timeout','restore','error','short','tamper'])
def test_retry_refuses_contact_incomplete_or_dirty_evidence(plan,change):
    path,root=plan;events=[(3,state(taps=1))] if change=='contact' else ()
    out,_,_=retained(path,root,events=events,go_at=None if change=='go-timeout' else 2,change=change)
    if change=='tamper':(out/'after.json').write_text('{}')
    with pytest.raises(ValueError):touch.register(path,'p4xc',out.parent/'attempt-02')


def test_unfinished_and_unregistered_attempts_cannot_be_hidden(plan):
    path,root=plan;out=root/'echoear/attempt-01';touch.register(path,'echoear',out);out.mkdir(parents=True)
    with pytest.raises(ValueError,match='not closed'):touch.register(path,'echoear',out.parent/'attempt-02')
    hidden=root/'hidden/attempt-01';hidden.mkdir(parents=True)
    with pytest.raises(ValueError,match='unregistered'):touch.register(path,'p4xc',root/'p4xc/attempt-01')


def test_malformed_original_is_sealed_as_incomplete_and_cannot_retry(plan):
    path,root=plan;out=root/'echoear/attempt-01';touch.register(path,'echoear',out);out.mkdir(parents=True);(out/'touch.json').write_text('[]')
    assert touch.finish(path,out)['disposition']=='incomplete'
    with pytest.raises(ValueError):touch.register(path,'echoear',out.parent/'attempt-02')


def test_host_refusal_is_before_registration(plan,monkeypatch):
    import p4_parity_preflight
    path,root=plan;p=touch.load(path)
    def refuse(*args):raise RuntimeError('no inherited bench lock')
    monkeypatch.setattr(p4_parity_preflight,'host_checks',refuse)
    with pytest.raises(RuntimeError,match='bench lock'):
        touch.main(['run','--plan',str(path),'--board','echoear','--out',str(root/'echoear/attempt-01'),
            '--bot-dir',str(root/'bot'),'--bot-identity',p['product_identity_file']])
    assert not (root/'attempts.jsonl').exists()


@pytest.mark.parametrize('failure',[None,'pass','window','cleanup','hold-cleanup','restore','timeline','panic'])
def test_live_lifecycle_always_disables_holds_restores_and_releases_signals(plan,monkeypatch,failure):
    import board_smoke as smoke
    import p4_product,p4_step14
    path,root=plan;p=touch.load(path);out=root/'echoear/attempt-01';order=[]
    args=SimpleNamespace(plan=path,out=out,board='echoear')
    class Guard:
        success=False;record=dict(clean=failure!='restore')
        def __enter__(self):order.append('guard');return self
        def touch(self):order.append('touch')
        def __exit__(self,*args):order.append('restore')
        def release_signals(self):order.append('release')
    class Board:
        cursor=1
        def command(self,text,*args):
            order.append(text)
            if (failure=='cleanup' and text=='touch-enable 0') or (failure=='hold-cleanup' and text=='demo hold'):
                raise RuntimeError('cleanup failed')
    class IO:
        def __init__(self,*args):pass
        def preflight_tools(self):pass
        def preflight_restore(self):pass
        def hold(self):return Guard()
        def flash(self,*args):order.append('flash')
        @contextmanager
        def open(self):
            (out/'serial.log').write_text('Synthetic lifecycle fixture\n')
            try:yield Board()
            finally:
                order.append('serial-close')
                if failure=='panic':
                    with (out/'serial.log').open('a') as stream:stream.write('Guru Meditation Error: late cleanup panic\n')
        def boot(self,*args):order.append('boot')
        def provision(self,*args):order.append('provision')
        def begin(self,*args):order.append('live')
    def observe(*args,**kwargs):
        order.append('observe');kwargs['restart']()
        if failure=='timeline':
            (out/'status-timeline.jsonl').write_text('{')
            raise KeyboardInterrupt('test abort with truncated timeline')
        if failure=='window':raise KeyboardInterrupt('test abort')
        return dict(touch_verified=failure=='pass',retryable=failure!='pass',status='passed' if failure=='pass' else 'unverified-no-contact')
    monkeypatch.setattr(smoke,'LiveIO',IO);monkeypatch.setattr(touch,'observe',observe)
    monkeypatch.setattr(p4_step14,'restoring',lambda *args:nullcontext())
    monkeypatch.setattr(p4_product,'candidate_storage',lambda *args:nullcontext())
    code=touch.live(args,p)
    assert code==(2 if failure is None else 0 if failure=='pass' else 1)
    assert 'touch-enable 0' in order and order.index('serial-close')<order.index('restore')<order.index('release')
    assert 'demo hold' in order
    data=touch.load(out/'touch.json');assert data['acquisition_passed']==(failure in (None,'pass'))
    if failure not in (None,'pass'):assert not data['retryable'] and not data['touch_verified']



@pytest.mark.parametrize('go_at',[900,901])
def test_go_wait_boundary_is_exactly_fifteen_minutes(tmp_path,go_at):
    io=ClockIO(tmp_path/'run',go_at=go_at);result=io.observe()
    assert result['go_observed']==(go_at==900)
    assert io.now==(1500 if go_at==900 else 900)


def test_offline_cli_never_needs_a_bench(plan,monkeypatch,capsys):
    import boards
    path,root=plan
    def forbidden():raise AssertionError('offline CLI attempted bench access')
    monkeypatch.setattr(boards,'require_bench',forbidden)
    assert touch.main(['check','--plan',str(path)])==0
    assert touch.main(['status','--plan',str(path)])==0
    assert 'no hardware accessed' in capsys.readouterr().out


def test_ledger_chain_and_sealed_original_cannot_be_rewritten(plan):
    path,root=plan;out,_,_=retained(path,root)
    ledger=root/'attempts.jsonl';rows=ledger.read_text().splitlines();value=json.loads(rows[0]);value['attempt']=2
    rows[0]=json.dumps(value);ledger.write_text('\n'.join(rows)+'\n')
    with pytest.raises(ValueError,match='chain'):touch.main(['status','--plan',str(path)])


@pytest.mark.parametrize('board',touch.BOARDS)
def test_go_refreshes_live_after_long_wait(board,tmp_path):
    io=ClockIO(tmp_path/'run',board,go_at=800,events=[(300,state(phase=2))])
    def restart():
        io.events=[*io.events,(io.now,state(generation=3))]
    io.on_restart=restart
    result=io.observe()
    assert io.restarts==[800] and result['before']['generation']==3
    assert result['opened_at']==800 and result['closed_at']==1400 and result['retryable']


@pytest.mark.parametrize('board',touch.BOARDS)
@pytest.mark.parametrize('idle_kind',['closed','new-generation'])
def test_idle_generation_change_rebaselines_and_can_pass_without_extending_window(board,tmp_path,idle_kind):
    io=ClockIO(tmp_path/'run',board,events=[(300,state(phase=2) if idle_kind=='closed' else state(generation=2)),
        (350,state(taps=2,double_taps=1,requests=1,generation=5))])
    def restart():
        if len(io.restarts)>1:io.events=sorted([*io.events,(io.now,state(generation=3))],key=lambda e:e[0])
    io.on_restart=restart;result=io.observe()
    assert io.restarts==[2,300] and result['touch_verified'] and result['closed_at']==350
    assert result['witness_before']['generation']==3 and result['witness']['generation_before']==3
    assert result['global_witness']['generation_before']==1
    assert len(result['baselines'])==2 and result['opened_at']==2
    assert (io.out/'baseline-001.json').is_file() and (io.out/'baseline-002.json').is_file()


def test_idle_renewal_time_counts_against_original_deadline(tmp_path):
    io=ClockIO(tmp_path/'run',events=[(300,state(phase=2))])
    def restart():
        if len(io.restarts)>1:
            io.now+=30;io.events=[*io.events,(io.now,state(generation=3))]
    io.on_restart=restart;result=io.observe()
    assert result['opened_at']==2 and result['closed_at']==602 and result['elapsed_s']==600
    assert result['retryable'] and len(result['baselines'])==2


def test_rebase_cannot_hide_contact_or_earn_absence_retry(tmp_path):
    io=ClockIO(tmp_path/'run',events=[(3,state(taps=1)),(300,state(taps=1,phase=2))])
    def restart():
        if len(io.restarts)>1:io.events=[*io.events,(io.now,state(taps=1,generation=3))]
    io.on_restart=restart;result=io.observe()
    assert result['contact_precheck']['detected'] and not result['retryable'] and not result['touch_verified']


def test_rebase_cannot_discard_an_unaccepted_double_tap(tmp_path):
    io=ClockIO(tmp_path/'run',events=[(300,state(taps=2,double_taps=1,phase=2))])
    with pytest.raises(ValueError,match='cannot be discarded'):io.observe()


@pytest.mark.parametrize('board',touch.BOARDS)
def test_sealed_absence_retry_replays_retained_idle_baselines(plan,board):
    path,root=plan
    out,io,result=retained(path,root,board,events=[(300,state(phase=2))],renew=True)
    assert io.restarts==[2,300] and result['elapsed_s']==600 and len(result['baselines'])==2
    assert touch.retry_eligible(touch.load(path),out)
    touch.register(path,board,out.parent/'attempt-02')
    baseline=touch.load(out/'baseline-002.json');baseline['status']['generation']+=1
    put(out/'baseline-002.json',baseline)
    assert not touch.retry_eligible(touch.load(path),out)
