#!/usr/bin/env python3
"""Step-16 candidate F18/F19, and F20 for demo profiles only.

Only live enters the bench. Bot stop/start belongs to the coordinator, using
restart-request.json and the replacement bot's ready.json. Both boards use
identical recovery/resource bounds as new candidates; timing stays report-only.
"""
import argparse
import copy
import json
import math
import os
from pathlib import Path
import re
import time

import board_smoke as smoke
from p4_parity import checked_plan,pinned_bundle,output_path
from p4_parity_liveness_proof import boundary,capture_controls,provenance
from make_bundle import digest, verify_bundle
from wire_contract import require, parse_capture, validate_capture, measured

LIMITS=dict(wifi_rejoin_s=30, wifi_ready_s=60, bot_ready_s=60, cycles=20,
    minimum_internal=81920, largest_internal=32768, failed_allocations=0,
    monotonic_decline_bytes=4096, stale_pongs=0, stale_display_applied=0,
    idle_s=360, idle_ready_s=60)
DECISION='step-16-brief; fixed step-14 limits; both boards are new candidates'
SETTLED=dict(tolerance_bytes=256,consecutive=3,timeout_s=20,period_s=.5)
DRAIN_MIN_SECONDS=150
HEAP_DECISION='decision-2026-11-01-liveness-heap-drain'


def session_labels(autostart):
    opening=['main','resume','cold'] if autostart else ['main','pre-wifi']
    return opening+['wifi','bot-before','bot-after']+['cycle-'+str(i) for i in range(1,LIMITS['cycles']+1)]+['idle']


def f20_check(evidence,autostart):
    if autostart:
        require(evidence['held']['held'] is True and evidence['resumed'] is True and evidence['cold'] is True and
            evidence['assets'].get('passed') is True,'F20 settings/assets restoration missing')
    else:require(evidence==dict(applicable=False,reason='normal/manual image'),'normal image must not claim F20')


class C6ResetObserved(RuntimeError):
    """Incomplete report-only acquisition, never a behavioural pass/failure."""
    def __init__(self,message,*,resets=(),window=None,failure_ns=None,failure_cursor=None):
        super().__init__(message);self.resets=list(resets);self.window=window;self.failure_ns=failure_ns;self.failure_cursor=failure_cursor


def unsolicited_c6_reset(line,*,boot=False):
    # Actual ordinary boot output from the retained 13b capture. GPIO tables
    # and the host's deliberate startup reset are never unsolicited evidence.
    if re.search(r'Slave_Reset\[\d+\]',line,re.I):return False  # configuration, not an event
    if re.search(r'Reset slave using GPIO\[\d+\]',line,re.I):return not boot
    return bool(re.search(r'\bC6\b.*\b(?:reset|reboot|restarted)\b|\b(?:reset|reboot)\b.*\bC6\b|'
                          r'\b(?:H_SDIO_DRV|esp_hosted)\b.*\bslave\b.*\b(?:unexpected reset|rebooted|watchdog)\b',line,re.I))


def reset_window_evidence(resets,window,failure_ns,failure_cursor):
    """Only observations preceding this failure in its active window qualify."""
    if not isinstance(window,dict) or type(failure_ns) is not int or type(failure_cursor) is not int:return []
    start_ns=window.get('start_ns');start_cursor=window.get('start_cursor')
    if type(start_ns) is not int or type(start_cursor) is not int or start_ns>failure_ns:return []
    return [row for row in resets if isinstance(row,dict) and type(row.get('host_ns')) is int and
        type(row.get('cursor')) is int and start_ns<=row['host_ns']<=failure_ns and start_cursor<=row['cursor']<=failure_cursor and
        unsolicited_c6_reset(row.get('line',''),boot=row.get('boot',False))]


def liveness_capture(lines,capability,board,bounds,*,aec=False):
    """Declared liveness profile: product plus optional stale-probe results.

    Validate the added result with the bench-display schema as well. Frozen
    product/bench contracts retain their exact required/forbidden surfaces.
    """
    parts=smoke.slice_sessions(lines);require(len(parts)==1,'need one closed liveness wire generation')
    wire=parts[0]+capability;capture=parse_capture(wire)
    product=copy.deepcopy(capture);product.records.pop('msg=display-result',None)
    validate_capture(product,profile='product',intended_aec=aec,target=smoke.CHIPS[board])
    if 'msg=display-result' in capture.records:
        rid=capture.records['msg=display-result']['values']['id']
        require(re.fullmatch(r'step14-stale-\d+',rid),'unexpected display result in liveness profile')
        bench=copy.deepcopy(capture);bench.capability=[]
        validate_capture(bench,profile='bench-display',intended_aec=aec,target=smoke.CHIPS[board],request_id=rid)
    require(len(capture.intervals)==1,'liveness needs one frozen interval generation')
    try:timing=dict(status='measured (report-only)',metrics=measured(capture.intervals[0],bounds))
    except ValueError as exc:timing=dict(status='violation (report-only)',violation=str(exc),intervals=capture.intervals[0])
    return capture,wire,timing


def write_new(path,value):
    with Path(path).open('x') as stream:stream.write(json.dumps(value,indent=2,allow_nan=False)+'\n')


def rows(path,*,tail=False):
    text=Path(path).read_text()
    if tail and text and not text.endswith('\n'):text=text.rsplit('\n',1)[0]+'\n' if '\n' in text else ''
    require(not text or text.endswith('\n'),'truncated control capture')
    return [json.loads(line) for line in text.splitlines() if line]


