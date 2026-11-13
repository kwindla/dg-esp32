"""Closed-candidate fatal checks; copied pin evidence and device-free exits."""
from collections import deque
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace

import pytest
import p4_parity as parity
import p4_parity_campaign as campaign
import p4_parity_display as display
import p4_parity_liveness as live
from p4_parity_serial import candidate_serial,serial_trace,window
from test_p4_parity import populate_liveness_proof
from test_p4_parity_fix1 import real_plan,copy_restore,display_case,put
from test_p4_parity_fix2 import insert_display,inconclusive
from test_p4_liveness import p4_run
from test_wire_contract import capture,serialize,IDENTITY

PANIC='Guru Meditation Error: asynchronous disconnect cleanup panicked'
BENIGN='I (9999) dg-app: intentional disconnect cleanup completed'


def late_receive(directory,line,*,traced=True,newline=True):
    """Append an honest later receive without changing any saved window."""
    if traced:
        path=directory/'serial-entries.jsonl';entries=[json.loads(l) for l in path.read_text().splitlines()]
        last=entries[-1];entry=dict(cursor=last['cursor']+1,monotonic_ns=last['monotonic_ns']+1,line=line)
        with path.open('a') as stream:stream.write(json.dumps(entry)+'\n')
    with (directory/'serial.log').open('ab') as stream:stream.write(line.encode()+(b'\n' if newline else b''))


@pytest.mark.parametrize('board',parity.BOARDS)
@pytest.mark.parametrize('traced',[True,False])
@pytest.mark.parametrize('line',[PANIC,'Stack canary watchpoint triggered',
    'abort() was called at PC 0x42001234 on core 0','assert failed: display worker',BENIGN])
def test_display_checks_late_journal_and_unterminated_raw_tail(tmp_path,board,traced,line):
    a=display_case(tmp_path,board,'normal');before=copy.deepcopy(a)
    assert display.score_display(tmp_path,board,'normal')['checks_passed']
    late_receive(tmp_path,line,traced=traced,newline=traced)
    assert parity.load(tmp_path/'acquisition.json')==before
    if line==BENIGN:
        assert display.score_display(tmp_path,board,'normal')['checks_passed']
        assert parity.replay('display',board,'normal',tmp_path)['status']=='pass'
    else:
        with pytest.raises(ValueError,match='fatal diagnostic in candidate capture'):
            display.score_display(tmp_path,board,'normal')
        assert parity.collect('display',board,'normal',tmp_path)['status']=='fail'


@pytest.mark.parametrize('line',['abort() was called at PC 0x42001234 on core 0','assert failed: display worker'])
@pytest.mark.parametrize('board',parity.BOARDS)
def test_active_abort_assert_are_fatal_for_single_and_pair(tmp_path,line,board):
    runs={}
    for b in parity.BOARDS:
        run=tmp_path/b;a=display_case(run,b,'normal');runs[b]=run
        if b==board:insert_display(run,a,[line],True)
    with pytest.raises(ValueError,match='fatal diagnostic'):display.score_display(runs[board],board,'normal')
    with pytest.raises(ValueError,match='fatal diagnostic'):display.pair(runs['echoear'],runs['p4xc'])


@pytest.mark.parametrize('late_panic',[False,True])
def test_six_display_originals_plan_ledger_collect_all_and_table(real_plan,late_panic):
    plan,root=real_plan;put(root/'display-identity.json',IDENTITY)
    for variant in parity.VARIANTS:
        for board in parity.BOARDS:
            run=root/'display'/(board+'-'+variant)/'attempt-01'
            with campaign.attempt(plan,'display',board,variant,run):
                a=display_case(run,board,variant);late_receive(run,PANIC if late_panic else BENIGN)
                assert parity.load(run/'acquisition.json')==a  # No success flag/window forgery.
    parity.collect_all(plan,root/'receipts')
    receipts=[parity.receipt(path) for path in sorted((root/'receipts').glob('*.json'))]
    assert len(receipts)==12 and {r['status'] for r in receipts}=={'fail' if late_panic else 'pass'}
    table=parity.render(receipts,plan)
    for feature in ('F7','F9','F10'):
        row=next(line for line in table.splitlines() if line.startswith('| '+feature+' '))
        assert ('| fail | fail |' if late_panic else '| pass | pass |') in row
    if late_panic:assert all('fatal diagnostic' in r['reason'] for r in receipts)


