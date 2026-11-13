#!/usr/bin/env python3
"""Offline event-based re-score of the decision's retained guard; no acquisition."""
import argparse
import json
import math
from pathlib import Path

import board_smoke as smoke
import p4_aec_measure as measure
import p4_aec_policy as policy
import p4_aec_reselect as fix4
from make_bundle import digest
from wire_contract import require

DECISION=fix4.JOBS/'decision-2026-11-02-p4-aec-guard-event-detection.md'
DECISION_SHA256='073face2753660d75b37f69a38b28c76d36a4070fec3c2c8775b6a9eb2b97d09'
RETAINED=Path('/home/khkramer/src/evidence/echoear/p4/step-15b/coordinator-fix4')
ATTEMPT=RETAINED/'guard-00/attempt-01/result.json'
ATTEMPT_SHA256='e40aceeda835a07a359e4b973853428115675a0458a75ee5123a918238b353b3'
GAIN=RETAINED/'reselection.json'
GAIN_SHA256='f61e1d7eab45a5cfef4c60fa9a1ce7d359eae9b6253b7e903f0e45de874e2e7d'
PLAN_SHA256='abaaca7f6c65dc380aef91c1dbd7dcb9ff69c3720c02f592f20f9c9e41894982'


def context():
    require(digest(DECISION)==DECISION_SHA256,'guard event-detection decision changed')
    return dict(id='p4-aec-guard-event-rescore-v1',decision=dict(id=DECISION.stem,
        path=str(DECISION),sha256=DECISION_SHA256),
        adapter_source=dict(path=str(Path(__file__).resolve()),sha256=digest(Path(__file__))),
        acquisition_scoring_context=fix4.scoring_context())


def event_probe(original,guard_ms,rearm_ms):
    """Apply only the decision's bot-event rule; sample-level timing is diagnostic."""
    frames=smoke.downstream_frames(original['events'])
    request=(original.get('near_playback') or {}).get('request_monotonic_s')
    bot_start=original.get('bot_started_ns')
    valid=type(request) in (int,float) and math.isfinite(request) and request>0 and type(bot_start) is int and bot_start>0
    injected=request*1e9 if valid else float('inf')
    starts=[e for e in frames if e.get('frame')=='UserStartedSpeakingFrame' and
        e.get('bot_active') is True and e['monotonic_ns']>=injected]
    interruptions=[e for e in frames if e.get('frame')=='InterruptionFrame' and e['monotonic_ns']>=injected]
    earlier=[e for e in frames if e.get('frame') in ('UserStartedSpeakingFrame','InterruptionFrame') and
        e.get('bot_active') is True and e['monotonic_ns']<injected]
    transcripts=[e for e in original['events'] if e.get('kind')=='aggregator-user-turn-stopped' and e['monotonic_ns']>=injected]
    transcript_ok=len(transcripts)==1 and isinstance(transcripts[0].get('text'),str) and bool(transcripts[0]['text'].strip())
    after=bool(valid and injected>bot_start+(guard_ms+rearm_ms)*1000000)
    return dict(detected=bool(valid and starts and interruptions and transcript_ok and not earlier),
        after_rearm=after,inside_guard=False,physical_timing_report_only=True,
        request_after_bot_start_ms=(injected-bot_start)/1000000 if valid else None,
        required_request_after_ms=guard_ms+rearm_ms,post_injection_bot_active_starts=len(starts),
        post_injection_interruptions=len(interruptions),earlier_bot_active_turns=len(earlier),
        transcript_count=len(transcripts),transcript=transcripts[0].get('text') if len(transcripts)==1 else None,
        transcript_nonempty=bool(transcript_ok),basis='retained DOWNSTREAM bot turn events; approved event-detection decision')


def score_events(row):
    turns=[]
    for original in row['long_turns']:
        require(isinstance(original.get('events'),list),'original long-turn events required')
        try:turn=smoke.turn_checks(original['events'],'long',True,calibration=True)
        except ValueError as exc:turn=dict(passed=False,error=str(exc))
        turns.append(turn)
    guard,rearm=policy.GUARDS[row['candidate_index']]
    return policy.guard_score(row['candidate_index'],turns,row['onsets'],
        event_probe(row['inside_original'],guard,rearm),event_probe(row['after_original'],guard,rearm))


