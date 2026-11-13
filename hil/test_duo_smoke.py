"""Pure driver/protocol and fake lifecycle checks. Never starts a live bot."""
import copy
from contextlib import contextmanager
import json
import re
from types import SimpleNamespace
import pytest
import duo_smoke as smoke


def status(**kw):
    row=dict(generation=3,toggles_sent=0,answered=1,retransmitted=0,resyncs=0,displayed=1,taps_injected=0,
             taps_physical=0,taps_ignored_pending=0,rev=1,character='angel',baseline=True,pending=False,errors=dict(timeout=0))
    return dict(row,**kw)


def proof(character='angel',rev=1,seq=1,**kw):
    pack=next(c for c in json.loads((smoke.ROOT/'hil/fixtures/echoear360/duo-pack.json').read_text())['characters'] if c['name']==character)
    frames=[pack['faceplate']]+[layer['frames'][0] for layer in pack['layers']]
    return dict(dict(session='boot',generation=3,conn='epoch',seq=seq,rev=rev,character=character,pack=pack['hash'],scene_revision=rev,
        layers=[[i,f['hash'],f['x'],f['y']] for i,f in enumerate(frames)],hidden=[3,4,5,6,7]),**kw)


def state(p,**kw):return dict(dict(type='duo',v=1,op='state',**{k:p[k] for k in ('session','conn','seq','rev','character')}),**kw)


def switched(*,physical=False,drops=0):
    before=status();prior=proof();after=status(toggles_sent=1,answered=2,displayed=2,rev=2,character='devil',
        taps_injected=0 if physical else 1,taps_physical=1 if physical else 0,retransmitted=min(drops,3),resyncs=int(drops==4))
    if drops==4:after['errors']['timeout']=1
    shown=proof('devil',2,3 if drops==4 else 2)
    request=dict(v=1,op='toggle',session='boot',seq=2,have='angel')
    wire=[dict(direction='received',monotonic_ns=10_000_000_000,data=copy.deepcopy(request)) for _ in range(1+min(drops,3))]
    if drops==4:wire.append(dict(direction='received',data=dict(request,op='hello',seq=3)))
    wire.append(dict(direction='sent',data=state(shown)))
    events=[dict(kind='outcome',**state(proof('devil',2,2))),dict(kind='verified',character='devil',reverting=False,transaction='tx')]
    events.extend(dict(kind='phase-timing',phase=p,success=True,elapsed_ms=1.5,session='boot',seq=2,conn='epoch',rev=2 if p=='publish' else 1,operation='op',transaction='tx' if p in ('mutation','publish') else 'interrupt-tx' if p=='interrupt' else None,reverting=False) for p in ('interrupt','retirement_wait','mutation','publish'))
    events.extend(dict(kind='dropped-state',seq=2) for _ in range(drops))
    return before,after,prior,shown,events,wire


@pytest.mark.parametrize('physical,drops',[(False,0),(True,0),(False,1),(False,4)])
def test_switch_witness_matches_physical_injected_and_loss_contracts(physical,drops):
    got=smoke.switch_witness(*switched(physical=physical,drops=drops),physical=physical,drops=drops)
    assert got['proof']['character']=='devil' and len(got['phase_timings'])==4


@pytest.mark.parametrize('bad',['generation','session','conn','revision','character','injection','extra-physical','answer','error','replay','double-commit','state','pack','layer','hidden','timing','seq'])
def test_switch_never_accepts_incomplete_or_foreign_evidence(bad):
    b,a,p,q,e,w=switched()
    if bad=='generation':a['generation']+=1
    if bad in ('session','conn'):q[bad]='other'
    if bad=='revision':q['rev']=4
    if bad=='character':q['character']='angel'
    if bad=='injection':a['taps_injected']=0
    if bad=='extra-physical':a['taps_physical']=1
    if bad=='answer':a['answered']=1
    if bad=='error':a['errors']['timeout']=1
    if bad=='replay':w.insert(0,dict(direction='received',data=dict(w[0]['data'],seq=4)))
    if bad=='double-commit':e.append(copy.deepcopy(e[1]))
    if bad=='state':w.pop()
    if bad=='pack':q['pack']='0'*64
    if bad=='layer':q['layers'][1][1]='0'*64
    if bad=='hidden':q['hidden']=[]
    if bad=='timing':e.pop()
    if bad=='seq':q['seq']=8
    with pytest.raises(ValueError):smoke.switch_witness(b,a,p,q,e,w)


def turn_events(text='What is the capital of France?',llm='anything at all'):
    return [dict(kind='aggregator-user-turn-stopped',text=text,monotonic_ns=1),
            dict(kind='frame',frame='BotStartedSpeakingFrame',monotonic_ns=2),
            dict(kind='tts-transport-write',non_silent=True,context_id='ctx',monotonic_ns=3),
            dict(kind='server-frame',frame='TTSStoppedFrame',boundary='tts-written',context_id='ctx',monotonic_ns=4),
            dict(kind='frame',frame='BotStoppedSpeakingFrame',monotonic_ns=5),
            dict(kind='aggregator-assistant-turn-stopped',text=llm,monotonic_ns=5)]


def voice(p):return dict(kind='duo-synthesis',voice={'angel':'flux-hannah-en','devil':'flux-rufus-en'}[p['character']],character=p['character'],conn=p['conn'],rev=p['rev'],context_id='ctx')


def test_stt_strict_llm_text_report_only_and_voice_is_real_boundary():
    p=proof();rows=turn_events(llm='not Paris')
    assert smoke.turn_verdict(rows,[voice(p)],p)['llm_content_report_only']==['not Paris']
    with pytest.raises(ValueError,match='STT'):smoke.turn_verdict(turn_events('noise'),[voice(p)],p)
    with pytest.raises(ValueError,match='voice'):smoke.turn_verdict(rows,[dict(voice(p),voice='wrong')],p)
    with pytest.raises(ValueError,match='written audio'):smoke.turn_verdict(rows,[dict(voice(p),context_id='foreign')],p)


@pytest.mark.parametrize('phase,frames,passed',[
    ('generating',['LLMFullResponseStartFrame'],True),('generating',['LLMFullResponseStartFrame','TTSStartedFrame'],False),
    ('playback',['TTSStartedFrame','BotStartedSpeakingFrame'],True),
    ('playback',['TTSStartedFrame','BotStartedSpeakingFrame','BotStoppedSpeakingFrame'],False)])
def test_scheduling_uses_observed_llm_tts_and_playback_events(phase,frames,passed):
    assert smoke.phase_ready([dict(frame=f) for f in frames],phase)==passed


