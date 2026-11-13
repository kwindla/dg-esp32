#!/usr/bin/env python3
"""Heap diagnosis round 2: twenty cycles, deferred frees and C++ new callers.

Additive entrypoint over frozen round-1 parsing/restore helpers. Never qualifies
an image or changes shipping bounds. Only live under bench.py touches a board.
"""
import argparse
import json
import math
import os
from pathlib import Path
import re

import p4_heap_attribution as base
from make_bundle import digest,verify_bundle
from wire_contract import require

MAX_CYCLES=20
MAX_SETTLE=600
PERIOD=5
NEW_CAPACITY=8192


def settings(args):
    require(type(args.cycles) is int and 1<=args.cycles<=MAX_CYCLES,'diagnosis requires 1..20 cycles')
    require(math.isfinite(args.active_seconds) and 1<=args.active_seconds<=30,'invalid active seconds')
    require(math.isfinite(args.settle_seconds) and 0<=args.settle_seconds<=MAX_SETTLE,'invalid settle seconds (0..600)')


def validate_tcp(row,window,values=None):
    require(isinstance(row,dict) and row.get('schema')==1 and row.get('kind')=='tcp','missing TCP state')
    base.integer(row.get('id'),'TCP window',0,MAX_CYCLES)
    require(row.get('id')==window,'foreign TCP state window')
    for key in ('active','time_wait','bound'):base.integer(row.get(key),key,0,1024)
    base.integer(row.get('tcp_msl_ms'),'TCP_MSL',1)
    base.integer(row.get('time_wait_ms'),'TIME_WAIT',1)
    base.integer(row.get('memp_num_tcp_pcb'),'MEMP_NUM_TCP_PCB',1,1024)
    require(row.get('time_wait_ms')==2*row['tcp_msl_ms'],'invalid TIME_WAIT lifetime')
    if values is not None:
        require(values==dict(TCP_MSL_ms=row['tcp_msl_ms'],TIME_WAIT_ms=row['time_wait_ms'],MEMP_NUM_TCP_PCB=row['memp_num_tcp_pcb']),'TCP state differs from build constants')
    return row


def tcp_state(board,window):
    return validate_tcp(base.single(base.records(board,'tcp','tcp'),'tcp'),window)


def validate_trace(trace):
    base.validate_trace(trace['start'],trace['stop'],trace['blocks'])
    for row in (trace['start'],trace['stop']):
        base.integer(row.get('new_caller_capacity'),'C++ caller capacity')
        require(row.get('new_caller_capacity')==NEW_CAPACITY,'round-2 C++ caller tracking required')
        require(type(row.get('new_caller_overflow')) is bool,'invalid C++ caller overflow flag')
    for row in trace['blocks']:
        base.integer(row.get('new_caller'),'C++ allocation caller')
        require(type(row.get('external')) is bool and not (row['external'] and row['internal']),'invalid allocation memory class')
    return 'table-overflow' if any(row['new_caller_overflow'] for row in (trace['start'],trace['stop'])) else 'complete'


def caller_status(caller):
    if not caller:return 'unavailable-or-not-wrapped-new'
    if any(re.search(r'libstdc\+\+|libsupc\+\+|\b__cxa_|\b__cxxabiv1::|\b(?:new_op\w*|cow-string[^/\s]*)\.(?:cc|o)\b',frame) for frame in caller):
        return 'runtime-internal'
    return 'recorded-return-PC'


def plateau(values):
    """Descriptive last-five evidence only: no new tolerance or pass rule."""
    tail=values[-5:];deltas=[a-b for a,b in zip(tail,tail[1:])]
    status='insufficient-cycles' if len(tail)<5 else 'flat' if all(v==0 for v in deltas) else \
           'continued-decline' if all(v>0 for v in deltas) else 'not-established'
    return dict(status=status,last_five=tail,range_bytes=max(tail)-min(tail),declines_bytes=deltas,
                basis='Exact last-five post-cycle values; no qualification or changed pass tolerance')