def authenticate(plan_path,gain_path,attempt_path):
    context()
    require(attempt_path.resolve()==ATTEMPT and digest(attempt_path)==ATTEMPT_SHA256,
        'decision applies only to the pinned retained guard attempt')
    require(gain_path.resolve()==GAIN and digest(gain_path)==GAIN_SHA256,'retained reselection changed')
    require(digest(plan_path)==PLAN_SHA256,'retained acquisition plan changed')
    plan=json.loads(plan_path.read_text());fix4.validate_frozen_plan(plan)
    gain_record=json.loads(gain_path.read_text())
    require(Path(gain_record['plan_file']).resolve()==plan_path.resolve(),'reselection plan path differs')
    gain=fix4.checked_gain(gain_path,plan)
    require(gain==30,'decision requires the authenticated selected 30 dB gain')
    row=json.loads(attempt_path.read_text())
    require(row.get('kind')=='p4-aec-guard' and row.get('board')=='p4xc' and row.get('candidate_index')==0 and
        row.get('status')=='acquired' and row.get('acquisition_passed') is True and
        row.get('restore',{}).get('clean') is True and row['restore'].get('action')=='p4-product-of-record',
        'clean retained guard candidate zero required')
    require(row.get('policy')==policy.POLICY and row.get('elf_sha256')==plan['elf_sha256'] and
        row.get('plan_sha256')==digest(plan_path) and row.get('gain_selection_sha256')==digest(gain_path),
        'guard plan/image/gain provenance differs')
    require(row.get('scoring_context')==fix4.scoring_context(),'original guard scorer/decisions changed')
    folder=attempt_path.parent
    require(json.loads((folder/'scored-original.json').read_text())==row,'original guard marker differs')
    payload=folder/'post-body-payload.json';require(digest(payload)==row['post_body_payload_sha256'],'guard payload hash differs')
    require(all(row.get(k)==v for k,v in json.loads(payload.read_text()).items()),'guard payload differs')
    for index,turn in enumerate(row['long_turns']):
        require(json.loads((folder/f'turn-{index+1:02d}.json').read_text())==turn,'retained long-turn original differs')
    for key,name in (('inside_original','inside-guard'),('after_original','after-rearm')):
        require(json.loads((folder/name/'original.json').read_text())==row[key],'retained probe original differs')
    # Authenticate the original physical scorer and all its PCM/dump/clock inputs.
    # Its failed source_match stays visible, without gating event detection.
    require(measure.score_guard_row(row,plan,prompt_scorer=fix4.direction_prompt_score)==row['score'],
        'original guard score does not reproduce')
    return plan,gain_record,row


def rescore(plan_path,gain_path,attempt_path):
    plan_path=Path(plan_path);gain_path=Path(gain_path);attempt_path=Path(attempt_path)
    plan,gain,row=authenticate(plan_path,gain_path,attempt_path)
    sources={str(p.resolve()):digest(p) for p in sorted(attempt_path.parent.rglob('*')) if p.is_file()}
    sources.update({str(p.resolve()):digest(p) for p in (plan_path,gain_path,DECISION)})
    score=score_events(row)
    return dict(kind='p4-aec-guard-event-rescore',qualification=False,applied=False,
        plan_file=str(plan_path.resolve()),gain_file=str(gain_path.resolve()),attempt_file=str(attempt_path.resolve()),
        source_elf_sha256=plan['elf_sha256'],selected_gain_db=30,scoring_context=context(),
        source_sha256=sources,original_score=row['score'],score=score,
        physical_timing_report_only=True,delay_provenance=gain['rescored_selection']['delay_provenance'])


def checked_rescore(path):
    row=json.loads(Path(path).read_text())
    require(row.get('kind')=='p4-aec-guard-event-rescore','event-based guard re-score required')
    require(row==rescore(Path(row['plan_file']),Path(row['gain_file']),Path(row['attempt_file'])),
        'guard re-score provenance or score changed')
    return row


def recommendation(plan_path,gain_path,guard_paths):
    plan_path=Path(plan_path);gain_path=Path(gain_path);guard_paths=[Path(p) for p in guard_paths]
    rows=[checked_rescore(p) for p in guard_paths]
    require(all(Path(r['plan_file'])==plan_path.resolve() and Path(r['gain_file'])==gain_path.resolve() for r in rows),
        'guard re-score belongs to another plan/gain')
    selected=policy.select_guard([r['score'] for r in rows]);require(selected['passed'],'no accepted guard; stop')
    plan=json.loads(plan_path.read_text());gain=json.loads(gain_path.read_text())['rescored_selection']
    delay=measure.delay_input(Path(gain['delay_file']),plan)
    sources={str(p.resolve()):digest(p) for p in (plan_path,gain_path,*guard_paths)}
    for row in rows:sources.update(row['source_sha256'])
    return dict(kind='p4-aec-event-guard-recommendation',passed=True,qualification=False,applied=False,
        plan_file=str(plan_path.resolve()),gain_file=str(gain_path.resolve()),guard_files=[str(p.resolve()) for p in guard_paths],
        source_elf_sha256=plan['elf_sha256'],source_sha256=sources,policy=policy.POLICY,scoring_context=context(),
        selected=dict(ref_gain_db=30,guard_ms=selected['guard_ms'],rearm_ms=selected['rearm_ms'],afe_delay_samples=delay['delay_samples']),
        delay_provenance=delay,physical_timing_report_only=True,
        replay='hil/p4_aec_guard_rescore.py recommend; fresh --out required',
        next='review follow-up defaults and adoption replay; rebuild/image/headroom/bundle checks; no firmware settings applied')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    for action in ('guard-rescore','recommend'):
        cmd=sub.add_parser(action)
        for name in ('plan','gain-selection','out'):cmd.add_argument('--'+name,type=Path,required=True)
        if action=='guard-rescore':cmd.add_argument('--attempt',type=Path,required=True)
        else:cmd.add_argument('--guard-results',type=Path,nargs='+',required=True)
    args=parser.parse_args(argv)
    require(not args.out.exists(),'fresh offline output required')
    require(not args.out.resolve().is_relative_to(RETAINED.resolve()),'fresh output must be outside retained acquisition')
    result=(rescore(args.plan,args.gain_selection,args.attempt) if args.action=='guard-rescore' else
        recommendation(args.plan,args.gain_selection,args.guard_results))
    policy.write_new(args.out,result)
    return 0 if (result['score']['passed'] if args.action=='guard-rescore' else result['passed']) else 1


if __name__=='__main__':raise SystemExit(main())