@pytest.mark.parametrize('board',parity.BOARDS)
@pytest.mark.parametrize('exit_case',['asset-panic','reader-panic','benign'])
def test_display_live_waits_for_context_exit_before_success(tmp_path,monkeypatch,board,exit_case):
    """The real trace sees simulated asset exit; raw tail outlives trace exit."""
    import boards
    import board_smoke
    import p4_audio
    source=tmp_path/'source';a=display_case(source,board,'normal')
    args=SimpleNamespace(board=board,bundle=Path(a['bundle']),bot_identity=source/'identity.json',out=tmp_path/'acquisition')
    order=[]
    class Guard:
        success=False;record=a['restore'];released=False
        def __enter__(self):return self
        def touch(self):order.append('touch')
        def __exit__(self,*exc):order.append('restore')
        def release_signals(self):self.released=True
    guard=Guard()
    class IO:
        def __init__(self,*args):pass
        def preflight_tools(self):pass
        def preflight_restore(self):pass
        def hold(self):return guard
    monkeypatch.setattr(boards,'require_bench',lambda:123)
    monkeypatch.setattr(p4_audio,'HardwareIO',IO)
    monkeypatch.setattr(display,'bot_identity',lambda *args:101)
    monkeypatch.setattr(board_smoke,'process_token',lambda pid:IDENTITY['bot_instance'])
    def acquire(args,*_):
        for path in source.iterdir():
            if path.name in ('image.json','identity.json','acquisition.json','serial-entries.jsonl'):continue
            if path.is_dir():shutil.copytree(path,args.out/path.name)
            else:shutil.copyfile(path,args.out/path.name)
        lines=(args.out/'serial.log').read_text().splitlines()
        entries=[dict(cursor=i+1,monotonic_ns=(i+1)*10**9,line=line) for i,line in enumerate(lines)]
        class Reader:
            _cv=threading.Condition();_lines=deque((e['cursor'],e['cursor'],e['line']) for e in entries)
            closed=False
            def emit(self,line):
                with self._cv:
                    cursor=self._lines[-1][0]+1
                    with (args.out/'serial.log').open('a') as stream:stream.write(line+'\n')
                    self._lines.append((cursor,float(cursor),line))
            def __enter__(self):return self
            def __exit__(self,*exc):
                self.emit(PANIC if exit_case=='reader-panic' else BENIGN)
                self.closed=True;order.append('reader closed')
        reader=Reader()
        @contextmanager
        def asset_server_exit():
            try:yield
            finally:reader.emit(PANIC if exit_case=='asset-panic' else BENIGN);order.append('asset exit')
        with reader,serial_trace(reader,args.out/'serial-entries.jsonl'),asset_server_exit():
            saved={k:copy.deepcopy(a[k]) for k in ('boot_verified','run_window','wire_window','screens','status_assets',
                'wire_requests','display_session')}
            for name in ('run_window','wire_window'):
                saved[name]=window(entries,saved[name]['start_cursor'],saved[name]['end_cursor'])
        assert reader.closed
        return saved
    monkeypatch.setattr(display,'acquire',acquire)
    assert display.acquire_live(args)==(0 if exit_case=='benign' else 1)
    result=parity.load(args.out/'acquisition.json')
    assert result['acquisition_passed'] is (exit_case=='benign') and guard.success is (exit_case=='benign')
    assert guard.released and order.index('reader closed')<order.index('restore')
    if exit_case=='benign':assert display.score_display(args.out,board,'normal')['checks_passed']
    else:
        assert 'fatal diagnostic' in result['error']
        assert parity.collect('display',board,'normal',args.out)['status']=='fail'