def finite(value,name):
    require(type(value) in (int,float) and math.isfinite(value) and value>=0,'invalid '+name)
    return value


def heap_check(samples):
    require(samples,'missing heap samples')
    for sample in samples:
        heap=sample['heap']['internal']
        require(finite(heap['minimum_free'],'minimum heap')>=LIMITS['minimum_internal'] and
                finite(heap['largest'],'largest block')>=LIMITS['largest_internal'],'internal heap floor')
        if heap.get('sampled_largest_minimum') is not None:
            require(finite(heap['sampled_largest_minimum'],'sampled largest block')>=LIMITS['largest_internal'],'sampled internal heap floor')
        require(all(type(sample['failed_allocations'][k]) is int and sample['failed_allocations'][k]==LIMITS['failed_allocations']
                    for k in ('total','internal')),'failed allocation')
    return dict(minimum_internal=min(s['heap']['internal']['minimum_free'] for s in samples),
                largest_internal=min(s['heap']['internal']['largest'] for s in samples),failed_allocations=0)


def drain_seconds(value):
    require(finite(value,'heap drain duration')>=DRAIN_MIN_SECONDS,'heap drain requires at least 150 s')
    return value


def validate_drain(drain):
    """Missing/short/reordered evidence is unavailable on either board."""
    try:
        require(isinstance(drain,dict),'missing tail')
        require(type(drain.get('after_cycle')) is int and drain['after_cycle']==LIMITS['cycles'],'tail is not after cycle 20')
        seconds=drain_seconds(drain['requested_seconds'])
        start,end,sample_start,sample_end=[finite(drain[key],key) for key in
            ('started_seconds','wait_ended_seconds','sample_started_seconds','sample_ended_seconds')]
        require(start<=end<=sample_start<=sample_end,'reordered tail')
        require(end-start>=seconds,'short tail')
        require(drain['held']['held'] is True,'tail did not remain held')
        finite(drain['sample']['heap']['internal']['free'],'drained internal heap')
        evidence=drain['settling']
        require(type(evidence['converged']) is bool and evidence['policy']==SETTLED and
                isinstance(evidence['samples'],list) and SETTLED['consecutive']<=len(evidence['samples'])<=SETTLED['timeout_s']/SETTLED['period_s']+1 and
                evidence['samples'][-1]==drain['sample'],'missing final settled sample')
        recent=[finite(row['heap']['internal']['free'],'drain settling heap') for row in evidence['samples'][-SETTLED['consecutive']:]]
        require(not evidence['converged'] or max(recent)-min(recent)<=SETTLED['tolerance_bytes'],'final sample did not converge')
    except (KeyError,TypeError,ValueError) as exc:
        raise ValueError('heap drain evidence unavailable: '+str(exc)) from exc
    return drain


def heap_decline(samples,drain=None,*,report_only=False):
    drain=validate_drain(drain)
    values=[finite(row['heap']['internal']['free'],'settled internal heap') for row in samples]
    require(len(values)==LIMITS['cycles']+1,'baseline plus 20 settled heap samples required')
    peak=values[0];worst=0
    for current in values[1:]:
        peak=max(peak,current)
        worst=max(worst,peak-current)
    final=drain['sample']['heap']['internal']['free'];decline=values[1]-final
    checks_passed=decline<=LIMITS['monotonic_decline_bytes']
    require(report_only or checks_passed,'post-cycle-1 to drained internal heap decline exceeds 4 KiB')
    return dict(settled_internal_free=values,drained_internal_free=final,post_cycle1_to_drained_decline_bytes=decline,
        limit_bytes=LIMITS['monotonic_decline_bytes'],checks_passed=checks_passed,heap_rule=HEAP_DECISION,
        report_only_metrics=dict(report_only=True,pre_cycle1_to_cycle20_net_decline_bytes=values[0]-values[-1],
            pre_cycle1_to_drained_net_decline_bytes=values[0]-final,in_run_running_peak_decline_bytes=worst,
            drain_recovery_bytes=final-values[-1]))


def control_check(sessions,events):
    """Actual channel receipt, not the bot's accepted-pong counter."""
    require(sessions,'missing sessions')
    by_channel={};seen_generations={};previous_session=None
    for session in sessions:
        require(type(session['generation']) is int and session['generation']>0,'missing generation')
        key=session['boot'];previous=seen_generations.get(key,0)
        require(session['generation']>previous,'generation did not increase on restart')
        seen_generations[key]=session['generation']
        channel=session['channel'];require(channel not in by_channel,'channel reused after restart')
        require(session['ended_ns']>session['ready_ns'],'invalid session interval')
        if previous_session is not None:
            require(session['ready_ns']>previous_session['ended_ns'],'overlapping session generations')
        previous_session=session;by_channel[channel]=session
    observed={};latest_ns=0
    for event in events:
        now=event['monotonic_ns'];require(type(now) is int and now>=latest_ns,'control evidence reordered')
        latest_ns=now;channel=event['channel']
        if channel not in by_channel:continue  # boot/provision sessions outside measured campaign
        state=observed.setdefault(channel,dict(last_ping=0,last_pong=0,pings=set(),pongs=0,probes={},acks=set()))
        session=by_channel[channel];kind=event['kind']
        if kind in ('ping','pong'):
            seq=event.get('seq');require(type(seq) is int and 0<seq<=0xffffffff,'invalid liveness sequence')
            if kind=='ping':
                require(seq>state['last_ping'],'ping sequence not strictly increasing')
                state['last_ping']=seq;state['pings'].add(seq)
            else:
                require(event.get('direction')=='received','pong is not device receipt')
                require(seq>state['last_pong'] and seq in state['pings'],'duplicate/reordered/unmatched pong')
                require(not any(s['ready_ns']<=now and s['ready_ns']>session['ready_ns'] for s in sessions),
                        'stale pong after restart')
                state['last_pong']=seq;state['pongs']+=1
        elif kind=='stale-display-probe':
            require(event['epoch']!=session['epoch'] and event['current_epoch']==session['epoch'],'probe did not use old epoch')
            state['probes'][event['id']]=event['epoch']
        elif kind=='display-result' and str(event.get('id','')).startswith('step14-stale-'):
            require(event['id'] in state['probes'],'unmatched stale display result')
            require(event.get('accepted') is False and event.get('epoch')==state['probes'][event['id']] and
                    event.get('error')=='stale-epoch','stale display applied or not rejected by epoch fence')
            state['acks'].add(event['id'])
    require(len(observed)==len(sessions) and all(s['pongs']>0 for s in observed.values()),'missing ping/pong witness in a generation')
    probes=sum(len(s['probes']) for s in observed.values())
    require(probes>0 and all(set(s['probes'])==s['acks'] for s in observed.values()),'missing stale display drop witness')
    return dict(stale_pongs=0,stale_display_applied=0,stale_display_drops=probes,
                generations=len(sessions),pongs=sum(s['pongs'] for s in observed.values()))


