#!/usr/bin/env python3
"""Offline P4 playback seed candidate from three original live long replies.

No device, bot, service, credentials, or Kconfig writes. Source PCM comes from
the unchanged opt-in SessionRecorder, never resynthesized/decoded/Jabra audio.
"""
import argparse
from decimal import Decimal,ROUND_HALF_UP
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import wave

ROOT=Path(__file__).resolve().parents[1]
FIELDS=('target_dbfs','seed_dbfs','max_gain_db','activity_dbfs','level_ms','attack_ms','release_ms',
    'lookahead_ms','limiter_release_ms','compression_ratio','compression_threshold_dbfs','ceiling_percent','highpass_hz')
KEYS=('TARGET_DBFS','SEED_DBFS_X10','MAX_GAIN_DB','ACTIVITY_DBFS','LEVEL_MS','ATTACK_MS','RELEASE_MS',
    'LOOKAHEAD_MS','LIMITER_RELEASE_MS','COMPRESSION_RATIO','COMPRESSION_THRESHOLD_DBFS','CEILING_PERCENT','HIGHPASS_HZ')
DSP=('components/dg_audio/include/dg_audio/playback_dsp.h','components/dg_audio/src/playback_dsp.cpp','hil/p4_seed_probe.cpp')

def require(ok,message):
    if not ok:raise ValueError(message)

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def write(path,value):
    with Path(path).open('x') as stream:stream.write(json.dumps(value,indent=2,allow_nan=False)+'\n')

def sources():return {name:digest(ROOT/name) for name in DSP}

def build_probe(out):
    out=Path(out).resolve();require(not out.exists(),'new probe path required')
    subprocess.run([str(ROOT/'scripts/build_locked.sh'),'g++','-std=c++17','-O2','-Wall','-Wextra','-Werror',
        '-fno-access-control','-I'+str(ROOT/'components/dg_audio/include'),str(ROOT/DSP[2]),str(ROOT/DSP[1]),'-o',str(out)],check=True)
    write(str(out)+'.json',dict(sources=sources(),executable_sha256=digest(out),host_only=True))

def policy(build,bundle):
    sys.path.insert(0,str(ROOT/'scripts'))
    import sdkconfig_keydiff
    from make_bundle import verify_bundle
    manifest,_=verify_bundle(bundle,'p4xc');project=json.loads((build/'project_description.json').read_text())
    require(project['target']=='esp32p4' and digest(build/'dg-esp32.elf')==manifest['elf_sha256'],'policy build/bundle mismatch')
    values=sdkconfig_keydiff.settings(Path(project['config_file']))
    require(values.get('CONFIG_DG_PLAYBACK_NORMALIZE')=='y','product speech normalization required')
    config={field:int(values['CONFIG_DG_PLAYBACK_'+key]) for field,key in zip(FIELDS,KEYS)}
    config['seed_dbfs']/=10
    return dict(schema=1,elf_sha256=manifest['elf_sha256'],bundle_manifest_sha256=digest(bundle/'manifest.json'),
        config=config,sources=sources(),basis='Allow-listed resolved product settings; sample rate 16000 Hz')

def pcm(path):
    with wave.open(str(path),'rb') as stream:
        require((stream.getnchannels(),stream.getsampwidth(),stream.getframerate(),stream.getcomptype())==(1,2,16000,'NONE'),'mono PCM16 16000 Hz required')
        count=stream.getnframes();data=stream.readframes(count)
        require(count>0 and len(data)==count*2,'empty/truncated source PCM')
        return data

