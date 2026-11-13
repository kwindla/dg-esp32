"""File-only reconnect and per-turn regressions; never instantiate live hardware."""
import copy
import json
from pathlib import Path

import pytest
import echoear_rollback as r
import echoear_rollback_rejudge as j
from test_echoear_rollback import rows,lines,counters

RETAINED=Path('/home/khkramer/src/evidence/echoear/aec-demo/step-9/rollback-01')


def turn(peer):
    return r.turn_witness([dict(e,pc_id=peer) for e in rows()],lines())


def states():return ['STATE Ready gen=1 desired=1 t=100','STATE Idle gen=2 desired=0 t=210',
                     'STATE Starting gen=3 desired=1 t=220','STATE Ready gen=3 desired=1 t=250']

def before():return dict(generation=1,device_us=200,pc_id='old')
def after():return dict(generation=3,device_us=300,pc_id='new')


def test_old_peer_teardown_is_not_reply_identity():
    events=[dict(e,pc_id='new') for e in rows()]
    events += [dict(kind='session-end',pc_id='old',monotonic_ns=2),
               dict(kind='server-frame',frame='CancelFrame',pc_id='old',monotonic_ns=2)]
    last=r.turn_witness(events,lines())
    assert last['passed'] and last['pc_ids']==['new']
    assert r.reconnect_witness([turn('old'),last],states(),before(),after())['passed']


@pytest.mark.parametrize('kind',['aggregator-user-turn-stopped','aggregator-assistant-turn-stopped','tts-transport-write'])
@pytest.mark.parametrize('peer',[None,'wrong'])
def test_mixed_or_missing_reply_identity_fails(kind,peer):
    events=rows()
    next(e for e in events if e['kind']==kind)['pc_id']=peer
    assert not r.turn_witness(events,lines())['passed']


def test_reconnect_compares_every_earlier_turn_not_only_cold():
    assert not r.reconnect_witness([turn('cold-peer'),turn('new'),turn('new')],states(),
        dict(before(),pc_id='new'),after())['passed']


@pytest.mark.parametrize('fault',['missing-state','old-generation','missing-generation','late-ready','wrong-board-peer','old-peer'])
def test_reconnect_requires_both_new_peer_and_in_window_board_generation(fault):
    log=states();a=before();b=after();results=[turn('old'),turn('new')]
    if fault=='missing-state':log=[]
    if fault=='old-generation':b['generation']=1;log[-1]='STATE Ready gen=1 desired=1 t=250'
    if fault=='missing-generation':del b['generation']
    if fault=='late-ready':log[-1]='STATE Ready gen=3 desired=1 t=350'
    if fault=='wrong-board-peer':b['pc_id']='other'
    if fault=='old-peer':results[-1]=turn('old')
    assert not r.reconnect_witness(results,log,a,b)['passed']


def complete_turn(offset=0,peer='peer'):
    def event(t,kind,**kw):return dict(monotonic_ns=int((t+offset)*1e9),kind=kind,pc_id=peer,**kw)
    context='context-'+str(offset)
    events=[event(1,'aggregator-user-turn-started'),event(2,'aggregator-user-turn-stopped',text='Question'),
        event(3,'server-frame',frame='TTSStartedFrame',boundary='tts-written',context_id=context),
        event(4,'tts-transport-write',non_silent=True,drop=False,context_id=context),
        event(5,'server-frame',frame='BotStoppedSpeakingFrame',boundary='tts-written'),
        event(5.1,'server-frame',frame='TTSStoppedFrame',boundary='tts-written',context_id=context),
        event(5.2,'aggregator-assistant-turn-stopped',text='Answer')]
    serial=[(0,offset+4,'MIC_GATE t=4 closed=1 pre=-20 post=-120'),
            (1,offset+6,'MIC_GATE t=6 closed=0 pre=-50 post=-50')]
    return events,serial


@pytest.mark.parametrize('failed_turn',[0,1])
@pytest.mark.parametrize('fault',['missing-assistant','missing-stop','missing-tts-stop','drop','interruption','peer','gate'])
def test_two_turn_window_cannot_hide_failed_turn(failed_turn,fault):
    events,serial=complete_turn();events2,serial2=complete_turn(10)
    batches=[events,events2];gates=[serial,serial2];bad=batches[failed_turn]
    if fault=='missing-assistant':bad[:]=[e for e in bad if e['kind']!='aggregator-assistant-turn-stopped']
    if fault=='missing-stop':bad[:]=[e for e in bad if e.get('frame')!='BotStoppedSpeakingFrame']
    if fault=='missing-tts-stop':bad[:]=[e for e in bad if e.get('frame')!='TTSStoppedFrame']
    if fault=='drop':next(e for e in bad if e['kind']=='tts-transport-write')['drop']=True
    if fault=='interruption':bad.append(dict(kind='frame',frame='InterruptionFrame',pc_id='peer',monotonic_ns=int((4.5+10*failed_turn)*1e9)))
    if fault=='peer':next(e for e in bad if e['kind']=='aggregator-assistant-turn-stopped')['pc_id']='other'
    if fault=='gate':gates[failed_turn].pop()
    got=r.per_turn_witnesses(events+events2,serial+serial2,0,20_000_000_000)
    assert len(got)==2 and not got[failed_turn]['passed'] and got[1-failed_turn]['passed']


