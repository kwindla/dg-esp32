"""Offline frozen-STT/reference/clip rules; no hardware or STT service calls."""
import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace
import pytest
import p4_defaults_validate as defaults

def perf(board,after=False):
    # AEC-off PERF shape from fix-7/coordinator/mic-{p4,s3}/result.json.
    # Driver/read counters are in capture; clip counters are separate top-level
    # fields. There is no aec object or capture.adc_clips/gain_clips.
    s3=board=='echoear'
    return dict(generation=1,pc_id='SmallWebRTCConnection#original',
        capture=dict(read_errors=0,read_frames=(329920 if after else 198720) if s3 else (267840 if after else 169600),
            read_timeouts=0,rx_completions=0 if s3 else (1117 if after else 707),rx_overflows=0),
        mic_in_turn_clips=0,mic_gate_open_clips=0,mic_gate_closed_clips=0)

def run(board='p4xc'):
    return dict(acquisition_passed=True,board=board,restore=dict(clean=True),identity=dict(profile_manifest={'profile':'demo'}),
        prompts=[dict(id=p['id'],expected=p['expected'],received_text=p['expected'],source_sha256=p['source_sha256'],native_adc_clips=0,
            before=perf(board),after=perf(board,True)) for p in defaults.prompts()])

@pytest.mark.parametrize('board',['p4xc','echoear'])
def test_same_prompt_profile_s3_band_and_zero_clips_pass(board):
    candidate=run(board)
    assert 'aec' not in candidate['prompts'][0]['before']
    result=defaults.scores(candidate,[run('echoear')]);assert result['passed'] and result['verified'] and not result['qualification']
    assert all(r['accuracy']==1 and r['wer']==0 for r in result['prompts'])
    assert all(r['microphone_clip_gate']['passed'] for r in result['prompts'])
    assert result['prompts'][0]['capture']['rx_completions']==(0 if board=='echoear' else 410)

@pytest.mark.parametrize('case',['absent','prompt','profile'])
def test_missing_mismatched_reference_is_unverified_not_a_new_threshold(case):
    reference=run('echoear')
    if case=='prompt':reference['prompts'][0]['source_sha256']='other waveform'
    elif case=='profile':reference['identity']['profile_manifest']={'profile':'other'}
    result=defaults.scores(run(),[] if case=='absent' else [reference]);assert not result['passed'] and not result['verified']

@pytest.mark.parametrize('case',['accuracy','wer','open-clip','closed-clip','native'])
def test_unchanged_accuracy_wer_and_clip_gates(case):
    candidate=run();reference=run('echoear');row=candidate['prompts'][0]
    if case in ('accuracy','wer'):row['received_text']='wrong words' if case=='accuracy' else row['expected']+' extra'
    elif case in ('open-clip','closed-clip'):
        row['after']['mic_in_turn_clips']+=1
        row['after']['mic_gate_'+case.replace('-clip','')+'_clips']+=1
    else:row['native_adc_clips']=1
    result=defaults.scores(candidate,[reference]);assert not result['passed'] and result['verified']

@pytest.mark.parametrize('board',['p4xc','echoear'])
@pytest.mark.parametrize('case',['capture-missing','legacy-only','capture-field-missing','capture-bool','capture-reset',
    'clip-missing','clip-bool','clip-reset','clip-inconsistent'])
def test_real_perf_fields_missing_malformed_reset_or_inconsistent_cannot_pass(board,case):
    candidate=run(board);row=candidate['prompts'][0];before,after=row['before'],row['after']
    if case=='capture-missing':after.pop('capture')
    elif case=='legacy-only':after.pop('capture');after['aec']=dict(capture=dict(adc_clips=0,gain_clips=0))
    elif case=='capture-field-missing':after['capture'].pop('read_errors')
    elif case=='capture-bool':after['capture']['rx_overflows']=False
    elif case=='capture-reset':after['capture']['read_frames']=0
    elif case=='clip-missing':after.pop('mic_gate_open_clips')
    elif case=='clip-bool':after['mic_in_turn_clips']=False
    elif case=='clip-reset':before['mic_in_turn_clips']=1
    else:after['mic_gate_closed_clips']=1
    if case=='clip-inconsistent':
        report=defaults.scores(candidate,[run('echoear')]);assert not report['passed']
        assert not report['prompts'][0]['microphone_clip_gate']['consistent']
    else:
        with pytest.raises(ValueError,match='capture counters|microphone clip counters'):
            defaults.scores(candidate,[run('echoear')])