class FakeIO:
    def __init__(self,part='idle'):
        self.part=part;self.now=10.;self.before,self.after,self.prior,self.shown,self.events,self.wire=switched(drops=int(part[-1]) if part.startswith('drop-state-') else 0)
        self.current=(self.before,self.prior);self.switched=False;self.commands=[];self.stimuli=[];self.turns=[]
        self.phase_events=[];self.out=None;self.reverted=False;self.tap_latencies=[]
    def clock(self):return self.now
    def pause(self,seconds):self.now+=seconds
    def mark(self):return int(self.now*1e9)
    def snapshot(self):return copy.deepcopy(self.current)
    def evidence(self,mark):
        events=list(self.phase_events)+(copy.deepcopy(self.events) if self.switched else [])
        if self.reverted:events.extend([dict(kind='outcome',monotonic_ns=11_000_000_000,**state(self.current[1],reason='reverted')),dict(frame='ErrorFrame',frame_id=99,monotonic_ns=10_600_000_000),dict(kind='aggregator-user-turn-stopped',text='capital France',monotonic_ns=10_200_000_000)])
        return events,copy.deepcopy(self.wire) if self.switched else [],[dict(voice(self.shown),kind='duo-synthesis',monotonic_ns=10_500_000_000),dict(voice(self.shown),kind='duo-synthesis-error',error='DUO_FAULT tts-fail-next',error_id=99,monotonic_ns=10_600_000_000)] if self.reverted else []
    def command(self,cmd):
        self.commands.append(cmd)
        if cmd=='duo tap':self.switched=True;self.current=(self.after,self.shown)
    def wait(self,predicate,seconds,label):
        assert predicate(),label
    def stimulate(self,kind):
        self.stimuli.append(kind)
        if self.part=='tts-fail-next' and self.switched:
            self.reverted=True;self.current=(dict(self.after,character='angel',rev=3),proof('angel',3,2))
        else:self.phase_events=[dict(frame=f,monotonic_ns=int(self.now*1e9)) for f in
            (['LLMFullResponseStartFrame'] if self.part=='generating' else ['TTSStartedFrame','BotStartedSpeakingFrame'])]
    def require_boot(self):pass
    def turn(self,p,*,prior_bracket=None):
        if prior_bracket:prior_bracket(12_000_000_000)
        self.turns.append(p['character']);return dict(stt=['capital France'],voice=voice(p)['voice'])
    def install_offline(self):return dict(origin_unavailable=True)


@pytest.mark.parametrize('part',['idle','generating','playback','drop-state-1','drop-state-4','tts-fail-next','quiet','turns','install'])
def test_each_resumable_driver_part_runs_with_fake_io(part):
    io=FakeIO(part);row=smoke.run_part(io,part)
    if part=='quiet':assert io.now>310 and io.turns==['angel']
    elif part=='turns':assert len(io.turns)==2 and set(io.turns)=={'angel','devil'}
    elif part=='install':assert row['origin_unavailable']
    else:assert io.commands==['duo tap'] and row['proof']['character']=='devil'
    if part=='tts-fail-next':assert row['reverted']['character']=='angel' and io.turns==['angel']


def test_phase_miss_is_failed_no_retired_retry():
    io=FakeIO('generating')
    def stimulate(kind):io.phase_events=[dict(frame=f) for f in ('LLMFullResponseStartFrame','TTSStartedFrame')]
    io.stimulate=stimulate
    with pytest.raises(AssertionError):smoke.run_part(io,'generating')
    assert not io.commands


def test_rules_cli_is_offline_and_all_steps_have_fixed_pass_rules(capsys):
    assert smoke.main(['rules'])==0
    rules=json.loads(capsys.readouterr().out)
    assert set(rules)==set(smoke.PARTS) and '300' in rules['quiet'] and 'report-only' in rules['turns']


@pytest.mark.parametrize('change,seconds',[(0,30),(9,30),(99999,301)])
def test_render_busy_report_is_a_rate_and_never_a_gate(change,seconds):
    io=FakeIO('quiet');io.count=7
    io.json_command=lambda *args:status(render_busy=io.count)
    report={}
    with smoke.render_busy_reporting(io,report):
        io.count+=change;io.pause(seconds)
    assert report==dict(report_only=True,delta=change,rate_per_minute=change*60/seconds,
                        seconds=seconds,observation='recorded')
    # Fault deltas remain strict even when busy is report-only.
    with pytest.raises(ValueError,match='errors changed'):
        smoke.delta(status(render_busy=0,errors=dict(render=0)),status(render_busy=change,errors=dict(render=1)))


def test_render_busy_report_survives_body_failure_and_missing_or_reset_counters():
    io=FakeIO();io.count=9;io.json_command=lambda *args:status(render_busy=io.count)
    report={}
    with pytest.raises(RuntimeError,match='body'):
        with smoke.render_busy_reporting(io,report):
            io.count=12;io.pause(60);raise RuntimeError('body')
    assert report['delta']==3 and report['rate_per_minute']==3
    with smoke.render_busy_reporting(io,report):io.count=0;io.pause(1)
    assert report['delta'] is None and report['rate_per_minute'] is None
    io.boots=[]
    with smoke.render_busy_reporting(io,report):io.boots.append({});io.pause(1)
    assert report['delta'] is None and 'reboot' in report['observation']
    io.json_command=lambda *args:status()
    with smoke.render_busy_reporting(io,report):io.pause(1)
    assert report['observation']=='unavailable' and report['delta'] is None


@pytest.mark.parametrize('failure',[None,'body','cleanup','restore','panic'])
def test_campaign_lifecycle_restores_after_failures_and_persists_before_signal_release(tmp_path,monkeypatch,failure):
    import boards,duo_bundle,p4_step14,echoear_item2_campaign
    monkeypatch.setattr(boards,'require_bench',lambda:7)
    monkeypatch.setattr(smoke,'EVIDENCE',tmp_path)
    monkeypatch.setattr(smoke,'authenticate',lambda *a,**kw:dict(manifest={}))
    monkeypatch.setattr(smoke,'verify_bundle',lambda *a:({},{}))
    monkeypatch.setattr(smoke,'digest',lambda *a:'test-sha')
    order=[]
    @contextmanager
    def adapters(*args):yield
    monkeypatch.setattr(duo_bundle,'frozen_adapters',adapters)
    monkeypatch.setattr(p4_step14,'restoring',adapters)
    class Guard:
        record={}
        pending_signals=[];phase_signals={};raising=False
        def __enter__(self):order.append('guard');return self
        def touch(self):order.append('armed')
        def __exit__(self,*exc):
            order.append('restore');self.record=dict(clean=failure!='restore')
        def release_signals(self):
            assert (args.out/'result.json').is_file()
            order.append('release')
    class IO:
        def require_boot(self):pass
        def __init__(self,*args):self.base=SimpleNamespace(hold=lambda:Guard());self.now=10;self.samples=0
        def clock(self):return self.now
        def json_command(self,*args):
            self.now+=30;self.samples+=1;return status(render_busy=9*(self.samples-1))
        @contextmanager
        def setup(self):
            order.append('setup')
            (args.out/'serial.log').write_text('panic: late cleanup failure' if failure=='panic' else 'synthetic serial')
            try:yield
            finally:
                order.append('cleanup')
                if failure=='cleanup':raise RuntimeError('cleanup')
    def run(io,part):
        order.append('body')
        if failure=='body':raise KeyboardInterrupt('body')
        return {'ok':True}
    monkeypatch.setattr(smoke,'run_part',run)
    args=SimpleNamespace(out=tmp_path/'echoear/idle/attempt-01',part='idle',board='echoear',bot_dir=tmp_path/'bot',bundle=tmp_path/'bundle')
    assert smoke.execute(args,factory=IO)==(0 if failure is None else 1)
    assert order.index('armed')<order.index('setup')<order.index('body')<order.index('cleanup')<order.index('restore')<order.index('release')
    result=json.loads((args.out/'result.json').read_text())
    assert result['passed']==(failure is None)
    assert result['restore']['clean']==(failure!='restore')
    assert result['render_busy']['delta']==9 and result['render_busy']['rate_per_minute']==18


