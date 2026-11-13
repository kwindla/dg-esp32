#!/usr/bin/env python3
"""Coordinator-only original standard-prompt capture; offline mic/STT scoring.

Separate from frozen voice qualification. Uses the existing restore hold and
unchanged live STT profile; never starts a bot or changes a microphone gain.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import time

from make_bundle import digest,verify_bundle
from wire_contract import require
from p4_seed_derive import write

ROOT=Path(__file__).resolve().parents[1]

def prompts():
    plan=json.loads((ROOT/'hil/fixtures/p4xc/audio-plan.json').read_text())
    bank=json.loads((ROOT/'hil/fixtures/agent_conversation/manifest.json').read_text())['turns']
    text={r['id']:r['text'] for r in bank}
    text['speech']=json.loads((ROOT/'server/fixtures/manifest.json').read_text())['prompt_france.wav']['text']
    return [dict(id=name,path=ROOT/path,expected=text[name],source_sha256=digest(ROOT/path)) for name,path in plan['prompts'].items()]

def transcripts(events):
    rows=[e for e in events if e.get('kind')=='aggregator-user-turn-stopped']
    received=[e['text'] for e in rows if isinstance(e.get('text'),str)]
    status='missing-transcript' if not received else 'single-transcript' if len(rows)==len(received)==1 else 'split-transcript'
    return dict(received_transcripts=received,transcript_rows=rows,received_text=' '.join(received),transcript_status=status)

def reference_issue(reference):
    if not isinstance(reference,dict):return 'standard-prompt S3 result object required'
    if reference.get('reference_load_error'):return reference['reference_load_error']
    if not (reference.get('board')=='echoear' and reference.get('acquisition_passed') is True and
            isinstance(reference.get('restore'),dict) and reference['restore'].get('clean') is True and
            not reference.get('error') and not reference.get('cleanup_errors')):return 'clean S3 reference required'
    identity=reference.get('identity');rows=reference.get('prompts')
    if not (isinstance(identity,dict) and isinstance(identity.get('profile_manifest'),dict) and isinstance(rows,list) and rows):
        return 'standard-prompt rows and profile required; voice-smoke results are not this reference schema'
    if not all(isinstance(row,dict) and all(isinstance(row.get(key),str) for key in ('expected','received_text','source_sha256')) for row in rows):
        return 'malformed standard-prompt reference rows'
    return None

def scores(run,references):
    from echoear_hot_stt import word_errors
    from speech_accuracy import speech_accuracy
    from playback_quality import microphone_clip_gate
    require(run.get('acquisition_passed') is True and run.get('restore',{}).get('clean') is True and not run.get('error'),'clean prompt acquisition required')
    rows=[];valid_references=[];reference_findings=[]
    for index,reference in enumerate(references):
        issue=reference_issue(reference)
        if issue:reference_findings.append(dict(index=index,reason=issue))
        else:valid_references.append(reference)
    verified=not reference_findings
    definitions=prompts()
    require([r['id'] for r in run['prompts']]==[r['id'] for r in definitions],'standard prompts incomplete or reordered')
    for row,spec in zip(run['prompts'],definitions):
        require(row['source_sha256']==spec['source_sha256'] and row['expected']==spec['expected'],'standard prompt source/text changed')
        errors=word_errors(row['expected'],row['received_text'])
        require(errors['accuracy']==speech_accuracy(row['expected'],row['received_text']),'frozen accuracy mismatch')
        matches=[]
        for reference in valid_references:
            if reference['identity']['profile_manifest']!=run['identity']['profile_manifest']:continue
            matches.extend(word_errors(r['expected'],r['received_text'])['wer'] for r in reference['prompts']
                if r['source_sha256']==row['source_sha256'] and r['expected']==row['expected'] and
                r.get('transcript_status','single-transcript')=='single-transcript')
        band=max(matches) if matches else None;verified &= band is not None
        before,after=row['before'],row['after']
        require(type(before.get('generation')) is int and before['generation']>0 and
            before['generation']==after.get('generation') and isinstance(before.get('pc_id'),str) and before['pc_id'] and
            before['pc_id']==after.get('pc_id'),'prompt session changed or unavailable')
        capture_keys=('rx_overflows','rx_completions','read_errors','read_timeouts','read_frames')
        require(all(isinstance(s.get('capture'),dict) and all(type(s['capture'].get(key)) is int and s['capture'][key]>=0
            for key in capture_keys) for s in (before,after)),'top-level capture counters unavailable or invalid')
        capture={key:after['capture'][key]-before['capture'][key] for key in capture_keys}
        require(all(v>=0 for v in capture.values()),'capture counters reset within prompt')
        clip_gate=microphone_clip_gate(before,after)
        require(clip_gate['available'],'top-level microphone clip counters unavailable or invalid')
        clips=dict(mic_in_turn_clips=clip_gate['delta'],mic_gate_open_clips=clip_gate['gate_open_delta'],
            mic_gate_closed_clips=clip_gate['gate_closed_delta'])
        status=row.get('transcript_status','single-transcript')
        rows.append(dict(prompt=row['id'],**errors,clips=clips,microphone_clip_gate=clip_gate,capture=capture,
            s3_worst_wer=band,transcript_status=status,
            transcript_finding=None if status=='single-transcript' else status,
            passed=status=='single-transcript' and errors['accuracy']>=.90 and clip_gate['passed'] and row['native_adc_clips']==0 and
                band is not None and errors['wer']<=band))
    return dict(qualification=False,verified=verified,passed=verified and all(r['passed'] for r in rows),prompts=rows,
        reference_findings=reference_findings,
        disposition='scored' if verified else 'unverified: invalid S3 reference input or matching prompt/profile WER evidence missing')

def live(args):
    import boards,board_smoke as smoke
    from p4_audio_fix4 import VoiceIO
    from p4_product import candidate_storage
    from p4_step14 import restoring
    from alsa import play_wav,resolve_jabra,set_volumes
    from bench_calibration import playback
    from speaker_dual_sweep_test import dump_mic
    import numpy as np
    from p4_seed_derive import pcm
    boards.require_bench();manifest,_=verify_bundle(args.bundle,args.board)
    require(manifest['profile']=='normal' and not manifest['aec_enabled'],'normal AEC-off bundle required')
    identity=json.loads(args.bot_identity.read_text());args.out.mkdir(parents=True,exist_ok=False)
    result=dict(board=args.board,identity=identity,elf_sha256=manifest['elf_sha256'],acquisition_passed=False,
        qualification=False,prompts=[],gain_policy='bundle default; no runtime analog/digital gain writes')
    guard=io=None
    try:
        card,device=resolve_jabra();set_volumes(card,args.out/'mixer.log')
        with restoring(args.restore_record,args.restore_record_sha256),candidate_storage(args.board,args.bundle,args.out):
            io=VoiceIO(args,args.out,manifest,identity);io.preflight_tools();io.preflight_restore();guard=io.hold()
            with guard:
                guard.touch();io.flash(args.bundle,manifest,'candidate')
                with io.open() as board:
                    try:
                        io.boot(board);io.prepare_manual(board);io.begin(board,'manual');io.pause(board,5)
                        for spec in prompts():
                            directory=args.out/spec['id'];directory.mkdir()
                            before=io.snapshot(board);at=time.monotonic_ns()
                            board.command('mic-record 1');io.pause(board,.4)
                            source=np.frombuffer(pcm(spec['path']),'<i2').astype(float)
                            stimulus=playback(directory/'stimulus.wav',source)
                            played=play_wav(stimulus,device);io.pause(board,.4);board.command('mic-record 0')
                            raw=directory/'native.wav';dump_mic(board,raw)
                            def events():return [e for e in io.events()[1] if e['monotonic_ns']>=at]
                            wait_error=None
                            try:io.wait(board,lambda:smoke.turn_finished(events(),1),60,label='standard prompt and original reply completion')
                            except TimeoutError as exc:wait_error=str(exc)
                            io.pause(board,2);after=io.snapshot(board);observed_events=events()
                            received=transcripts(observed_events)
                            received['reply_wait_timeout']=wait_error
                            if wait_error is not None:
                                received['transcript_status']='no-reply' if received['transcript_rows'] else 'missing-transcript'
                            write(directory/'transcripts.json',received)
                            native=np.frombuffer(pcm(raw),'<i2').astype(float)
                            result['prompts'].append(dict(id=spec['id'],expected=spec['expected'],source_sha256=spec['source_sha256'],
                                **received,events=observed_events,before=before,after=after,playback=played,
                                native_path=str(raw),native_sha256=digest(raw),native_peak=float(np.max(np.abs(native))),
                                native_adc_clips=int(np.count_nonzero(np.abs(native)>=32767)),recorder=str(io.events()[0])))
                            write(directory/'prompt.json',result['prompts'][-1])
                        io.pause(board,max(0,13-(io.clock()-io.since)));io.freeze(board);io.snapshot(board)
                        io.finish(board,'main');smoke.resource_check([r[k] for r in result['prompts'] for k in ('before','after')],args.board)
                        result.update(acquisition_passed=True,boot_audio=getattr(io,'boot_audio',None));guard.success=True
                    finally:
                        import sys
                        error=sys.exc_info()[1]
                        try:io.cleanup(board)
                        except Exception as exc:
                            result.setdefault('cleanup_errors',[]).append(type(exc).__name__+': '+str(exc));guard.success=False
                            if error is None:raise
            result['restore']=guard.record;require(guard.record.get('clean') is True,'unclean restoration')
            if args.board=='p4xc':require(guard.record.get('action')=='p4-product-of-record','pinned normal P4 restore required')
    except BaseException as exc:
        result.update(acquisition_passed=False,error=type(exc).__name__+': '+str(exc))
        if guard is not None:result['restore']=guard.record
    finally:
        try:write(args.out/'result.json',result)
        finally:
            if guard is not None:guard.release_signals()
    return 0 if result['acquisition_passed'] else 1

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('live');p.add_argument('--board',choices=('p4xc','echoear'),required=True)
    for name in ('bundle','out','bot-dir','bot-identity'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--bot-repo',type=Path,default=Path.home()/'src/dg-esp32')
    p.add_argument('--restore-record',type=Path);p.add_argument('--restore-record-sha256')
    p.add_argument('--factory-backup',dest='p4_factory_backup',type=Path,default=Path.home()/'src/evidence/echoear/p4/factory-20261031T060646Z')
    p.set_defaults(headless=True,calibration=True,reference_only=False,p4_restore_bundle=None,p4_restore_elf=None)
    p=sub.add_parser('score');p.add_argument('run',type=Path);p.add_argument('--s3',type=Path,nargs='*',default=[]);p.add_argument('--out',type=Path,required=True)
    args=parser.parse_args(argv)
    if args.action=='live':
        print('Coordinator runner PID='+str(os.getpid()),flush=True)
        return live(args)
    references=[];reference_hashes={}
    for directory in args.s3:
        path=directory/'result.json'
        try:data=path.read_bytes()
        except FileNotFoundError:
            references.append(dict(reference_load_error='S3 reference result.json missing: '+str(path)))
            reference_hashes[str(directory)]=None
        else:
            references.append(json.loads(data));reference_hashes[str(directory)]=hashlib.sha256(data).hexdigest()
    result=scores(json.loads((args.run/'result.json').read_text()),references)
    result.update(run_sha256=digest(args.run/'result.json'),reference_sha256=reference_hashes);write(args.out,result)
    return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