def test_failed_reference_reordered_prompts_and_session_change_fail_closed():
    reference=run('echoear');reference['acquisition_passed']=False
    report=defaults.scores(run(),[reference]);assert not report['verified'] and not report['passed']
    assert report['disposition'].startswith('unverified') and report['reference_findings']
    candidate=run();candidate['prompts'].reverse()
    with pytest.raises(ValueError):defaults.scores(candidate,[])
    candidate=run();candidate['prompts'][0]['after']['generation']=2
    with pytest.raises(ValueError):defaults.scores(candidate,[])

@pytest.mark.parametrize('cleanup_fails',[False,True])
def test_original_acquisition_error_restores_releases_signals_and_survives_cleanup(tmp_path,monkeypatch,cleanup_fails):
    import boards,alsa,p4_product,p4_step14,p4_audio_fix4
    monkeypatch.setattr(boards,'require_bench',lambda:0)
    monkeypatch.setattr(p4_product,'candidate_storage',lambda *a:nullcontext())
    monkeypatch.setattr(p4_step14,'restoring',lambda *a:nullcontext())
    monkeypatch.setattr(alsa,'resolve_jabra',lambda:('card','device'))
    monkeypatch.setattr(alsa,'set_volumes',lambda *a:None)
    monkeypatch.setattr(defaults,'verify_bundle',lambda *a:(dict(profile='normal',aec_enabled=False,elf_sha256='a'*64),{}))
    identity=tmp_path/'identity.json';identity.write_text('{}')
    args=SimpleNamespace(board='p4xc',bundle=tmp_path,bot_identity=identity,out=tmp_path/'out',restore_record='pin',restore_record_sha256='b'*64)
    state=SimpleNamespace(touched=False,restored=False,released=False)
    class Hold:
        success=False
        def __enter__(self):return self
        def touch(self):state.touched=True
        def __exit__(self,*a):state.restored=True;self.record=dict(clean=True,action='p4-product-of-record')
        def release_signals(self):assert state.restored;state.released=True
    class IO:
        def __init__(self,*a):pass
        def preflight_tools(self):pass
        def preflight_restore(self):pass
        def hold(self):return Hold()
        def flash(self,*a):assert state.touched
        def open(self):return nullcontext(object())
        def boot(self,*a):raise RuntimeError('original boot failure')
        def cleanup(self,*a):
            if cleanup_fails:raise TimeoutError('cleanup failure')
    monkeypatch.setattr(p4_audio_fix4,'VoiceIO',IO)
    assert defaults.live(args)==1 and state.restored and state.released
    result=json.loads((args.out/'result.json').read_text())
    assert result['error']=='RuntimeError: original boot failure'
    assert bool(result.get('cleanup_errors'))==cleanup_fails

@pytest.mark.parametrize('reference',[None,{},dict(board='echoear',acquisition_passed=True,restore=dict(clean=True),
    identity=dict(profile_manifest={}),turns=[]),dict(board='echoear',acquisition_passed=True,restore=dict(clean=True),
    identity=dict(profile_manifest={}),prompts=[None])])
def test_wrong_reference_schema_is_unverified_even_alongside_valid_reference(reference):
    result=defaults.scores(run(),[run('echoear'),reference])
    assert not result['verified'] and not result['passed'] and result['disposition'].startswith('unverified')
    assert result['reference_findings'][0]['index']==1

def test_cli_voice_smoke_reference_writes_unverified_result_instead_of_keyerror(tmp_path):
    candidate=tmp_path/'p4';candidate.mkdir();(candidate/'result.json').write_text(json.dumps(run()))
    reference=tmp_path/'voice';reference.mkdir()
    voice=run('echoear');voice.pop('prompts');voice['turns']=[]
    (reference/'result.json').write_text(json.dumps(voice));output=tmp_path/'score.json'
    assert defaults.main(['score',str(candidate),'--s3',str(reference),'--out',str(output)])==1
    report=json.loads(output.read_text())
    assert report['verified'] is False and report['disposition'].startswith('unverified')