def test_retained_seven_turns_reconnect_and_skipped_final_checks():
    hashes=j.file_hashes(RETAINED)
    value=j.evaluate(RETAINED)
    assert j.file_hashes(RETAINED)==hashes
    assert value['functional_claims_supported'] and all(value['functional_checks'].values())
    assert not value['all_declared_checks_passed'] and not value['declared_stimulus_sequence_followed']
    assert len([t for w in value['windows'] for t in w['individual_turns']])==7
    warm=value['windows'][2]['individual_turns']
    assert len(warm)==2 and all(t['passed'] and t['complete_reply'] for t in warm)
    assert [t['transport_writes'] for t in warm]==[114,42]
    assert all(not t['audible_reply_witness']['separate_per_turn_note'] for t in warm)
    assert value['windows'][1]['user_transcripts']==['blue and orange']
    assert len(value['windows'][-1]['original_window_pc_ids'])==2
    assert len(value['windows'][-1]['pc_ids'])==1
    assert value['reconnect']['before_generation']==1 and value['reconnect']['after_generation']==3
    assert value['snapshots']['after-smoke']['reached'] and value['snapshots']['after-smoke']['serial_match']
    assert value['final_audit']['counters']['passed'] and len(value['final_audit']['counters']['epochs'])==2
    assert value['counter_epoch_journal_matches']


def test_final_faults_still_fail():
    log='DEMO_DUPLEX aec_full_duplex=False user_mute=True source=override-0\n'*2
    serial=['elf_sha256='+r.NORMAL_HASH]
    epochs=[r.counter_check(counters(),counters())]*2
    assert r.final_audit(log,serial,epochs)['passed']
    assert not r.final_audit(log+'LIVENESS alarm',serial,epochs)['passed']
    assert not r.final_audit(log,serial+serial,epochs)['passed']
    assert not r.final_audit(log,serial+['Guru Meditation'],epochs)['passed']
    assert not r.final_audit('',serial,epochs)['passed']
    bad=counters();bad['pcm_queue_drops']=1
    assert not r.final_audit(log,serial,[epochs[0],r.counter_check(counters(),bad)])['passed']


def test_report_writer_hash_guard_no_original_mutation_no_live_path(tmp_path,monkeypatch):
    original=tmp_path/'result.json';original.write_text('{"passed":false}')
    monkeypatch.setattr(j,'evaluate',lambda root:dict(report_only=True))
    monkeypatch.setattr(r,'head',lambda:'test-commit')
    monkeypatch.setattr(r,'Live',lambda *a:pytest.fail('constructed hardware'))
    before=original.read_bytes()
    value=j.rejudge(tmp_path)
    assert original.read_bytes()==before and value['original_files_unchanged']
    assert set(p.name for p in tmp_path.iterdir())=={'result.json','rejudge.json'}
    with pytest.raises(FileExistsError):j.rejudge(tmp_path)


def test_report_refuses_changed_source(tmp_path,monkeypatch):
    original=tmp_path/'result.json';original.write_text('original')
    def mutate(root):original.write_text('changed');return {}
    monkeypatch.setattr(j,'evaluate',mutate)
    with pytest.raises(RuntimeError,match='changed'):j.rejudge(tmp_path)
    assert not (tmp_path/'rejudge.json').exists()


def test_live_smoke_uses_fixed_identity_per_turn_and_final_audits(tmp_path,monkeypatch):
    windows=json.loads((RETAINED/'turns.json').read_text())
    idle=json.loads((RETAINED/'idle.json').read_text())
    times=[]
    for window in windows:
        if window['id']=='reconnected':times.extend([idle['start_ns'],idle['end_ns']])
        times.extend([window['start_ns'],window['end_ns']])
    clock=iter(times);monkeypatch.setattr(r.time,'monotonic_ns',lambda:next(clock))
    monkeypatch.setattr(r.time,'sleep',lambda _:None)
    notes=iter(w['operator_note'] for w in windows)
    monkeypatch.setattr(r,'operator_note',lambda *args:next(notes))
    retained_events=r.events(RETAINED);monkeypatch.setattr(r,'events',lambda _:retained_events)
    (tmp_path/'bot.log').write_bytes((RETAINED/'bot.log').read_bytes())
    serial=(RETAINED/'rollback-serial.log').read_text().splitlines()
    after=json.loads((RETAINED/'after-smoke.json').read_text())
    end=next(i for i,line in enumerate(serial) if line.startswith('PERF ') and json.loads(line[5:])=={k:v for k,v in after.items() if k!='audio'})
    windows_by_cursor={i+1:w['id'] for i,w in enumerate(windows[:5])}
    windows_by_cursor[7]='reconnected'
    class Board:
        count=0
        @property
        def cursor(self):self.count+=1;return self.count
        def lines_since(self,cursor):
            if cursor==0:return [(i,0,line) for i,line in enumerate(serial[:end+1])]
            if cursor==6:return [(0,0,'idle; no STATE loss')]
            return json.loads((RETAINED/(windows_by_cursor[cursor]+'-serial.json')).read_text())
    live=r.Live({},tmp_path);live.board=Board()
    live.snapshot=lambda name:json.loads((RETAINED/(name+'.json')).read_text())
    calls=[];live.command=lambda text:calls.append(text);live.connect=lambda:calls.append('connect')
    live.alive=lambda:None
    result=live.smoke()
    assert result['passed'] and result['reconnect']['passed']
    assert len(result['turns'][2]['individual_turns'])==2
    assert calls==['audio 0','disconnect','connect']
    assert json.loads((tmp_path/'counter-check.json').read_text())['passed']
    assert json.loads((tmp_path/'final-audit.json').read_text())['passed']