def test_install_offline_reboots_after_origin_close_and_checks_residency(tmp_path,monkeypatch):
    import board_smoke
    seen=[]
    monkeypatch.setattr(board_smoke,'boot_check',lambda *a:seen.append('boot-check'))
    io=object.__new__(smoke.LiveIO)
    io.args=SimpleNamespace(part='install',board='echoear')
    io.manifest={};io.installed=dict(fetched='31',pinned='31');io.installed_mem=dict(used=310944,largest=100000,pinned=31)
    class Board:
        cursor=4
        def reset(self):seen.append('reset')
        def wait_for(self,pattern,*args,**kw):
            return 'DUO_INSTALL result=ready restored=31 fetched=0 pinned=31 screen=available'
        def lines_since(self,at):return []
    io.board=Board();io.json_command=lambda *a:io.installed_mem
    io.installed_inventory=[];io.inventory=lambda:[]
    io.authenticate_boot=lambda *a:board_smoke.boot_check([],{},'echoear');io.origin_record=dict(closed=True)
    result=io.install_offline()
    assert result['origin_unavailable'] and seen==['reset','boot-check']
    io.installed['fetched']='0'
    with pytest.raises(ValueError,match='fresh pack storage'):io.install_offline()


def test_setup_uses_owned_fixture_then_independent_cleanup_even_on_failure(tmp_path,monkeypatch):
    import duo_nvs,p4_product,step9a_assets
    from contextlib import nullcontext
    io=object.__new__(smoke.LiveIO);io.out=tmp_path
    io.args=SimpleNamespace(board='echoear',part='idle',bundle=tmp_path/'bundle',origin='http://origin:7874',server_url='http://bot:7861')
    io.manifest={};seen=[]
    class Board:
        cursor=1
        def reset(self):seen.append('reset')
        def command(self,text,*args):
            seen.append(text)
            if text=='touch-enable 0':raise RuntimeError('disable failed')
        def wait_for(self,*args,**kwargs):return 'DUO_INSTALL result=ready restored=0 fetched=31 pinned=31'
    board=Board()
    @contextmanager
    def opened():
        try:yield board
        finally:seen.append('serial-close')
    io.base=SimpleNamespace(flash=lambda *a:seen.append('flash'),open=opened)
    io.command=lambda text:board.command(text)
    io.json_command=lambda *a:dict(used=310944,largest=100000,pinned=31)
    io.inventory=lambda:[];io.begin=lambda:seen.append('begin');io.retain=lambda:seen.append('retain')
    io.authenticate_boot=lambda *a:seen.append('boot-check')
    monkeypatch.setattr(p4_product,'candidate_storage',lambda *a:nullcontext())
    monkeypatch.setattr(duo_nvs,'erase_candidate_nvs',lambda *a:seen.append('nvs-erase'))
    @contextmanager
    def origin(*args):
        seen.append('origin-open')
        try:yield dict(closed=False,requests=[])
        finally:seen.append('origin-closed')
    monkeypatch.setattr(smoke,'owned_origin',origin)
    with pytest.raises(ValueError,match='cleanup failed'):
        with io.setup():
            assert seen.index('origin-closed')<seen.index('begin')
            seen.append('body');raise KeyboardInterrupt('body')
    assert seen[-3:]==['touch-enable 0','demo hold','serial-close'] and 'retain' in seen


def test_inventory_requires_every_duo_hash_pinned_resident_and_persisted(tmp_path,monkeypatch):
    import step9a_support
    descriptor=json.loads((smoke.ROOT/'hil/fixtures/echoear360/duo-pack.json').read_text())
    frames=[c['faceplate'] for c in descriptor['characters']]+[f for c in descriptor['characters'] for layer in c['layers'] for f in layer['frames']]
    rows=[[f['hash'],f['bytes'],1,True,True,True,'none'] for f in frames]
    io=object.__new__(smoke.LiveIO);io.board=object()
    monkeypatch.setattr(step9a_support,'read',lambda *a:dict(revision=10,items=rows,next=256))
    assert io.inventory()==rows
    rows[0][5]=False
    with pytest.raises(ValueError,match='persisted'):io.inventory()


@pytest.mark.parametrize('bad',[False,True])
def test_connect_requires_hello_state_then_proof_on_current_generation(bad):
    io=FakeIO('connect');p=io.prior
    io.evidence=lambda mark:([],[
        dict(direction='received',monotonic_ns=1,data=dict(v=1,op='hello',session=p['session'],seq=1,have='angel')),
        dict(direction='sent',monotonic_ns=2,data=state(p))],[])
    io.proof_received_at=lambda p:1 if bad else 3
    if bad:
        with pytest.raises(ValueError,match='order'):smoke.run_part(io,'connect')
    else:assert smoke.run_part(io,'connect')['proof']==p


def test_playback_stop_caused_by_injected_interrupt_is_not_a_phase_miss():
    io=FakeIO('playback')
    original_evidence=io.evidence
    def evidence(mark):
        events,wire,voices=original_evidence(mark)
        if io.switched:events.append(dict(frame='BotStoppedSpeakingFrame',monotonic_ns=10_100_000_000))
        return events,wire,voices
    io.evidence=evidence
    assert smoke.run_part(io,'playback')['proof']['character']=='devil'


@pytest.mark.parametrize('ending',['LLMFullResponseEndFrame','InterruptionFrame','ErrorFrame','CancelFrame'])
def test_generation_window_requires_current_unmatched_start(ending):
    assert not smoke.phase_ready([dict(frame='LLMFullResponseStartFrame',frame_id=1),dict(frame=ending,frame_id=2)],'generating')
    assert not smoke.phase_ready([dict(frame='LLMFullResponseStartFrame',frame_id=1),dict(frame='LLMFullResponseEndFrame',frame_id=2),
                                  dict(frame='TranscriptionFrame',frame_id=3)],'generating')


def test_generation_deduplicates_observer_copies_and_allows_new_response_after_older_completion():
    rows=[dict(frame='LLMFullResponseStartFrame',frame_id=1),dict(frame='LLMFullResponseEndFrame',frame_id=2),
          dict(frame='LLMFullResponseStartFrame',frame_id=3)]
    assert smoke.phase_ready(rows+[rows[-1]],'generating')
    assert not smoke.phase_ready(rows+[dict(kind='tts-transport-write',context_id='current')],'generating')
    assert not smoke.phase_ready(rows+[dict(frame='InterruptionFrame',frame_id=4),rows[-1]],'generating')


