"""Portable offline source/provenance and real-product DSP seed controls."""
import copy
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess
import wave

import pytest
import p4_seed_derive as seed

CONFIG=dict(target_dbfs=-12,seed_dbfs=-12,max_gain_db=0,activity_dbfs=-45,level_ms=400,attack_ms=100,
    release_ms=1000,lookahead_ms=2,limiter_release_ms=75,compression_ratio=1,compression_threshold_dbfs=-18,
    ceiling_percent=26,highpass_hz=0)

def save(path,value):path.write_text(json.dumps(value));return path

def wav(path,data):
    with wave.open(str(path),'wb') as stream:
        stream.setparams((1,2,16000,0,'NONE','not compressed'));stream.writeframes(data)

@pytest.fixture(scope='module')
def probe(tmp_path_factory):
    path=tmp_path_factory.mktemp('seed-probe')/'probe';seed.build_probe(path);return path

def original(tmp_path):
    run=tmp_path/'run';run.mkdir();session=tmp_path/'session';session.mkdir()
    identity=dict(bot_instance='original-process',bot_source_sha='b'*40,policy_source_sha='c'*40,
        profile_manifest=dict(config=dict(profile='demo',tts='recorded voice identity'),versions=dict(pipecat='pinned')))
    save(run/'identity.json',identity)
    save(run/'result.json',dict(acquisition_passed=True,board='p4xc',identity=identity,restore=dict(clean=True)))
    events=[dict(kind='stt-profile',monotonic_ns=1,pc_id='peer',**identity['profile_manifest'])];data=b''
    for index,amplitude in enumerate((1500,2500,3500),1):
        first=index*100;audio=struct.pack('<32000h',*[round(amplitude*math.sin(2*math.pi*1000*i/16000)) for i in range(32000)])
        start=len(data)//2;data+=audio
        turn_events=[dict(kind='aggregator-user-turn-stopped',monotonic_ns=first,pc_id='peer'),
            dict(kind='frame',frame='BotStartedSpeakingFrame',monotonic_ns=first+1,pc_id='peer'),
            dict(kind='frame',frame='BotStoppedSpeakingFrame',monotonic_ns=first+4,pc_id='peer'),
            dict(kind='aggregator-assistant-turn-stopped',monotonic_ns=first+5,pc_id='peer')]
        events.extend([turn_events[0],turn_events[1],dict(kind='audio',stream='tts',pc_id='peer',
            monotonic_ns=first+2,sample_start=start,sample_end=start+len(audio)//2,drop=False),*turn_events[2:]])
        save(run/f'turn-{index:02d}.json',dict(kind='long',passed=True,events=turn_events,
            users=['original long prompt'],assistants=['original reply '+str(index)],recording=str(run/f'reply-{index}.wav')))
    events.append(dict(kind='session-end',monotonic_ns=400,pc_id='peer'))
    (session/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events));wav(session/'tts.wav',data)
    selection=save(tmp_path/'selection.json',seed.select(run,session,[1,2,3]))
    policy=save(tmp_path/'policy.json',dict(config=CONFIG,sources=seed.sources(),elf_sha256='a'*64))
    return run,session,selection,policy

@pytest.mark.parametrize('value,expected',[(-28.95,-290),(-28.85,-289),(-9,-100),(-70,-600)])
def test_half_away_rounding_and_kconfig_clamp(value,expected):
    assert seed.candidate([value-1,value,value+1])['candidate_seed_dbfs_x10']==expected

@pytest.mark.parametrize('values',[[1,2],[1,2,float('nan')],[1,2,float('inf')],[1,2,True]])
def test_three_finite_independent_levels_required(values):
    with pytest.raises(ValueError):seed.candidate(values)

def test_original_source_replayed_by_real_dsp_and_median_is_candidate_only(tmp_path,probe):
    run,session,selection,policy=original(tmp_path)
    before={p:seed.digest(p) for p in [run/'result.json',session/'tts.wav',session/'events.jsonl']}
    result=seed.derive(selection,policy,probe,tmp_path/'derived')
    assert not result['passed'] and not result['qualification'] and result['confirmation'].startswith('pending P4')
    for row,amplitude in zip(result['replies'],(1500,2500,3500)):
        expected=20*math.log10(amplitude/32768/math.sqrt(2))
        assert row['active_effective_dbfs']==pytest.approx(expected,abs=.02)
        assert row['active_samples']==32000-319 and row['source_clips']==row['output_clips']==0
    assert result['candidate_seed_dbfs_x10']==seed.candidate([r['active_effective_dbfs'] for r in result['replies']])['candidate_seed_dbfs_x10']
    assert all(seed.digest(p)==sha for p,sha in before.items())
    for index,row in enumerate(result['replies'],1):
        mask=(tmp_path/'derived'/f'reply-{index}-product.pcm.active').read_bytes()
        assert len(mask)==row['samples'] and sum(mask)==row['active_samples']

@pytest.mark.parametrize('case',['foreign-peer','profile','unclosed','interrupted','reordered','truncated-wav','not-long','failed-run','duplicate-turn'])
def test_damaged_or_unbound_source_selection_fails(tmp_path,case):
    run,session,_,_=original(tmp_path)
    events=[json.loads(line) for line in (session/'events.jsonl').read_text().splitlines()];turns=[1,2,3]
    if case=='foreign-peer':events[3]['pc_id']='other'
    elif case=='profile':events[0]['config']['profile']='other'
    elif case=='unclosed':events.pop()
    elif case=='interrupted':events.insert(4,dict(kind='frame',frame='InterruptionFrame',monotonic_ns=events[3]['monotonic_ns'],pc_id='peer'))
    elif case=='reordered':events[3]['monotonic_ns']=0
    elif case=='truncated-wav':(session/'tts.wav').write_bytes((session/'tts.wav').read_bytes()[:-2])
    elif case=='not-long':row=json.loads((run/'turn-01.json').read_text());row['kind']='france';save(run/'turn-01.json',row)
    elif case=='failed-run':row=json.loads((run/'result.json').read_text());row['restore']['clean']=False;save(run/'result.json',row)
    else:turns=[1,1,3]
    (session/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    with pytest.raises(ValueError):seed.select(run,session,turns)

def test_normal_user_start_interruption_before_reply_is_not_source_damage(tmp_path):
    run,session,_,_=original(tmp_path)
    events=[json.loads(line) for line in (session/'events.jsonl').read_text().splitlines()]
    events.insert(2,dict(kind='frame',frame='InterruptionFrame',monotonic_ns=100,pc_id='peer'))
    (session/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    assert len(seed.select(run,session,[1,2,3])['replies'])==3

@pytest.mark.parametrize('case',['source','profile','selection','policy-sources','probe-stamp'])
def test_changed_evidence_or_dsp_provenance_fails(tmp_path,probe,case):
    run,session,selection,policy=original(tmp_path)
    if case=='source':(session/'events.jsonl').write_text((session/'events.jsonl').read_text()+'{}\n')
    elif case=='profile':row=json.loads((run/'identity.json').read_text());row['bot_source_sha']='changed';save(run/'identity.json',row)
    elif case=='selection':row=json.loads(selection.read_text());row['replies'][0]['sample_start']+=1;save(selection,row)
    elif case=='policy-sources':row=json.loads(policy.read_text());row['sources']={};save(policy,row)
    else:
        # Copy the binary but change its recorded digest, preserving shared fixture.
        local=tmp_path/'probe';local.write_bytes(probe.read_bytes());local.chmod(0o755)
        stamp=json.loads(Path(str(probe)+'.json').read_text());stamp['executable_sha256']='0'*64
        save(Path(str(local)+'.json'),stamp);probe=local
    with pytest.raises(ValueError):seed.derive(selection,policy,probe,tmp_path/'derived')

def test_compression_is_before_limiter_and_product_activity_mask_is_used(tmp_path,probe):
    _,_,selection,policy=original(tmp_path)
    base=seed.derive(selection,policy,probe,tmp_path/'plain')
    p=json.loads(policy.read_text());p['config'].update(compression_ratio=3,compression_threshold_dbfs=-30)
    save(policy,p);compressed=seed.derive(selection,policy,probe,tmp_path/'compressed')
    assert all(a['active_effective_dbfs']>b['active_effective_dbfs'] for a,b in zip(base['replies'],compressed['replies']))
    assert all(a['active_samples']==b['active_samples'] for a,b in zip(base['replies'],compressed['replies']))
    # Lowering the product safety ceiling changes delivered PCM but must not
    # change the effective pre-limiter source measurement when gain is fixed.
    p['config']['ceiling_percent']=1;save(policy,p)
    limited=seed.derive(selection,policy,probe,tmp_path/'limited')
    assert all(row['limited_samples']>0 for row in limited['replies'])
    assert [r['active_effective_dbfs'] for r in limited['replies']]==[r['active_effective_dbfs'] for r in compressed['replies']]

def test_hpf_and_silence_are_not_mistaken_for_active_source(tmp_path,probe):
    raw=tmp_path/'low.pcm';output=tmp_path/'out.pcm'
    raw.write_bytes(struct.pack('<32000h',*[round(3000*math.sin(2*math.pi*50*i/16000)) for i in range(32000)]))
    c=dict(CONFIG,highpass_hz=500)
    completed=subprocess.run([str(probe),str(raw),str(output),*[str(c[f]) for f in seed.FIELDS]],capture_output=True,text=True)
    if completed.returncode==0:assert json.loads(completed.stdout)['active_samples']<320
    else:assert completed.returncode==4
    raw.write_bytes(b'\0'*64000)
    assert subprocess.run([str(probe),str(raw),str(output),*[str(CONFIG[f]) for f in seed.FIELDS]],capture_output=True).returncode==4

@pytest.mark.parametrize('case',['duplicate','overlap'])
def test_original_long_reply_pcm_cannot_be_replayed_or_overlap(tmp_path,probe,case):
    run,session,selection,policy=original(tmp_path)
    events=[json.loads(line) for line in (session/'events.jsonl').read_text().splitlines()]
    rows=[e for e in events if e.get('kind')=='audio']
    if case=='duplicate':
        data=seed.pcm(session/'tts.wav');wav(session/'tts.wav',data[:128000]+data[:64000])
    else:rows[-1].update(sample_start=100,sample_end=1000)
    (session/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    save(selection,seed.select(run,session,[1,2,3]))
    with pytest.raises(ValueError,match='duplicate|overlapping'):seed.derive(selection,policy,probe,tmp_path/'derived')
