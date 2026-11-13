#!/usr/bin/env python3
"""Offline heap diagnosis and coordinator-only short cycle acquisition.

No qualification bounds are changed. P4 requires an explicit diagnostic image;
S3 uses the unchanged demo reference and supplies heap/task-census contrast only.
"""
import argparse
import json
import math
from pathlib import Path
import re
import subprocess

from make_bundle import digest,verify_bundle
from wire_contract import require

CAPACITY=4096
DEPTH=8
PAGE=3
REFERENCE='5bbd868adceb8f4d8ff99cfe0e077d5b249a29d29a5aae766b8c61f664939057'
CANDIDATES={
    'hosted-rpc-sdio':r'esp_hosted|wifi_remote|rpc_|sdio|h_sdio',
    'lwip-pcb-pbuf':r'lwip/|\b(?:tcp|udp|pbuf|memp|netconn|tcpip)_',
    'mbedtls-dtls':r'mbedtls|dtls',
    'esp-peer':r'esp_peer|esp-peer|peer_.*(?:create|alloc|open)',
    'display-touch':r'dg_display|demo_ui|demo_touch|touch\.cpp',
    'console':r'console|performance_snapshot|performance\.cpp|dg_diagnostics',
}


def write(path,data):
    with Path(path).open('x') as stream:stream.write(json.dumps(data,indent=2,allow_nan=False)+'\n')


def integer(value,name,minimum=0,maximum=0xffffffff):
    require(type(value) is int and minimum<=value<=maximum,'invalid '+name)
    return value


def records(board,command,kind):
    """Bounded command response; missing output is an error, never zero leaks."""
    at=board.cursor;board.command('heap-attribution '+command,30)
    board.wait_for(r'^HEAP_ATTRIBUTION .*"kind":"'+kind+'"',10,since=at)
    result=[]
    for _,_,line in board.lines_since(at):
        if line.startswith('HEAP_ATTRIBUTION '):
            row=json.loads(line.split(' ',1)[1]);require(row.get('schema')==1,'unknown trace schema');result.append(row)
    return result


def single(rows,kind):
    require(len(rows)==1 and rows[0].get('kind')==kind,'missing/duplicate '+kind)
    return rows[0]


def validate_trace(start,stop,blocks):
    require(start.get('kind')=='start' and stop.get('kind')=='stop','trace window missing')
    identity=integer(start.get('id'),'window id',1)
    require(stop.get('id')==identity,'trace window changed')
    for row in (start,stop):
        require(row.get('schema')==1 and row.get('capacity')==CAPACITY and row.get('depth')==DEPTH,'trace configuration differs')
        require(row.get('isr_allocations')=='excluded-PSRAM-buffer','unknown trace ISR coverage')
        for key in ('before_free','before_largest','after_free','after_largest'):integer(row.get(key),key)
    count=integer(stop.get('records'),'trace record count',0,CAPACITY)
    require(type(stop.get('overflow')) is bool and not stop['overflow'],'trace overflow: incomplete attribution')
    integer(stop.get('high_water'),'high water',count,CAPACITY)
    require(len(blocks)==count,'missing trace blocks')
    seen=set()
    for index,row in enumerate(blocks):
        require(row.get('schema')==1 and row.get('kind')=='block' and row.get('id')==identity and row.get('index')==index,
                'duplicate/reordered/foreign trace block')
        address=integer(row.get('address'),'allocation address',1)
        require(address not in seen,'duplicate retained allocation');seen.add(address)
        integer(row.get('bytes'),'allocation size',1)
        require(type(row.get('internal')) is bool and row.get('freed') is False,'invalid live allocation')
        callers=row.get('callers');require(isinstance(callers,list) and len(callers)==DEPTH,'incomplete call stack')
        for pc in callers:integer(pc,'caller PC')
    return blocks