def sample_check(sample,board):
    heap_check([sample])
    if board=='p4xc':
        heap_check([sample])
        require(sample['power'].get('available')==0 and sample['power'].get('valid') is False,
                'P4 power must be unavailable; no low-battery input')
    return sample


def settled_heap(board,io):
    """Bounded convergence of multiple post-hold samples, not a fixed delay."""
    deadline=io.clock()+SETTLED['timeout_s'];samples=[]
    while io.clock()<deadline:
        sample=sample_check(io.snapshot(board),io.args.board);samples.append(sample)
        recent=samples[-SETTLED['consecutive']:]
        free=[finite(row['heap']['internal']['free'],'settled internal heap') for row in recent]
        if len(recent)==SETTLED['consecutive'] and max(free)-min(free)<=SETTLED['tolerance_bytes']:
            return sample,dict(samples=samples,converged=True,policy=SETTLED)
        io.pause(board,SETTLED['period_s'])
    raise ValueError('settled internal heap did not converge within 20 s')


def convergence(selected,evidence):
    require(isinstance(evidence,dict) and evidence.get('policy')==SETTLED and evidence.get('converged') is True and
        not evidence.get('report_only'),'strict convergence witness missing/nonconverged')
    samples=evidence.get('samples')
    require(isinstance(samples,list) and SETTLED['consecutive']<=len(samples)<=SETTLED['timeout_s']/SETTLED['period_s']+1 and
        samples[-1]==selected,'selected settled sample/witness differs')
    recent=[finite(row['heap']['internal']['free'],'settled convergence heap') for row in samples[-SETTLED['consecutive']:]]
    require(max(recent)-min(recent)<=SETTLED['tolerance_bytes'],'settled samples did not converge')
    return evidence


def drain_heap(board,io,seconds):
    """Stay in the existing held state, then sample before any new session."""
    drain_seconds(seconds)
    at=board.cursor
    tail=dict(after_cycle=LIMITS['cycles'],requested_seconds=seconds,started_seconds=io.clock())
    write_new(io.out/'heap-drain-start.json',tail)
    io.pause(board,seconds);tail['wait_ended_seconds']=io.clock()
    require(tail['wait_ended_seconds']-tail['started_seconds']>=seconds,'heap drain evidence unavailable: short tail')
    tail['held']=io.held(board);tail['sample_started_seconds']=io.clock()
    tail['sample'],tail['settling']=settled_heap(board,io)
    tail['sample_ended_seconds']=io.clock()
    require(not any(re.search(r'\bSTATE (?:Starting|Ready)\b',line) for _,_,line in board.lines_since(at)),
            'heap drain evidence unavailable: session started during Idle tail')
    write_new(io.out/'heap-drain.json',tail)
    return validate_drain(tail)


def resource_reports(samples,settled,board,drain):
    validate_drain(drain)
    # Both images are new candidates. The old S3 reference-only exception
    # cannot turn their resource failures into passing evidence.
    return [dict(heap_check(samples),checks_passed=True,report_only=False),
        dict(heap_decline(settled,drain),report_only=False)]


