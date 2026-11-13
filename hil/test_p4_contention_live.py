"""Execute the real acquisition/restore orchestration with no device, port or audio IO."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import importlib.util
import os
import p4_contention as c
from console_contract import ConsoleCommands
if os.environ.get('DG_P4_CONTENTION_SOURCE'):
    spec=importlib.util.spec_from_file_location('contention_under_test',os.environ['DG_P4_CONTENTION_SOURCE'])
    c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
from test_p4_contention import perf, real_display
from test_wire_contract import capture, serialize, IDENTITY
from wire_contract import Bounds


@pytest.fixture
def rig(tmp_path,monkeypatch):
    import alsa, boards, board_serial, board_smoke, demo_session, make_bundle
    import p4_audio, step9a_support as display, step9a_merge_checks, transport_test
    commands=ConsoleCommands.from_firmware()
    clock=SimpleNamespace(value=1000.)
    def advance(seconds):clock.value+=seconds
    monkeypatch.setattr(c,'time',SimpleNamespace(monotonic=lambda:clock.value,sleep=advance))
    monkeypatch.setattr(c,'bot_identity',lambda *args:101)
    monkeypatch.setattr(c,'normal_references',lambda _: (Bounds(68411,7000),[dict(p99_us=n) for n in (42999,38999,44999)]))
    monkeypatch.setattr(board_smoke,'process_token',lambda _:IDENTITY['bot_instance'])
    monkeypatch.setattr(board_smoke,'boot_check',lambda *args:None)
    monkeypatch.setattr(board_smoke,'verify_flash_mac',lambda _: 'fake-port-never-opened')
    monkeypatch.setattr(boards,'require_bench',lambda:0)
    monkeypatch.setattr(c.subprocess,'run',Mock())
    def wait_until(predicate,timeout=10):
        end=clock.value+timeout
        while clock.value<end:
            value=predicate()
            if value:return value
            advance(.1)
        raise AssertionError('Timed out waiting for server evidence')
    monkeypatch.setattr(transport_test,'wait_until',wait_until)
    monkeypatch.setattr(demo_session,'prepare_board',lambda *args,**kwargs:None)
    monkeypatch.setattr(display,'host_address',lambda:'127.0.0.1')
    monkeypatch.setattr(alsa,'resolve_jabra',lambda:('fake-card','fake-audio-never-opened'))
    monkeypatch.setattr(alsa,'set_volumes',lambda *args:None)
    bot=tmp_path/'bot';bot.mkdir()
    for name in ('events.jsonl','bot.log'):(bot/name).write_text('')
    identity=tmp_path/'identity.json';identity.write_text(json.dumps(IDENTITY))
    bundle=tmp_path/'bundle';bundle.mkdir()
    manifest=dict(profile='normal',aec_enabled=False,board='p4xc',elf_sha256='a'*64)
    monkeypatch.setattr(make_bundle,'verify_bundle',lambda directory,board:(dict(manifest,board=board),dict(flash_files={})))
    monkeypatch.setattr(c,'display_static_ram',lambda _:dict(bytes=12000,elf_sha256='a'*64,basis='modeled ELF for fake IO'))
    args=SimpleNamespace(board='p4xc',bundle=bundle,out=tmp_path/'run',bounds=tmp_path/'bounds.json',
        bot_dir=bot,bot_identity=identity,commands=tmp_path/'commands.json',server_url='http://127.0.0.1:7861')
    args.bounds.write_text('{}')
    gets=[];noise=dict(hash='f'*64,bytes=18500)
    @contextmanager
    def assets(_):yield noise,gets
    monkeypatch.setattr(c,'assets',assets)
    state=SimpleNamespace(missing_capture=False,capture_callback_registered=True,startup_overflow=False,abort=False,commands=[],freezes=[],released=False,pending=None)
    def event(**kwargs):
        row=dict(mode='live-test',pc_id='fake-peer-'+str(board.gen));row.update(kwargs)
        with (bot/'events.jsonl').open('a') as stream:stream.write(json.dumps(row)+'\n')
    class Board:
        gen=0;connected=False;ready_at=0;sequence=0
        def __init__(self):self.lines=[];self.seen=set()
        @property
        def cursor(self):return len(self.lines)
        def emit(self,line):
            self.lines.append((self.cursor,clock.value,line))
            with (args.out/'serial.log').open('a') as stream:stream.write(line+'\n')
        def lines_since(self,at):return self.lines[at:]
        def reset(self):self.emit('READY transport-console')
        def wait_for(self,pattern,timeout,since=0):
            return next((line for _,_,line in self.lines_since(since) if re.search(pattern,line)),None) or pytest.fail('fake board lacks '+pattern)
        def command(self,text,*unused):
            commands.validate(text)
            state.commands.append(text)
            if text=='connect':
                advance(3);self.gen+=1;self.connected=True;self.ready_at=clock.value;self.seen=set();self.sequence=0
                value=capture('esp32p4' if args.board=='p4xc' else 'esp32s3',False,'bench-display');value.intervals=[]
                value.records['msg=display-result']['values']['id']=getattr(state,'request_id',c.REQUEST_ID)
                for line in serialize(value):self.emit(line)
                self.emit('STATE Ready gen='+str(self.gen));event(event='session-start')
                event(event='display-ready',epoch=self.gen,next_seq=1);event(event='stt-settings')
                if state.pending:publish(None,*state.pending)
            elif text=='stats':
                row=perf(self.gen,int(clock.value*1e6),board=args.board);row['pc_id']='fake-peer-'+str(self.gen)
                if state.missing_capture:row.pop('capture')
                elif not state.capture_callback_registered:
                    # P4 mutant: model omitting the I2S RX callbacks, leaving
                    # both IRAM counters dead even though reads still succeed.
                    row['capture'].update(rx_completions=0,rx_overflows=0)
                elif state.startup_overflow:row['capture']['rx_overflows']=1
                self.emit('PERF '+json.dumps(row))
            elif text=='touch-status':
                self.emit('DEMO_TOUCH_STATUS '+json.dumps(dict(initialized=True,enabled=False,
                    driver=dict(reads=int(clock.value*50),interrupts=0))))
            elif text=='uplink-intervals':
                state.freezes.append(self.gen)
                row=capture('esp32p4' if args.board=='p4xc' else 'esp32s3',False,'bench-display');row.records={}
                span=int((clock.value-self.ready_at-3)*1e6);n=span//20000-101
                hist=[0]*201;hist[20]=n-100;hist[40]=100
                row.intervals=[dict(gen=self.gen,start_us=int((self.ready_at+3)*1e6),end_us=int(clock.value*1e6),
                    n=n,ok=n+1,attempts=n+1,max_us=40000,last_ok_age_us=20000,hist=hist)]
                for line in serialize(row):self.emit(line)
            elif text=='disconnect':
                self.connected=False;self.emit('PHASE closed')
    board=Board();state.board=board
    @contextmanager
    def open_board(*args,**kwargs):
        board.emit('READY transport-console');yield board
    monkeypatch.setattr(board_serial,'open_board',open_board)
    def publish(path,operation,body,rid):
        state.pending=(operation,body,rid)
        if not board.connected:
            if board.gen:
                # Old bridge observes the republish before its session-end.
                # It sends on the dead connection and receives no result.
                board.sequence+=1
                event(event='display-sent',packet=dict(d=dict(epoch=board.gen,seq=board.sequence,id=rid)))
            return
        if rid in board.seen:return
        board.seen.add(rid);board.sequence+=1
        packet=dict(epoch=board.gen,seq=board.sequence,id=rid)
        event(event='display-sent',packet=dict(d=packet))
        event(event='display-result',pc_id='other-connection',result=dict(packet,accepted=False,parse_failed=True))
        event(event='display-result',result=dict(packet,accepted=True,parse_failed=False))
        board.emit('DISPLAY_WORK_RESULT '+json.dumps(dict(id=rid,epoch=board.gen,error='none',persisted=operation=='persist'),separators=(',',':')))
        if operation=='load' and body['hash']==noise['hash']:gets.append(clock.value)
    monkeypatch.setattr(step9a_merge_checks,'publish_command',publish)
    def read(board,action,prefix):
        commands.validate('disp '+action)
        if action.startswith('inventory'):return dict(items=[],next=256)
        row=copy.deepcopy(real_display());row['epoch']=board.gen;row['updates'][2]=int(clock.value*30)
        row['bench']=[1,30,int(clock.value*30)];return row
    monkeypatch.setattr(display,'read',read)
    monkeypatch.setattr(display,'request',lambda *args,**kwargs:None)
    monkeypatch.setattr(display,'terminal',lambda *args,**kwargs:dict(error='none'))
    def play(*args):
        if state.abort:raise KeyboardInterrupt('modeled abort')
        advance(2.4);event(event='final-transcript');event(event='first-tts-byte')
    monkeypatch.setattr(alsa,'play_wav',play)
    class IO:
        python='never-executed-python'
        def __init__(self,*args):pass
        def preflight_tools(self):pass
        def preflight_restore(self):pass
        def flash(self,*args):state.commands.append('fake-flash')
        def hold(self):
            class Hold:
                success=False;record={}
                def __enter__(self):return self
                def touch(self):pass
                def __exit__(self,*exc):self.record=dict(clean=True,action='echoear-55152f25' if args.board=='echoear' else 'keep-tested-p4' if self.success and not exc[0] else 'p4-factory')
                def release_signals(self):state.released=True
            return Hold()
    monkeypatch.setattr(p4_audio,'HardwareIO',IO)
    return args,state,clock


def test_live_acquires_two_real_schema_generations_and_scores_before_restore(rig):
    args,state,clock=rig
    assert c.live(args)==0
    acquisition=json.loads((args.out/'acquisition.json').read_text())
    score=json.loads((args.out/'score.json').read_text())
    assert acquisition['elapsed_s']>=300 and score['checks_passed']
    assert state.freezes==[1,2] and acquisition['restore']['action']=='keep-tested-p4' and state.released
    assert all('capture' in session['samples'][0] and 'aec' not in session['samples'][0] for session in acquisition['sessions'])
    assert 'not a timestamped scan-during-DTLS witness' in acquisition['restart'][0]['basis']
    assert len(acquisition['sessions'][0]['cycles'])>=20


def test_live_missing_real_aec_off_counter_aborts_before_timed_window(rig):
    args,state,clock=rig;state.missing_capture=True
    assert c.live(args)==1
    result=json.loads((args.out/'acquisition.json').read_text())
    assert 'pre-window: missing board-neutral PERF.capture counters' in result['error']
    assert clock.value<1010 and not state.freezes and state.released
    assert result['restore']['action']=='p4-factory' and not result['acquisition_passed']


def test_live_abort_keeps_r7_restore_armed(rig):
    args,state,clock=rig;state.abort=True
    assert c.live(args)==1
    result=json.loads((args.out/'acquisition.json').read_text())
    assert result['restore']['action']=='p4-factory' and state.released
    assert 'KeyboardInterrupt' in result['error']


def test_p4_missing_rx_callback_mutant_fails_under_restore_hold(rig):
    args,state,clock=rig;state.capture_callback_registered=False
    assert c.live(args)==1
    result=json.loads((args.out/'acquisition.json').read_text())
    assert 'mic capture counter not live' in result['error']
    assert 'rx_completions' in result['error']
    assert not result['acquisition_passed'] and result['restore']['action']=='p4-factory'
    assert state.released and not (args.out/'score.json').exists()


def test_pre_window_overflow_failure_is_identified_before_audio_or_timer(rig):
    args,state,clock=rig;state.startup_overflow=True
    assert c.live(args)==1
    result=json.loads((args.out/'acquisition.json').read_text())
    assert 'pre-window: mic capture overflow' in result['error']
    assert 'audio 1' not in state.commands and clock.value<1010
    assert result['restore']['action']=='p4-factory' and state.released


def test_s3_dead_callback_completes_as_reference_and_keeps_unmeasured_result(rig):
    args,state,clock=rig;args.board='echoear';state.capture_callback_registered=False
    assert c.live(args)==0
    result=json.loads((args.out/'score.json').read_text())
    assert result['checks_passed'] and not result['qualification']
    assert all(g['resources']['capture_overflows'] is None and
               g['resources']['capture_overflows_result']=='unmeasured (report-only)' for g in result['generations'])
    acquisition=json.loads((args.out/'acquisition.json').read_text())
    assert acquisition['restore']['action']=='echoear-55152f25' and state.released


def test_live_sends_top_level_touch_commands(rig):
    args,state,clock=rig
    assert c.live(args)==0
    assert 'touch-enable 0' in state.commands and state.commands.count('touch-status')==4
    assert not any(command.startswith('demo touch-') for command in state.commands)


def test_restart_ignores_stale_bridge_and_records_active_connections(rig):
    args,state,clock=rig
    assert c.live(args)==0
    result=json.loads((args.out/'acquisition.json').read_text())
    assert [(s['display_session']['pc_id'],s['display_session']['epoch']) for s in result['sessions']]==[('fake-peer-1',1),('fake-peer-2',2)]
    assert result['sessions'][0]['stale_bridge_sends']==[]
    for session in result['sessions']:
        sent=next(e for e in session['bot_events'] if e.get('event')=='display-sent' and e['pc_id']==session['display_session']['pc_id'])
        assert sent['packet']['d']['seq']==1
    stale=result['sessions'][1]['stale_bridge_sends']
    assert len(stale)==1 and stale[0]['pc_id']=='fake-peer-1'
    assert stale[0]['packet']['d']['epoch']==1 and stale[0]['packet']['d']['id']==c.REQUEST_ID
    assert result['restore']['action']=='keep-tested-p4' and state.freezes==[1,2]


def test_old_id_only_send_matching_reproduces_restart_timeout(rig,monkeypatch):
    args,state,clock=rig
    monkeypatch.setattr(c,'display_sent',lambda events,session,rid:next((e for e in events if
        e.get('event')=='display-sent' and e['packet']['d']['id']==rid),None))
    assert c.live(args)==1
    result=json.loads((args.out/'acquisition.json').read_text())
    assert 'Timed out waiting for server evidence' in result['error']
    assert clock.value>=1160 and state.freezes==[1]
    assert result['restore']['action']=='p4-factory' and state.released


def test_display_acquire_uses_same_session_matching_with_old_bridge(rig,monkeypatch):
    import p4_display, step9a_assets, demo_session, echoear_status_assets, step9a_support, step9a_merge_checks
    args,state,clock=rig;args.board='echoear';args.seconds=.2;args.out.mkdir()
    state.board.gen=1;state.board.sequence=91;state.request_id=p4_display.REQUEST_ID
    monkeypatch.setattr(p4_display,'bot_identity',lambda *args:101)
    monkeypatch.setattr(p4_display,'time',c.time)
    @contextmanager
    def asset_server():yield
    monkeypatch.setattr(step9a_assets,'asset_server',asset_server)
    monkeypatch.setattr(demo_session,'prepare_board',lambda *args,**kwargs:dict(images=[dict(hash='f'*64,bytes=18500)]))
    monkeypatch.setattr(echoear_status_assets,'provision_status',lambda *args:{})
    def read(board,action,prefix):
        if prefix=='DISPLAY_DEMO_PROOF':return dict(total=0,items=[dict(screen=n) for n in range(3)])
        return dict(epoch=board.gen)
    monkeypatch.setattr(step9a_support,'read',read)
    monkeypatch.setattr(step9a_support,'terminal',lambda board,rid:dict(error='none',epoch=board.gen))
    monkeypatch.setattr(step9a_merge_checks,'rendered_show',lambda *args:True)
    io=SimpleNamespace(python='never-executed-python',flash=lambda *args:None)
    result=p4_display.acquire(args,io,dict(elf_sha256='a'*64),IDENTITY)
    assert result['display_session']['pc_id']=='fake-peer-2' and result['display_session']['epoch']==2
    assert len(result['stale_bridge_sends'])==1
    assert all(row['sent']['pc_id']==row['ack']['pc_id']=='fake-peer-2' for row in result['wire_requests'])