@pytest.mark.parametrize('directory_exists',[False,True])
@pytest.mark.parametrize('alongside_valid',[False,True])
def test_cli_s3_directory_without_result_writes_unverified_record(tmp_path,directory_exists,alongside_valid):
    candidate=tmp_path/'p4';candidate.mkdir();(candidate/'result.json').write_text(json.dumps(run()))
    reference=tmp_path/'missing-result'
    if directory_exists:reference.mkdir()
    refs=[str(reference)]
    if alongside_valid:
        valid=tmp_path/'valid-s3';valid.mkdir();(valid/'result.json').write_text(json.dumps(run('echoear')))
        refs.insert(0,str(valid))
    output=tmp_path/'score.json'
    assert defaults.main(['score',str(candidate),'--s3',*refs,'--out',str(output)])==1
    report=json.loads(output.read_text())
    assert not report['verified'] and not report['passed'] and report['disposition'].startswith('unverified')
    assert 'result.json missing' in report['reference_findings'][0]['reason']
    assert report['reference_sha256'][str(reference)] is None

@pytest.mark.parametrize('mode',['split','missing','no-reply','wait-error'])
@pytest.mark.parametrize('board,action,success',[('p4xc','p4-product-of-record',True),
    ('p4xc','p4-factory',False),('p4xc',None,False),('echoear','echoear-55152f25',True)])