def score(directory,*,acquisition=None):
    directory=Path(directory);a=acquisition if acquisition is not None else json.loads((directory/'acquisition.json').read_text())
    from p4_parity_serial import candidate_serial
    candidate_serial(directory)
    if a.get('inconclusive'):
        text=(directory/'serial.log').read_text(errors='replace')
        resets=reset_window_evidence(a.get('c6_resets',[]),a.get('c6_window'),a.get('failure_ns'),a.get('failure_cursor'))
        resets=[row for row in resets if row['line'] in text.splitlines()]
        from p4_parity_serial import causal_reset_journal
        resets=causal_reset_journal(directory,a,resets)
        require(a.get('acquisition_passed') is False and a.get('inconclusive') is True and resets and
                a.get('restore',{}).get('clean') is True and not a.get('cleanup_error') and not a.get('cleanup_errors'),
                'inconclusive reset report requires actual unsolicited-reset evidence and clean restoration')
        return dict(checks_passed=False,passed=False,qualification=False,inconclusive=True,
            decision=DECISION,disposition='unsolicited C6 reset (report-only)',
            error=a.get('error'),c6_reset_lines=[row['line'] for row in resets])
    require(a.get('acquisition_passed') is True and not a.get('error') and a.get('restore',{}).get('clean') is True,
            'unclean acquisition/restore')
    require(a['limits']==LIMITS,'predeclared liveness bounds changed')
    require(a['board'] in ('p4xc','echoear'),'unknown board')
    manifest=pinned_bundle(a['bundle'],a['board'],a['variant'])
    require(a['elf_sha256']==manifest['elf_sha256'] and not a['reference_only'] and
        a['aec_enabled']==manifest['aec_enabled'],'candidate identity differs')
    require(not a.get('cleanup_error'),'cleanup failed')
    bot_proof=provenance(directory,a)
    evidence=a.get('settled_evidence')
    require(isinstance(evidence,list) and len(evidence)==len(a['settled_samples'])==21,'all 21 convergence records required')
    for selected,witness in zip(a['settled_samples'],evidence):convergence(selected,witness)
    convergence(a['heap_drain']['sample'],a['heap_drain']['settling'])
    require(len(a['cycles'])==LIMITS['cycles'] and [c['index'] for c in a['cycles']]==list(range(1,LIMITS['cycles']+1)) and
            all(c['connected'] is True and c['disconnected'] is True for c in a['cycles']),'20 cycles require zero failures')
    drain=validate_drain(a.get('heap_drain'))
    try:
        require(a.get('drain_seconds')==drain['requested_seconds'] and
                finite(a['cycles'][-1].get('settled_seconds'),'cycle 20 settled time')<=drain['started_seconds'] and
                drain['sample_ended_seconds']<=finite(a.get('idle_started_seconds'),'idle start time'),
                'changed duration or reordered cycle/drain/idle')
    except (KeyError,TypeError,ValueError) as exc:
        raise ValueError('heap drain evidence unavailable: '+str(exc)) from exc
    require(drain['settling']['converged'] is True,'heap drain evidence unavailable: final sample did not converge')
    require([s['label'] for s in a['sessions']]==session_labels(manifest['autostart']),'missing recovery/cycle session evidence')
    for key in ('wifi_rejoin_s','wifi_ready_s','bot_ready_s','idle_ready_s'):
        require(finite(a['recovery'][key],key)<=LIMITS[key],key+' exceeds fixed bound')
    require(finite(a['idle_elapsed_s'],'idle duration')>=LIMITS['idle_s'],'six-minute idle missing')
    f20_check(a['f20'],manifest['autostart'])
    require(all(s.get('wire_verified') is True for s in a['sessions']),
            'missing product wire evidence')
    require(a['board']!='p4xc' or a['reference_only'] is False,'P4 cannot use reference-only scoring')
    timings=[]
    if not a['reference_only']:
        require(digest(a['bounds_path'])==a['bounds_sha256'],'fixed bounds changed')
        bounds=smoke.bounds_for(a['bounds_path'],a['variant'])
        for session in a['sessions']:
            path=directory/session['wire_file'];require(digest(path)==session['wire_sha256'],'wire capture changed')
            lines=path.read_text().splitlines()
            _,_,timing=liveness_capture(lines,[line for line in lines if 'DEMO_CAPABILITY ' in line],a['board'],bounds,aec=a['aec_enabled'])
            timings.append(dict(label=session['label'],**timing))
    require(digest(directory/'control.jsonl')==a['control_sha256'],'control capture changed')
    from p4_parity_serial import liveness_sessions
    liveness_sessions(directory,a)
    control=control_check(a['sessions'],rows(directory/'control.jsonl'))
    samples=[sample for s in a['sessions'] for sample in s['samples']]+a['settled_samples']+drain['settling']['samples']
    samples += [sample for witness in evidence for sample in witness['samples']]
    for sample in samples:sample_check(sample,a['board'])
    memory,decline=resource_reports(samples,a['settled_samples'],a['board'],drain)
    evidence=a.get('settled_evidence',[])
    decline['settled_points']=[dict(index=i,free=row['heap']['internal']['free'],
        converged=evidence[i].get('converged') if i<len(evidence) else None,
        report_only=False) for i,row in enumerate(a['settled_samples'])]
    decline['drained_point']=dict(free=drain['sample']['heap']['internal']['free'],converged=drain['settling']['converged'],
        report_only=False)
    text=(directory/'serial.log').read_text()
    return dict(checks_passed=True,passed=False,qualification=False,limits=LIMITS,board=a['board'],variant=a['variant'],
        elf_sha256=a['elf_sha256'],aec_enabled=a['aec_enabled'],
        control=control,resources=memory,heap_decline=decline,heap_drain=drain,recovery=a['recovery'],f20=a['f20'],
        wire_timing=timings,decision=DECISION,unsolicited_c6_reset='report-only',
        bot_provenance=bot_proof,
        c6_reset_lines=[row['line'] for row in a.get('c6_resets',[]) if row.get('line') in text.splitlines()])