def smoke_case(directory,board):
    copy_restore(directory,board);source=parity.PIN_VERIFICATION/('smoke-'+board+'-normal')
    for pattern in ('result.json','identity.json','serial.log','main-serial.log','main-bot.log','main-bot-events.json','turn-*.json','perf-*.json'):
        for path in source.glob(pattern):shutil.copyfile(path,directory/path.name)
    image=parity.PIN/'final-bundles'/('bundle-'+board+'-normal')/'manifest.json'
    shutil.copyfile(image,directory/'image.json')
    lines=(directory/'serial.log').read_text(errors='replace').splitlines()
    start=max(i for i,line in enumerate(lines) if 'elf_sha256=' in line)
    end=next(i for i in range(start+1,len(lines)) if lines[i].strip()=='READY transport-console')
    (directory/'boot.log').write_text('\n'.join(lines[start:end+1])+'\n')


@pytest.mark.parametrize('board',parity.BOARDS)
def test_real_retained_smoke_and_product_pair_reject_late_panic(tmp_path,board):
    runs={}
    for b in parity.BOARDS:
        run=tmp_path/b;smoke_case(run,b);runs[b]=run
        assert parity.replay('smoke',b,'normal',run)['status']=='pass'
    assert parity.product_pair(runs['echoear'],runs['p4xc'],live.smoke.BOUNDS_FILE)['checks_passed']
    with (runs[board]/'serial.log').open('a') as stream:stream.write(PANIC+'\n')
    assert parity.collect('smoke',board,'normal',runs[board])['status']=='fail'
    with pytest.raises(ValueError,match='fatal diagnostic'):
        parity.product_pair(runs['echoear'],runs['p4xc'],live.smoke.BOUNDS_FILE)


@pytest.mark.parametrize('board',parity.BOARDS)
@pytest.mark.parametrize('line',[PANIC,BENIGN])
def test_liveness_whole_capture_beyond_final_session(real_plan,board,line):
    plan,root=real_plan;run=root/'liveness'/(board+'-demo')/'attempt-01'
    with campaign.attempt(plan,'liveness',board,'demo',run):
        a=copy_restore(run,board,'demo');base=p4_run(run);base.update(a);a=base
        manifest=parity.load(Path(a['bundle'])/'manifest.json')
        a.update(elf_sha256=manifest['elf_sha256'],variant='demo',aec_enabled=False,reference_only=False)
        for s in a['sessions']:
            lines=serialize(capture(manifest['target'],False));cap=[l for l in lines if 'DEMO_CAPABILITY ' in l]
            (run/s['wire_file']).write_text('\n'.join([l for l in lines if 'DEMO_CAPABILITY ' not in l]+['PHASE closed']+cap)+'\n')
        populate_liveness_proof(run,a,None,planned=plan)
        assert live.score(run,acquisition=a)['checks_passed']
        late_receive(run,line);put(run/'acquisition.json',a)
    row=parity.bound_receipt(plan,'liveness',board,'demo',run)
    assert row['status']==('fail' if line==PANIC else 'pass')
    if line==PANIC:assert 'fatal diagnostic' in row['reason']


def test_late_candidate_panic_cannot_become_inconclusive(real_plan):
    plan,root=real_plan;run=root/'liveness/p4xc-normal/attempt-01'
    with campaign.attempt(plan,'liveness','p4xc','normal',run):
        a=inconclusive(run);late_receive(run,PANIC);put(run/'acquisition.json',a)
    row=parity.bound_receipt(plan,'liveness','p4xc','normal',run)
    assert row['status']=='fail' and row['attempt_binding']['disposition']=='failed'


@pytest.mark.parametrize('board',parity.BOARDS)
def test_intentional_resets_normal_teardown_and_benign_late_raw_tail_pass(tmp_path,board):
    smoke_case(tmp_path,board)
    with (tmp_path/'serial.log').open('a') as stream:
        stream.write('rst:0xc (SW_CPU_RESET),boot:0x8 (SPI_FAST_FLASH_BOOT)\n'+BENIGN)
    assert candidate_serial(tmp_path)
    result=parity.replay('smoke',board,'normal',tmp_path)
    assert result['status']=='pass' and str(tmp_path/'serial.log') in result['sources']