@pytest.mark.parametrize('bad',['before-status','after-status','verified','operation','mutation-conn','publish-rev','publish-tx','outcome-conn','resync-seq'])
def test_status_and_transaction_evidence_must_form_one_coherent_witness(bad):
    b,a,p,q,events,wire=switched(drops=4 if bad=='resync-seq' else 0)
    if bad=='before-status':b.update(character='devil',rev=999)
    if bad=='after-status':a.update(character='angel',rev=999)
    if bad=='verified':events[1]['transaction']='unrelated'
    if bad=='outcome-conn':events[0]['conn']='other'
    for row in events:
        if row.get('kind')!='phase-timing':continue
        if bad=='operation' and row['phase']=='interrupt':row['operation']='other'
        if bad=='mutation-conn' and row['phase']=='mutation':row['conn']='other'
        if bad=='publish-rev' and row['phase']=='publish':row['rev']=999
        if bad=='publish-tx' and row['phase']=='publish':row['transaction']='other'
    if bad=='resync-seq':next(r['data'] for r in wire if r.get('data',{}).get('op')=='hello')['seq']=4
    with pytest.raises(ValueError):smoke.switch_witness(b,a,p,q,events,wire,drops=4 if bad=='resync-seq' else 0)


@pytest.mark.parametrize('bad',['pre-publication-respeak','restored-remainder','post-publication-respeak','wrong-error-context','wrong-error-id','target-audio','transport-remainder'])
def test_failed_turn_bracket_rejects_respeak_and_correlates_expected_fault(bad):
    io=FakeIO('tts-fail-next');original=io.evidence
    def evidence(mark):
        events,wire,voices=original(mark)
        if io.reverted:
            if bad in ('pre-publication-respeak','post-publication-respeak'):
                stamp=10_900_000_000 if bad=='pre-publication-respeak' else 11_500_000_000
                voices.append(dict(voice(proof('angel',3,2)),kind='duo-synthesis',context_id='forbidden',monotonic_ns=stamp))
                events.append(dict(kind='tts-transport-write',context_id='forbidden',samples=320,non_silent=True,monotonic_ns=stamp+10))
            if bad=='transport-remainder':events.append(dict(kind='tts-transport-write',context_id='restored',non_silent=True,monotonic_ns=10_900_000_000))
            if bad=='restored-remainder':
                voices.append(dict(kind='duo-voice-audio',voice='flux-hannah-en',context_id='prior-angel-context',bytes=640,monotonic_ns=10_900_000_000))
            if bad=='wrong-error-context':voices[1]['context_id']='unrelated'
            if bad=='wrong-error-id':events[-2]['frame_id']=123
            if bad=='target-audio':voices.append(dict(kind='duo-voice-audio',context_id='ctx',bytes=640,monotonic_ns=10_700_000_000))
        return events,wire,voices
    io.evidence=evidence
    with pytest.raises(ValueError):smoke.run_part(io,'tts-fail-next')


def test_failed_turn_bracket_closes_at_actual_next_playback_start():
    io=FakeIO('tts-fail-next');original=io.evidence
    def evidence(mark):
        events,wire,voices=original(mark)
        if io.reverted:voices.append(dict(kind='duo-synthesis',context_id='next-user-turn',monotonic_ns=12_100_000_000))
        return events,wire,voices
    io.evidence=evidence
    row=smoke.run_part(io,'tts-fail-next')
    assert row['no_respeak']['end']==12_000_000_000 and row['no_respeak']['first_context']=='ctx'


def test_missed_receipt_phase_is_retryable_only_after_other_switch_gates_pass():
    io=FakeIO('generating');original=io.evidence
    def evidence(mark):
        events,wire,voices=original(mark)
        if io.switched:events.append(dict(frame='LLMFullResponseEndFrame',monotonic_ns=10_000_000_000))
        return events,wire,voices
    io.evidence=evidence
    with pytest.raises(smoke.MissedInjectionWindow) as caught:smoke.run_part(io,'generating')
    assert caught.value.witness['tap_latency']['milliseconds']==0
    io.after['rev']=999
    # Invalid evidence is final even when the observed phase was missed.
    with pytest.raises(ValueError) as caught:smoke.run_part(io,'generating')
    assert not isinstance(caught.value,smoke.MissedInjectionWindow)


def boot_manifest(board='echoear'):
    return dict(schema=1,board=board,target={'echoear':'esp32s3','p4xc':'esp32p4'}[board],elf_sha256='a'*64,aec_enabled=True,autostart=True,boot_ref_gain_db=30)


def boot_lines(board='echoear',bad=None):
    chip='ESP32-S3 step 7' if board=='echoear' else 'esp32p4'
    return ['I (1) dg-esp32: '+chip+' audio elf_sha256='+('b' if bad=='elf' else 'a')*64,
        'DG_BOOT board='+board+' chip='+{'echoear':'esp32s3','p4xc':'esp32p4'}[board]+' variant=duo',
        'MIC3_GAIN=0x19 analog_db=27' if bad=='gain' else 'MIC3_GAIN=0x1a analog_db=30','READY transport-console']


@pytest.mark.parametrize('path,board,part',[
    ('precondition','echoear','install'),('offline','echoear','install'),('offline','p4xc','install')]+
    [('candidate',board,part) for board in ('echoear','p4xc') for part in smoke.PARTS])