class LivenessIO(smoke.LiveIO):
    # Bot restart is an explicit part of this campaign, unlike smoke turns.
    # Every replacement still needs a new verified process/source/profile.
    def poll(self,board):
        batch=board.lines_since(self.serial_seen)
        def lost(line):
            changed=re.search(r'\bSTATE (?:Disconnected|Starting|Idle|WifiConnecting) gen=(\d+)',line)
            return self.active and not self.stopping and self.session_gen is not None and (
                re.search(r'SESSION FAIL|Peer disconnected|RTCP_BYE',line) or changed and int(changed[1])!=self.session_gen)
        def booting(cursor):return any(w['start_cursor']<=cursor<=w['end_cursor'] for w in getattr(self,'boot_windows',[]))
        resets=[dict(cursor=cursor,host_ns=int(stamp*1e9),line=line,boot=booting(cursor))
                for cursor,stamp,line in batch if unsolicited_c6_reset(line,boot=booting(cursor))]
        # A reset logged after an already-observed failure cannot explain it,
        # even when both records arrived in one polling batch.
        failures=[(cursor,int(stamp*1e9)) for cursor,stamp,line in batch
            if lost(line) or re.search(r'Guru Meditation|panic.ed|Stack canary',line,re.I)]
        failure_cursor=failures[0][0] if failures else max([self.serial_seen]+[cursor for cursor,_,_ in batch])
        failure_ns=min(time.monotonic_ns(),failures[0][1]) if failures else time.monotonic_ns()
        window=getattr(self,'current_window',None)
        resets=reset_window_evidence(resets,window,failure_ns,failure_cursor)
        if resets:
            self.c6_resets=getattr(self,'c6_resets',[])+resets
            raise C6ResetObserved('unsolicited C6 reset observed; acquisition incomplete (report-only)',
                resets=resets,window=dict(window),failure_ns=failure_ns,failure_cursor=failure_cursor)
        for cursor,_,line in batch:
            self.serial_seen=cursor
            require(not re.search(r'Guru Meditation|panic.ed|Stack canary',line,re.I),'fatal diagnostic')
            require(not lost(line),'unintentional session loss: '+line)
        require(smoke.process_token(self.pid)==self.identity['bot_instance'],'unplanned bot restart')

    def boot(self,board):
        start=board.cursor
        try:return super().boot(board)
        finally:
            ready=next((cursor for cursor,_,line in board.lines_since(start) if 'READY transport-console' in line),board.cursor)
            self.boot_windows=getattr(self,'boot_windows',[])+[dict(start_cursor=start,end_cursor=ready)]

    def observe_ready(self,board,cursor,stamp,line):
        self.session_gen=int(re.search(r'gen=(\d+)',line)[1]);self.ready_ns=int(stamp*1e9)
        self.serial_seen=cursor;self.stopping=False;self.active=True;self.interval_requested=False;self.pc_id=None
        self.current_window=dict(start_cursor=cursor,start_ns=self.ready_ns,generation=self.session_gen)
        self.c6_resets=[]
        self.since=self.clock()
        def ready():
            return next((row for row in rows(self.args.bot_dir/'dc-events.jsonl',tail=True)
                if row['kind']=='display-ready' and self.ready_ns-5000000000<=row['monotonic_ns']),None)
        self.display_ready=self.wait(board,ready,10,label='observed display-ready')
        require(type(self.display_ready.get('epoch')) is int and self.display_ready['epoch']>0,'missing display epoch')
        from step9a_support import read
        require(read(board,'status','DISPLAY')['epoch']==self.display_ready['epoch'],
            'device/control display epoch differs')
        self.ready_cursor=cursor

    def begin(self,board,action):
        self.serial_at=(self.out/'serial.log').stat().st_size
        self.bot_at=(self.args.bot_dir/'bot.log').stat().st_size
        self.active=True;self.stopping=False;self.session_gen=None
        demo=self.manifest['autostart']
        at=self.boot(board) if action=='cold' else board.cursor
        self.session_start_cursor=at
        if not demo:board.command('connect',90)
        elif action!='cold':board.command('demo run',90)
        cursor,stamp,line=board.wait_for_entry(r'\bSTATE Ready gen=\d+',120,since=at)
        self.observe_ready(board,cursor,stamp,line)
        if demo:board.wait_for(r'demo-boot: RESTORED assets=16 no_http=1',10,since=at)
        else:board.command('audio 1')

    def finish(self,board,label,*,hold=False):
        return super().finish(board,label,hold=hold and self.manifest['autostart'])

    def held(self,board):
        if self.manifest['autostart']:return super().held(board)
        at=board.cursor;self.pause(board,2)
        require(not any(re.search(r'\bSTATE (?:Starting|Ready)\b',line) for _,_,line in board.lines_since(at)),
            'manual disconnected image reconnected')
        return dict(held=True,scope='manual disconnected idle; F20 not applicable')

    def frozen_sample(self,board):
        sample=sample_check(self.snapshot(board),self.args.board)
        self.pc_id=sample['pc_id'];_,events=self.events();smoke.check_bot_session(events)
        profiles=[event for event in events if event.get('kind')=='stt-profile']
        require(len(profiles)==1,'missing bot profile identity witness')
        observed={k:v for k,v in profiles[0].items() if k not in ('kind','monotonic_ns','pc_id')}
        require(observed==self.identity['profile_manifest'],'bot profile manifest differs')
        return sample