def test_live_stt_failures_are_saved_and_scored_after_clean_acquisition(tmp_path,monkeypatch,board,action,success,mode):
    import boards,alsa,p4_product,p4_step14,p4_audio_fix4,board_smoke,bench_calibration,speaker_dual_sweep_test,wave
    monkeypatch.setattr(boards,'require_bench',lambda:0)
    monkeypatch.setattr(p4_product,'candidate_storage',lambda *a:nullcontext())
    monkeypatch.setattr(p4_step14,'restoring',lambda *a:nullcontext())
    monkeypatch.setattr(alsa,'resolve_jabra',lambda:('card','device'))
    monkeypatch.setattr(alsa,'set_volumes',lambda *a:None)
    monkeypatch.setattr(alsa,'play_wav',lambda *a:dict(original=True))
    monkeypatch.setattr(bench_calibration,'playback',lambda path,source:path)
    monkeypatch.setattr(defaults,'verify_bundle',lambda *a:(dict(profile='normal',aec_enabled=False,elf_sha256='a'*64),{}))
    monkeypatch.setattr(board_smoke,'resource_check',lambda *a:dict(passed=True))
    def dump(board,path):
        with wave.open(str(path),'wb') as stream:
            stream.setparams((1,2,16000,0,'NONE','not compressed'));stream.writeframes(b'\0\0'*3200)
    monkeypatch.setattr(speaker_dual_sweep_test,'dump_mic',dump)
    definitions=defaults.prompts();identity=tmp_path/'identity.json';identity.write_text(json.dumps(run()['identity']))
    args=SimpleNamespace(board=board,bundle=tmp_path,bot_identity=identity,out=tmp_path/'out',restore_record='pin',restore_record_sha256='b'*64)
    state=SimpleNamespace(touched=False,restored=False,released=False,prompt=-1,commands=[],finished=False)
    class Board:
        def command(self,text,*a):
            state.commands.append(text)
            if text=='mic-record 1':state.prompt+=1
    device=Board()
    class Hold:
        success=False
        def __enter__(self):return self
        def touch(self):state.touched=True
        def __exit__(self,*a):state.restored=True;self.record=dict(clean=True,action=action)
        def release_signals(self):assert state.restored;state.released=True
    class IO:
        since=0
        def __init__(self,*a):pass
        def preflight_tools(self):pass
        def preflight_restore(self):pass
        def hold(self):return Hold()
        def flash(self,*a):assert state.touched
        def open(self):return nullcontext(device)
        def boot(self,*a):pass
        def prepare_manual(self,*a):pass
        def begin(self,*a):pass
        def pause(self,*a):pass
        def clock(self):return 13
        def snapshot(self,*a):return copy.deepcopy(run()['prompts'][0]['before'])
        def events(self):
            spec=definitions[state.prompt];text=spec['expected'];stamp=10**20
            if state.prompt==0 and mode=='missing':return 'original-recorder',[]
            texts=text.split(' ',2) if state.prompt==0 and mode=='split' else [text]
            rows=[dict(kind='aggregator-user-turn-stopped',text=t,monotonic_ns=stamp+i) for i,t in enumerate(texts)]
            if state.prompt==0 and mode=='no-reply':return 'original-recorder',rows
            return 'original-recorder',rows+[
                dict(kind='aggregator-assistant-turn-stopped',text='reply',monotonic_ns=stamp+10),
                dict(kind='frame',frame='BotStoppedSpeakingFrame',direction='DOWNSTREAM',monotonic_ns=stamp+11)]
        def wait(self,board,condition,*a,**kw):
            if state.prompt==0 and mode=='wait-error':raise RuntimeError('unrelated wait failure')
            if state.prompt==0 and mode in ('missing','no-reply'):
                assert not condition()
                raise TimeoutError('Smoke wait for standard prompt and original reply completion exceeded 60 seconds')
            assert condition()
        def freeze(self,*a):pass
        def finish(self,*a):
            assert all((args.out/p['id']/'prompt.json').exists() for p in definitions)
            state.finished=True
        def cleanup(self,*a):pass
    monkeypatch.setattr(p4_audio_fix4,'VoiceIO',IO)
    writes=[];original_write=defaults.write
    def recorded_write(path,value):writes.append(path.name);return original_write(path,value)
    monkeypatch.setattr(defaults,'write',recorded_write)
    expected_success=success and mode!='wait-error'
    assert defaults.live(args)==int(not expected_success)
    result=json.loads((args.out/'result.json').read_text())
    assert result['acquisition_passed']==expected_success and state.restored and state.released
    if mode=='wait-error':
        assert result['error']=='RuntimeError: unrelated wait failure' and not state.finished
        assert state.prompt==0 and result['prompts']==[]
        return
    assert state.finished
    assert len(result['prompts'])==3 and state.prompt==2
    prompt=result['prompts'][0]
    count={'split':3,'missing':0,'no-reply':1}[mode]
    assert len(prompt['received_transcripts'])==len(prompt['transcript_rows'])==count
    assert prompt['received_text']==('' if mode=='missing' else definitions[0]['expected'])
    status={'split':'split-transcript','missing':'missing-transcript','no-reply':'no-reply'}[mode]
    assert prompt['transcript_status']==status
    assert bool(prompt['reply_wait_timeout'])==(mode in ('missing','no-reply'))
    assert defaults.Path(prompt['native_path']).is_file() and defaults.digest(prompt['native_path'])==prompt['native_sha256']
    assert json.loads((args.out/'france/prompt.json').read_text())==prompt
    saved=json.loads((args.out/'france/transcripts.json').read_text())
    assert saved['transcript_rows']==prompt['transcript_rows']
    assert writes[:2]==['transcripts.json','prompt.json']
    assert state.commands.count('mic-record 1')==state.commands.count('mic-record 0')==3
    assert not any(command.startswith(('mic-gain','mic-analog-gain')) for command in state.commands)
    if board=='p4xc' and success:
        report=defaults.scores(result,[run('echoear')])
        assert report['verified'] and not report['passed']
        assert report['prompts'][0]['accuracy']==(0 if mode=='missing' else 1)
        assert report['prompts'][0]['transcript_finding']==status
        assert all(p['passed'] for p in report['prompts'][1:])
        assert 'error' not in result  # Preserved STT failure, never a failed acquisition inviting a retake.

def test_runbook_drops_four_variant_smoke_debt_and_records_probe_sensitivity():
    text=defaults.Path(__file__).with_name('p4_defaults.md').read_text()
    assert 'No four-variant S3 R3 smokes are owed' in text and 'zero unmasked differing bytes' in text
    assert 'smoke-s3-$variant' not in text
    assert '319' in text and '20 ms' in text and '−255' in text and '−257' in text