def test_bootloader_flash_requires_captured_reset_before_authentication(tmp_path,monkeypatch,path,board,part):
    import duo_nvs,make_bundle,p4_product
    from contextlib import nullcontext
    from echoear_aec_bench import NORMAL_HASH
    io=object.__new__(smoke.LiveIO);io.out=tmp_path;io.manifest=boot_manifest(board)
    io.args=SimpleNamespace(board=board,part=part,bundle=tmp_path/'bundle',reference_only=False,
                            origin='http://origin:7874',server_url='http://bot:7861')
    io.boots=[];io.candidate_boot=True;seen=[]
    class Board:
        cursor=7
        capturing=False
        running=False
        rows=[]
        def reset(self):
            assert self.capturing,'reset must follow serial reader/log acquisition'
            assert not io.candidate_boot,'stale boot authorization must be cleared before reset'
            seen.append('reset');self.running=True
            lines=['ESP-ROM: captured first ROM line']+boot_lines(board)
            if path=='precondition':
                lines=[line.replace('a'*64,NORMAL_HASH).replace('variant=duo','variant=normal') for line in lines]
            for line in lines:
                self.cursor+=1;self.rows.append((self.cursor,float(self.cursor),line))
            (io.base.out/'serial.log').write_text('\n'.join(lines)+'\n')
        def wait_for(self,pattern,*args,since=0):
            assert self.running,'flash leaves the bootloader silent until an explicit reset'
            matches=[line for _,_,line in self.lines_since(since) if re.search(pattern,line)]
            if matches:return matches[0]
            if 'DUO_INSTALL' in pattern:
                return 'DUO_INSTALL result=ready restored=31 fetched=0 pinned=31'
            raise TimeoutError('no fresh matching serial line')
        def lines_since(self,at):return [row for row in self.rows if row[0]>at]
        def command(self,text,*args):
            assert self.running and io.boots[-1]['passed']
            seen.append(text)
    link=Board()
    def flash(*args):
        seen.append('flash');link.running=False
        assert not link.rows
    @contextmanager
    def opened():
        seen.append('open');link.capturing=True
        assert not link.running and not link.rows
        try:yield link
        finally:link.capturing=False
    io.base=SimpleNamespace(out=tmp_path,manifest=io.manifest,flash=flash,open=opened)
    authenticate=io.authenticate_boot
    def checked(since,label,manifest=None):
        seen.append('authenticate')
        assert link.running,'authenticate must follow reset'
        return authenticate(since,label,manifest)
    io.authenticate_boot=checked
    io.command=lambda text:link.command(text)
    io.json_command=lambda *args:dict(used=310944,largest=100000,pinned=31)
    io.inventory=lambda **kw:[];io.retain=lambda:None;io.begin=lambda:None
    io.prepare_s3_storage=io.prepare_s3_storage if path=='precondition' else lambda:None
    monkeypatch.setattr(p4_product,'candidate_storage',lambda *args:nullcontext())
    def erase(bundle,out,selected):
        assert bundle==io.args.bundle and out==tmp_path and selected==board
        seen.append('nvs-erase')
    monkeypatch.setattr(duo_nvs,'erase_candidate_nvs',erase)
    @contextmanager
    def origin(*args):yield dict(closed=True,requests=[dict(path='/'+sha+'.dgi') for sha in smoke.duo_hashes()])
    monkeypatch.setattr(smoke,'owned_origin',origin)
    if path=='precondition':
        monkeypatch.setattr(make_bundle,'verify_bundle',lambda *args,**kw:(dict(elf_sha256=NORMAL_HASH,aec_enabled=False),{}))
        io.prepare_s3_storage()
        serial=tmp_path/'fresh-storage/serial.log'
        assert io.storage_precondition['passed'] and not io.candidate_boot
    elif path=='candidate':
        with io.setup():assert io.candidate_boot
        assert seen.index('nvs-erase')<seen.index('flash')
        configurations=[json.loads(text.split(' ',2)[2]) for text in seen if text.startswith('demo configure ')]
        assert configurations[0]==dict(url=io.args.server_url,volume_db=-6,enabled=False)
        assert [row['enabled'] for row in configurations]==([False] if part=='install' else [False,True])
        assert 'duo origin '+io.args.origin in seen
        serial=tmp_path/'serial.log'
    else:
        io.args.part='install';io.board=link;link.capturing=True
        io.installed=dict(fetched='26',pinned='31');io.installed_mem=io.json_command()
        io.installed_inventory=[];io.origin_record=dict(closed=True)
        assert io.install_offline()['origin_unavailable'] and io.candidate_boot
        serial=tmp_path/'serial.log'
    assert seen.index('reset')<seen.index('authenticate')
    if path!='offline':assert seen.index('flash')<seen.index('open')<seen.index('reset')
    record=json.loads((tmp_path/'boot-01.json').read_text())
    assert record['passed'] and record['since']==7
    assert record['lines'][0]=='ESP-ROM: captured first ROM line'
    assert serial.read_text().splitlines()==record['lines']
    if path!='candidate':assert 'nvs-erase' not in seen


@pytest.mark.parametrize('part',smoke.PARTS)
@pytest.mark.parametrize('board',['echoear','p4xc'])
@pytest.mark.parametrize('bad',['elf','gain'])
def test_every_part_rejects_wrong_live_image_before_scoring(part,board,bad,tmp_path,monkeypatch):
    import duo_nvs,p4_product
    from contextlib import nullcontext
    io=object.__new__(smoke.LiveIO);io.out=tmp_path;io.manifest=boot_manifest(board)
    io.args=SimpleNamespace(board=board,part=part,bundle=tmp_path/'bundle')
    io.boots=[];io.candidate_boot=False;commands=[]
    class Board:
        cursor=1
        def reset(self):pass
        def command(self,text,*args):commands.append(text)
        def wait_for(self,*args,**kw):return 'READY transport-console'
        def lines_since(self,*args):return [(n,n,line) for n,line in enumerate(boot_lines(board,bad),1)]
    @contextmanager
    def opened():yield Board()
    io.base=SimpleNamespace(flash=lambda *args:None,open=opened)
    io.retain=lambda:None;io.prepare_s3_storage=lambda:None
    monkeypatch.setattr(p4_product,'candidate_storage',lambda *args:nullcontext())
    monkeypatch.setattr(duo_nvs,'erase_candidate_nvs',lambda *args:None)
    with pytest.raises(ValueError,match='hash|GAIN'):
        with io.setup():pytest.fail('wrong firmware reached scoring')
    assert json.loads((tmp_path/'boot-01.json').read_text())['passed'] is False
    assert not io.candidate_boot and commands==['touch-enable 0','demo hold']


@pytest.mark.parametrize('board',['echoear','p4xc'])
def test_successful_acquisition_retains_live_boot_record(tmp_path,board):
    io=object.__new__(smoke.LiveIO);io.out=tmp_path;io.manifest=boot_manifest(board);io.args=SimpleNamespace(board=board)
    io.boots=[];io.candidate_boot=False
    io.board=SimpleNamespace(wait_for=lambda *a,**kw:None,lines_since=lambda *a:[(n,n,line) for n,line in enumerate(boot_lines(board))])
    assert io.authenticate_boot(0,'candidate')['elf_sha256']=='a'*64
    io.require_boot()
    assert json.loads((tmp_path/'boot-01.json').read_text())['passed'] and len(io.boots)==1


@pytest.mark.parametrize('present_count,damage',[(26,None),(26,'legacy-changed'),(26,'duo-remains'),(0,None),(5,None)])
def test_s3_script_deletes_only_present_duo_hashes_and_preserves_other_assets(tmp_path,monkeypatch,present_count,damage):
    import make_bundle,step9a_support
    from echoear_aec_bench import NORMAL_HASH
    targets=smoke.duo_hashes();legacy=[['f'*64,100,0,False,True,False,'none'],['e'*64,200,0,False,True,False,'none']]
    present=set(sorted(targets)[:present_count])
    before=legacy+[[h,20,0,False,True,False,'none'] for h in sorted(present)]
    storage=copy.deepcopy(before);deleted=[];seen=[]
    monkeypatch.setattr(make_bundle,'verify_bundle',lambda *a,**kw:(dict(elf_sha256=NORMAL_HASH),{}))
    io=object.__new__(smoke.LiveIO);io.out=tmp_path;io.manifest={}
    io.args=SimpleNamespace(reference_only=False)
    class Board:
        cursor=1
        def reset(self):seen.append('legacy-reset')
        def command(self,text,*args):seen.append(text)
    @contextmanager
    def opened():yield Board()
    io.base=SimpleNamespace(out=tmp_path,manifest={},flash=lambda *args:seen.append('legacy-flash'),open=opened)
    io.authenticate_boot=lambda *args:seen.append('legacy-boot-checked')
    io.inventory=lambda **kw:copy.deepcopy(storage)
    def request(board,kind,data,rid):
        assert kind=='delete' and data['hash'] in present
        assert any(row[0]==data['hash'] for row in storage)
        deleted.append(data['hash'])
        storage[:]=[r for r in storage if r[0]!=data['hash']]
        if len(deleted)==26:
            if damage=='legacy-changed':storage[0][1]+=1
            if damage=='duo-remains':storage.append(next(r for r in before if r[0] in targets))
        return dict(id=rid)
    monkeypatch.setattr(step9a_support,'request',request)
    monkeypatch.setattr(step9a_support,'terminal',lambda *args:dict(error='none'))
    if damage:
        with pytest.raises(ValueError):io.prepare_s3_storage()
    else:
        io.prepare_s3_storage()
        assert storage==legacy and io.storage_precondition['passed']
    assert set(deleted)==present and len(deleted)==present_count and 'disp init' in seen
    assert io.storage_precondition['present']==sorted(present)
    assert io.storage_precondition['absent']==sorted(targets-present)
    assert io.base.out==tmp_path and not io.args.reference_only
    assert (tmp_path/'fresh-storage/precondition.json').is_file()