def acquire(args,io,manifest):
    """Run in an armed inherited RestoreHold; all timing uses host monotonic."""
    drain_seconds(args.drain_seconds)
    sessions=[];cycles=[];settled=[];settled_evidence=[];boot=0;recovery={};control_sources=[]
    current_bot=args.bot_dir;control_sources.append(current_bot/'dc-events.jsonl')
    control_starts=[boundary(control_sources[0])];bot_identities=[dict(io.identity)]
    from p4_parity_serial import serial_trace,journal,window,select,text
    with io.open() as board,serial_trace(board,args.out/'serial-entries.jsonl'):
        io.boot(board)
        provision=io.provision(board) if manifest['autostart'] else io.prepare_manual(board)
        board.command('touch-enable 0')
        f20={}
        active=None
        def begin(label,action):
            nonlocal active,boot
            if action=='cold':boot+=1
            io.begin(board,action)
            active=dict(label=label,boot=boot,generation=io.session_gen,ready_ns=io.ready_ns,
                epoch=io.display_ready['epoch'],samples=[],bot_instance=io.identity['bot_instance'])
        def freeze():
            io.pause(board,max(0,13-(io.clock()-io.since)))
            io.freeze(board);active['samples'].append(io.frozen_sample(board))
            # Separate the old /start capability from an auto-retry's /start,
            # even if the new bot log line arrives before our Ready wait.
            return (current_bot/'bot.log').stat().st_size
        def finish(label,*,already_stopped=False):
            nonlocal active
            if not already_stopped:
                active['stop_cursor']=board.cursor
                lines,capability=io.finish(board,label,hold=True)
            else:
                io.active=False
                with (args.out/'serial.log').open('rb') as stream:
                    stream.seek(io.serial_at);lines=stream.read().decode(errors='replace').splitlines()
                with (current_bot/'bot.log').open('rb') as stream:
                    stream.seek(io.bot_at);bot_lines=stream.read(bot_end-io.bot_at).decode(errors='replace').splitlines()
                capability=[line for line in bot_lines if 'DEMO_CAPABILITY ' in line]
                closed=next((i for i,line in enumerate(lines) if re.search(r'\bPHASE closed\b',line)),None)
                require(closed is not None,'induced outage did not close the previous generation')
                lines=lines[:closed+1]
                (args.out/(label+'-serial.log')).write_text('\n'.join(lines)+'\n')
                (args.out/(label+'-bot.log')).write_text('\n'.join(bot_lines)+'\n')
            entries=journal(args.out)
            closed=[row for row in entries if row['cursor']>=io.ready_cursor and re.search(r'\bPHASE closed\b',row['line'])]
            require(closed,'missing session close timestamp')
            bounds=window(entries,io.session_start_cursor,closed[0]['cursor']);segment=select(entries,bounds)
            lines=[row['line'] for row in segment]
            (args.out/(label+'-serial.log')).write_text(text(segment))
            active.update(ended_ns=closed[0]['monotonic_ns'],serial_window=bounds,ready_cursor=io.ready_cursor,
                close_cursor=closed[0]['cursor'],boot_cursor=max(row['cursor'] for row in entries
                    if row['cursor']<io.ready_cursor and row['line'].strip()=='READY transport-console'))
            events=rows(current_bot/'dc-events.jsonl',tail=True)
            matches=[row for row in events if row['kind']=='display-ready' and row.get('epoch')==active['epoch'] and
                     active['ready_ns']-5000000000<=row['monotonic_ns']<=active['ended_ns']]
            require(len(matches)==1,'missing/ambiguous channel display-ready')
            active['channel']=matches[0]['channel']
            active['wire_verified']=False
            if not args.reference_only:
                capture,wire,timing=liveness_capture(lines,capability,args.board,
                    smoke.bounds_for(args.bounds,manifest['profile']),aec=manifest['aec_enabled'])
                path=args.out/(label+'-wire.log');path.write_text('\n'.join(wire)+'\n')
                active.update(wire_verified=True,wire_file=path.name,wire_sha256=digest(path),timing=timing)
            sessions.append(active);active=None
        try:
            begin('main','cold' if manifest['autostart'] else 'manual');freeze();finish('main')
            if manifest['autostart']:
                f20['held']=io.held(board)
                begin('resume','resume');freeze();finish('resume');f20['resumed']=True;io.held(board)
                begin('cold','cold');f20['assets']=io.restored_assets(board,provision);f20['cold']=True
            else:
                f20=dict(applicable=False,reason='normal/manual image');begin('pre-wifi','manual')
            bot_end=freeze()
            # Freeze while Ready. The induced outage never extends that window.
            io.stopping=True;at=board.cursor;started=io.clock();board.command('wifi-drop')
            active['stop_cursor']=at
            _,wifi_at,_=board.wait_for_entry(r'\bWIFI READY\b',LIMITS['wifi_rejoin_s'],since=at)
            recovery['wifi_rejoin_s']=wifi_at-started
            # Save the old closed generation before observing the replacement.
            board.wait_for(r'\bPHASE closed\b',45,since=at);finish('cold' if manifest['autostart'] else 'pre-wifi',already_stopped=True)
            ready_cursor,stamp,line=board.wait_for_entry(r'\bSTATE Ready gen=\d+',max(.01,LIMITS['wifi_ready_s']-(io.clock()-started)),since=at)
            recovery['wifi_ready_s']=stamp-started
            def adopt(label,since,bot_start):
                nonlocal active
                io.serial_at=0  # replaced below by a per-session line slice on disk
                io.bot_at=bot_start;io.observe_ready(board,since,stamp,line)
                # Extract from WIRE_CONTRACT start of the new generation. The
                # new start may precede Ready or our wait/old-session save.
                data=(args.out/'serial.log').read_bytes()
                marker=(rb'(?m)^.*STATE Starting gen='+str(io.session_gen).encode()+rb'\b' if args.reference_only else
                        rb'(?m)^.*WIRE_CONTRACT start=')
                starts=list(re.finditer(marker,data))
                require(starts,'missing replacement start capture')
                io.serial_at=starts[-1].start()
                serial_rows=journal(args.out)
                io.session_start_cursor=next(row['cursor']-1 for row in reversed(serial_rows)
                    if 'WIRE_CONTRACT start=' in row['line'])
                active=dict(label=label,boot=boot,generation=io.session_gen,ready_ns=io.ready_ns,
                    epoch=io.display_ready['epoch'],samples=[],bot_instance=io.identity['bot_instance'])
            adopt('wifi',ready_cursor,bot_end);freeze();finish('wifi')
            begin('bot-before','resume');bot_end=freeze()
            io.stopping=True;at=board.cursor;request_ns=time.monotonic_ns()
            active['stop_cursor']=at
            write_new(args.out/'restart-request.json',dict(bot_instance=io.identity['bot_instance'],
                request_monotonic_ns=request_ns,replacement_dir=str(args.restart_dir),
                instruction='Coordinator stops this bot and launches the same observed demo bot in replacement_dir'))
            print('Coordinator bot restart requested: '+str(args.out/'restart-request.json'),flush=True)
            # No process control here. Dead time before replacement startup is
            # coordinator time; the device's scored deadline starts at startup.
            deadline=request_ns/1e9+180
            def old_alive():
                try:return smoke.process_token(io.pid)==io.identity['bot_instance']
                except (OSError,ValueError):return False
            while old_alive() and io.clock()<deadline:time.sleep(.1)
            require(not old_alive(),'coordinator did not stop the requested bot within 180 s')
            board.wait_for(r'\bPHASE closed\b',60,since=at);finish('bot-before',already_stopped=True)
            while not (args.restart_dir/'ready.json').exists() and io.clock()<deadline:time.sleep(.1)
            require((args.restart_dir/'ready.json').exists(),'coordinator replacement bot not ready within 180 s')
            ready=json.loads((args.restart_dir/'ready.json').read_text());up=ready['up_monotonic_ns']
            require(type(up) is int and up>request_ns,'replacement startup marker is stale')
            identity=json.loads((args.restart_dir/'identity.json').read_text())
            write_new(args.out/'replacement-ready.json',ready)
            write_new(args.out/'replacement-identity.json',identity)
            require(identity==ready['identity'] and identity['bot_instance']!=io.identity['bot_instance'],'replacement identity mismatch')
            require(ready['observer_sha256']==digest(Path(__file__).with_name('p4_liveness_bot.py')),
                    'replacement bot observer changed')
            require(all(identity[k]==io.identity[k] for k in ('bot_source_sha','policy_source_sha','profile_manifest')),
                    'bot source/profile changed on restart')
            pid=smoke.live_bot_identity(args.restart_dir,args.bot_repo,identity)
            ready_cursor,stamp,line=board.wait_for_entry(r'\bSTATE Ready gen=\d+',max(.01,LIMITS['bot_ready_s']-(time.monotonic_ns()-up)/1e9),since=at)
            require(stamp*1e9>=up,'Ready predates replacement bot startup')
            recovery['bot_ready_s']=(stamp*1e9-up)/1e9
            current_bot=args.restart_dir;control_sources.append(current_bot/'dc-events.jsonl')
            control_starts.append(0);bot_identities.append(identity)
            io.args.bot_dir=current_bot;io.identity=identity;io.pid=pid
            adopt('bot-after',ready_cursor,0);freeze();finish('bot-after')
            def settled_sample():
                sample,evidence=settled_heap(board,io);settled.append(sample);settled_evidence.append(evidence)
            settled_sample()
            for index in range(1,LIMITS['cycles']+1):
                begin('cycle-'+str(index),'resume');freeze();finish('cycle-'+str(index))
                io.held(board);settled_sample()
                cycles.append(dict(index=index,connected=True,disconnected=True,settled_seconds=io.clock()))
            drain=drain_heap(board,io,args.drain_seconds)
            idle_start=io.clock();io.pause(board,LIMITS['idle_s']);elapsed=io.clock()-idle_start
            request=io.clock();begin('idle','resume');recovery['idle_ready_s']=active['ready_ns']/1e9-request
            freeze();finish('idle')
            bindings=capture_controls(control_sources,control_starts,bot_identities,args.out)
            control=args.out/'control.jsonl'
            return dict(acquisition_passed=True,sessions=sessions,cycles=cycles,settled_samples=settled,settled_evidence=settled_evidence,
                recovery=recovery,f20=f20,idle_elapsed_s=elapsed,idle_started_seconds=idle_start,
                drain_seconds=args.drain_seconds,heap_drain=drain,control_sha256=digest(control),
                boot_windows=getattr(io,'boot_windows',[]),c6_resets=getattr(io,'c6_resets',[]),
                bot_identities=bot_identities,control_sources=bindings)
        finally:
            # Keep the inherited hold armed through all cleanup errors.
            import sys
            root_error=sys.exc_info()[1];io.stopping=True
            try:board.command('demo hold' if manifest['autostart'] else 'disconnect',45)
            except Exception as exc:
                io.cleanup_error=type(exc).__name__+': '+str(exc)
                if root_error is None:raise
            finally:io.active=False


