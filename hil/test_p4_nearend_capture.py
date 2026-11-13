"""Offline plan/retry and fake-device R7 safety coverage; no bench access."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import numpy as np
import p4_nearend_capture as c
from echoear_aec_timing import sample_interval as physical_sample_interval
from test_wire_contract import IDENTITY


@pytest.fixture
def rig(tmp_path,monkeypatch):
    import alsa,boards,p4_step14,p4_product
    state=SimpleNamespace(calls=[],fail=None,noise=False,clean=True,restore_action='p4-product-of-record')
    bundle=tmp_path/'bundle';bundle.mkdir()
    manifest=dict(elf_sha256=c.ELFS['p4xc'],board='p4xc',profile='aec-normal',aec_enabled=True,autostart=False)
    (bundle/'manifest.json').write_text(json.dumps(manifest))
    identity=tmp_path/'identity.json';identity.write_text(json.dumps(IDENTITY))
    plan=dict(bundles={b:dict(path=str(bundle)) for b in c.ELFS},identity=IDENTITY,
        schedule={b:c.schedule(b) for b in c.ELFS},s3_restore_elf=c.S3_RESTORE,
        fixtures=[dict(id='long',path='long.wav'),dict(id='italy',path='italy.wav')])
    args=SimpleNamespace(board='p4xc',bundle=bundle,out=tmp_path/'p4xc/part-0/attempt-01',part=0,
        restore_record=tmp_path/'restore.json',restore_record_sha256='f'*64,bot_identity=identity)
    monkeypatch.setattr(c,'validate_plan',lambda p:p)
    monkeypatch.setattr(boards,'require_bench',lambda:state.calls.append('bench'))
    def checked(*args):
        state.calls.append('pin-check')
        if state.fail=='pin':raise ValueError('bad pin')
    monkeypatch.setattr(p4_step14,'restore_record',checked)
    def room(*args):
        state.calls.append('room')
        if state.fail=='room':raise ValueError('room failed')
        return dict(noise_contaminated=state.noise)
    monkeypatch.setattr(alsa,'record_room_baseline',room)
    monkeypatch.setattr(alsa,'resolve_jabra',lambda:('card','device'))
    monkeypatch.setattr(alsa,'set_volumes',lambda *args:None)
    monkeypatch.setattr(c,'verify_bundle',lambda *args:(manifest,{}))
    @contextmanager
    def restoring(path,sha):
        assert path==args.restore_record and sha==args.restore_record_sha256
        state.calls.append('R7-enter')
        try:yield
        finally:state.calls.append('R7-exit')
    @contextmanager
    def storage(board,bundle,out):
        assert board==args.board and bundle==args.bundle and out==args.out
        state.calls.append('storage-enter')
        try:yield
        finally:state.calls.append('storage-exit')
    monkeypatch.setattr(p4_step14,'restoring',restoring);monkeypatch.setattr(p4_product,'candidate_storage',storage)
    class Guard:
        success=False
        def touch(self):state.calls.append('touch')
        def __enter__(self):state.calls.append('hold-enter');return self
        def __exit__(self,*exc):
            state.calls.append('restore');self.record=dict(clean=state.clean,action=state.restore_action,
                restored_elf_sha256=c.S3_RESTORE)
        def release_signals(self):state.calls.append('signals-released')
    class IO:
        def __init__(self,*args):
            state.calls.append('backend')
            if state.fail=='bot':raise ValueError('bot identity differs')
        def preflight_tools(self):state.calls.append('tools')
        def preflight_restore(self):state.calls.append('preflight-restore')
        def hold(self):return Guard()
        def flash(self,*args):
            state.calls.append('flash')
            if state.fail=='flash':raise RuntimeError('flash failed')
        @contextmanager
        def open(self):state.calls.append('open');yield object()
        def boot(self,*args):state.calls.append('boot')
        def prepare_manual(self,*args):state.calls.append('prepare')
        def begin(self,*args):
            state.calls.append('begin')
            if state.fail=='begin':raise RuntimeError('begin failed')
        def original(self,board,folder,*args):
            state.calls.append('stimulus')
            assert (folder.parent/'trial-start.json').is_file()
            if state.fail=='abort':raise KeyboardInterrupt()
            if state.fail=='capture':raise RuntimeError('capture failed')
            folder.mkdir();return dict(capture='fake')
        near_only=reply=original
        def finish(self,*args):
            state.calls.append('finish')
            if state.fail=='finish':raise RuntimeError('finish failed')
        def cleanup(self,*args):
            state.calls.append('cleanup')
            if state.fail=='cleanup':raise RuntimeError('cleanup failed')
    def media(*args):
        if state.fail=='media':raise RuntimeError('media failed')
        return dict(pc_id='peer')
    monkeypatch.setattr(c,'retain_media',media)
    return args,plan,state,IO


@pytest.mark.parametrize('board',['echoear','p4xc'])
def test_schedule_is_predeclared_balanced_randomized_and_bounded(board):
    rows=c.schedule(board)
    assert rows==c.schedule(board) and len(rows)==40
    assert [r['order'] for r in rows]==list(range(40))
    assert len({r['id'] for r in rows})==40
    assert sum(r['arm']=='A' for r in rows)==20
    for part in range(4):
        records=[r for r in rows if r['part']==part]
        assert len(records)==10 and sum(r['arm']=='B' for r in records)==5
    for pair in range(1,21):assert {r['arm'] for r in rows if r['pair']==pair}=={'A','B'}
    assert {r['arm'] for r in rows[::2]}=={'A','B'}
    assert c.schedule('echoear')!=c.schedule('p4xc')


def test_success_cleans_up_and_restores_before_releasing_signals(rig):
    args,plan,state,IO=rig;r=c.acquire(args,plan,io_factory=IO)
    assert r['acquisition_passed'] and len(r['trials'])==10
    assert state.calls.index('pin-check')<state.calls.index('room')<state.calls.index('touch')<state.calls.index('flash')
    assert state.calls.index('cleanup')<state.calls.index('restore')<state.calls.index('storage-exit')<state.calls.index('R7-exit')
    assert state.calls[-1]=='signals-released'
    assert json.loads((args.out/'result.json').read_text())==r
    assert len(c.consumed(args.out.parent,plan,args.board,0))==10


def test_s3_uses_the_unchanged_s3_restore_not_p4_action(rig):
    args,plan,state,IO=rig;args.board='echoear';state.restore_action=None
    assert c.acquire(args,plan,io_factory=IO)['acquisition_passed']


def test_parts_interleave_between_boards_without_requiring_later_s3_parts(rig):
    args,plan,state,IO=rig;root=args.out.parents[2]
    for part in range(4):
        for board in ('echoear','p4xc'):
            args.part=part;args.board=board;args.out=root/board/f'part-{part}'/'attempt-01'
            state.restore_action='p4-product-of-record' if board=='p4xc' else None
            if board=='p4xc' and part<3:assert not (root/'echoear'/f'part-{part+1}').exists()
            result=c.acquire(args,plan,io_factory=IO)
            assert result['acquisition_passed'] and len(result['trials'])==10
            assert len(c.consumed(args.out.parent,plan,board,part))==10


def test_other_board_parts_do_not_satisfy_own_board_resume_requirement(rig):
    args,plan,state,IO=rig;root=args.out.parents[2]
    args.board='echoear';args.out=root/'echoear/part-0/attempt-01';state.restore_action=None
    assert c.acquire(args,plan,io_factory=IO)['acquisition_passed']
    args.board='p4xc';args.part=1;args.out=root/'p4xc/part-1/attempt-01'
    with pytest.raises(ValueError,match='earlier declared'):c.acquire(args,plan,io_factory=IO)
    assert not args.out.exists()


@pytest.mark.parametrize('failure',['flash','begin','capture','abort','finish','media','cleanup','unclean','wrong-action'])
def test_every_failure_and_abort_restores_then_releases(rig,failure):
    args,plan,state,IO=rig;state.fail=failure
    if failure=='unclean':state.clean=False
    if failure=='wrong-action':state.restore_action='p4-factory'
    r=c.acquire(args,plan,io_factory=IO)
    assert not r['acquisition_passed']
    assert 'restore' in state.calls and state.calls[-1]=='signals-released'
    assert state.calls.index('restore')<state.calls.index('R7-exit')
    if failure in ('begin','capture','abort','finish','media'):
        assert len(c.consumed(args.out.parent,plan,args.board,0))==1


@pytest.mark.parametrize('failure',['pin','room','bot','noise'])
def test_preflight_failure_is_unscored_and_does_not_touch_device(rig,failure):
    args,plan,state,IO=rig;state.fail=failure;state.noise=failure=='noise'
    r=c.acquire(args,plan,io_factory=IO)
    assert r['device_untouched'] and not r['acquisition_passed']
    assert not {'touch','flash','restore'}&set(state.calls)
    assert not c.consumed(args.out.parent,plan,args.board,0)
    assert r['status']==('preflight-noisy' if failure=='noise' else 'preflight-error')


def test_resumption_keeps_failed_original_and_only_runs_unstarted_trials(rig):
    args,plan,state,IO=rig;state.fail='capture';first=c.acquire(args,plan,io_factory=IO)
    original=first['trials'][0];state.fail=None;args.out=args.out.with_name('attempt-02')
    second=c.acquire(args,plan,io_factory=IO)
    assert second['acquisition_passed'] and len(second['trials'])==9
    assert original not in second['trials']
    assert len(c.consumed(args.out.parent,plan,args.board,0))==10
    args.out=args.out.with_name('attempt-03')
    with pytest.raises(ValueError,match='no retakes'):c.acquire(args,plan,io_factory=IO)


def test_noise_retry_keeps_same_order_and_full_denominator(rig):
    args,plan,state,IO=rig;state.noise=True;c.acquire(args,plan,io_factory=IO)
    state.noise=False;args.out=args.out.with_name('attempt-02')
    r=c.acquire(args,plan,io_factory=IO)
    assert len(r['trials'])==10
    ids=[Path(p).name for p in r['trials']]
    assert ids==[r['id'] for r in plan['schedule']['p4xc'] if r['part']==0]


def test_duplicate_original_refused(rig):
    args,plan,state,IO=rig;c.acquire(args,plan,io_factory=IO)
    path=next(args.out.glob('*/trial-start.json'));copy_path=args.out.parent/'attempt-02'/path.parent.name
    copy_path.mkdir(parents=True);(copy_path/'trial-start.json').write_bytes(path.read_bytes())
    with pytest.raises(ValueError,match='duplicate'):c.consumed(args.out.parent,plan,args.board,0)


@pytest.mark.parametrize('mutation',['arm','pair','plan','board','part'])
def test_foreign_trial_journals_refused(rig,mutation):
    args,plan,state,IO=rig;c.acquire(args,plan,io_factory=IO)
    path=next(args.out.glob('*/trial-start.json'));row=json.loads(path.read_text())
    if mutation=='plan':row['plan_sha256']='b'*64
    else:row['spec'][mutation]='invalid'
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError,match='foreign'):c.consumed(args.out.parent,plan,args.board,0)


def test_changed_candidate_or_s3_restore_refused_before_room_or_flash(rig,monkeypatch):
    args,plan,state,IO=rig
    monkeypatch.setattr(c.smoke,'NORMAL_HASH','bad')
    with pytest.raises(ValueError,match='S3 restore'):c.acquire(args,plan,io_factory=IO)
    assert not {'room','flash','touch'}&set(state.calls)


def test_restore_predicate_requires_clean_pinned_action():
    assert c.restore_ok('p4xc',dict(clean=True,action='p4-product-of-record'))
    assert not c.restore_ok('p4xc',dict(clean=True,action='p4-factory'))
    assert not c.restore_ok('p4xc',dict(clean=False,action='p4-product-of-record'))
    assert not c.restore_ok('echoear',dict(clean=True,restored_elf_sha256='wrong'))


def test_missing_trial_artifacts_are_disclosed_not_dropped(rig):
    args,plan,state,IO=rig;folder=args.out/'trial';folder.mkdir(parents=True)
    spec=plan['schedule']['p4xc'][0]
    (folder/'trial-start.json').write_text(json.dumps(dict(spec=spec)))
    row=c.evaluate(folder,plan)
    assert row['levels']=={} and row['hit'] is None and row['unavailable_reason']


def test_mutated_plan_cannot_validate(monkeypatch):
    plan=dict(bundles={b:dict(path=b) for b in c.ELFS},bot_identity='identity',restore_record='restore',restore_record_sha256='f'*64)
    monkeypatch.setattr(c,'make_plan',lambda *args:dict(plan,rules='original'))
    with pytest.raises(ValueError,match='changed'):c.validate_plan(plan)


@pytest.fixture
def planned(tmp_path,monkeypatch):
    import p4_step14
    s3=tmp_path/'s3';p4=tmp_path/'p4'
    for board,path in (('echoear',s3),('p4xc',p4)):
        path.mkdir();(path/'manifest.json').write_text(json.dumps(dict(board=board,elf_sha256=c.ELFS[board],
            profile='aec-normal',aec_enabled=True,autostart=False)))
    identity=tmp_path/'identity.json';identity.write_text(json.dumps(IDENTITY))
    restore=tmp_path/'restore.json';restore.write_text('{}')
    monkeypatch.setattr(p4_step14,'restore_record',lambda *args:{})
    monkeypatch.setattr(c,'verify_bundle',lambda path,board:(json.loads((path/'manifest.json').read_text()),{}))
    args=(s3,p4,identity,restore,c.digest(restore))
    return args,c.make_plan(*args)


def test_plan_pins_exact_bundles_fixture_bot_restore_rules_and_helpers(planned):
    args,plan=planned
    assert c.validate_plan(plan)==plan
    assert plan['fixtures'][2]['id']=='italy' and plan['fixtures'][2]['sha256']==c.ITALY_SHA
    assert plan['bot_identity_sha256']==c.digest(args[2])
    assert len(plan['source_sha256'])==len(c.SOURCES)
    assert plan['rules']['afe_delay_samples']==1024


@pytest.mark.parametrize('mutation',['elf','profile','enabled','autostart'])
def test_wrong_images_rejected_before_planning(planned,mutation):
    args,plan=planned;path=args[0]/'manifest.json';row=json.loads(path.read_text())
    row[dict(elf='elf_sha256',profile='profile',enabled='aec_enabled',autostart='autostart')[mutation]]=dict(
        elf='f'*64,profile='aec-demo',enabled=False,autostart=True)[mutation]
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError,match='exact normal'):c.make_plan(*args)


@pytest.mark.parametrize('mutation',['seed','rule','helper','identity','fixture','restore'])
def test_plan_mutation_is_refused(planned,mutation):
    args,plan=planned
    if mutation=='seed':plan['schedule']['echoear'].reverse()
    elif mutation=='rule':plan['rules']=dict(plan['rules'],activity_db=-20)
    elif mutation=='helper':plan['source_sha256']['alsa.py']='0'*64
    elif mutation=='identity':plan['identity']=dict(plan['identity'],bot_instance='changed')
    elif mutation=='fixture':plan['fixtures'][0]['sha256']='0'*64
    else:plan['s3_restore_elf']='0'*64
    with pytest.raises(ValueError,match='changed'):c.validate_plan(plan)


@pytest.fixture
def evaluation(tmp_path,monkeypatch):
    import numpy as np
    import wave
    import echoear_aec_alignment,echoear_aec_timing
    from test_p4_nearend_analysis import fixture_pcm
    source=fixture_pcm();folder=tmp_path/'attempt-01'/'trial';folder.mkdir(parents=True)
    plan=dict(identity=IDENTITY);spec=dict(id='p4xc-01-A',board='p4xc',arm='A',pair=1)
    (folder/'trial-start.json').write_text(json.dumps(dict(spec=spec)))
    original=dict(before=dict(pc_id='peer'),after=dict(pc_id='peer'),capture=dict(path=str(folder/'capture.npz'),anchors=str(folder/'anchors.json')),
        near_playback=dict(request_monotonic_s=10.),bot_started_ns=None)
    np.savez(folder/'capture.npz',timing=np.array([[0,40000,0,3000000,0]]))
    (folder/'anchors.json').write_text('[]')
    (folder/'audio').mkdir();(folder/'audio/original.json').write_text(json.dumps(original))
    events=[dict(kind='peer-binding',pc_id='peer',monotonic_ns=1),
        dict(kind='aggregator-user-turn-stopped',pc_id='peer',monotonic_ns=12_000_000_000,text='What about Italy?'),
        dict(kind='audio',stream='uplink',sample_start=0,sample_end=36759,monotonic_ns=12_000_000_000,arrival_monotonic_ns=12_000_000_000),
        dict(kind='session-end',pc_id='peer',monotonic_ns=13_000_000_000,samples=dict(uplink=36759,tts=36759))]
    (folder/'events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in events))
    for name in ('uplink.wav','tts.wav'):
        y=np.r_[np.zeros(5000),source*.5,np.zeros(10000)]
        with wave.open(str(folder/name),'wb') as w:
            w.setparams((1,2,16000,0,'NONE','not compressed'));w.writeframes(y.astype('<i2').tobytes())
    trial=dict(spec=spec,status='retained',original=original,media={name:dict(path=str(folder/name),sha256=c.digest(folder/name))
        for name in ('events.jsonl','uplink.wav','tts.wav')})
    trial['media']['pc_id']='peer'
    plan['fixtures']=[dict(id='italy',path='italy.wav')]
    pcm=np.zeros((40000,5));pcm[3000:3000+len(source),0]=source;pcm[4024:4024+len(source),4]=source*.5
    monkeypatch.setattr(c.measure,'load_capture',lambda row:pcm)
    monkeypatch.setattr(echoear_aec_alignment,'load_nearend_source',lambda path:source)
    monkeypatch.setattr(echoear_aec_timing,'sample_interval',lambda *args:[10.1,11.4])
    def save():
        (folder/'trial.json').write_text(json.dumps(trial))
        (folder/'audio/original.json').write_text(json.dumps(trial['original']))
        (folder.parent/'result.json').write_text(json.dumps(dict(plan_sha256=c.digest_plan(plan),restore=dict(clean=True,action='p4-product-of-record'))))
    save()
    return folder,plan,trial,pcm,save


def test_evaluate_rederives_all_stages_transcript_and_word_arrivals(evaluation):
    folder,plan,trial,pcm,save=evaluation;r=c.evaluate(folder,plan)
    assert r.get('unavailable_reason') is None
    assert set(r['levels'])=={'raw','aec','uplink'} and r['hit'] is True
    assert r['word_arrivals'][0]['arrival_monotonic_ns']==12_000_000_000
    assert r['missing_packets_word'] is None and r['reference_dbfs']<-60


@pytest.mark.parametrize('fault',['no-restore','media-hash','peer','original','reference','no-injection'])
def test_invalid_evidence_keeps_consumed_slot_without_measurements(evaluation,fault):
    folder,plan,trial,pcm,save=evaluation
    if fault=='media-hash':trial['media']['uplink.wav']['sha256']='f'*64
    if fault=='peer':trial['original']['after']['pc_id']='other'
    if fault=='original':(folder/'audio/original.json').write_text('{}')
    if fault=='reference':pcm[:,2]=1000*np.sin(np.arange(len(pcm))*.3)
    if fault=='no-injection':trial['original']['near_playback']=None
    if fault!='original':save()
    if fault=='no-restore':(folder.parent/'result.json').write_text('{}')
    row=c.evaluate(folder,plan)
    assert row['unavailable_reason']
    if fault=='media-hash':
        assert set(row['levels'])=={'raw','aec'} and not row['stage_validity']['uplink'] and row['stt_valid']
    else:assert row['levels']=={}
    if fault in ('media-hash','reference','no-injection'):
        assert row['hit'] is True and row['stt_observed']
        if fault!='media-hash':assert not row['stt_valid'] and row['stt_exclusion_reason']
    else:assert row['hit'] is None and not row['stt_observed']


def test_wrong_italy_is_a_scored_miss_not_unavailable(evaluation):
    folder,plan,trial,pcm,save=evaluation
    path=folder/'events.jsonl';path.write_text(path.read_text().replace('Italy','illness'))
    trial['media']['events.jsonl']['sha256']=c.digest(path);save()
    row=c.evaluate(folder,plan)
    assert row['hit'] is False and row['levels']


def test_talkover_request_and_physical_onset_boundaries(evaluation,monkeypatch):
    import echoear_aec_timing
    folder,plan,trial,pcm,save=evaluation
    trial['spec']['arm']='B';trial['original']['bot_started_ns']=7_000_000_000
    (folder/'trial-start.json').write_text(json.dumps(dict(spec=trial['spec'])))
    path=folder/'events.jsonl';events=[json.loads(s) for s in path.read_text().splitlines()]
    for frame,stamp in [('BotStartedSpeakingFrame',7_000_000_000),('BotStoppedSpeakingFrame',12_000_000_000)]:
        events.append(dict(kind='frame',frame=frame,direction='DOWNSTREAM',monotonic_ns=stamp))
    path.write_text(''.join(json.dumps(e)+'\n' for e in events));trial['media']['events.jsonl']['sha256']=c.digest(path)
    trial['original']['near_playback']['request_monotonic_s']=10.25;save()
    assert c.evaluate(folder,plan).get('unavailable_reason') is None
    trial['original']['near_playback']['request_monotonic_s']=10.25001;save()
    assert 'missed declared' in c.evaluate(folder,plan)['unavailable_reason']
    trial['original']['near_playback']['request_monotonic_s']=10.;save()
    monkeypatch.setattr(echoear_aec_timing,'sample_interval',lambda *args:[11.9,12.0001])
    assert 'physical near onset' in c.evaluate(folder,plan)['unavailable_reason']


def test_cannot_acquire_later_part_first(rig):
    args,plan,state,IO=rig;args.part=1;args.out=args.out.parent.parent/'part-1/attempt-01'
    with pytest.raises(ValueError,match='earlier declared'):c.acquire(args,plan,io_factory=IO)
    assert 'flash' not in state.calls


def test_cannot_skip_original_in_the_declared_random_order(rig):
    args,plan,state,IO=rig;c.acquire(args,plan,io_factory=IO)
    first=next(r for r in plan['schedule']['p4xc'] if r['part']==0)
    (args.out/first['id']/'trial-start.json').unlink()
    args.out=args.out.with_name('attempt-02')
    with pytest.raises(ValueError,match='order prefix'):c.acquire(args,plan,io_factory=IO)


def test_retain_media_copies_finalized_session_with_arrivals_and_unknown_rtp(tmp_path):
    import wave
    source=tmp_path/'source';source.mkdir();folder=tmp_path/'retained';folder.mkdir()
    rows=[dict(kind='peer-binding',pc_id='peer',monotonic_ns=1),
        dict(kind='audio',stream='uplink',arrival_monotonic_ns=10,sample_start=0,sample_end=320),
        dict(kind='session-end',samples=dict(uplink=320,tts=320))]
    (source/'events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    for name in ('uplink','tts'):
        with wave.open(str(source/(name+'.wav')),'wb') as w:
            w.setparams((1,2,16000,0,'NONE','not compressed'));w.writeframes(np.zeros(320,dtype='<i2').tobytes())
    io=SimpleNamespace(pc_id='peer',events=lambda:(source/'events.jsonl',rows))
    media=c.retain_media(io,folder)
    assert media['rtp_sequences']['missing_packets_word'] is None
    assert media['rtp_sequences']['available'] is False
    for name in ('events.jsonl','uplink.wav','tts.wav'):
        assert Path(media[name]['path']).read_bytes()==(source/name).read_bytes()
        assert media[name]['sha256']==c.digest(source/name)


def test_retain_media_rejects_unfinalized_pcm_instead_of_copying_partial_header(tmp_path,monkeypatch):
    source=tmp_path/'source';source.mkdir();folder=tmp_path/'retained';folder.mkdir()
    (source/'events.jsonl').write_text(json.dumps(dict(kind='session-end',samples=dict(uplink=320,tts=320)))+'\n')
    clock=iter([0,11]);monkeypatch.setattr(c.time,'monotonic',lambda:next(clock))
    io=SimpleNamespace(pc_id='peer',events=lambda:(source/'events.jsonl',[]))
    with pytest.raises(ValueError,match='did not finalize'):c.retain_media(io,folder)
    assert not list(folder.iterdir())


@pytest.mark.parametrize('board,gain',[('echoear',15),('p4xc',30)])
def test_real_adapter_only_reads_boot_gains_and_sets_quiet_no_gain_mutations(tmp_path,monkeypatch,board,gain):
    monkeypatch.setattr(c.measure.MeasurementIO,'prepare_manual',lambda *args:None)
    commands=[]
    class Board:
        cursor=0
        def command(self,command):commands.append(command)
        def wait_for(self,pattern,*args,**kwargs):
            if pattern.startswith('MIC'):return 'MIC_GAIN_PATH analog_db=30 reg=0x1a digital_percent=125'
            return f'AEC_REF_GAIN db={gain} reg45=0x{0x10+gain//3:02x}'
    io=c.NearendIO.__new__(c.NearendIO);io.out=tmp_path;io.manifest=dict(boot_ref_gain_db=gain)
    io.prepare_manual(Board())
    assert commands==['quiet 1','mic-gain-status','aec-ref-gain-status']
    assert json.loads((tmp_path/'boot-settings.json').read_text())['delay_kind']==c.analysis.RULES['delay_kind']


@pytest.mark.parametrize('mic,ref',[
    ('MIC_GAIN_PATH analog_db=27 reg=0x19 digital_percent=125','AEC_REF_GAIN db=30 reg45=0x1a'),
    ('MIC_GAIN_PATH analog_db=30 reg=0x1a digital_percent=100','AEC_REF_GAIN db=30 reg45=0x1a'),
    ('MIC_GAIN_PATH analog_db=30 reg=0x19 digital_percent=125','AEC_REF_GAIN db=30 reg45=0x1a'),
    ('MIC_GAIN_PATH analog_db=30 reg=0x1a digital_percent=125','AEC_REF_GAIN db=15 reg45=0x15')])
def test_changed_boot_gains_are_refused(tmp_path,monkeypatch,mic,ref):
    monkeypatch.setattr(c.measure.MeasurementIO,'prepare_manual',lambda *args:None)
    board=SimpleNamespace(cursor=0,command=lambda *args:None,
        wait_for=lambda pattern,*args,**kwargs:mic if pattern.startswith('MIC') else ref)
    io=c.NearendIO.__new__(c.NearendIO);io.out=tmp_path;io.manifest=dict(boot_ref_gain_db=30)
    with pytest.raises(ValueError,match='gain'):io.prepare_manual(board)


@pytest.mark.parametrize('guard,valid',[(400,True),(399,False),(401,False)])
def test_real_adapter_preserves_and_checks_boot_guard_delay_rearm(tmp_path,monkeypatch,guard,valid):
    monkeypatch.setattr(c.measure.MeasurementIO,'boot',lambda *args:0)
    lines=[(0,0,f'AEC_GUARD_CONFIG guard_ms={guard} afe_delay_samples=1024 rearm_ms=2000')]
    board=SimpleNamespace(lines_since=lambda at:lines)
    io=c.NearendIO.__new__(c.NearendIO);io.out=tmp_path
    if valid:
        assert io.boot(board)==0 and (tmp_path/'boot.log').is_file()
    else:
        with pytest.raises(ValueError,match='boot guard'):io.boot(board)


def test_late_arrival_rejection_cannot_enter_uplink_summary_or_completeness(evaluation):
    from test_p4_nearend_analysis import rows
    folder,plan,trial,pcm,save=evaluation
    path=folder/'events.jsonl';events=[json.loads(line) for line in path.read_text().splitlines()]
    path.write_text(''.join(json.dumps(e)+'\n' for e in events if e.get('kind')!='audio'))
    trial['media']['events.jsonl']['sha256']=c.digest(path);save()
    rejected=c.evaluate(folder,plan)
    assert rejected['stage_validity']==dict(raw=True,aec=True,uplink=False)
    assert set(rejected['levels'])=={'raw','aec'}
    assert rejected['hit'] is True and rejected['stt_valid'] and rejected['unavailable_reason']
    records=rows();rejected.update(pair=records[-1]['pair'],arm=records[-1]['arm'])
    records[-1]=rejected
    report=c.analysis.summarize(records);arm=report['boards']['p4xc']['B']
    assert not report['complete'] and arm['stages']['uplink']['word']['n']==19
    assert arm['stages']['raw']['word']['n']==20 and arm['stages']['aec']['word']['n']==20
    assert arm['stt']['n']==20 and arm['stt']['hits']==20


@pytest.mark.parametrize('text,hit',[('What about Italy?',True),('What about illness?',False),('',False)])
def test_zero_aec_does_not_censor_observed_stt(evaluation,text,hit):
    from test_p4_nearend_analysis import rows
    folder,plan,trial,pcm,save=evaluation;pcm[:,4]=0
    path=folder/'events.jsonl';events=[json.loads(line) for line in path.read_text().splitlines()]
    events=[e for e in events if e.get('kind')!='aggregator-user-turn-stopped']
    if text:events.append(dict(kind='aggregator-user-turn-stopped',pc_id='peer',monotonic_ns=12_000_000_000,text=text))
    path.write_text(''.join(json.dumps(e)+'\n' for e in events))
    trial['media']['events.jsonl']['sha256']=c.digest(path);save()
    observed=c.evaluate(folder,plan)
    assert observed['hit'] is hit and observed['stt_observed'] and observed['stt_valid']
    assert observed['control_valid'] and observed['stage_validity']==dict(raw=True,aec=False,uplink=False)
    assert 'zero/nonfinite' in observed['unavailable']['aec']
    records=rows();observed.update(pair=records[-1]['pair'],arm=records[-1]['arm']);records[-1]=observed
    report=c.analysis.summarize(records)
    assert report['boards']['p4xc']['B']['stt']['n']==20
    assert report['boards']['p4xc']['B']['stt']['hits']==19+int(hit)


def test_control_exclusion_retains_observed_stt_with_explicit_reason(evaluation):
    from test_p4_nearend_analysis import rows
    folder,plan,trial,pcm,save=evaluation;pcm[:,2]=1000*np.sin(np.arange(len(pcm))*.3)
    observed=c.evaluate(folder,plan)
    assert observed['hit'] is True and observed['stt_observed'] and not observed['stt_valid']
    assert 'near-only control' in observed['stt_exclusion_reason']
    assert not any(observed['stage_validity'].values())
    records=rows();observed.update(pair=records[-1]['pair'],arm=records[-1]['arm']);records[-1]=observed
    arm=c.analysis.summarize(records)['boards']['p4xc']['B']
    assert arm['stt_observed']['n']==20 and arm['stt_observed']['hits']==20
    assert arm['stt']['n']==19 and len(arm['stt_control_excluded'])==1
    assert arm['stt_control_excluded'][0]['hit'] is True


def test_arm_a_reference_keeps_full_band_and_block_diagnostics(evaluation):
    folder,plan,trial,pcm,save=evaluation
    # An out-of-band burst is visible in full-band diagnostics even when band mean passes.
    pcm[4600:4920,2]=200*np.sin(np.arange(320)*2*np.pi*6000/16000)
    row=c.evaluate(folder,plan);diag=row['reference_diagnostics']
    assert diag['blocks'] and diag['full_band_max_block_dbfs']>diag['full_band_mean_dbfs']
    assert diag['full_band_max_block_dbfs']>diag['band_mean_dbfs']
    assert 'not per-sample silence' in diag['gate']


def physical_timing(samples):
    timing=np.array([[start,min(start+320,samples),start*1000000//16000,
        min(start+320,samples)*1000000//16000,0] for start in range(0,samples,320)])
    anchors=[dict(coordinate=int(x),lower_s=x/1e6,upper_s=x/1e6)
        for x in sorted(set(timing[:,2])|set(timing[:,3]))]
    return timing,anchors


def test_residual_windows_use_physical_bounds_and_segment_length():
    samples=80000;timing,anchors=physical_timing(samples)
    masks={'phrase':np.arange(5760),'word':np.arange(6400)}
    windows=c.pre_onset_windows(timing,anchors,[3.34,3.36],53440,.5,masks)
    for segment,w in windows.items():
        assert w['samples']==len(masks[segment])
        assert w['raw_end']/16000<=3.34-.04
        assert w['raw_start']/16000>=.5
    assert windows['phrase']['raw_end']==windows['word']['raw_end']


@pytest.mark.parametrize('available,valid',[(1599,False),(1600,True),(1920,True)])
def test_residual_window_minimum_100ms_and_shortening(available,valid):
    # A single exact physical block isolates the 100 ms boundary without rounding to 20 ms.
    timing=np.array([[0,available,0,available*1000000//16000,0]])
    anchors=[dict(coordinate=0,lower_s=0,upper_s=0),
        dict(coordinate=int(timing[0,3]),lower_s=available/16000,upper_s=available/16000)]
    masks={'phrase':np.arange(5760),'word':np.arange(6400)}
    if valid:
        windows=c.pre_onset_windows(timing,anchors,[.5,.52],7000,0,masks)
        assert all(w['samples']==available for w in windows.values())
    else:
        with pytest.raises(ValueError,match='100 ms'):c.pre_onset_windows(timing,anchors,[.5,.52],7000,0,masks)


def test_residual_window_cannot_extrapolate_or_bridge_gaps():
    timing,anchors=physical_timing(6400);masks={'phrase':np.arange(5760),'word':np.arange(6400)}
    with pytest.raises(ValueError,match='100 ms'):c.pre_onset_windows(timing,[],[.4,.42],6000,0,masks)
    timing[::2,4]=1
    with pytest.raises(ValueError,match='100 ms'):c.pre_onset_windows(timing,anchors,[.4,.42],6000,0,masks)


def waveform_trials(kind):
    """Full-stage retained-source reproduction, with real capture-clock mapping and no acquisition."""
    from scipy import signal
    from echoear_aec_alignment import load_nearend_source
    source=load_nearend_source(c.ROOT/'hil/fixtures/agent_conversation/italy.wav')*.01
    masks=c.analysis.segments(source);rng=np.random.default_rng(511)
    sos=signal.butter(4,[300,3500],fs=16000,btype='bandpass',output='sos')
    envelope=np.sqrt(signal.convolve(source*source,np.ones(320)/320,mode='same'))
    onset=48000;delay=1024;samples=80000;timing,anchors=physical_timing(samples)
    first_sample=onset+int(masks['phrase'][0]);bounds=[first_sample/16000,first_sample/16000+.00001]
    windows=c.pre_onset_windows(timing,anchors,bounds,first_sample,0,masks)
    records=[]
    for pair in range(20):
        shaped=signal.sosfilt(sos,rng.normal(size=len(source)))*envelope
        stationary=signal.sosfilt(sos,rng.normal(size=samples))*2
        for board in ('echoear','p4xc'):
            for arm in ('A','B'):
                pcm=np.zeros((samples,5));pcm[onset:onset+len(source),0]=source
                output=source.copy()
                if arm=='B':
                    pcm[onset:onset+len(source),0]+=2*shaped
                    if kind=='residual-counterexample':
                        if board=='echoear':output+=1.8*shaped
                    else:
                        if board=='p4xc':output*=.5
                        pcm[:,4]=stationary
                pcm[onset+delay:onset+delay+len(source),4]+=output
                uplink=np.r_[np.zeros(1600),pcm[:,4],np.zeros(1600)]
                measurement=c.analysis.stage_levels(source,pcm,uplink)
                assert all(measurement['stage_validity'].values())
                row=dict(board=board,arm=arm,pair=pair,hit=True,stt_observed=True,stt_valid=True,control_valid=True,**measurement)
                if arm=='B':
                    events=[dict(kind='audio',stream='uplink',sample_start=0,sample_end=len(uplink),arrival_monotonic_ns=1)]
                    row['residual_estimates']=c.residual_estimates(pcm,uplink,measurement,masks,windows,events)
                records.append(row)
    return records


def test_full_stage_reviewer_residual_output_counterexample_never_localizes_near_loss():
    records=waveform_trials('residual-counterexample');report=c.analysis.summarize(records)
    assert report['complete']
    assert report['p4_vs_s3']['double_talk_output/word']['deficit']
    assert report['p4_vs_s3']['double_talk_output/word']['p4_minus_s3_db']<-2
    # Echo switches on with near speech; a quiet pre-window cannot remove that bias.
    assert report['p4_vs_s3']['double_talk_output_corrected/word']['deficit']
    assert not report['double_talk_near_end_localized']
    assert report['localization']=='mixture difference observed; near-end localization unresolved'
    assert not report['double_talk_near_end']['aec']['word']['criteria']['p4_within_board_loss']


def test_full_stage_true_p4_near_loss_localizes_under_stationary_residual_assumption():
    records=waveform_trials('true-loss');report=c.analysis.summarize(records)
    assert report['complete'] and report['double_talk_near_end_localized']
    claim=report['double_talk_near_end']['aec']['word']
    assert claim['localized'] and claim['p4_pairs']==claim['s3_pairs']==20
    assert report['contributions']['p4xc']['double_talk_output_corrected/word']['median']==pytest.approx(-6.02,abs=.1)
    assert 'double_talk_output_corrected/word' in report['localized_deficits']
    assert all(report['contributions'][b]['double_talk_output_corrected/uplink/word']['n']==20 for b in ('echoear','p4xc'))


def test_finalized_wav_extent_failure_excludes_only_uplink(evaluation):
    folder,plan,trial,pcm,save=evaluation
    path=folder/'events.jsonl';events=[json.loads(s) for s in path.read_text().splitlines()]
    next(e for e in events if e.get('kind')=='session-end')['samples']['uplink']+=320
    path.write_text(''.join(json.dumps(e)+'\n' for e in events));trial['media']['events.jsonl']['sha256']=c.digest(path);save()
    row=c.evaluate(folder,plan)
    assert row['stage_validity']==dict(raw=True,aec=True,uplink=False)
    assert row['stt_valid'] and row['hit'] is True
    assert 'finalized recorder extent' in row['unavailable']['uplink']


def test_arrival_extent_cannot_exceed_finalized_pcm(evaluation):
    folder,plan,trial,pcm,save=evaluation
    path=folder/'events.jsonl';events=[json.loads(s) for s in path.read_text().splitlines()]
    next(e for e in events if e.get('kind')=='audio')['sample_end']+=320
    path.write_text(''.join(json.dumps(e)+'\n' for e in events));trial['media']['events.jsonl']['sha256']=c.digest(path);save()
    row=c.evaluate(folder,plan)
    assert not row['stage_validity']['uplink'] and row['stt_valid']
    assert 'exceeds finalized PCM' in row['unavailable']['uplink']


def test_evaluator_derives_own_b_residual_from_retained_physical_clock(evaluation,monkeypatch):
    import echoear_aec_timing
    folder,plan,trial,pcm,save=evaluation
    trial['spec']['arm']='B';trial['original']['bot_started_ns']=7_000_000_000
    (folder/'trial-start.json').write_text(json.dumps(dict(spec=trial['spec'])))
    timing,anchors=physical_timing(len(pcm))
    for anchor in anchors:
        anchor['lower_s']+=9.95;anchor['upper_s']+=9.95
    np.savez(folder/'capture.npz',timing=timing)
    (folder/'anchors.json').write_text(json.dumps(anchors))
    monkeypatch.setattr(echoear_aec_timing,'sample_interval',physical_sample_interval)
    pcm[:,4]+=np.random.default_rng(92).normal(size=len(pcm))*2
    path=folder/'events.jsonl';events=[json.loads(s) for s in path.read_text().splitlines()]
    for frame,stamp in [('BotStartedSpeakingFrame',7_000_000_000),('BotStoppedSpeakingFrame',12_000_000_000)]:
        events.append(dict(kind='frame',frame=frame,direction='DOWNSTREAM',monotonic_ns=stamp))
    path.write_text(''.join(json.dumps(e)+'\n' for e in events));trial['media']['events.jsonl']['sha256']=c.digest(path);save()
    row=c.evaluate(folder,plan)
    assert row['control_valid'] and row['stt_valid'] and row['stage_validity']['aec']
    for segment in ('phrase','word'):
        estimate=row['residual_estimates']['aec'][segment]
        assert estimate['valid'] and estimate['residual_power']>0
        assert estimate['window']['samples']>=1600
        assert estimate['window']['cutoff_host_s']==pytest.approx(row['near_host_bounds_s'][0][0]-.04)
        assert estimate['near_power']==pytest.approx(estimate['segment_power']-estimate['residual_power'])