def test_origin_accepts_only_local_bound_fixed_fixture_endpoint():
    bound=[]
    class Socket:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def bind(self,address):bound.append(address)
    assert smoke.check_origin('http://192.168.7.222:7874',socket_factory=lambda *a:Socket())==7874
    assert bound==[('192.168.7.222',0)]
    for url in ('http://host:7875','https://host:7874','http://host:7874/path','http://user@host:7874'):
        with pytest.raises(ValueError):smoke.check_origin(url,socket_factory=lambda *a:Socket())
    class Remote(Socket):
        def bind(self,address):raise OSError('not a local interface')
    with pytest.raises(OSError):smoke.check_origin('http://remote:7874',socket_factory=lambda *a:Remote())


def test_owned_origin_records_requests_and_marks_closure_without_any_real_socket(tmp_path,monkeypatch):
    import step9a_assets
    monkeypatch.setattr(smoke,'check_origin',lambda url:7874)
    calls=[]
    monkeypatch.setattr(step9a_assets.SimpleHTTPRequestHandler,'do_GET',lambda handler:calls.append(handler.path))
    original=step9a_assets.SimpleHTTPRequestHandler.do_GET
    @contextmanager
    def server(**kw):
        handler=SimpleNamespace(path='/'+next(iter(smoke.duo_hashes()))+'.dgi',client_address=('192.168.7.10',123),
                                server=SimpleNamespace(server_address=('0.0.0.0',7874)))
        step9a_assets.SimpleHTTPRequestHandler.do_GET(handler)
        yield object()
    monkeypatch.setattr(step9a_assets,'asset_server',server)
    with smoke.owned_origin('http://192.168.7.222:7874',tmp_path) as row:assert not row['closed']
    saved=json.loads((tmp_path/'owned-origin.json').read_text())
    assert saved['closed'] and saved['local_bind_verified'] and saved['requests'][0]['client']=='192.168.7.10'
    assert calls and step9a_assets.SimpleHTTPRequestHandler.do_GET is original


@pytest.mark.parametrize('failure',['miss','phase-not-reached','wrong-evidence','miss-cleanup','miss-restore','miss-panic'])
def test_driver_enforces_retry_classification_and_preserves_both_attempts(tmp_path,monkeypatch,failure):
    import boards,duo_bundle,p4_step14
    from contextlib import nullcontext
    monkeypatch.setattr(boards,'require_bench',lambda:7);monkeypatch.setattr(smoke,'EVIDENCE',tmp_path)
    monkeypatch.setattr(smoke,'authenticate',lambda *a,**kw:{});monkeypatch.setattr(smoke,'verify_bundle',lambda *a:({},{}))
    monkeypatch.setattr(smoke,'digest',lambda *a:'fake-manifest-hash')
    monkeypatch.setattr(duo_bundle,'frozen_adapters',lambda:nullcontext());monkeypatch.setattr(p4_step14,'restoring',lambda *a:nullcontext())
    class Guard:
        pending_signals=[];phase_signals={};record={};raising=False
        def __enter__(self):return self
        def touch(self):pass
        def __exit__(self,*a):self.record=dict(clean=failure!='miss-restore')
        def release_signals(self):pass
    class IO:
        def __init__(self,*a):self.base=SimpleNamespace(hold=lambda:Guard());self.tap_latencies=[dict(milliseconds=15)];self.boots=[]
        def require_boot(self):pass
        @contextmanager
        def setup(self):
            (args.out/'serial.log').write_text('panic: after phase miss' if failure=='miss-panic' else 'synthetic serial')
            try:yield
            finally:
                if failure=='miss-cleanup':raise RuntimeError('cleanup failed')
    def part(io,part):
        if failure.startswith('miss'):raise smoke.MissedInjectionWindow(dict(phase='generating',tap_latency=dict(milliseconds=15)))
        if failure=='phase-not-reached':raise TimeoutError('phase not reached; no injected request')
        raise ValueError('incoherent evidence')
    monkeypatch.setattr(smoke,'run_part',part)
    parent=tmp_path/'echoear/generating'
    args=SimpleNamespace(out=parent/'attempt-01',part='generating',board='echoear',bot_dir=tmp_path/'bot',bundle=tmp_path/'bundle')
    assert smoke.execute(args,factory=IO)==1
    row=json.loads((args.out/'result.json').read_text())
    assert row['retryable']==(failure=='miss')
    assert row['tap_latencies']==[dict(milliseconds=15)]
    args.out=parent/'attempt-02'
    if failure=='miss':
        assert smoke.execute(args,factory=IO)==1
        second=json.loads((args.out/'result.json').read_text());assert not second['retryable'] and second['attempt']==2
        summary=json.loads((parent/'attempts-summary.json').read_text());assert len(summary['attempts'])==2
        args.out=parent/'attempt-03'
        with pytest.raises(ValueError):smoke.execute(args,factory=IO)
    else:
        with pytest.raises(ValueError):smoke.execute(args,factory=IO)


@pytest.mark.parametrize('bad',['elf','gain'])
@pytest.mark.parametrize('board',['echoear','p4xc'])
def test_offline_reset_requires_fresh_live_boot_identity_before_install_scoring(tmp_path,bad,board):
    io=object.__new__(smoke.LiveIO);io.out=tmp_path;io.args=SimpleNamespace(part='install',board=board)
    io.manifest=boot_manifest(board);io.candidate_boot=True;io.boots=[]
    class Board:
        cursor=5
        def reset(self):pass
        def wait_for(self,*args,**kw):pass
        def lines_since(self,at):return [(at+i,i,line) for i,line in enumerate(boot_lines(board,bad))]
    io.board=Board()
    with pytest.raises(ValueError):io.install_offline()
    assert not io.candidate_boot and not json.loads((tmp_path/'boot-01.json').read_text())['passed']


def test_turn_completion_must_belong_to_real_voice_context():
    p=proof();events=turn_events()
    next(row for row in events if row.get('frame')=='TTSStoppedFrame')['context_id']='other'
    with pytest.raises(ValueError,match='completed'):smoke.turn_verdict(events,[voice(p)],p)


def test_injection_latency_is_recorded_even_when_no_outcome_or_proof_arrives():
    io=FakeIO('idle')
    def never_settles(predicate,seconds,label):
        io.current=(dict(io.before,pending=True),io.prior)
        assert not predicate()
        raise TimeoutError('no answer/proof')
    io.wait=never_settles
    with pytest.raises(TimeoutError):smoke.run_part(io,'idle')
    assert len(io.tap_latencies)==1 and io.tap_latencies[0]['milliseconds']==0
    smoke.record_injected_latency(io,io.wire)
    assert len(io.tap_latencies)==1  # cleanup does not duplicate the receipt