def collect(board,start,stop,out):
    blocks=[];pages=[]
    count=integer(stop.get('records'),'trace record count',0,CAPACITY)
    # The raw stop record is already saved; do not export a partial overflow.
    require(stop.get('overflow') is False,'trace overflow: incomplete attribution')
    for page in range((count+PAGE-1)//PAGE):
        rows=records(board,'page '+str(page),'page')
        write(out/('page-'+str(page)+'.json'),rows)
        require(rows and rows[-1].get('kind')=='page','trace page trailer missing')
        trailer=rows[-1];first=page*PAGE;end=min(count,first+PAGE)
        require(trailer.get('id')==start['id'] and trailer.get('first')==first and trailer.get('next')==end and len(rows)==end-first+1,
                'missing/duplicate trace page')
        blocks.extend(rows[:-1]);pages.append(trailer)
    return dict(start=start,stop=stop,blocks=blocks,pages=pages)


def symbolize(elf,pcs,runner=subprocess.run):
    """Fixed-size addr2line batches, bound by the authenticated raw PCs."""
    result={};pcs=sorted(set(pcs)-{0})
    for offset in range(0,len(pcs),128):
        batch=pcs[offset:offset+128]
        output=runner(['riscv32-esp-elf-addr2line','-a','-f','-C','-i','-e',str(elf),
            *[hex(pc-1) for pc in batch]],check=True,capture_output=True,text=True,timeout=30).stdout
        groups=[]
        for line in output.splitlines():
            if re.fullmatch(r'0x[0-9a-fA-F]+',line):groups.append((int(line,16),[]))
            else:
                require(groups,'unframed symbolizer output');groups[-1][1].append(line)
        require([address for address,_ in groups]==[pc-1 for pc in batch],'incomplete symbolizer output')
        for pc,(_,lines) in zip(batch,groups):
            require(lines and len(lines)%2==0,'incomplete symbol frame')
            result[pc]=lines
    return result


def category(frames):
    for frame in frames:
        for name,pattern in CANDIDATES.items():
            if re.search(pattern,frame,re.I):return name
    return 'unclassified'


def analyze(directory,elf=None,symbolizer=symbolize):
    directory=Path(directory);raw=json.loads((directory/'acquisition.json').read_text())
    image=json.loads((directory/'image.json').read_text());board=image['board']
    require(raw.get('acquisition_complete') is True and raw.get('restore',{}).get('clean') is True,'incomplete diagnostic acquisition/restore')
    cycles=raw['cycles'];require(1<=len(cycles)<=6,'diagnosis is bounded to 1..6 cycles')
    samples=[raw['baseline']]+[row['settled'] for row in cycles]
    free=[integer(s['heap']['internal']['free'],'settled free') for s in samples]
    result=dict(kind='heap-attribution-diagnosis',board=board,qualification=False,passed=False,
        source_sha256=digest(directory/'acquisition.json'),internal_free=free,net_decline_bytes=free[0]-free[-1],
        largest_internal=[s['heap']['internal'].get('largest') for s in samples],
        psram_free=[s['heap'].get('psram',{}).get('free') for s in samples],
        task_census=[[(t.get('id'),t.get('name')) for t in s.get('tasks',[])] for s in samples],
        scope='Report only; bounded allocation windows, not a leak proof or qualification run',cycles=[])
    if board=='echoear':
        require(image['elf_sha256']==REFERENCE,'S3 contrast needs the unchanged demo reference')
        require(all(row.get('trace') is None for row in cycles),'unexpected S3 instrumentation')
        result['attribution']='unavailable on unchanged S3 reference; heap/task-census contrast only'
        result['candidates']={name:'unmeasured on S3 reference' for name in CANDIDATES}
        return result
    require(board=='p4xc','unsupported diagnostic board')
    elf=Path(elf) if elf else Path(image['bundle'])/'dg-esp32.elf'
    require(digest(elf)==image['elf_sha256'],'symbolization ELF hash mismatch')
    pcs=[]
    for row in cycles:
        trace=row['trace'];validate_trace(trace['start'],trace['stop'],trace['blocks'])
        pcs.extend(pc for block in trace['blocks'] for pc in block['callers'])
    symbols=symbolizer(elf,pcs)
    for index,row in enumerate(cycles,1):
        require(row.get('index')==index and row['trace']['start']['id']==index,'replayed/reordered diagnostic cycle')
        buckets={name:dict(bytes=0,blocks=0) for name in (*CANDIDATES,'unclassified')};details=[]
        for block in row['trace']['blocks']:
            if not block['internal']:continue
            frames=[text for pc in block['callers'] if pc for text in symbols[pc]]
            owner=category(frames);buckets[owner]['bytes']+=block['bytes'];buckets[owner]['blocks']+=1
            details.append(dict(block,component_hint=owner,symbols=frames))
        stop=row['trace']['stop'];direct_delta=stop['before_free']-stop['after_free']
        retained_bytes=sum(b['bytes'] for b in details)
        result['cycles'].append(dict(index=index,internal_decline_bytes=free[index-1]-free[index],
            trace_internal_decline_bytes=direct_delta,retained_requested_bytes=retained_bytes,
            unaccounted_delta_bytes=direct_delta-retained_bytes,candidates=buckets,blocks=details))
    result.update(elf_sha256=image['elf_sha256'],attribution='task-context allocations still live at each stop; ISR allocations excluded',
        limits='No pre-window allocations, allocator overhead or ISR attribution; eight frames; component hints inferred from symbols. Do not sum windows as a net leak.')
    return result


def retained(directories):
    """Re-derive the original decline without changing its scoring policy."""
    result=[]
    for directory in directories:
        path=Path(directory)/'acquisition.json';data=json.loads(path.read_text());samples=data['settled_samples']
        free=[s['heap']['internal']['free'] for s in samples]
        first={(t['id'],t['name']) for t in samples[0]['tasks']};last={(t['id'],t['name']) for t in samples[-1]['tasks']}
        result.append(dict(directory=str(directory),source_sha256=digest(path),board=data['board'],error=data.get('error'),
            internal_free=free,net_decline_bytes=free[0]-free[-1],
            largest_internal=[s['heap']['internal']['largest'] for s in samples],
            psram_free=[s['heap']['psram']['free'] for s in samples],tasks_added=sorted(last-first),tasks_removed=sorted(first-last),
            attribution='not recorded; task identity/stack census does not measure allocation ownership',
            candidates={name:'not attributable from retained PERF; requires diagnostic trace' for name in CANDIDATES}))
    return dict(kind='retained-heap-decline',qualification=False,passed=False,runs=result)


def acquire(args,io):
    from p4_liveness import settled_heap
    cycles=[];trace_running=False
    with io.open() as board:
        try:
            io.boot(board);io.provision(board)
            if args.board=='p4xc':
                init=single(records(board,'init','init'),'init')
                require(init.get('capacity')==CAPACITY and init.get('depth')==DEPTH,'diagnostic build required')
            baseline,evidence=settled_heap(board,io)
            for index in range(1,args.cycles+1):
                start=None
                if args.board=='p4xc':
                    trace_out=args.out/f'cycle-{index:02d}-trace';trace_out.mkdir()
                    start=single(records(board,'start','start'),'start');trace_running=True
                    write(trace_out/'start.json',start)
                at=board.cursor;board.command('demo run',90)
                cursor,_,line=board.wait_for_entry(r'\bSTATE Ready gen=\d+',120,since=at)
                io.session_gen=int(re.search(r'gen=(\d+)',line)[1]);io.serial_seen=cursor;io.active=True;io.stopping=False
                io.pause(board,args.active_seconds)
                io.stopping=True;at=board.cursor;board.command('demo hold',45)
                board.wait_for(r'\bPHASE closed\b',30,since=at);io.active=False
                sample,settling=settled_heap(board,io)
                trace=None
                if start:
                    stop=single(records(board,'stop','stop'),'stop');trace_running=False
                    write(trace_out/'stop.json',stop)
                    trace=collect(board,start,stop,trace_out)
                row=dict(index=index,settled=sample,settling=settling,trace=trace)
                write(args.out/f'cycle-{index:02d}.json',row);cycles.append(row)
                if trace:validate_trace(trace['start'],trace['stop'],trace['blocks'])
            return dict(baseline=baseline,baseline_settling=evidence,cycles=cycles)
        finally:
            import sys
            error=sys.exc_info()[1]
            io.stopping=True;io.active=False
            try:
                board.command('demo hold',45)
                if trace_running:records(board,'stop','stop')
            except Exception as exc:
                io.cleanup_error=type(exc).__name__+': '+str(exc)
                if error is None:raise


def live(args):
    import boards,board_smoke as smoke
    from p4_product import candidate_storage
    from p4_step14 import restoring
    boards.require_bench()
    require(1<=args.cycles<=6 and math.isfinite(args.active_seconds) and 1<=args.active_seconds<=30,'diagnostic cycle/time bounds')
    manifest,_=verify_bundle(args.bundle,args.board,args.reference_only)
    require(manifest['profile']=='demo' and not manifest['aec_enabled'],'AEC-off demo image required')
    require(args.board!='echoear' or args.reference_only and manifest['elf_sha256']==REFERENCE,'S3 uses unchanged demo reference')
    if args.board=='p4xc':require(manifest.get('heap_attribution')==dict(capacity=CAPACITY,depth=DEPTH,mode='LEAKS',isr='excluded'),
                                 'explicit P4 diagnostic bundle required; use scripts/p4_heap_bundle.py')
    identity=json.loads(args.bot_identity.read_text());args.out.mkdir(parents=True,exist_ok=False)
    write(args.out/'image.json',dict(manifest,bundle=str(args.bundle.resolve())))
    write(args.out/'identity.json',identity)
    result=dict(acquisition_complete=False,qualification=False,passed=False,board=args.board,cycles=[]);guard=io=None
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
        try:write(args.out/'acquisition.json',result)
        finally:
            if guard is not None:guard.release_signals()
    return 0 if result['acquisition_complete'] and not result.get('cleanup_error') else 1


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    old=sub.add_parser('retained');old.add_argument('directories',type=Path,nargs='+');old.add_argument('--out',type=Path,required=True)
    score=sub.add_parser('analyze');score.add_argument('directory',type=Path);score.add_argument('--elf',type=Path);score.add_argument('--out',type=Path,required=True)
    run=sub.add_parser('live');run.add_argument('--board',choices=('p4xc','echoear'),required=True)
    run.add_argument('--bundle',type=Path,required=True);run.add_argument('--out',type=Path,required=True)
    run.add_argument('--bot-dir',type=Path,required=True);run.add_argument('--bot-identity',type=Path,required=True);run.add_argument('--bot-repo',type=Path,required=True)
    run.add_argument('--cycles',type=int,default=6);run.add_argument('--active-seconds',type=float,default=5)
    run.add_argument('--reference-only',action='store_true');run.add_argument('--restore-record',type=Path)
    run.add_argument('--restore-record-sha256');run.add_argument('--factory-backup',dest='p4_factory_backup',type=Path,
        default=Path.home()/'src/evidence/echoear/p4/factory-20261031T060646Z')
    run.set_defaults(headless=False,calibration=False,p4_restore_bundle=None,p4_restore_elf=None)
    args=parser.parse_args(argv)
    if args.action=='live':
        import os
        print('Coordinator runner PID='+str(os.getpid()),flush=True);return live(args)
    write(args.out,retained(args.directories) if args.action=='retained' else analyze(args.directory,args.elf));return 0


if __name__=='__main__':raise SystemExit(main())
