"""Heap diagnosis controls: no board, socket, bot, flash or audio access."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import p4_heap_attribution as h
from make_bundle import digest
from test_p4_liveness import sample


def trace(window=1):
    start=dict(schema=1,kind='start',id=window,capacity=h.CAPACITY,depth=h.DEPTH,isr_allocations='excluded-PSRAM-buffer',
        before_free=350000,before_largest=249856,after_free=349488,after_largest=249856)
    stop=dict(start,kind='stop',records=1,overflow=False,high_water=3)
    block=dict(schema=1,kind='block',id=window,index=0,address=0x4ff10000+window*1024,
        bytes=512,internal=True,freed=False,callers=[0x40000100]+[0]*7)
    return dict(start=start,stop=stop,blocks=[block])


@pytest.mark.parametrize('bad',['overflow','missing','duplicate','foreign','order','depth','address','boolean','pc','count','high-water'])
def test_corrupt_or_incomplete_trace_cannot_claim_attribution(bad):
    t=trace()
    if bad=='overflow':t['stop']['overflow']=True
    elif bad=='missing':t['blocks']=[]
    elif bad=='duplicate':t['blocks']*=2;t['stop']['records']=2
    elif bad=='foreign':t['blocks'][0]['id']=2
    elif bad=='order':t['blocks'][0]['index']=1
    elif bad=='depth':t['blocks'][0]['callers'].pop()
    elif bad=='address':t['blocks'][0]['address']=0
    elif bad=='boolean':t['blocks'][0]['internal']=1
    elif bad=='pc':t['blocks'][0]['callers'][0]=-1
    elif bad=='count':t['stop']['records']=h.CAPACITY+1
    else:t['stop']['high_water']=0
    with pytest.raises(ValueError):h.validate_trace(t['start'],t['stop'],t['blocks'])


@pytest.mark.parametrize('symbol,expected',[
    ('esp_hosted/common/rpc/rpc_core.c','hosted-rpc-sdio'),('sdio_rx_buf_alloc','hosted-rpc-sdio'),
    ('wifi_remote/esp_wifi_remote.c','hosted-rpc-sdio'),('lwip/src/core/tcp.c','lwip-pcb-pbuf'),
    ('pbuf_alloc','lwip-pcb-pbuf'),('mbedtls_ssl_setup','mbedtls-dtls'),('esp_peer_open','esp-peer'),
    ('components/dg_display/runtime.cpp','display-touch'),('main/demo_touch.cpp','display-touch'),
    ('main/performance.cpp','console'),('opaque_vendor_binary','unclassified')])
def test_all_candidate_components_are_checked_without_guessing_unknown_symbols(symbol,expected):
    assert h.category(['heap_caps_malloc',symbol])==expected


def recorded(tmp_path,board='p4xc'):
    elf=tmp_path/'bound.elf';elf.write_bytes(b'bound ELF')
    image=dict(board=board,elf_sha256=h.REFERENCE if board=='echoear' else digest(elf),bundle=str(tmp_path))
    raw=dict(acquisition_complete=True,restore=dict(clean=True),baseline=sample(350000),
        cycles=[dict(index=1,settled=sample(349488),trace=None if board=='echoear' else trace())])
    (tmp_path/'image.json').write_text(json.dumps(image));(tmp_path/'acquisition.json').write_text(json.dumps(raw))
    return elf,raw


def test_analysis_binds_elf_and_exposes_coverage_and_unattributed_bytes(tmp_path):
    elf,_=recorded(tmp_path)
    out=h.analyze(tmp_path,elf,symbolizer=lambda *args:{0x40000100:['mbedtls_ssl_setup','ssl_tls.c:9']})
    assert out['net_decline_bytes']==512 and not out['qualification'] and not out['passed']
    assert out['cycles'][0]['candidates']['mbedtls-dtls']==dict(bytes=512,blocks=1)
    assert 'ISR allocations excluded' in out['attribution']
    elf.write_bytes(b'wrong ELF')
    with pytest.raises(ValueError,match='ELF hash'):h.analyze(tmp_path,elf)


def test_unknown_callers_are_not_labeled_as_a_clean_component(tmp_path):
    elf,_=recorded(tmp_path)
    out=h.analyze(tmp_path,elf,symbolizer=lambda *args:{0x40000100:['??','??:0']})
    assert out['cycles'][0]['candidates']['unclassified']['bytes']==512


def test_s3_contrast_explicitly_lacks_allocation_attribution(tmp_path):
    recorded(tmp_path,'echoear');out=h.analyze(tmp_path)
    assert 'unavailable' in out['attribution'] and all('unmeasured' in v for v in out['candidates'].values())
    assert out['cycles']==[] and not out['passed']


def test_symbolizer_batches_and_authenticates_pc_order(tmp_path):
    calls=[]
    def run(command,**kwargs):
        addresses=command[command.index('-e')+2:];calls.append(addresses)
        return SimpleNamespace(stdout=''.join(a+'\nfunc\nfile.cpp:2\n' for a in addresses))
    pcs=list(range(0x40000001,0x40000101))
    symbols=h.symbolize(tmp_path/'elf',pcs,run)
    assert len(symbols)==256 and [len(row) for row in calls]==[128,128]
    with pytest.raises(ValueError,match='incomplete symbolizer'):
        h.symbolize(tmp_path/'elf',pcs,lambda *a,**kw:SimpleNamespace(stdout='0x1\nwrong\nfile.cpp:1\n'))


@pytest.mark.parametrize('board',['p4xc','echoear'])
@pytest.mark.parametrize('failure',[None,'ready','page','overflow'])
def test_real_short_cycle_flow_exports_only_after_close_and_cleans_up(tmp_path,board,failure):
    state=SimpleNamespace(idle=True,running=False,cycle=0,clock=0,commands=[])
    class Board:
        def __init__(self):self.lines=[]
        @property
        def cursor(self):return len(self.lines)
        def lines_since(self,at):return self.lines[at:]
        def emit(self,row):self.lines.append((self.cursor,0,'HEAP_ATTRIBUTION '+json.dumps(row,separators=(',',':'))))
        def command(self,text,*args):
            state.commands.append(text)
            if text=='demo run':state.idle=False;state.cycle+=1;return
            if text=='demo hold':state.idle=True;return
            assert board=='p4xc' and state.idle,'no S3 command or active-session trace export'
            action=text.split()[1];t=trace(state.cycle if action!='start' else state.cycle+1)
            if action=='init':self.emit(dict(t['start'],kind='init'))
            elif action=='start':state.running=True;self.emit(t['start'])
            elif action=='stop':
                state.running=False
                if failure=='overflow':t['stop']['overflow']=True
                self.emit(t['stop'])
            elif action=='page':
                if failure!='page':self.emit(t['blocks'][0])
                self.emit(dict(schema=1,kind='page',id=t['start']['id'],first=0,next=1))
        def wait_for(self,pattern,*args,since=0):
            if pattern==r'\bPHASE closed\b':assert state.idle;return 'PHASE closed'
            import re
            return next(line for _,_,line in self.lines_since(since) if re.search(pattern,line))
        def wait_for_entry(self,*args,**kwargs):
            if failure=='ready':raise TimeoutError('no Ready')
            return self.cursor,0,'STATE Ready gen='+str(state.cycle)
    device=Board()
    class IO:
        args=SimpleNamespace(board=board)
        @contextmanager
        def open(self):yield device
        def boot(self,*args):pass
        def provision(self,*args):pass
        def snapshot(self,*args):return sample(350000-state.cycle*512)
        def clock(self):return state.clock
        def pause(self,board,seconds):state.clock+=seconds
    args=SimpleNamespace(board=board,out=tmp_path,cycles=2,active_seconds=5)
    if failure=='ready' or board=='p4xc' and failure:
        with pytest.raises((ValueError,TimeoutError)):h.acquire(args,IO())
    else:
        out=h.acquire(args,IO());assert len(out['cycles'])==2
        assert (out['cycles'][0]['trace'] is None)==(board=='echoear')
    assert state.idle and not state.running


@pytest.mark.parametrize('failed',[False,True])
def test_live_still_restores_under_hold_and_releases_signals(tmp_path,monkeypatch,failed):
    import boards,board_smoke,p4_product,p4_step14
    monkeypatch.setattr(boards,'require_bench',lambda:0)
    monkeypatch.setattr(p4_step14,'restoring',lambda *a:__import__('contextlib').nullcontext())
    monkeypatch.setattr(p4_product,'candidate_storage',lambda *a:__import__('contextlib').nullcontext())
    manifest=dict(board='p4xc',profile='demo',aec_enabled=False,elf_sha256='a'*64,
        heap_attribution=dict(capacity=h.CAPACITY,depth=h.DEPTH,mode='LEAKS',isr='excluded'))
    monkeypatch.setattr(h,'verify_bundle',lambda *a:(manifest,{}))
    identity=tmp_path/'identity.json';identity.write_text('{}')
    args=SimpleNamespace(board='p4xc',cycles=1,active_seconds=5,bundle=tmp_path,reference_only=False,
        bot_identity=identity,out=tmp_path/'run',restore_record=None,restore_record_sha256=None)
    state=SimpleNamespace(touched=False,restored=False,released=False)
    class Hold:
        def __enter__(self):return self
        def touch(self):state.touched=True
        def __exit__(self,*args):state.restored=True;self.record=dict(clean=True,action='p4-product-of-record')
        def release_signals(self):assert state.restored;state.released=True
    class IO:
        def __init__(self,*args):pass
        def preflight_tools(self):pass
        def preflight_restore(self):pass
        def hold(self):return Hold()
        def flash(self,*args):assert state.touched
    monkeypatch.setattr(board_smoke,'LiveIO',IO)
    def acquire(*args):
        if failed:raise TimeoutError('diagnostic failure')
        return dict(baseline=sample(),cycles=[])
    monkeypatch.setattr(h,'acquire',acquire)
    assert h.live(args)==int(failed) and state.released and state.restored
    assert json.loads((args.out/'acquisition.json').read_text())['acquisition_complete'] is not failed


def test_default_firmware_is_rejected_before_live_io(tmp_path,monkeypatch):
    import boards
    monkeypatch.setattr(boards,'require_bench',lambda:0)
    monkeypatch.setattr(h,'verify_bundle',lambda *a:(dict(profile='demo',aec_enabled=False),{}))
    args=SimpleNamespace(board='p4xc',cycles=1,active_seconds=5,bundle=tmp_path,reference_only=False)
    with pytest.raises(ValueError,match='explicit P4 diagnostic bundle'):h.live(args)


def test_diagnostic_runbook_bench_launches_use_checked_environment():
    import re,shlex
    from test_bench_launch_docs import validate
    text=Path(__file__).with_name('p4_heap_attribution.md').read_text();count=0
    for block in re.findall(r'```bash\n(.*?)```',text,re.S):
        for line in block.splitlines():
            if line.startswith('python3 hil/bench.py '):
                validate(shlex.split(line.split(' || ')[0])[2:]);count+=1
    assert count==2


@pytest.mark.parametrize('missing',['CONFIG_DG_HEAP_ATTRIBUTION','CONFIG_HEAP_TRACING_STANDALONE',
    'CONFIG_ESP_SYSTEM_USE_FRAME_POINTER','CONFIG_HEAP_TRACING_STACK_DEPTH','CONFIG_DG_DEMO_AUTOSTART','CONFIG_DG_BOARD_P4XC'])
def test_bundle_marker_requires_resolved_diagnostic_configuration(monkeypatch,missing):
    import sys
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    import p4_heap_bundle
    values={key:'y' for key in ('CONFIG_DG_HEAP_ATTRIBUTION','CONFIG_HEAP_TRACING_STANDALONE',
        'CONFIG_ESP_SYSTEM_USE_FRAME_POINTER','CONFIG_DG_DEMO_AUTOSTART','CONFIG_DG_BOARD_P4XC')}
    values['CONFIG_HEAP_TRACING_STACK_DEPTH']='8'
    assert p4_heap_bundle.diagnostics(values)['depth']==8
    values.pop(missing)
    with pytest.raises(ValueError):p4_heap_bundle.diagnostics(values)