@pytest.mark.parametrize('failure_frame',['ErrorFrame','FatalErrorFrame'])
@pytest.mark.parametrize('phase',['generating','playback'])
def test_service_failure_before_toggle_receipt_is_final_through_driver_and_ledger(tmp_path,monkeypatch,failure_frame,phase):
    import boards,duo_bundle,p4_step14
    from contextlib import nullcontext
    monkeypatch.setattr(boards,'require_bench',lambda:7);monkeypatch.setattr(smoke,'EVIDENCE',tmp_path)
    monkeypatch.setattr(smoke,'authenticate',lambda *a,**kw:{});monkeypatch.setattr(smoke,'verify_bundle',lambda *a:({},{}))
    monkeypatch.setattr(smoke,'digest',lambda *a:'fake-manifest-hash')
    monkeypatch.setattr(duo_bundle,'frozen_adapters',lambda:nullcontext());monkeypatch.setattr(p4_step14,'restoring',lambda *a:nullcontext())
    created=[]
    class Guard:
        pending_signals=[];phase_signals={};record={};raising=False
        def __enter__(self):return self
        def touch(self):pass
        def __exit__(self,*a):self.record=dict(clean=True)
        def release_signals(self):pass
    class IO(FakeIO):
        def __init__(self,args,out,*a):
            super().__init__(phase);self.out=out;self.base=SimpleNamespace(hold=lambda:Guard());self.boots=[]
            created.append(self)
            for row in self.wire:
                if row.get('direction')=='received':row['monotonic_ns']=10_100_000_000
        def evidence(self,mark):
            events,wire,voices=super().evidence(mark)
            if self.switched:events.append(dict(frame=failure_frame,frame_id=989,error='provider failed',monotonic_ns=10_050_000_000))
            return events,wire,voices
        @contextmanager
        def setup(self):
            (self.out/'serial.log').write_text('synthetic clean serial\n')
            try:yield
            finally:
                from duo_identity import write_new
                write_new(self.out/'bot-evidence.json',dict(events=self.evidence(0)[0]))
    parent=tmp_path/'echoear'/phase
    args=SimpleNamespace(out=parent/'attempt-01',part=phase,board='echoear',bot_dir=tmp_path/'bot',bundle=tmp_path/'bundle')
    assert smoke.execute(args,factory=IO)==1
    result=json.loads((args.out/'result.json').read_text())
    assert not result['passed'] and result['retryable'] is False
    assert result['restore']['clean'] and not result['cleanup_errors']
    assert 'service/pipeline failure' in result['error']
    assert result['tap_latencies'][0]['milliseconds']==100
    retained=json.loads((args.out/'bot-evidence.json').read_text())
    assert any(row.get('frame')==failure_frame for row in retained['events'])
    args.out=parent/'attempt-02'
    with pytest.raises(ValueError,match='only a clean'):smoke.execute(args,factory=IO)
    assert len(created)==1 and not args.out.exists()
    ledger=json.loads((parent/'attempts.json').read_text())
    assert len(ledger)==1 and ledger[0]['retryable'] is False


@pytest.mark.parametrize('phase,ending',[('generating','LLMFullResponseEndFrame'),('generating','TTSStartedFrame'),('playback','BotStoppedSpeakingFrame')])
def test_clean_window_miss_still_earns_one_retry_and_second_attempt_can_pass(tmp_path,monkeypatch,phase,ending):
    import boards,duo_bundle,p4_step14
    from contextlib import nullcontext
    monkeypatch.setattr(boards,'require_bench',lambda:7);monkeypatch.setattr(smoke,'EVIDENCE',tmp_path)
    monkeypatch.setattr(smoke,'authenticate',lambda *a,**kw:{});monkeypatch.setattr(smoke,'verify_bundle',lambda *a:({},{}))
    monkeypatch.setattr(smoke,'digest',lambda *a:'fake-manifest-hash')
    monkeypatch.setattr(duo_bundle,'frozen_adapters',lambda:nullcontext());monkeypatch.setattr(p4_step14,'restoring',lambda *a:nullcontext())
    class Guard:
        pending_signals=[];phase_signals={};record={};raising=False
        def __enter__(self):return self
        def touch(self):pass
        def __exit__(self,*a):self.record=dict(clean=True)
        def release_signals(self):pass
    class IO(FakeIO):
        def __init__(self,args,out,*a):
            super().__init__(phase);self.out=out;self.miss=out.name=='attempt-01';self.base=SimpleNamespace(hold=lambda:Guard());self.boots=[]
            for row in self.wire:
                if row.get('direction')=='received':row['monotonic_ns']=10_100_000_000
        def evidence(self,mark):
            events,wire,voices=super().evidence(mark)
            if self.switched and self.miss:events.append(dict(frame=ending,frame_id=989,monotonic_ns=10_050_000_000))
            return events,wire,voices
        @contextmanager
        def setup(self):
            (self.out/'serial.log').write_text('synthetic clean serial\n')
            yield
    parent=tmp_path/'echoear'/phase
    args=SimpleNamespace(out=parent/'attempt-01',part=phase,board='echoear',bot_dir=tmp_path/'bot',bundle=tmp_path/'bundle')
    assert smoke.execute(args,factory=IO)==1
    first=json.loads((args.out/'result.json').read_text())
    assert first['retryable'] and not first['passed'] and not first['cleanup_errors']
    args.out=parent/'attempt-02'
    assert smoke.execute(args,factory=IO)==0
    second=json.loads((args.out/'result.json').read_text())
    assert second['passed'] and not second['retryable']
    summary=json.loads((parent/'attempts-summary.json').read_text())
    assert len(summary['attempts'])==2 and summary['attempts'][0]['retryable'] and summary['attempts'][1]['passed']


@pytest.mark.parametrize('phase',['generating','playback'])
def test_toggle_interruption_after_receipt_is_kept_as_legitimate(phase):
    io=FakeIO(phase);original=io.evidence
    def evidence(mark):
        events,wire,voices=original(mark)
        if io.switched:events.append(dict(frame='InterruptionFrame',frame_id=989,monotonic_ns=10_200_000_000))
        return events,wire,voices
    io.evidence=evidence
    assert smoke.run_part(io,phase)['proof']['character']=='devil'


@pytest.mark.parametrize('frame',['CancelFrame','EndFrame','StopFrame','InterruptionFrame'])
def test_pipeline_end_or_response_interruption_before_receipt_is_not_a_clean_miss(frame):
    io=FakeIO('generating');original=io.evidence
    for row in io.wire:
        if row.get('direction')=='received':row['monotonic_ns']=10_100_000_000
    def evidence(mark):
        events,wire,voices=original(mark)
        if io.switched:events.append(dict(frame=frame,frame_id=989,monotonic_ns=10_050_000_000))
        return events,wire,voices
    io.evidence=evidence
    with pytest.raises(ValueError) as caught:smoke.run_part(io,'generating')
    assert not isinstance(caught.value,smoke.MissedInjectionWindow)