def preflight(args):
    drain_seconds(args.drain_seconds)
    plan=checked_plan(args.plan)
    args.out=output_path(plan,args.out)
    args.bot_dir=args.bot_dir.resolve();args.restart_dir=args.restart_dir.resolve()
    manifest=pinned_bundle(args.bundle,args.board)
    require(plan['images'][args.board+'/'+manifest['profile']]['elf_sha256']==manifest['elf_sha256'],'image not in parity plan')
    require(str(args.restore_record.resolve())==plan['restore_record'] and args.restore_record_sha256==plan['restore_record_sha256'],
        'restore selection differs from parity plan')
    smoke.bounds_for(args.bounds,manifest['profile'])
    identity=json.loads(args.bot_identity.read_text())
    from wire_contract import identity_schema
    identity_schema(identity)
    policy=plan['fault_bot_policy']
    require(all(identity[k]==policy[k] for k in ('bot_source_sha','policy_source_sha','profile_manifest')) and
        identity['bot_instance']!=policy['distinct_from_product_instance'],'fault-bot identity policy differs')
    require((args.bot_dir/'dc-events.jsonl').is_file(),'launch the observed bot before acquisition')
    ready=json.loads((args.bot_dir/'ready.json').read_text())
    require(ready['identity']==identity and json.loads((args.bot_dir/'identity.json').read_text())==identity and
            type(ready.get('up_monotonic_ns')) is int and ready['up_monotonic_ns']>0 and
            ready['observer_sha256']==digest(Path(__file__).with_name('p4_liveness_bot.py')),
            'initial observed bot readiness/identity mismatch')
    require(not args.restart_dir.exists(),'replacement bot directory must not exist before the run')
    from p4_parity_preflight import host_checks
    host_checks(args,plan,manifest,identity)
    return plan,manifest,identity,ready


