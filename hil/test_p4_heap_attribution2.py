"""Round-2 diagnosis controls; virtual clock, no hardware or service access."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import p4_heap_attribution2 as h
from make_bundle import digest
from test_p4_heap_attribution import trace as old_trace
from test_p4_liveness import sample

TCP=dict(TCP_MSL_ms=60000,TIME_WAIT_ms=120000,MEMP_NUM_TCP_PCB=16)

def traced(window):
    t=old_trace(window)
    for row in (t['start'],t['stop']):row.update(new_caller_capacity=h.NEW_CAPACITY,new_caller_overflow=False)
    b=t['blocks'][0];b.update(external=False,new_caller=0,bytes=208)
    other=dict(b,index=1,address=b['address']+64,bytes=32,callers=[0x40000300]+[0]*7,new_caller=0x48000200)
    t['blocks'].append(other);t['stop']['records']=2
    return t

def fake_io(tmp_path,board,cycles,settle,fail=None,overflow_window=None):
    state=SimpleNamespace(cycle=0,clock=0.,idle=True,running=False,held_at=0.,commands=[],events=[],tail_snapshots=0)
    def expired():return state.cycle==cycles and state.clock-state.held_at>=120
    class Board:
        def __init__(self):self.lines=[]
        @property
        def cursor(self):return len(self.lines)
        def lines_since(self,at):return self.lines[at:]
        def emit(self,row):self.lines.append((self.cursor,state.clock,'HEAP_ATTRIBUTION '+json.dumps(row,separators=(',',':'))))
        def command(self,text,*args):
            state.commands.append(text)
            state.events.append(('command',text,state.running,state.cycle))
            if text=='demo run':state.idle=False;state.cycle+=1;return
            if text=='demo hold':
                if not state.idle:state.held_at=state.clock
                state.idle=True;return
            assert board=='p4xc' and state.idle
            action=text.split()[1];t=traced(state.cycle+int(action=='start'))
            if action=='init':self.emit(dict(t['start'],kind='init'))
            elif action=='start':state.running=True;self.emit(t['start'])
            elif action=='stop':
                if not fail and state.cycle==cycles:assert state.clock-state.held_at>=settle
                if state.cycle==overflow_window:t['stop']['new_caller_overflow']=True
                state.running=False;self.emit(t['stop'])
            elif action=='page':
                for b in t['blocks']:self.emit(b)
                self.emit(dict(schema=1,kind='page',id=state.cycle,first=0,next=2))
            elif action=='tcp':
                self.emit(dict(schema=1,kind='tcp',id=state.cycle,active=0,bound=0,
                    time_wait=0 if expired() else min(state.cycle*2,16),tcp_msl_ms=60000,time_wait_ms=120000,memp_num_tcp_pcb=16))
        def wait_for(self,pattern,*args,since=0):
            if pattern==r'\bPHASE closed\b':assert state.idle;return 'PHASE closed'
            import re
            return next(line for _,_,line in self.lines_since(since) if re.search(pattern,line))
        def wait_for_entry(self,*args,**kwargs):
            if fail=='ready':raise TimeoutError('no Ready')
            return self.cursor,state.clock,'STATE Ready gen='+str(state.cycle)
    device=Board()
    class IO:
        args=SimpleNamespace(board=board)
        @contextmanager
        def open(self):yield device
        def boot(self,*args):pass
        def provision(self,*args):pass
        def clock(self):return state.clock
        def pause(self,board,seconds):state.clock+=seconds
        def snapshot(self,board):
            state.events.append(('snapshot',None,state.running,state.cycle))
            if state.cycle==cycles and state.idle:
                if board_name=='p4xc':assert state.running,'trace stopped before tail sampling'
                state.tail_snapshots+=1
                if fail=='tail' and state.tail_snapshots>3:raise TimeoutError('tail sampling failure')
            free=350000 if board_name=='echoear' else 350000-(0 if expired() else min(state.cycle,8)*416)
            row=sample(free);row['heap']['psram']=dict(free=12000000-state.cycle*(400 if board_name=='echoear' else 0))
            return row
    board_name=board
    args=SimpleNamespace(board=board,out=tmp_path,cycles=cycles,active_seconds=5,settle_seconds=settle)
    return args,IO(),state

def test_tcp_warmup_follows_init_and_precedes_baseline_and_every_trace(tmp_path):
    args,io,state=fake_io(tmp_path,'p4xc',2,0)
    raw=h.acquire(args,io)
    assert state.events[:3]==[('command','heap-attribution init',False,0),
        ('command','heap-attribution tcp',False,0),('snapshot',None,False,0)]
    first_start=next(i for i,event in enumerate(state.events) if event[1]=='heap-attribution start')
    assert first_start>2 and all(not event[2] for event in state.events[:first_start])
    assert raw['tcp_warmup']['id']==0
    assert json.loads((tmp_path/'tcp-warmup.json').read_text())==raw['tcp_warmup']

@pytest.mark.parametrize('board',['p4xc','echoear'])
@pytest.mark.parametrize('seconds',[0,150])
def test_twenty_cycles_and_final_settle_keep_trace_open_and_preserve_series(tmp_path,board,seconds):
    args,io,state=fake_io(tmp_path,board,20,seconds)
    raw=h.acquire(args,io)
    assert len(raw['cycles'])==20 and len(raw['final_settle']['points'])==1+seconds//5
    assert raw['final_settle']['points'][-1]['elapsed_seconds']>=seconds
    assert state.idle and not state.running
    if board=='p4xc':
        assert raw['cycles'][-1]['trace']['host_stop_seconds']>=raw['final_settle']['started_seconds']+seconds
        assert raw['cycles'][-1]['tcp']['time_wait']==16
        assert raw['final_settle']['points'][-1]['tcp']['time_wait']==(0 if seconds else 16)
    else:assert not any('heap-attribution' in command for command in state.commands)

@pytest.mark.parametrize('failure',['ready','tail'])
def test_diagnostic_failure_closes_session_and_trace(tmp_path,failure):
    args,io,state=fake_io(tmp_path,'p4xc',1,150,failure)
    with pytest.raises(TimeoutError):h.acquire(args,io)
    assert state.idle and not state.running

def recorded(tmp_path,board='p4xc',cycles=5,seconds=150,overflow_window=None):
    args,io,_=fake_io(tmp_path,board,cycles,seconds,overflow_window=overflow_window)
    raw=h.acquire(args,io);raw.update(acquisition_complete=True,requested_cycles=cycles,settle_seconds=seconds,
        restore=dict(clean=True,action='p4-product-of-record'))
    elf=tmp_path/'bound.elf';elf.write_bytes(b'ELF-bound-caller-decoding')
    image=dict(board=board,elf_sha256=digest(elf) if board=='p4xc' else h.base.REFERENCE,bundle=str(tmp_path),
        heap_tcp_values=TCP,heap_attribution=dict(round=2))
    (tmp_path/'image.json').write_text(json.dumps(image));(tmp_path/'acquisition.json').write_text(json.dumps(raw))
    return raw,elf

def symbolize(elf,pcs):
    assert 0x48000200 in pcs,'wrapper caller PC omitted from decoding'
    return {0x40000100:['tcp_alloc','lwip/tcp.c:1'],0x40000300:['operator new(unsigned int)','new_op.cc:1'],
            0x48000200:['performance_snapshot','main/performance.cpp:1']}

def test_new_callsite_is_decoded_independently_of_truncated_malloc_stack(tmp_path):
    raw,elf=recorded(tmp_path);result=h.analyze(tmp_path,elf,symbolize)
    row=result['cycles'][0];assert row['candidates']['internal']['console']==dict(bytes=32,blocks=1)
    assert row['blocks'][1]['new_caller_symbols']==['performance_snapshot','main/performance.cpp:1']
    assert result['time_wait_observation']['before']==10 and result['time_wait_observation']['after']==0
    assert result['settle_internal_recovered_bytes']>0 and not result['qualification'] and not result['passed']
    assert len(result['internal_free'])==6 and result['psram_plateau']['status']=='flat'
    elf.write_bytes(b'changed')
    with pytest.raises(ValueError,match='ELF hash'):h.analyze(tmp_path,elf,symbolize)

@pytest.mark.parametrize('frames',[
    ['__cxa_thread_atexit','atexit_thread.cc:1'],
    ['runtime_helper','/toolchain/libstdc++-v3/libsupc++/atexit_thread.cc:1'],
    ['runtime_helper','libsupc++.a:1'],
    ['std::string::_Rep::_S_create','cow-string-inst.cc:1'],
    ['operator new(unsigned int, std::nothrow_t const&)','new_opnt.cc:1']])
def test_cpp_runtime_caller_does_not_claim_to_identify_requester(tmp_path,frames):
    _,elf=recorded(tmp_path)
    def runtime_symbols(elf,pcs):return symbolize(elf,pcs)|{0x48000200:frames}
    result=h.analyze(tmp_path,elf,runtime_symbols)
    block=result['cycles'][0]['blocks'][1]
    assert block['new_caller_status']=='runtime-internal' and block['new_caller_symbols']==frames
    assert result['cycles'][0]['caller_attribution_valid']

@pytest.mark.parametrize('seconds',[0,150])
def test_trace_phase_only_claims_a_tail_when_one_was_requested(tmp_path,seconds):
    _,elf=recorded(tmp_path,seconds=seconds)
    result=h.analyze(tmp_path,elf,symbolize)
    assert [row['trace_phase'] for row in result['cycles']]==['after-cycle']*4+[
        'after-final-settle' if seconds else 'after-cycle']

@pytest.mark.parametrize('window',[1,20])
def test_caller_overflow_preserves_twenty_cycles_tail_and_other_windows(tmp_path,window):
    raw,elf=recorded(tmp_path,cycles=20,overflow_window=window)
    assert raw['cycles'][window-1]['caller_status']=='table-overflow'
    assert json.loads((tmp_path/f'cycle-{window:02d}.json').read_text())['caller_status']=='table-overflow'
    result=h.analyze(tmp_path,elf,symbolize)
    assert len(result['cycles'])==20 and len(result['internal_free'])==21
    assert result['final_settle']['points'][-1]['elapsed_seconds']>=150
    for row in result['cycles']:
        assert row['caller_attribution_valid']==(row['index']!=window)
    row=result['cycles'][window-1]
    assert row['caller_status']=='table-overflow'
    assert all(b['new_caller_status']=='invalid-table-overflow' for b in row['blocks'])
    assert row['candidates']['internal']['console']['bytes']==0
    assert row['candidates']['internal']['lwip-pcb-pbuf']['bytes']==208
    assert result['cycles'][1 if window==1 else 0]['blocks'][1]['new_caller_status']=='recorded-return-PC'
    assert not result['passed'] and not result['qualification']

@pytest.mark.parametrize('bad',['caller-missing','overflow-missing','overflow-untyped','heap-trace-overflow','foreign-tcp','short-tail','changed-duration','trace-stopped-early','replay','memory-class'])
def test_invalid_new_caller_and_settle_evidence_cannot_be_reported_as_complete(tmp_path,bad):
    raw,elf=recorded(tmp_path)
    if bad=='caller-missing':raw['cycles'][0]['trace']['blocks'][1].pop('new_caller')
    elif bad=='overflow-missing':raw['cycles'][0]['trace']['stop'].pop('new_caller_overflow')
    elif bad=='overflow-untyped':raw['cycles'][0]['trace']['stop']['new_caller_overflow']=1
    elif bad=='heap-trace-overflow':raw['cycles'][0]['trace']['stop']['overflow']=True
    elif bad=='foreign-tcp':raw['final_settle']['points'][-1]['tcp']['id']=4
    elif bad=='short-tail':raw['final_settle']['points'][-1]['elapsed_seconds']=149
    elif bad=='changed-duration':raw['final_settle']['requested_seconds']=0
    elif bad=='trace-stopped-early':raw['cycles'][-1]['trace']['host_stop_seconds']=raw['final_settle']['started_seconds']
    elif bad=='replay':raw['cycles'][2]['index']=2
    else:raw['cycles'][0]['trace']['blocks'][0]['external']=True
    (tmp_path/'acquisition.json').write_text(json.dumps(raw))
    with pytest.raises(ValueError):h.analyze(tmp_path,elf,symbolize)

def test_s3_reports_psram_decline_without_fake_ownership(tmp_path):
    recorded(tmp_path,'echoear');result=h.analyze(tmp_path)
    assert result['psram_plateau']['status']=='continued-decline' and result['internal_plateau']['status']=='flat'
    assert 'unavailable' in result['attribution'] and result['cycles']==[]

@pytest.mark.parametrize('cycles,seconds',[(21,150),(0,150),(20,-1),(20,float('nan')),(20,601)])
def test_diagnostic_limits_reject_invalid_settings(cycles,seconds):
    with pytest.raises(ValueError):h.settings(SimpleNamespace(cycles=cycles,settle_seconds=seconds,active_seconds=5))

def test_plateau_does_not_add_a_relaxed_pass_bound():
    assert h.plateau([1000,1000,1000,1000,1000])['status']=='flat'
    assert h.plateau([1000,990,980,970,960])['status']=='continued-decline'
    assert h.plateau([1000,1001,999,1000,1001])['status']=='not-established'

@pytest.mark.parametrize('field,value',[('id',True),('time_wait_ms',120000.0),('time_wait',-1),('tcp_msl_ms',0)])
def test_tcp_protocol_requires_typed_nonnegative_counters(field,value):
    row=dict(schema=1,kind='tcp',id=1,active=0,bound=0,time_wait=2,tcp_msl_ms=60000,time_wait_ms=120000,memp_num_tcp_pcb=16)
    row[field]=value
    with pytest.raises(ValueError):h.validate_tcp(row,1,TCP)

def test_probe_command_uses_build_flags_without_echoing_config(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    from p4_heap_tcp_values import probe_arguments
    assert probe_arguments('g++ -Iconfig -DKEEP=1 -MD -MT target -MF dep -o x.obj -c /repo/main/heap_tcp_state.cpp')==['g++','-Iconfig','-DKEEP=1']

@pytest.mark.parametrize('configured',[False,True])
def test_tcp_probe_temp_directory_respects_override_and_defaults_to_cache(tmp_path,monkeypatch,configured):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    import p4_heap_tcp_values as probe
    monkeypatch.setattr(Path,'home',lambda:tmp_path/'home')
    temporary=tmp_path/'configured' if configured else tmp_path/'home/.cache/gap3-tmp'
    if configured:monkeypatch.setenv('TMPDIR',str(temporary))
    else:monkeypatch.delenv('TMPDIR',raising=False)
    (tmp_path/'compile_commands.json').write_text(json.dumps([dict(file='/repo/main/heap_tcp_state.cpp',
        command='g++ -c /repo/main/heap_tcp_state.cpp',directory=str(tmp_path))]))
    calls=[]
    def fail_compile(command,**kwargs):
        source=Path(command[command.index('-c')+1]);assert source.parent.parent==temporary and source.exists()
        calls.append(command)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(probe.subprocess,'run',fail_compile)
    with pytest.raises(ValueError,match='compiler diagnostics suppressed'):probe.tcp_values(tmp_path)
    assert len(calls)==1 and temporary.is_dir() and not list(temporary.iterdir())

@pytest.mark.parametrize('failure',[None,'tail','abort','factory','overflow'])
def test_live_restores_and_releases_signals_after_tail_errors_or_abort(tmp_path,monkeypatch,failure):
    import boards,board_smoke,p4_product,p4_step14
    from contextlib import nullcontext
    monkeypatch.setattr(boards,'require_bench',lambda:0)
    monkeypatch.setattr(p4_step14,'restoring',lambda *args:nullcontext())
    monkeypatch.setattr(p4_product,'candidate_storage',lambda *args:nullcontext())
    manifest=dict(board='p4xc',profile='demo',aec_enabled=False,elf_sha256='a'*64,
        heap_attribution=dict(capacity=4096,depth=8,mode='LEAKS',isr='excluded',round=2,new_caller_capacity=8192))
    monkeypatch.setattr(h,'verify_bundle',lambda *args:(manifest,{}))
    identity=tmp_path/'identity.json';identity.write_text('{}')
    args=SimpleNamespace(board='p4xc',cycles=20,active_seconds=5,settle_seconds=150,bundle=tmp_path,
        reference_only=False,bot_identity=identity,out=tmp_path/'run',restore_record=None,restore_record_sha256=None)
    state=SimpleNamespace(touched=False,restored=False,released=False)
    class Hold:
        def __enter__(self):return self
        def touch(self):state.touched=True
        def __exit__(self,*args):state.restored=True;self.record=dict(clean=failure!='factory',action='p4-factory' if failure=='factory' else 'p4-product-of-record')
        def release_signals(self):assert state.restored;state.released=True
    class IO:
        def __init__(self,*args):pass
        def preflight_tools(self):pass
        def preflight_restore(self):pass
        def hold(self):return Hold()
        def flash(self,*args):assert state.touched
    monkeypatch.setattr(board_smoke,'LiveIO',IO)
    real_acquire=h.acquire
    def acquire(*args):
        if failure=='tail':raise TimeoutError('tail failed')
        if failure=='abort':raise KeyboardInterrupt('coordinator abort')
        if failure=='overflow':
            _,io,state=fake_io(args[0].out,'p4xc',20,150,overflow_window=1)
            raw=real_acquire(args[0],io)
            assert len(raw['cycles'])==20 and state.idle and not state.running
            return raw
        return dict(cycles=[])
    monkeypatch.setattr(h,'acquire',acquire)
    success=failure in (None,'overflow')
    assert h.live(args)==int(not success) and state.released
    assert json.loads((args.out/'acquisition.json').read_text())['acquisition_complete']==success

def test_round2_runbook_launches_use_checked_environment():
    import re,shlex
    from test_bench_launch_docs import validate
    text=Path(__file__).with_name('p4_heap_attribution2.md').read_text();count=0
    for line in text.splitlines():
        if line.startswith('python3 hil/bench.py '):validate(shlex.split(line.split(' || ')[0])[2:]);count+=1
    assert count==2 and '--settle-seconds 150' in text and '--cycles 20' in text