def nvs_table(offset=0x9000,size=0x3000,*,damage=None):
    import hashlib,struct
    rows=[('nvs',1,2,offset,size,0),('factory',0,0,0x100000,0x200000,0),
          ('storage',1,0x83,0x410000,0x400000,0)]
    if damage=='missing':rows=rows[1:]
    if damage=='duplicate':rows.append(rows[0])
    if damage=='type':rows[0]=('nvs',0,2,offset,size,0)
    if damage=='subtype':rows[0]=('nvs',1,0x83,offset,size,0)
    if damage=='flags':rows[0]=('nvs',1,2,offset,size,1)
    if damage=='unaligned':rows[0]=('nvs',1,2,offset+1,size,0)
    if damage=='overlap':rows.append(('phy',1,1,offset,0x1000,0))
    data=b''.join(struct.pack('<2sBBLL16sL',b'\xaa\x50',kind,subtype,at,length,name.encode(),flags)
                  for name,kind,subtype,at,length,flags in rows)
    data+=b'\xeb\xeb'+b'\xff'*14+hashlib.md5(data).digest()
    data+=b'\xff'*(0xc00-len(data))
    if damage=='md5':data=data[:10]+bytes([data[10]^1])+data[11:]
    if damage=='padding':data=data[:-1]+b'\x00'
    return data


@pytest.fixture
def nvs_bundle(tmp_path,monkeypatch):
    import boards,board_smoke,duo_bundle
    root=tmp_path/'bundle';root.mkdir()
    table=root/'partition-table.bin';table.write_bytes(nvs_table())
    (root/'app.bin').write_bytes(b'APP');(root/'manifest.json').write_text('{}')
    manifest=dict(elf_sha256='a'*64)
    flash=dict(flash_files={'0x8000':'partition-table.bin','0x100000':'app.bin'})
    monkeypatch.setattr(duo_bundle,'verify_bundle',lambda *a:(manifest,flash))
    monkeypatch.setattr(boards,'require_bench',lambda:123)
    monkeypatch.setattr(boards,'checked_esptool_python',lambda:'fake-esptool-python')
    monkeypatch.setattr(board_smoke,'verify_flash_mac',lambda board:boards.BOARDS[board]['by_id'])
    return root,table,flash


@pytest.mark.parametrize('board',['echoear','p4xc'])
@pytest.mark.parametrize('offset,size',[(0x9000,0x3000),(0x20000,0x5000)])
def test_nvs_erase_uses_authenticated_partition_and_inherited_lock(tmp_path,nvs_bundle,board,offset,size):
    import duo_nvs
    from pathlib import Path
    bundle,table,flash=nvs_bundle;table.write_bytes(nvs_table(offset,size));commands=[]
    def runner(command,**kw):
        commands.append(command)
        assert kw['pass_fds']==(123,) and kw['check'] and kw['timeout']==180
        assert command[command.index('--after')+1]=='no-reset'
        assert command[command.index('--chip')+1]=={'echoear':'esp32s3','p4xc':'esp32p4'}[board]
        if 'read-flash' in command:Path(command[-1]).write_bytes(b'\xff'*size)
    result=duo_nvs.erase_candidate_nvs(bundle,tmp_path,board,runner=runner)
    assert result['partition']['offset']==offset and result['partition']['size']==size
    assert commands[0][-3:]==['erase-region',hex(offset),hex(size)]
    assert commands[1][-4:-1]==['read-flash',hex(offset),hex(size)]
    assert result['passed'] and result['readback_verified'] and result['non_ff_bytes']==0
    assert json.loads((tmp_path/'candidate-nvs-erase.json').read_text())==result
    assert (bundle/'app.bin').read_bytes()==b'APP' and table.read_bytes()==nvs_table(offset,size)


@pytest.mark.parametrize('damage',['missing','duplicate','type','subtype','flags','unaligned','overlap','md5','padding','flash-overlap'])
def test_nvs_plan_rejects_unsafe_partition_before_device_access(nvs_bundle,monkeypatch,damage):
    import boards,duo_nvs
    bundle,table,flash=nvs_bundle
    table.write_bytes(nvs_table(damage=damage))
    if damage=='flash-overlap':flash['flash_files']['0x9000']='app.bin'
    monkeypatch.setattr(boards,'require_bench',lambda:pytest.fail('unsafe partition reached bench access'))
    with pytest.raises(ValueError):duo_nvs.erase_candidate_nvs(bundle,bundle.parent,'echoear')


@pytest.mark.parametrize('board',['echoear','p4xc'])
@pytest.mark.parametrize('failure',['erase-region','read-flash','short','dirty','missing'])
def test_nvs_failure_is_final_before_candidate_flash_and_still_restores(tmp_path,nvs_bundle,monkeypatch,board,failure):
    import duo_nvs,duo_bundle,p4_step14,p4_product,subprocess
    from contextlib import nullcontext
    from pathlib import Path
    bundle,table,flash=nvs_bundle;order=[]
    args=SimpleNamespace(out=tmp_path/board/'connect/attempt-01',board=board,part='connect',bundle=bundle,bot_dir=tmp_path/'bot')
    monkeypatch.setattr(smoke,'EVIDENCE',tmp_path)
    monkeypatch.setattr(smoke,'authenticate',lambda *a,**kw:dict(manifest={}))
    monkeypatch.setattr(smoke,'verify_bundle',lambda *a:({},{}))
    monkeypatch.setattr(duo_bundle,'frozen_adapters',lambda:nullcontext())
    monkeypatch.setattr(p4_step14,'restoring',lambda *a:nullcontext())
    monkeypatch.setattr(p4_product,'candidate_storage',lambda *a:nullcontext())
    class Guard:
        record={};pending_signals=[];phase_signals={};raising=False
        def __enter__(self):return self
        def touch(self):order.append('armed')
        def __exit__(self,*exc):order.append('restore');self.record=dict(clean=True)
        def release_signals(self):
            assert (args.out/'result.json').is_file()
            order.append('release')
    class IO(smoke.LiveIO):
        def __init__(self,args,out,manifest,identity):
            self.args,self.out,self.manifest=args,out,manifest
            self.base=SimpleNamespace(hold=lambda:Guard(),flash=lambda *a:pytest.fail('failed erase reached flash'))
    def runner(command,**kw):
        phase='erase-region' if 'erase-region' in command else 'read-flash';order.append(phase)
        if failure==phase:raise subprocess.CalledProcessError(1,command)
        if phase=='read-flash' and failure!='missing':
            Path(command[-1]).write_bytes(b'\xff'*0x2000 if failure=='short' else b'\xff'*0x2fff+b'\x00')
    erase=duo_nvs.erase_candidate_nvs
    monkeypatch.setattr(duo_nvs,'erase_candidate_nvs',lambda *a:erase(*a,runner=runner))
    assert smoke.execute(args,factory=IO)==1
    result=json.loads((args.out/'result.json').read_text())
    assert not result['passed'] and not result['retryable'] and result['restore']['clean']
    assert order[0]=='armed' and order.index('restore')<order.index('release')
    assert order.index('erase-region')<order.index('restore')
    evidence=json.loads((args.out/'candidate-nvs-erase.json').read_text())
    assert not evidence['passed'] and not evidence['readback_verified'] and 'error' in evidence
    args.out=args.out.parent/'attempt-02'
    with pytest.raises(ValueError,match='only a clean'):smoke.execute(args,factory=IO)