def validate_settle(raw):
    settle=raw['final_settle'];seconds=settle.get('requested_seconds')
    require(type(seconds) in (float,int) and math.isfinite(seconds) and 0<=seconds<=MAX_SETTLE,'invalid recorded settle duration')
    require(seconds==raw.get('settle_seconds'),'requested settle duration changed')
    points=settle.get('points');require(isinstance(points,list) and 1<=len(points)<=MAX_SETTLE//PERIOD+2,'missing/unbounded settle samples')
    require(type(settle.get('started_seconds')) in (int,float) and math.isfinite(settle['started_seconds']) and settle['started_seconds']>=0,'invalid settle start')
    previous=-1
    for point in points:
        elapsed=point.get('elapsed_seconds');require(type(elapsed) in (int,float) and math.isfinite(elapsed) and elapsed>=previous and elapsed>=0,'reordered settle samples')
        previous=elapsed
        base.integer(point['sample']['heap']['internal']['free'],'settle internal free')
    require(previous>=seconds,'settle window ended early')
    require(points[0]['sample']==raw['cycles'][-1]['settled'],'settle baseline differs from final cycle')
    return settle


def analyze(directory,elf=None,symbolizer=base.symbolize):
    directory=Path(directory);raw=json.loads((directory/'acquisition.json').read_text());image=json.loads((directory/'image.json').read_text())
    require(raw.get('acquisition_complete') is True and raw.get('restore',{}).get('clean') is True and not raw.get('cleanup_error'),'incomplete diagnosis/restore')
    cycles=raw['cycles'];require(1<=len(cycles)<=MAX_CYCLES and len(cycles)==raw.get('requested_cycles'),'missing diagnostic cycles')
    require([row.get('index') for row in cycles]==list(range(1,len(cycles)+1)),'reordered/replayed cycles')
    settle=validate_settle(raw);samples=[raw['baseline']]+[row['settled'] for row in cycles]
    internal=[base.integer(s['heap']['internal']['free'],'internal free') for s in samples]
    psram=[base.integer(s['heap']['psram']['free'],'PSRAM free') for s in samples]
    result=dict(kind='heap-attribution-round-2',board=image['board'],qualification=False,passed=False,
        source_sha256=digest(directory/'acquisition.json'),internal_free=internal,psram_free=psram,
        per_cycle_internal_decline=[a-b for a,b in zip(internal,internal[1:])],
        per_cycle_psram_decline=[a-b for a,b in zip(psram,psram[1:])],
        internal_plateau=plateau(internal[1:]),psram_plateau=plateau(psram[1:]),
        final_settle=settle,cycles=[],scope='Diagnosis only; no per-window byte sums are a net leak')
    result['settle_internal_recovered_bytes']=settle['points'][-1]['sample']['heap']['internal']['free']-internal[-1]
    result['settle_psram_recovered_bytes']=settle['points'][-1]['sample']['heap']['psram']['free']-psram[-1]
    result['settle_recovery_basis']='Final tail io.snapshot minus final-cycle settled_heap; with zero settle both use the same settled_heap sample'
    if image['board']=='echoear':
        require(image['elf_sha256']==base.REFERENCE and all(c.get('trace') is None for c in cycles),'unchanged S3 reference required')
        result['attribution']='unavailable on S3 reference; same lifecycle/settle heap contrast'
        return result
    require(image['board']=='p4xc','unsupported board')
    require(raw['restore'].get('action')=='p4-product-of-record','pinned normal restore required')
    require(isinstance(image.get('heap_tcp_values'),dict) and image.get('heap_attribution',{}).get('round')==2,'round-2 build provenance missing')
    elf=Path(elf) if elf else Path(image['bundle'])/'dg-esp32.elf';require(digest(elf)==image['elf_sha256'],'symbolization ELF hash mismatch')
    pcs=[]
    for row in cycles:
        t=row['trace'];validate_trace(t);require(t['start']['id']==row['index'],'replayed trace window')
        validate_tcp(row.get('tcp'),row['index'],image.get('heap_tcp_values'))
        pcs.extend(pc for b in t['blocks'] for pc in [*b['callers'],b['new_caller']])
    symbols=symbolizer(elf,pcs)
    for row in cycles:
        status=validate_trace(row['trace'])
        buckets={memory:{name:dict(bytes=0,blocks=0) for name in (*base.CANDIDATES,'unclassified')} for memory in ('internal','psram','other')}
        blocks=[]
        for b in row['trace']['blocks']:
            frames=[text for pc in b['callers'] if pc for text in symbols[pc]]
            caller=symbols[b['new_caller']] if b['new_caller'] else []
            source=caller_status(caller) if status=='complete' else 'invalid-table-overflow'
            hint=base.category((caller if source=='recorded-return-PC' else [])+frames);memory='internal' if b['internal'] else 'psram' if b['external'] else 'other'
            buckets[memory][hint]['bytes']+=b['bytes'];buckets[memory][hint]['blocks']+=1
            blocks.append(dict(b,symbols=frames,new_caller_symbols=caller,
                new_caller_status=source,memory=memory,component_hint=hint))
        result['cycles'].append(dict(index=row['index'],trace_phase='after-final-settle' if row['index']==len(cycles) and settle['requested_seconds']>0 else 'after-cycle',
            caller_status=status,caller_attribution_valid=status=='complete',candidates=buckets,blocks=blocks,tcp=row.get('tcp')))
    initial=cycles[-1]['tcp'];final=settle['points'][-1]['tcp']
    require(initial is not None and final is not None,'missing TIME_WAIT observations')
    for point in settle['points']:
        validate_tcp(point.get('tcp'),len(cycles),image.get('heap_tcp_values'))
    last_trace=cycles[-1]['trace']
    for key in ('host_start_seconds','host_stop_seconds'):
        require(type(last_trace.get(key)) in (int,float) and math.isfinite(last_trace[key]) and last_trace[key]>=0,'invalid trace time')
    require(last_trace['host_stop_seconds']>=settle['started_seconds']+settle['points'][-1]['elapsed_seconds'] and
            last_trace['host_start_seconds']<=settle['started_seconds'],'trace did not span final settle')
    result.update(elf_sha256=image['elf_sha256'],tcp_values=image.get('heap_tcp_values'),
        time_wait_observation=dict(before=initial['time_wait'],after=final['time_wait'],
            elapsed_seconds=settle['points'][-1]['elapsed_seconds'],configured_lifetime_ms=final['time_wait_ms'],
            conclusion='TIME_WAIT list drained during settle' if initial['time_wait']>0 and final['time_wait']==0 else 'drain not demonstrated'),
        attribution='Wrapper return PC plus bounded malloc stack; task-context only, no pass/fix inferred',
        limits='ISR and pre-window allocations excluded; vendor/internal same-object calls may bypass linker wrappers. Zero new_caller means unavailable, not proof of a C allocation. Overflow invalidates that window\'s C++ caller attribution only; malloc stacks remain available. Runtime-internal PCs do not identify the requester. TCP census omits listening and unlisted PCBs; exported constants are compile-time values. Caller-table locking and probe chains may perturb timing.')
    return result


def acquire(args,io):
    from p4_liveness import settled_heap,sample_check
    settings(args);cycles=[];trace_running=False;tcp_warmup=None
    with io.open() as board:
        try:
            io.boot(board);io.provision(board)
            if args.board=='p4xc':
                init=base.single(base.records(board,'init','init'),'init')
                require(init.get('capacity')==base.CAPACITY and init.get('depth')==base.DEPTH and
                        init.get('new_caller_capacity')==NEW_CAPACITY and init.get('new_caller_overflow') is False,'round-2 diagnostic firmware required')
                tcp_warmup=tcp_state(board,0)
                base.write(args.out/'tcp-warmup.json',tcp_warmup)
            baseline,evidence=settled_heap(board,io);final_settle=None
            for index in range(1,args.cycles+1):
                start=None
                if args.board=='p4xc':
                    trace_out=args.out/f'cycle-{index:02d}-trace';trace_out.mkdir()
                    trace_began=io.clock()
                    start=base.single(base.records(board,'start','start'),'start');trace_running=True;base.write(trace_out/'start.json',start)
                at=board.cursor;board.command('demo run',90)
                cursor,_,line=board.wait_for_entry(r'\bSTATE Ready gen=\d+',120,since=at)
                io.session_gen=int(re.search(r'gen=(\d+)',line)[1]);io.serial_seen=cursor;io.active=True;io.stopping=False
                io.pause(board,args.active_seconds);io.stopping=True;at=board.cursor;board.command('demo hold',45)
                board.wait_for(r'\bPHASE closed\b',30,since=at);io.active=False
                sample,settling=settled_heap(board,io);began=io.clock()
                tcp=tcp_state(board,index) if args.board=='p4xc' else None
                if index==args.cycles:
                    points=[dict(elapsed_seconds=io.clock()-began,sample=sample,tcp=tcp)]
                    deadline=began+args.settle_seconds
                    while io.clock()<deadline:
                        require(len(points)<=MAX_SETTLE//PERIOD+1,'settle sample limit')
                        io.pause(board,min(PERIOD,deadline-io.clock()))
                        point=sample_check(io.snapshot(board),args.board)
                        state=tcp_state(board,index) if args.board=='p4xc' else None
                        points.append(dict(elapsed_seconds=io.clock()-began,sample=point,tcp=state))
                        base.write(args.out/f'settle-{len(points)-1:03d}.json',points[-1])
                    final_settle=dict(requested_seconds=args.settle_seconds,period_seconds=PERIOD,started_seconds=began,points=points)
                    base.write(args.out/'final-settle.json',final_settle)
                trace=None
                if start:
                    stop=base.single(base.records(board,'stop','stop'),'stop');trace_running=False;base.write(trace_out/'stop.json',stop)
                    trace_ended=io.clock()
                    trace=base.collect(board,start,stop,trace_out)
                    trace.update(host_start_seconds=trace_began,host_stop_seconds=trace_ended)
                row=dict(index=index,settled=sample,settling=settling,tcp=tcp,trace=trace)
                if trace:row['caller_status']='table-overflow' if any(r.get('new_caller_overflow') is True for r in (start,stop)) else 'complete'
                base.write(args.out/f'cycle-{index:02d}.json',row);cycles.append(row)
                if trace:validate_trace(trace)
            return dict(baseline=baseline,baseline_settling=evidence,tcp_warmup=tcp_warmup,cycles=cycles,final_settle=final_settle)
        finally:
            import sys
            error=sys.exc_info()[1];io.stopping=True;io.active=False
            try:
                board.command('demo hold',45)
                if trace_running:base.records(board,'stop','stop')
            except Exception as exc:
                io.cleanup_error=type(exc).__name__+': '+str(exc)
                if error is None:raise


def live(args):
    import boards,board_smoke as smoke
    from p4_product import candidate_storage
    from p4_step14 import restoring
    boards.require_bench();settings(args)
    manifest,_=verify_bundle(args.bundle,args.board,args.reference_only)
    require(manifest['profile']=='demo' and not manifest['aec_enabled'],'AEC-off demo required')
    if args.board=='p4xc':
        require(manifest.get('heap_attribution')==dict(capacity=base.CAPACITY,depth=base.DEPTH,mode='LEAKS',isr='excluded',round=2,new_caller_capacity=NEW_CAPACITY),'round-2 diagnostic bundle required')
    else:require(args.reference_only and manifest['elf_sha256']==base.REFERENCE,'unchanged S3 demo reference required')
    identity=json.loads(args.bot_identity.read_text());args.out.mkdir(parents=True,exist_ok=False)
    base.write(args.out/'image.json',dict(manifest,bundle=str(args.bundle.resolve())));base.write(args.out/'identity.json',identity)
    result=dict(acquisition_complete=False,qualification=False,passed=False,board=args.board,requested_cycles=args.cycles,settle_seconds=args.settle_seconds,cycles=[]);guard=io=None
    try:
        with restoring(args.restore_record,args.restore_record_sha256),candidate_storage(args.board,args.bundle,args.out):
            io=smoke.LiveIO(args,args.out,manifest,identity);io.preflight_tools();io.preflight_restore();guard=io.hold()
            with guard:
                guard.touch();io.flash(args.bundle,manifest,'candidate');result.update(acquire(args,io));guard.success=True
            result['restore']=guard.record;require(guard.record.get('clean') is True,'unclean restore')
            if args.board=='p4xc':require(guard.record.get('action')=='p4-product-of-record','pinned normal restore required')
            result['acquisition_complete']=True
    except BaseException as exc:
        result['error']=type(exc).__name__+': '+str(exc)
        if guard is not None:result['restore']=guard.record
    finally:
        if io is not None and getattr(io,'cleanup_error',None):result['cleanup_error']=io.cleanup_error
        try:base.write(args.out/'acquisition.json',result)
        finally:
            if guard is not None:guard.release_signals()
    return 0 if result['acquisition_complete'] and not result.get('cleanup_error') else 1


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    score=sub.add_parser('analyze');score.add_argument('directory',type=Path);score.add_argument('--elf',type=Path);score.add_argument('--out',type=Path,required=True)
    run=sub.add_parser('live');run.add_argument('--board',choices=('p4xc','echoear'),required=True)
    run.add_argument('--bundle',type=Path,required=True);run.add_argument('--out',type=Path,required=True)
    run.add_argument('--bot-dir',type=Path,required=True);run.add_argument('--bot-identity',type=Path,required=True);run.add_argument('--bot-repo',type=Path,required=True)
    run.add_argument('--cycles',type=int,default=20);run.add_argument('--active-seconds',type=float,default=5);run.add_argument('--settle-seconds',type=float,default=0)
    run.add_argument('--reference-only',action='store_true');run.add_argument('--restore-record',type=Path);run.add_argument('--restore-record-sha256')
    run.add_argument('--factory-backup',dest='p4_factory_backup',type=Path,default=Path.home()/'src/evidence/echoear/p4/factory-20261031T060646Z')
    run.set_defaults(headless=False,calibration=False,p4_restore_bundle=None,p4_restore_elf=None)
    args=parser.parse_args(argv)
    if args.action=='live':print('Coordinator runner PID='+str(os.getpid()),flush=True);return live(args)
    base.write(args.out,analyze(args.directory,args.elf));return 0


if __name__=='__main__':raise SystemExit(main())