def live(args,prepared):
    from p4_product import candidate_storage
    from p4_step14 import restoring
    plan,manifest,identity,ready=prepared
    args.out.mkdir(parents=True,exist_ok=False)
    write_new(args.out/'identity.json',identity);write_new(args.out/'image.json',manifest)
    write_new(args.out/'initial-ready.json',ready)
    write_new(args.out/'limits.json',LIMITS)
    result=dict(acquisition_passed=False,passed=False,qualification=False,board=args.board,
        reference_only=False,elf_sha256=manifest['elf_sha256'],limits=LIMITS,
        bundle=str(args.bundle.resolve()),variant=manifest['profile'],aec_enabled=manifest['aec_enabled'],
        bounds_path=str(args.bounds.resolve()),bounds_sha256=digest(args.bounds),
        helper_sha256=digest(Path(__file__)),observer_sha256=ready['observer_sha256'])
    result.update(plan_file=str(args.plan.resolve()),plan_sha256=digest(args.plan),source_sha256=plan['source_sha256'],
        initial_bot_dir=str(args.bot_dir.resolve()),replacement_dir=str(args.restart_dir.resolve()))
    guard=io=None
    try:
        with restoring(args.restore_record,args.restore_record_sha256),candidate_storage(args.board,args.bundle,args.out):
            io=LivenessIO(args,args.out,manifest,identity);io.preflight_tools();io.preflight_restore();guard=io.hold()
            with guard:
                guard.touch();io.flash(args.bundle,manifest,'candidate')
                result.update(acquire(args,io,manifest))
                # Gate while still inside R7; all scoring failures restore too.
                assessment=score(args.out,acquisition=dict(result,restore=dict(clean=True)));guard.success=True
            result['restore']=guard.record
            require(guard.record.get('clean') is True,'unclean restore')
            write_new(args.out/'score.json',assessment)
    except BaseException as exc:
        result.update(acquisition_passed=False,error=type(exc).__name__+': '+str(exc))
        resets=reset_window_evidence(exc.resets,exc.window,exc.failure_ns,exc.failure_cursor) if isinstance(exc,C6ResetObserved) else []
        if resets:
            result.update(inconclusive=True,disposition='unsolicited C6 reset (report-only)',
                c6_resets=resets,c6_window=exc.window,failure_ns=exc.failure_ns,failure_cursor=exc.failure_cursor,
                c6_reset_lines=[row['line'] for row in resets],report='acquisition incomplete; reset observed before failure')
        if guard is not None:result['restore']=guard.record
    finally:
        if io is not None and getattr(io,'cleanup_error',None):result['cleanup_error']=io.cleanup_error
        smoke.write(args.out/'acquisition.json',result)
        if guard is not None:guard.release_signals()
    from p4_parity import validated_unavailable
    if validated_unavailable('liveness',args.board,manifest['profile'],args.out,result):return 2
    return 0 if result['acquisition_passed'] and not result.get('error') else 1


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    check=sub.add_parser('score');check.add_argument('directory',type=Path);check.add_argument('--out',type=Path,required=True)
    run=sub.add_parser('live');run.add_argument('--board',choices=('p4xc','echoear'),required=True)
    for key in ('bundle','out','bot-dir','bot-identity','restart-dir'):run.add_argument('--'+key,type=Path,required=True)
    run.add_argument('--bot-repo',type=Path,default=Path.home()/'src/dg-esp32')
    run.add_argument('--plan',type=Path,required=True)
    run.add_argument('--restore-record',type=Path,required=True);run.add_argument('--restore-record-sha256',required=True)
    run.add_argument('--factory-backup',dest='p4_factory_backup',type=Path,default=smoke.FACTORY_BACKUP)
    run.add_argument('--bounds',type=Path,default=smoke.BOUNDS_FILE)
    run.add_argument('--drain-seconds',type=float,default=DRAIN_MIN_SECONDS)
    run.set_defaults(calibration=False,headless=False,reference_only=False,p4_restore_bundle=None,p4_restore_elf=None)
    args=parser.parse_args(argv)
    if args.action=='live':
        print('Coordinator runner PID='+str(os.getpid()),flush=True)
        from p4_parity_campaign import attempt
        prepared=preflight(args);variant=prepared[1]['profile']
        with attempt(args.plan,'liveness',args.board,variant,args.out):return live(args,prepared)
    result=score(args.directory);write_new(args.out,result);return 2 if result.get('inconclusive') else 0


if __name__=='__main__':raise SystemExit(main())
