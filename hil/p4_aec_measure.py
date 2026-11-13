#!/usr/bin/env python3
"""Bounded P4 AEC captures and offline selection. Live commands are coordinator-only."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import time

import numpy as np
import board_smoke as smoke
from p4_audio_fix4 import VoiceIO
from make_bundle import digest,verify_bundle
from p4_aec_policy import (ROOT,POLICY,GAINS,GUARDS,GAIN_PARTS,write_new,make_plan,validate_plan,
    capture,delay_score,clip_evidence,reference_gain,echo_score,prompt_score,select_gain,guard_score,select_guard,GUARD_RE)
from wire_contract import require
from atomic_json import atomic_write_json


class MeasurementIO(VoiceIO):
    """Reuse boot/session policing, but do not score dump stalls as wire smokes."""
    def prepare_manual(self,board):
        if self.manifest.get('autostart') is True:board.command('demo hold',45)
        return super().prepare_manual(board)

    def finish(self,board,label,*,hold=False):
        return smoke.LiveIO.finish(self,board,label,hold=hold)

    def configure(self,board,gain,guard=400,rearm=2000):
        board.command('audio 0');board.command('quiet 1');board.command('gate auto')
        board.command(f'aec-ref-gain {gain}');board.command(f'aec-guard-ms {guard}')
        board.command(f'aec-guard-rearm-ms {rearm}')
        at=board.cursor;board.command('aec-ref-gain-status')
        line=board.wait_for(r'AEC_REF_GAIN db=\d+ reg45=0x[0-9a-f]+',5,since=at)
        match=re.search(r'db=(\d+) reg45=0x([0-9a-f]+)',line)
        require(int(match[1])==gain and int(match[2],16)==0x10+gain//3,'runtime reference gain readback differs')
        return dict(gain_db=gain,guard_ms=guard,rearm_ms=rearm,readback=line)

    def play(self,board,path):
        from alsa import play_wav
        from echoear_aec_bench import clock_anchor
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(play_wav,path,self.device)
            while not future.done():
                self.poll(board);self.anchors.append(clock_anchor(board));time.sleep(.025)
            return future.result()

    def start_capture(self,board):
        from echoear_aec_bench import clock_anchor
        self.anchors=[clock_anchor(board)]
        self.pause(board,.1) # Anchor precedes DMA residence bounds of the first recorded block.
        self.capture_started=self.clock()
        board.command('aec-record-slots 1')

    def stop_capture(self,board,path):
        from echoear_aec_capture import dump_capture
        from echoear_aec_bench import clock_anchor
        from echoear_aec_timing import require_bracketed_capture
        from dump_verify import verify_dump
        board.command('aec-record-slots 0');self.anchors.append(clock_anchor(board))
        at=board.cursor;value=dump_capture(board,path,timeout=150)
        verify_dump([line for _,_,line in board.lines_since(at)],prefix='AEC_DUMP')
        require(value['schema']==2,'all-slot AEC capture required')
        require_bracketed_capture(value['timing'],self.anchors)
        log=path.with_suffix('.dump.log');log.write_text('\n'.join(l for _,_,l in board.lines_since(at))+'\n')
        anchor_path=path.with_suffix('.anchors.json');write_new(anchor_path,self.anchors)
        return dict(path=str(path.resolve()),sha256=digest(path),dump=str(log.resolve()),dump_sha256=digest(log),
                    anchors=str(anchor_path.resolve()),anchors_sha256=digest(anchor_path))

    def simple_capture(self,board,path,action):
        self.start_capture(board);self.pause(board,.5)
        playback=action();self.pause(board,.6)
        return dict(**self.stop_capture(board,path),playback=playback)

    def quiet(self,board,path):
        self.start_capture(board);self.pause(board,2)
        return self.stop_capture(board,path)

    def reply(self,board,folder,spec,prompt=None,probe=False):
        from echoear_aec_bench import clock_anchor
        folder.mkdir();before=self.snapshot(board);started=time.monotonic_ns();cursor=board.cursor;self.start_capture(board)
        self.pause(board,.3);trigger=self.play(board,Path(spec['path']))
        rows=lambda:[e for e in self.events()[1] if e['monotonic_ns']>=started]
        near=None;bot_started=None;complete=False;injection_status='not-observed'
        until=self.capture_started+POLICY['max_capture_seconds']-2
        while self.clock()<until:
            self.poll(board);self.anchors.append(clock_anchor(board));events=rows()
            speech=[e for e in smoke.downstream_frames(events) if e.get('frame')=='BotStartedSpeakingFrame']
            if speech and bot_started is None:bot_started=speech[0]['monotonic_ns']
            guard_entries=[(stamp,match) for _,stamp,line in board.lines_since(cursor) for match in [GUARD_RE.search(line)] if match]
            target=(guard_entries[0][0]+.05 if guard_entries else float('inf')) if probe else (bot_started/1e9+3 if bot_started else float('inf'))
            if prompt and near is None and bot_started and self.clock()>=target:
                active=[e for e in smoke.downstream_frames(events) if e.get('frame') in ('BotStartedSpeakingFrame','BotStoppedSpeakingFrame')]
                if active[-1]['frame']!='BotStartedSpeakingFrame':
                    injection_status='missed-bot-playback'
                else:
                    near=self.play(board,Path(prompt['path']));injection_status='played; physical timing scored separately'
            if smoke.turn_finished(events,2 if prompt and near and not probe else 1):complete=True;break
            time.sleep(.025)
        self.pause(board,min(2,max(0,self.capture_started+POLICY['max_capture_seconds']-self.clock())))
        captured=self.stop_capture(board,folder/'capture.npz');after=self.snapshot(board);events=rows()
        # Missing near-end STT is a scored failure, not an acquisition timeout
        # that can be retried for a better transcript. Inside injection is diagnostic.
        if prompt and smoke.turn_finished(events,1):complete=True
        # Drain an overlong reply before another original, without extending or
        # relabelling the declared capture. It will remain unscorable.
        if not complete:
            self.wait(board,lambda:smoke.turn_finished(rows(),1),60,label='original reply drain')
        stopped=[e['monotonic_ns'] for e in smoke.downstream_frames(events) if e.get('frame')=='BotStoppedSpeakingFrame']
        if prompt and near is None:injection_status='missed-bot-playback'
        result=dict(capture=captured,before=before,after=after,events=events,complete=complete,
            trigger=trigger,near_playback=near,injection_status=injection_status,bot_started_ns=bot_started,bot_stopped_ns=stopped[0] if stopped else None,
            guard_lines=[line for _,_,line in board.lines_since(cursor) if GUARD_RE.search(line)])
        write_new(folder/'original.json',result)
        return result


def load_capture(row):
    from dump_verify import verify_dump
    from echoear_aec_capture import decode_capture
    from echoear_aec_timing import require_bracketed_capture
    require(digest(Path(row['dump']))==row['dump_sha256'],'dump log hash mismatch')
    require(digest(Path(row['anchors']))==row['anchors_sha256'],'capture clock anchors changed')
    lines=Path(row['dump']).read_text().splitlines();verify_dump(lines,prefix='AEC_DUMP')
    decoded=decode_capture(lines);pcm,timing=capture(Path(row['path']),row['sha256'])
    require(np.array_equal(decoded['pcm'],pcm) and np.array_equal(decoded['timing'],timing),'NPZ differs from original checked dump')
    require_bracketed_capture(timing,json.loads(Path(row['anchors']).read_text()))
    return pcm


DELAY_DECISION_PATH=Path('/home/khkramer/src/dg-esp32/proj-2026-10-30-2143/codex-jobs/decision-2026-11-02-p4-aec-delay-1024.md')
DELAY_DECISION_SHA256='5f476857a8297174b910c4c306c50a784d91b04d738975f925092960dc68a438'
DELAY_SOURCE_PATH=Path('/home/khkramer/src/evidence/echoear/p4/step-15b/coordinator/delay/attempt-01/result.json')
DELAY_SOURCE_SHA256='aa8ef6a2dc7088fc871885ba44b3306af47b552307ac7403d57558312212637c'


def delay_decision(decision,source):
    decision=Path(decision).resolve();source=Path(source).resolve()
    require(decision==DELAY_DECISION_PATH and digest(decision)==DELAY_DECISION_SHA256,'approved delay decision path/hash required')
    require(source==DELAY_SOURCE_PATH and digest(source)==DELAY_SOURCE_SHA256,'approved source attempt path/hash required')
    original=json.loads(source.read_text())
    require(original.get('kind')=='p4-aec-delay' and original.get('board')=='p4xc' and
        original.get('acquisition_passed') is True and original.get('restore',{}).get('clean') is True and
        original.get('policy')==POLICY and original.get('score',{}).get('available') is False,
        'approved clean, unavailable policy-v2 delay original required')
    return dict(kind='p4-aec-delay-decision',decision_file=str(decision),decision_sha256=digest(decision),
        source_attempt_file=str(source),source_attempt_sha256=digest(source),delay_samples=1024,
        elf_sha256=original['elf_sha256'],policy=POLICY,measured_pass=False,qualification=False,
        basis='Decision-backed 1024 samples; source measurement remains unavailable; pending reference diagnosis')


def delay_input(path,plan):
    path=Path(path);row=json.loads(path.read_text())
    common=dict(record_file=str(path.resolve()),record_sha256=digest(path),qualification=False)
    if row.get('kind')=='p4-aec-delay-decision':
        require(type(row.get('delay_samples')) is int and row.get('measured_pass') is False and
            row==delay_decision(Path(row['decision_file']),Path(row['source_attempt_file'])),
            'delay decision record altered or invalid')
        require(row['elf_sha256']==plan['elf_sha256'],'same-ELF delay decision required')
        return dict(common,kind='decision-backed',delay_samples=row['delay_samples'],measured_pass=False,
            decision_file=row['decision_file'],decision_sha256=row['decision_sha256'],
            source_attempt_file=row['source_attempt_file'],source_attempt_sha256=row['source_attempt_sha256'])
    require(row.get('kind')=='p4-aec-delay' and row.get('acquisition_passed') is True and
        row.get('restore',{}).get('clean') is True and row.get('elf_sha256')==plan['elf_sha256'] and
        row.get('policy')==POLICY,'clean same-ELF independent delay required')
    result=delay_score(load_capture(row['dac']),load_capture(row['near']),
        __import__('echoear_aec_alignment').load_nearend_source(Path(plan['fixtures'][-1]['path'])))
    require(row['score']==result and result['available'],'independent delay unavailable or altered; stop calibration')
    return dict(common,kind='measured',delay_samples=result['measured_delay_samples'],measured_pass=True)


def delay_proof(path,plan):
    return delay_input(path,plan)['delay_samples']


ATTEMPT_RE=re.compile(r'attempt-[0-9]{2}')
PREFLIGHT_ERROR_EXIT=3


def attempt_rows(directory,*,mark_unscored=False):
    paths=sorted(Path(directory).glob('attempt-*'));rows=[]
    require(all(p.is_dir() and ATTEMPT_RE.fullmatch(p.name) for p in paths),'attempt-NN directories required')
    for path in paths:
        source=path/'result.json';marker=path/'scored-original.json'
        row=None
        if source.exists():
            try:row=json.loads(source.read_text())
            except (ValueError,OSError):pass
        if not isinstance(row,dict):row=None
        if marker.exists():
            try:saved=json.loads(marker.read_text())
            except (ValueError,OSError):saved=None
            if isinstance(saved,dict):
                require(saved.get('status')=='acquired' and saved.get('acquisition_passed') is True,'invalid scored-original marker')
                require(row is None or row==saved,'result differs from scored-original marker')
                if row is None:source=marker
                row=saved
        payload=path/'post-body-payload.json'
        if payload.exists() and row and row.get('post_body_payload_sha256'):
            require(digest(payload)==row['post_body_payload_sha256'],'post-body payload changed')
        scored=payload.exists() or bool(row and (row.get('score_produced') is True or row.get('status')=='acquired' or
                             'score' in row or 'cases' in row))
        if not scored:
            require(row is None or row.get('acquisition_passed') is not True,'ambiguous acquired original')
            note=dict(status='unscored',score_produced=False,attempt=path.name,
                reason=row.get('status','incomplete-result') if row else 'missing-or-incomplete-result',
                result_sha256=digest(source) if source.exists() else None,
                scored_marker_sha256=digest(marker) if marker.exists() else None,
                device_untouched=row.get('device_untouched') if row else None)
            unscored=path/'unscored-attempt.json'
            if mark_unscored:
                if unscored.exists():require(json.loads(unscored.read_text())==note,'unscored attempt evidence changed')
                else:write_new(unscored,note)
        rows.append((source,row,scored))
    return rows


def held(args,plan,body,*,io_factory=MeasurementIO,metadata=None,plan_validator=None):
    """One bounded part; unscored failures retryable, scored originals sealed."""
    import boards
    from p4_step14 import restoring,restore_record
    from p4_product import candidate_storage
    from alsa import record_room_baseline,resolve_jabra,set_volumes
    boards.require_bench()
    require(ATTEMPT_RE.fullmatch(args.out.name),'--out must end in attempt-NN (two digits)')
    require(not args.out.resolve().is_relative_to(args.bundle.resolve()),'capture output must be outside bundle')
    require(not args.out.exists(),'fresh attempt-NN required')
    prior=attempt_rows(args.out.parent,mark_unscored=True)
    require(not any(scored for _,_,scored in prior),'acquired scored originals cannot be retaken')
    require(not prior or args.out.name>prior[-1][0].parent.name,'attempt-NN must increase')
    args.out.mkdir(parents=True,exist_ok=False)
    result=dict(kind='p4-aec-'+args.action,board='p4xc',elf_sha256=plan['elf_sha256'],policy=POLICY,
        acquisition_passed=False,qualification=False,device_untouched=True,score_produced=False,
        prior_attempts=[dict(path=str(p.parent.resolve()),result_sha256=digest(p) if p.exists() else None) for p,_,_ in prior],
        cleanup_errors=[])
    result.update(metadata or {})
    guard=io=None
    try:
        (plan_validator or validate_plan)(plan);restore_record(args.restore_record,args.restore_record_sha256)
        result.update(plan_sha256=digest(args.plan),identity=json.loads(args.bot_identity.read_text()))
        room=record_room_baseline(args.out/'room','P4 AEC '+args.action);result['room']=room
        if room['noise_contaminated']:
            result.update(status='preflight-noisy',retryable=True);return result
        manifest,_=verify_bundle(args.bundle,'p4xc');identity=result['identity']
        with restoring(args.restore_record,args.restore_record_sha256),candidate_storage('p4xc',args.bundle,args.out):
            io=io_factory(args,args.out,manifest,identity);io.preflight_tools();io.preflight_restore()
            card,io.device=resolve_jabra();set_volumes(card,args.out/'mixer.log')
            guard=io.hold()
            with guard:
                guard.touch();result['device_untouched']=False;io.flash(args.bundle,manifest,'candidate')
                with io.open() as board:
                    primary=None
                    try:
                        io.boot(board);io.prepare_manual(board);payload=body(io,board)
                        result.update(payload,status='scored',score_produced=True,retryable=False)
                        payload_path=args.out/'post-body-payload.json';write_new(payload_path,payload)
                        result['post_body_payload_sha256']=digest(payload_path)
                        atomic_write_json(args.out/'result.json',result)
                        if args.action in ('gain','guard','double-talk'):io.finish(board,'main')
                    except BaseException as exc:
                        primary=exc;raise
                    finally:
                        try:io.cleanup(board)
                        except BaseException as exc:
                            result['cleanup_errors'].append(type(exc).__name__+': '+str(exc))
                            if primary is None:raise
                guard.success=True
            result['restore']=guard.record
            require(guard.record.get('clean') is True and guard.record.get('action')=='p4-product-of-record','clean pinned-normal P4 restore required')
        result.update(status='acquired',acquisition_passed=True)
    except BaseException as exc:
        result.update(status='preflight-error' if result['device_untouched'] else 'acquisition-error',
            acquisition_passed=False,retryable=not result['score_produced'],error=type(exc).__name__+': '+str(exc))
        if guard is not None:result['restore']=guard.record
    finally:
        try:
            if result['acquisition_passed']:write_new(args.out/'scored-original.json',result)
            atomic_write_json(args.out/'result.json',result)
        finally:
            if guard is not None:guard.release_signals()
    return result


def acquire_delay(args,plan):
    from echoear_aec_alignment import load_nearend_source
    def body(io,board):
        io.configure(board,0)
        dac=io.simple_capture(board,args.out/'dac.npz',lambda:board.command('aec-probe',15))
        near=io.simple_capture(board,args.out/'near.npz',lambda:io.play(board,Path(plan['fixtures'][-1]['path'])))
        score=delay_score(load_capture(dac),load_capture(near),load_nearend_source(Path(plan['fixtures'][-1]['path'])))
        return dict(dac=dac,near=near,score=score)
    return held(args,plan,body)


def acquire_gain(args,plan,*,plan_validator=None,delay_reader=None,prompt_scorer=None):
    require(args.gain_db in GAINS and args.part in range(4),'declared gain/part required')
    provenance=(delay_reader or delay_input)(args.delay,plan);delay=provenance['delay_samples']
    metadata=dict(gain_db=args.gain_db,part=args.part,delay_file=str(args.delay.resolve()),
        delay_sha256=provenance['record_sha256'],delay_provenance=provenance)
    def body(io,board):
        settings=io.configure(board,args.gain_db);quiet=io.quiet(board,args.out/'quiet.npz')
        io.begin(board,'manual');cases=[]
        for index in GAIN_PARTS[args.part]:
            prompt=plan['fixtures'][index-9] if index>=10 else None
            name=f'echo-{index+1:02d}' if prompt is None else 'prompt-'+prompt['id']
            original=io.reply(board,args.out/name,plan['fixtures'][0],prompt)
            pcm=load_capture(original['capture']);reference_gain(original['before'],original['after'],args.gain_db)
            clips=clip_evidence(original['before'],original['after'],pcm)
            if prompt is None:
                try:score=echo_score(pcm,load_capture(quiet),delay,original['events'],original['complete'])
                except ValueError as exc:score=dict(available=False,error=str(exc),median_erle_db=None,false_user_turns=None)
            else:
                score=(prompt_scorer or prompt_score)(prompt,original['events'],original['near_playback'] or dict(request_monotonic_s=0),
                    original['bot_started_ns'] or 0,original['bot_stopped_ns'] or 0)
            cases.append(dict(id=name,original=original,clips=clips,score=score))
        return dict(metadata,settings=settings,quiet=quiet,cases=cases)
    return held(args,plan,body,metadata=metadata,plan_validator=plan_validator)


def original_part(directory):
    attempts=attempt_rows(directory)
    scored=[(path,row) for path,row,scored in attempts if scored]
    require(len(scored)==1,'one acquired scored original required; unscored attempts are unavailable')
    path,row=scored[0]
    require(attempts[-1][0]==path,'attempt after acquired scored original')
    require(row is not None and row.get('status')=='acquired' and row.get('acquisition_passed') is True and row.get('restore',{}).get('clean') is True,
            'clean acquired scored original required')
    return path,row


def gain_selection(plan,plan_path,delay_path,root,only_gain=None,*,prompt_scorer=None,plan_validator=None,part_reader=None,delay_reader=None,acquisition_prompt_scorer=None):
    (plan_validator or validate_plan)(plan)
    provenance=(delay_reader or delay_input)(delay_path,plan);delay=provenance['delay_samples'];points=[];sources={};identity=None
    delay_cache={str(Path(delay_path).resolve()):provenance};acquired_delays={}
    require(only_gain is None or type(only_gain) is int and only_gain in GAINS,'declared report gain required')
    for gain in GAINS if only_gain is None else (only_gain,):
        echoes=[];prompts=[];clips=True
        for part in range(4):
            path,row=(part_reader or original_part)(Path(root)/f'gain-{gain:02d}'/f'part-{part}')
            require(row['gain_db']==gain and row['part']==part and row['settings']['gain_db']==gain and row['policy']==POLICY and row['plan_sha256']==digest(plan_path)
                and row['elf_sha256']==plan['elf_sha256'],'gain part provenance differs')
            original_delay=Path(row['delay_file']).resolve()
            require(digest(original_delay)==row['delay_sha256'],'acquisition delay record changed')
            if str(original_delay) not in delay_cache:delay_cache[str(original_delay)]=(delay_reader or delay_input)(original_delay,plan)
            acquired=delay_cache[str(original_delay)]
            require(row['delay_provenance']==acquired,'acquisition delay provenance changed')
            acquired_delays[str(original_delay)]=acquired
            sources[str(path.resolve())]=digest(path)
            for attempt in sorted(path.parent.parent.glob('attempt-*')):
                for name in ('result.json','scored-original.json','unscored-attempt.json','post-body-payload.json'):
                    evidence=attempt/name
                    if evidence.is_file():sources[str(evidence.resolve())]=digest(evidence)
            quiet=load_capture(row['quiet'])
            if identity is None:identity=row['identity']
            require(row['identity']==identity,'gain sweep bot process/profile changed')
            require(len(row['cases'])==len(GAIN_PARTS[part]),'missing bounded-part cases')
            for case,index in zip(row['cases'],GAIN_PARTS[part]):
                original=case['original'];pcm=load_capture(original['capture'])
                reference_gain(original['before'],original['after'],gain)
                observed=clip_evidence(original['before'],original['after'],pcm);clips &= observed['passed']
                if index<10:
                    try:score=echo_score(pcm,quiet,acquired['delay_samples'],original['events'],original['complete'])
                    except ValueError as exc:score=dict(available=False,error=str(exc),median_erle_db=None,false_user_turns=None)
                    require(case['id']==f'echo-{index+1:02d}','echo cases reordered')
                    cached=score
                    if acquired['delay_samples']!=delay:
                        try:score=echo_score(pcm,quiet,delay,original['events'],original['complete'])
                        except ValueError as exc:score=dict(available=False,error=str(exc),median_erle_db=None,false_user_turns=None)
                    echoes.append(score)
                else:
                    spec=plan['fixtures'][index-9]
                    score=(acquisition_prompt_scorer or prompt_score)(spec,original['events'],original['near_playback'] or dict(request_monotonic_s=0),
                        original['bot_started_ns'] or 0,original['bot_stopped_ns'] or 0)
                    require(case['id']=='prompt-'+spec['id'],'standard prompts reordered')
                    cached=score
                    if prompt_scorer is not None:
                        score=prompt_scorer(spec,original['events'],original['near_playback'] or dict(request_monotonic_s=0),
                            original['bot_started_ns'] or 0,original['bot_stopped_ns'] or 0)
                    prompts.append(dict(id=spec['id'],**score))
                require(case['score']==cached and case['clips']==observed,'cached measurement score changed')
        points.append(dict(gain_db=gain,echoes=echoes,prompts=prompts,clips_passed=bool(clips)))
    provenance=dict(plan_file=str(Path(plan_path).resolve()),root=str(Path(root).resolve()),
        delay_file=str(Path(delay_path).resolve()),plan_sha256=digest(plan_path),delay_sha256=digest(delay_path),
        delay_provenance=provenance,acquisition_delays=acquired_delays,
        delay_rescored=any(v['record_sha256']!=provenance['record_sha256'] for v in acquired_delays.values()),
        elf_sha256=plan['elf_sha256'],policy=POLICY,identity=identity,sources=sources,points=points)
    if only_gain is None:return dict(provenance,kind='p4-aec-gain-selection',**select_gain(points))
    echoes=points[0]['echoes'];available=all(e.get('available') is True for e in echoes)
    return dict(provenance,kind='p4-aec-gain-report',profile=plan.get('profile'),report_only=True,qualification=False,gain_db=only_gain,
        available=available,median_erle_db=float(np.median([e['median_erle_db'] for e in echoes])) if available else None,
        residuals=[e.get('residual') for e in echoes],s3_reference=plan['s3_reference'])


def checked_report(path):
    row=json.loads(Path(path).read_text());plan=json.loads(Path(row['plan_file']).read_text())
    expected=gain_selection(plan,Path(row['plan_file']),Path(row['delay_file']),Path(row['root']),row['gain_db'])
    require(row==expected and row['available'],'authentic complete same-image ERLE/residual report required')
    return row


def checked_gain(path,plan):
    row=json.loads(Path(path).read_text())
    expected=gain_selection(plan,Path(row['plan_file']),Path(row['delay_file']),Path(row['root']))
    require(row==expected and row['passed'],'authentic complete eligible gain selection required')
    return row['selected_gain_db']


def probe_score(original,spec,inside,*,prompt_scorer=None):
    from echoear_aec_alignment import source_match,load_nearend_source
    from echoear_aec_timing import sample_interval,map_interval
    pcm,timing=capture(Path(original['capture']['path']),original['capture']['sha256'])
    source=load_nearend_source(Path(spec['path']));match=source_match(source,pcm[:,0])
    load_capture(original['capture']) # Recheck dump, PCM and clock hashes before using timing.
    anchors=json.loads(Path(original['capture']['anchors']).read_text())
    parsed=[tuple(map(int,m.groups())) for line in original['guard_lines'] for m in [GUARD_RE.search(line)] if m]
    if not parsed or not match.get('available'):return dict(inside_guard=False,detected=False,after_rearm=False,match=match)
    onset,deadline,guard,rearm,previous=parsed[0]
    powers=np.mean(source[:len(source)//320*320].reshape(-1,320)**2,axis=1)
    active=np.flatnonzero(powers>max(1e-24,float(powers.max())*.001))
    require(len(active)>0,'known probe has no source-active speech')
    active_offset=int(active[0])*320
    physical=sample_interval(match['lag_samples']+active_offset,timing,anchors)
    first,last=map_interval(onset,anchors),map_interval(deadline,anchors)
    certainly_inside=bool(physical and first and last and first[1]<=physical[0] and physical[1]<last[0])
    certainly_after=bool(physical and last and physical[0]>=last[1] and previous>0 and onset-previous>=rearm*1000)
    prompt=(prompt_scorer or prompt_score)(spec,original['events'],original['near_playback'] or dict(request_monotonic_s=0),
        original['bot_started_ns'] or 0,original['bot_stopped_ns'] or 0)
    detected=bool(prompt['available'] and prompt['intentional_user_turn_witnessed'] and
        prompt['playback_overlap'] and prompt['false_user_turns']==0)
    return dict(inside_guard=certainly_inside,detected=detected,after_rearm=certainly_after,
        match=match,source_active_offset_samples=active_offset,physical_onset_seconds=physical,source_onset_seconds=first,deadline_seconds=last,
        prompt=prompt,clips=clip_evidence(original['before'],original['after'],pcm))


def acquire_guard(args,plan,*,gain_checker=None,plan_validator=None,prompt_scorer=None,scoring_metadata=None):
    gain=(gain_checker or checked_gain)(args.gain_selection,plan);previous=[]
    for path in args.previous:
        row=json.loads(path.read_text())
        require(row.get('acquisition_passed') is True and row.get('restore',{}).get('clean') is True and
            row.get('gain_selection_sha256')==digest(args.gain_selection) and row.get('elf_sha256')==plan['elf_sha256'] and
            row.get('policy')==POLICY and row.get('plan_sha256')==digest(args.plan),
            'clean previous candidate bound to this gain/ELF required')
        previous.append(score_guard_row(row,plan,prompt_scorer=prompt_scorer))
    require(len(previous)==args.candidate_index and (not previous or not select_guard(previous)['passed']),
            'start at 400/2000; change only after all earlier declared candidates failed')
    guard,rearm=GUARDS[args.candidate_index]
    def body(io,board):
        io.configure(board,gain,guard,rearm);io.begin(board,'manual');cursor=board.cursor;turns=[]
        io.args.calibration=True # Bot content/persona findings are report-only; false turns remain hard.
        for index in range(10):
            started=time.monotonic_ns()
            try:turn=io.turn(board,'long',index+1)
            except ValueError as exc:
                if 'self-interruption or unexpected user turn' not in str(exc):raise
                events=[e for e in io.events()[1] if e['monotonic_ns']>=started]
                turn=dict(passed=False,error=str(exc),events=events)
                write_new(args.out/f'long-failure-{index+1:02d}.json',turn)
            turns.append(turn)
        inside=io.reply(board,args.out/'inside-guard',plan['fixtures'][0],plan['fixtures'][1],probe=True)
        io.pause(board,rearm/1000+2.5)
        after=io.reply(board,args.out/'after-rearm',plan['fixtures'][0],plan['fixtures'][1])
        onsets=[line for _,_,line in board.lines_since(cursor) if GUARD_RE.search(line)]
        row=dict(candidate_index=args.candidate_index,long_turns=turns,onsets=onsets,inside_original=inside,after_original=after)
        score=score_guard_row(row,plan,prompt_scorer=prompt_scorer)
        return dict(candidate_index=args.candidate_index,gain_selection_sha256=digest(args.gain_selection),
            long_turns=turns,onsets=onsets,inside_original=inside,after_original=after,score=score)
    return held(args,plan,body,plan_validator=plan_validator,metadata=scoring_metadata)


def score_guard_row(row,plan,*,prompt_scorer=None):
    turns=[]
    for original in row['long_turns']:
        require(isinstance(original.get('events'),list),'original guard turn events required')
        try:turn=smoke.turn_checks(original['events'],'long',True,calibration=True)
        except ValueError as exc:turn=dict(passed=False,error=str(exc))
        turns.append(turn)
    def probe(original,inside):
        if prompt_scorer is None:return probe_score(original,plan['fixtures'][1],inside)
        return probe_score(original,plan['fixtures'][1],inside,prompt_scorer=prompt_scorer)
    return guard_score(row['candidate_index'],turns,row['onsets'],
        probe(row['inside_original'],True),probe(row['after_original'],False))


def recommendation(plan_path,gain_path,guard_paths,*,gain_checker=None,plan_validator=None,prompt_scorer=None):
    plan=json.loads(plan_path.read_text());(plan_validator or validate_plan)(plan)
    gain=(gain_checker or checked_gain)(gain_path,plan)
    rows=[];sources={str(plan_path.resolve()):digest(plan_path),str(gain_path.resolve()):digest(gain_path)}
    for path in guard_paths:
        row=json.loads(path.read_text())
        require(row.get('acquisition_passed') is True and row.get('restore',{}).get('clean') is True and
            row.get('elf_sha256')==plan['elf_sha256'] and row.get('policy')==POLICY and
            row.get('gain_selection_sha256')==digest(gain_path),'clean measured guard evidence bound to selected gain required')
        require(row.get('plan_sha256')==digest(plan_path),'guard plan identity differs')
        evaluated=score_guard_row(row,plan,prompt_scorer=prompt_scorer)
        require(row['score']==evaluated,'guard score changed');rows.append(evaluated);sources[str(path.resolve())]=digest(path)
    selected=select_guard(rows);require(selected['passed'],'no accepted guard/re-arm candidate; stop')
    gain_row=json.loads(gain_path.read_text());gain_row=gain_row.get('rescored_selection',gain_row)
    delay_provenance=delay_input(Path(gain_row['delay_file']),plan);delay=delay_provenance['delay_samples']
    return dict(kind='p4-aec-calibration-recommendation',passed=True,qualification=False,applied=False,
        plan_file=str(plan_path.resolve()),gain_file=str(gain_path.resolve()),guard_files=[str(p.resolve()) for p in guard_paths],
        source_elf_sha256=plan['elf_sha256'],source_sha256=sources,policy=POLICY,delay_provenance=delay_provenance,
        selected=dict(ref_gain_db=gain,guard_ms=selected['guard_ms'],rearm_ms=selected['rearm_ms'],afe_delay_samples=delay),
        next='follow-up Kconfig/timing rebuild, image check, headroom, fresh bundle; repeat smoke/pair; no direct firmware write')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    plan=sub.add_parser('plan');plan.add_argument('--bundle',type=Path,required=True);plan.add_argument('--out',type=Path,required=True)
    decision=sub.add_parser('delay-decision')
    for name in ('decision','source-attempt','out'):decision.add_argument('--'+name,type=Path,required=True)
    for action in ('select-gain','report-gain'):
        score=sub.add_parser(action)
        for name in ('plan','delay','root','out'):score.add_argument('--'+name,type=Path,required=True)
        if action=='report-gain':score.add_argument('--gain-db',type=int,choices=GAINS,required=True)
    recommend=sub.add_parser('recommend')
    for name in ('plan','gain-selection','out'):recommend.add_argument('--'+name,type=Path,required=True)
    recommend.add_argument('--guard-results',type=Path,nargs='+',required=True)
    for action in ('delay','gain','guard'):
        p=sub.add_parser(action)
        for name in ('plan','out','bot-dir','bot-identity','restore-record'):p.add_argument('--'+name,type=Path,required=True)
        p.add_argument('--restore-record-sha256',required=True);p.add_argument('--bot-repo',type=Path,default=Path.home()/'src/dg-esp32')
        p.add_argument('--factory-backup',dest='p4_factory_backup',type=Path,default=smoke.FACTORY_BACKUP)
        if action=='gain':
            p.add_argument('--delay',type=Path,required=True);p.add_argument('--gain-db',type=int,choices=GAINS,required=True)
            p.add_argument('--part',type=int,choices=range(4),required=True)
        if action=='guard':
            p.add_argument('--gain-selection',type=Path,required=True)
            p.add_argument('--candidate-index',type=int,choices=range(len(GUARDS)),required=True)
            p.add_argument('--previous',type=Path,nargs='*',default=[])
    args=parser.parse_args(argv)
    if args.action=='plan':write_new(args.out,make_plan(args.bundle));return 0
    if args.action=='delay-decision':write_new(args.out,delay_decision(args.decision,args.source_attempt));return 0
    plan=json.loads(args.plan.read_text());validate_plan(plan)
    if args.action=='recommend':write_new(args.out,recommendation(args.plan,args.gain_selection,args.guard_results));return 0
    if args.action in ('select-gain','report-gain'):
        result=gain_selection(plan,args.plan,args.delay,args.root,args.gain_db if args.action=='report-gain' else None)
        write_new(args.out,result);return 0 if result.get('passed',result.get('available')) else 1
    args.bundle=Path(plan['bundle']);args.board='p4xc';args.reference_only=False;args.calibration=True;args.headless=True
    args.p4_restore_bundle=None;args.p4_restore_elf=None
    print('Coordinator runner PID='+str(os.getpid()),flush=True)
    result=acquire_delay(args,plan) if args.action=='delay' else acquire_gain(args,plan) if args.action=='gain' else acquire_guard(args,plan)
    if result.get('status')=='preflight-noisy':return 2
    if result.get('status')=='preflight-error':return PREFLIGHT_ERROR_EXIT
    if args.action=='delay' and result.get('score',{}).get('available') is not True:return 1
    return 0 if result['acquisition_passed'] else 1


if __name__=='__main__':raise SystemExit(main())