def select(run,session,turns):
    """Bind turn receipt times to the recorder's original TTS storage offsets."""
    result=json.loads((run/'result.json').read_text());identity=json.loads((run/'identity.json').read_text())
    require(result['acquisition_passed'] is True and not result.get('error') and not result.get('cleanup_errors') and
        result['restore']['clean'] is True and result['board']=='p4xc' and result['identity']==identity,'clean original P4 voice acquisition required')
    require(len(turns)==3 and len(set(turns))==3,'three distinct original turns required')
    events=[json.loads(line) for line in (session/'events.jsonl').read_text().splitlines()]
    require(events and events[-1].get('kind')=='session-end','closed source recorder required')
    require(all(a['monotonic_ns']<=b['monotonic_ns'] for a,b in zip(events,events[1:])),'reordered source events')
    data=pcm(session/'tts.wav');replies=[]
    for index in turns:
        turn=run/f'turn-{index:02d}.json';row=json.loads(turn.read_text())
        require(row['kind']=='long' and row['passed'] is True,'original completed long reply required')
        evidence=row['events'];first=min(e['monotonic_ns'] for e in evidence);last=max(e['monotonic_ns'] for e in evidence)
        peers={e['pc_id'] for e in evidence if e.get('pc_id')};require(len(peers)==1,'one source peer required')
        selected=[e for e in events if first<=e['monotonic_ns']<=last]
        require(not any(e.get('drop') or e.get('frame') in ('ErrorFrame','FatalErrorFrame','CancelFrame') for e in selected),'damaged original reply')
        audio=[e for e in selected if e.get('kind')=='audio' and e.get('stream')=='tts']
        require(audio and all(e.get('pc_id') in peers for e in audio),'TTS source peer/turn mismatch')
        require(not any(e.get('frame')=='InterruptionFrame' and audio[0]['monotonic_ns']<=e['monotonic_ns']<=audio[-1]['monotonic_ns']
            for e in selected),'interrupted original reply')
        require(all(a['sample_end']==b['sample_start'] for a,b in zip(audio,audio[1:])),'missing source PCM chunks')
        start,end=audio[0]['sample_start'],audio[-1]['sample_end']
        require(type(start) is int and type(end) is int and 0<=start<end<=len(data)//2,'source range out of bounds')
        profile=[e for e in events if e.get('kind')=='stt-profile' and e.get('pc_id') in peers]
        require(len(profile)==1 and {k:v for k,v in profile[0].items() if k not in ('kind','monotonic_ns','pc_id')}==identity['profile_manifest'],'source profile witness mismatch')
        paths={str(p.resolve()):digest(p) for p in (run/'result.json',run/'identity.json',turn,session/'events.jsonl',session/'tts.wav')}
        replies.append(dict(id=f'{next(iter(peers))}:turn-{index}',original_long=True,source=str((session/'tts.wav').resolve()),
            sample_start=start,sample_end=end,pcm_sha256=hashlib.sha256(data[start*2:end*2]).hexdigest(),
            identity=identity,provenance=paths,prompt_users=row.get('users'),reply_text=row.get('assistants'),
            jabra_recording=row['recording']))
    return dict(schema=1,run=str(run.resolve()),session=str(session.resolve()),turns=turns,replies=replies,qualification=False,
        source_kind='live pre-transport TTS; recorder storage offsets, not an acoustic clock')

def candidate(levels):
    require(len(levels)==3 and all(type(v) in (int,float) and math.isfinite(v) for v in levels),'three finite reply levels required')
    median=statistics.median(levels)
    rounded=int((Decimal(str(median))*10).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    return dict(median_active_effective_dbfs=median,rounded_dbfs_x10=rounded,candidate_seed_dbfs_x10=max(-600,min(-100,rounded)))

def derive(selection,settings,probe,out):
    chosen=json.loads(selection.read_text());p=json.loads(settings.read_text());stamp=json.loads(Path(str(probe)+'.json').read_text())
    require(chosen==select(Path(chosen['run']),Path(chosen['session']),chosen['turns']),'original source selection changed')
    require(p['sources']==stamp['sources']==sources() and digest(probe)==stamp['executable_sha256'],'DSP/probe provenance changed; rebuild host probe')
    require(set(p['config'])==set(FIELDS) and all(type(v) in (int,float) and math.isfinite(v) for v in p['config'].values()),'invalid product DSP policy')
    replies=chosen['replies'];require(len(replies)==3 and len({r['id'] for r in replies})==3,'three independent original replies required')
    require(len({r['pcm_sha256'] for r in replies})==3,'replayed/duplicate source PCM')
    profiles=[{k:r['identity'][k] for k in ('profile_manifest','bot_source_sha','policy_source_sha')} for r in replies]
    require(all(profile==profiles[0] for profile in profiles),'reply source/profile mismatch')
    ranges={};results=[];out.mkdir(parents=True,exist_ok=False)
    for index,row in enumerate(replies,1):
        require(row.get('original_long') is True and row.get('provenance'),'original long reply provenance required')
        for name,sha in row['provenance'].items():require(digest(name)==sha,'source evidence changed: '+name)
        source=Path(row['source']);data=pcm(source);start,end=row['sample_start'],row['sample_end']
        require(type(start) is int and type(end) is int and 0<=start<end<=len(data)//2,'invalid source range')
        prior=ranges.setdefault(str(source.resolve()),[]);require(all(end<=a or start>=b for a,b in prior),'overlapping original replies');prior.append((start,end))
        selected=data[start*2:end*2];require(hashlib.sha256(selected).hexdigest()==row['pcm_sha256'],'source PCM changed')
        raw=out/f'reply-{index}.pcm';raw.write_bytes(selected);rendered=out/f'reply-{index}-product.pcm'
        stats=json.loads(subprocess.check_output([str(probe.resolve()),str(raw),str(rendered),*[str(p['config'][f]) for f in FIELDS]],text=True))
        require(stats['source_clips']==stats['output_clips']==0 and stats['active_samples']>0 and math.isfinite(stats['active_effective_dbfs']),'clipped/inactive source replay')
        results.append(dict(id=row['id'],**stats,rendered_sha256=digest(rendered),activity_sha256=digest(str(rendered)+'.active'),source=row))
    result=dict(schema=1,qualification=False,passed=False,confirmation='pending P4 cold-first-word and steady-state Jabra clean-band/zero-clip validation',
        selection_sha256=digest(selection),policy_sha256=digest(settings),policy=p,probe_sha256=digest(probe),replies=results,
        **candidate([r['active_effective_dbfs'] for r in results]))
    write(out/'candidate.json',result);return result

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('build-probe');p.add_argument('--out',type=Path,required=True)
    p=sub.add_parser('policy');p.add_argument('--build',type=Path,required=True);p.add_argument('--bundle',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p=sub.add_parser('select');p.add_argument('--run',type=Path,required=True);p.add_argument('--session',type=Path,required=True);p.add_argument('--turns',type=int,nargs=3,required=True);p.add_argument('--out',type=Path,required=True)
    p=sub.add_parser('derive');p.add_argument('--selection',type=Path,required=True);p.add_argument('--policy',type=Path,required=True);p.add_argument('--probe',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    args=parser.parse_args(argv)
    if args.action=='build-probe':build_probe(args.out)
    elif args.action=='policy':write(args.out,policy(args.build,args.bundle))
    elif args.action=='select':write(args.out,select(args.run,args.session,args.turns))
    else:print(json.dumps(derive(args.selection,args.policy,args.probe,args.out)))
    return 0

if __name__=='__main__':raise SystemExit(main())
