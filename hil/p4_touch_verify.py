#!/usr/bin/env python3
"""User-paced physical F11 verification; only `run` may access hardware."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from p4_parity import BOARDS,COMMIT,PIN,RESTORE_SHA256,digest,load,pinned_bundle,require,write_new
from p4_parity_touch import touch_witness

ROOT=Path.home()/'src/evidence/echoear/p4/touch-verify'
BRIEF=Path.home()/'src/dg-esp32/proj-2026-10-30-2143/codex-jobs/f11-redesign-brief.md'
WAIT=900
WINDOW=600
POLL=1
DRIVER=('taps','double_taps','ignored','rejected','interrupts','reads','read_errors','coalesced','injected','injected_ignored')
APP=('requests','request_rejected','phase_ignored')
INSTRUCTIONS=('First one short single tap in the middle of the scene; wait for CONTACT PRE-CHECK. '
    'Then two short taps on the same spot, each 20–250 ms, with the second release '
    '80–399 ms after the first. Stop touching after that double tap.')


def sources():
    names=('p4_touch_verify.py','p4_parity.py','p4_parity_touch.py','p4_parity_restore.py',
        'p4_parity_serial.py','p4_step14.py','p4_product.py','board_smoke.py','board_serial.py',
        'boards.py','board_port.py','esp_serial.py','make_bundle.py','echoear_aec_bench.py','echoear_restore_verify.py',
        'echoear_item2_campaign.py','echoear_rollback.py','p4_parity_preflight.py')
    return {name:digest(Path(__file__).with_name(name)) for name in names}


def create_plan(root,identity_file):
    from p4_step14 import restore_record
    from p4_parity_restore import S3_RESTORE_ELF
    from echoear_aec_bench import NORMAL,verify_bundle
    from wire_contract import identity_schema
    root=Path(root).resolve();identity_file=Path(identity_file).resolve()
    require(root.is_relative_to(ROOT) and root!=ROOT,'fresh campaign root must be below '+str(ROOT))
    identity=load(identity_file);identity_schema(identity)
    require(identity['profile_manifest']['config']['profile']=='demo','product demo bot required')
    record=PIN/'restore-of-record.json';restore_record(record,RESTORE_SHA256)
    images={}
    for board in BOARDS:
        bundle=PIN/'final-bundles'/('bundle-'+board+'-demo');m=pinned_bundle(bundle,board,'demo')
        images[board]=dict(bundle=str(bundle),elf_sha256=m['elf_sha256'],manifest_sha256=digest(bundle/'manifest.json'))
    return dict(schema=1,kind='user-paced-f11-plan',qualification=False,commit=COMMIT,root=str(root),
        product_identity_file=str(identity_file),product_identity_sha256=digest(identity_file),product_identity=identity,
        images=images,restore_record=str(record),restore_record_sha256=RESTORE_SHA256,
        s3_restore=dict(bundle=str(NORMAL),elf_sha256=S3_RESTORE_ELF,files=verify_bundle(NORMAL,S3_RESTORE_ELF)),
        policy=dict(go='GO',wait_seconds=WAIT,window_seconds=WINDOW,poll_seconds=POLL,max_attempts=2,
            retry='one retry only after a complete clean post-GO window with no contact evidence',
            reads='P4 poll reads alone are not contacts; S3 IRQ/read activity prevents absence retry',
            sessions='fresh Live at GO; no-request idle renewal with retained baselines and a fixed 600 s deadline'),
        brief_file=str(BRIEF),brief_sha256=digest(BRIEF),source_sha256=sources())


def checked_plan(path):
    row=load(path)
    require(row.get('kind')=='user-paced-f11-plan' and row==create_plan(row['root'],row['product_identity_file']),
        'touch plan, pins, identity, brief or source changed')
    return row


def validate_status(row):
    require(isinstance(row,dict) and type(row.get('initialized')) is bool and type(row.get('enabled')) is bool and
        type(row.get('pending')) is bool,'typed touch state required')
    for key in ('phase','generation',*APP):require(type(row.get(key)) is int and row[key]>=0,'invalid touch '+key)
    for key in DRIVER:require(type(row.get('driver',{}).get(key)) is int and row['driver'][key]>=0,'invalid touch driver '+key)
    return row


def delta(before,after):
    validate_status(before);validate_status(after)
    result={key:after['driver'][key]-before['driver'][key] for key in DRIVER}
    result.update({key:after[key]-before[key] for key in APP})
    require(all(n>=0 for n in result.values()) and after['generation']>=before['generation'],'touch counters/generation reset')
    return result


def contact(d,board):
    detected=any(d[key]>0 for key in ('taps','double_taps','ignored','rejected'))
    activity=detected or (board=='echoear' and any(d[key]>0 for key in ('interrupts','reads','coalesced')))
    return dict(detected=detected,activity=activity,valid_tap_contacts=d['taps'],
        double_taps=d['double_taps'],ignored=d['ignored'],rejected=d['rejected'])


def live_status(row):
    require(row['initialized'] and row['enabled'] and row['phase']==1 and not row['pending'],
        'touch must be initialized/enabled in Live with no pending request')


def observe(out,board,status,mark,serial,*,restart,clock=time.monotonic,sleep=time.sleep,emit=print):
    """Pure IO-injected state machine; pre-GO samples are retained, never scored."""
    out=Path(out);go=out/'GO';require(not go.exists(),'GO must be created only after ready-for-go.json')
    with (out/'status-timeline.jsonl').open('x') as timeline:
        index=0
        def sample(phase):
            nonlocal index
            value=validate_status(status());index+=1
            timeline.write(json.dumps(dict(index=index,at=clock(),phase=phase,status=value),allow_nan=False)+'\n');timeline.flush()
            return value
        prepared=sample('waiting-for-go');live_status(prepared);write_new(out/'prepared-before.json',prepared)
        write_new(out/'ready-for-go.json',dict(board=board,go_file=str(go),wait_seconds=WAIT,
            window_seconds=WINDOW,instructions=INSTRUCTIONS,prepared_at=clock()))
        emit('READY FOR GO '+str(out/'ready-for-go.json')+' — confirm user is ready, then create '+str(go),flush=True)
        waiting_until=clock()+WAIT;after=prepared
        while not go.exists():
            if clock()>=waiting_until:
                write_new(out/'before.json',prepared);write_new(out/'after.json',after)
                return dict(go_observed=False,touch_verified=False,status='go-timeout',before=prepared,after=after,
                    retryable=False,window_complete=False,contact_precheck=dict(detected=False,activity=False))
            sleep(min(POLL,waiting_until-clock()));after=sample('waiting-for-go')
        require(go.is_file() and not go.is_symlink(),'GO must be a regular file')
        restart()
        before=sample('go-baseline');live_status(before);write_new(out/'before.json',before)
        opened=clock();deadline=opened+WINDOW;start=mark();go_sha=digest(go)
        write_new(out/'window-opened.json',dict(opened_at=opened,deadline=deadline,go_sha256=go_sha,serial_start=start))
        emit('WINDOW OPEN '+board+' — 600 s after GO. '+INSTRUCTIONS,flush=True)
        baseline=before;baseline_start=start
        baselines=[dict(at=opened,reason='go',status=baseline,serial_start=baseline_start)]
        write_new(out/'baseline-001.json',baselines[0])
        last=before;after=before;checked=None;precheck=False;single=False;witness=None;global_witness=None
        while True:
            sleep(min(POLL,max(0,deadline-clock())));after=sample('window');now=clock()
            d=delta(before,after);delta(last,after)
            require(after['initialized'] and after['enabled'],'touch disabled or uninitialized during window')
            require(not d['injected'] and not d['injected_ignored'],'injection changed during physical touch window')
            require(not d['read_errors'],'touch read error during window')
            checked=contact(d,board);single |= d['taps']==1 and d['double_taps']==0
            if checked['detected'] and not precheck:
                precheck=True;emit('CONTACT PRE-CHECK '+json.dumps(checked)+'; then perform one double tap.',flush=True)
            if after!=last:
                emit('TOUCH COUNTERS '+json.dumps(dict(board=board,elapsed_s=now-opened,delta=d,
                    phase=after['phase'],generation=after['generation'],pending=after['pending'])),flush=True)
            idle=after['phase']!=1 or after['pending'] or after['generation']!=baseline['generation']
            if not d['requests'] and idle:
                require(not d['double_taps'] and not d['request_rejected'] and not d['phase_ignored'],
                    'touch request evidence cannot be discarded by an idle renewal')
                if now>=deadline:break
                emit('RENEW LIVE: no accepted touch request; original window deadline unchanged',flush=True)
                restart();after=sample('rebaseline');live_status(after)
                renewed=delta(before,after);delta(last,after)
                require(not any(renewed[k] for k in ('requests','double_taps','request_rejected','phase_ignored','injected','injected_ignored','read_errors')),
                    'touch/request/error evidence changed during idle renewal')
                baseline=after;baseline_start=mark()
                baselines.append(dict(at=clock(),reason='no-request-idle',status=baseline,serial_start=baseline_start))
                write_new(out/f'baseline-{len(baselines):03d}.json',baselines[-1])
                last=after
                if clock()>=deadline:break
                continue
            if now<=deadline and d['double_taps'] and d['requests'] and after['phase']==1 and not after['pending']:
                try:
                    witness=touch_witness(baseline,after,serial(baseline_start))
                    global_witness=touch_witness(before,after,serial(start))
                except ValueError:witness=global_witness=None
                if witness:break
            if now>=deadline:break
            last=after
        closed=clock();lines=serial(start);write_new(out/'after.json',after)
        (out/'window-serial.log').write_text('\n'.join(lines)+'\n')
        d=delta(before,after);checked=contact(d,board)
        complete=closed>=deadline
        absent=complete and not checked['activity'] and not any(d[k] for k in (*APP,'injected','injected_ignored','read_errors'))
        return dict(go_observed=True,go_sha256=go_sha,before=before,after=after,delta=d,
            opened_at=opened,closed_at=closed,elapsed_s=closed-opened,window_seconds=WINDOW,window_complete=complete,
            serial_start=start,serial_witness=lines,witness=witness,global_witness=global_witness,
            witness_before=baseline,witness_serial_start=baseline_start,baselines=baselines,touch_verified=bool(witness),
            contact_precheck=dict(checked,standalone_single_observed=single),retryable=absent,
            status='passed' if witness else 'unverified-no-contact' if absent else 'unverified-contact')


def retry_eligible(plan,run):
    """No contact means gesture evidence, not P4's steadily increasing poll count."""
    try:
        from p4_parity_restore import restore_proof
        from p4_parity_serial import candidate_serial
        run=Path(run);row=load(run/'touch.json')
        require(row['acquisition_passed'] is True and row['touch_verified'] is False and row['retryable'] is True and
            row['status']=='unverified-no-contact' and row['go_observed'] is True and row['window_complete'] is True and
            row['window_seconds']==WINDOW and row['closed_at']-row['opened_at']>=WINDOW and
            math.isfinite(row['elapsed_s']) and row['elapsed_s']==row['closed_at']-row['opened_at'] and
            not row.get('error') and not row.get('cleanup_errors'),'no complete clean absent-user window')
        require(row['plan_file']==str(Path(plan['root'])/'plan.json') and row['plan_sha256']==digest(row['plan_file']) and checked_plan(row['plan_file'])==plan,'attempt plan changed')
        require(row['kind']=='user-paced-f11-result' and row['board']==run.parent.name and
            row['identity']==plan['product_identity'] and row['bundle']==plan['images'][row['board']]['bundle'] and
            row['elf_sha256']==plan['images'][row['board']]['elf_sha256'],'touch identity/image differs')
        require(load(run/'before.json')==row['before'] and load(run/'after.json')==row['after'],'raw touch bracket differs')
        require(load(run/'ready-for-go.json')['board']==row['board'] and
            load(run/'window-opened.json')['opened_at']==row['opened_at'] and digest(run/'GO')==row['go_sha256'],'GO evidence differs')
        samples=[json.loads(line) for line in (run/'status-timeline.jsonl').read_text().splitlines()]
        window=[s for s in samples if s['phase'] in ('go-baseline','rebaseline','window')]
        require(window and window[0]['status']==row['before'] and window[-1]['status']==row['after'] and
            window[0]['at']<=row['opened_at'] and window[-1]['at']>=row['opened_at']+WINDOW,'post-GO timeline incomplete')
        previous=row['before'];baseline=previous;baseline_index=0
        for index,sample in enumerate(window):
            state=sample['status'];d=delta(row['before'],state);delta(previous,state)
            require(state['initialized'] and state['enabled'],'touch disabled during absence window')
            require(not contact(d,row['board'])['activity'] and not any(d[k] for k in (*APP,'injected','injected_ignored','read_errors')),
                'contact or invalid state prevents absence retry')
            if sample['phase'] in ('go-baseline','rebaseline'):
                live_status(state);baseline=state
                if 'baselines' in row:
                    require(baseline_index<len(row['baselines']),'missing renewal baseline')
                    record=row['baselines'][baseline_index];baseline_index+=1
                    require(record['status']==state and record==load(run/f'baseline-{baseline_index:03d}.json'),
                        'renewal baseline differs')
            else:
                idle=state['phase']!=1 or state['pending'] or state['generation']!=baseline['generation']
                require(not idle or (index+1<len(window) and window[index+1]['phase']=='rebaseline') or
                    (index==len(window)-1 and sample['at']>=row['opened_at']+WINDOW),'unrecovered idle state')
            previous=state
        if 'baselines' in row:require(baseline_index==len(row['baselines']),'missing renewal baseline')
        require(delta(row['before'],row['after'])==row['delta'],'contact delta differs')
        restore_proof(run,row,plan);candidate_serial(run)
        return True
    except (ValueError,OSError,KeyError,TypeError,UnicodeError):return False


def canonical(row):return json.dumps(row,sort_keys=True,separators=(',',':')).encode()


def verify_sources(finished):
    for path,sha in finished['sources'].items():require(digest(path)==sha,'sealed attempt evidence changed')


def entries(stream,plan_file,plan):
    stream.seek(0);raw=stream.read();require(not raw or raw.endswith('\n'),'truncated touch ledger')
    rows=[];registered={};finished={};previous='0'*64
    for line in raw.splitlines():
        row=json.loads(line);value=dict(row);claimed=value.pop('entry_sha256')
        require(row['sequence']==len(rows)+1 and row['plan_sha256']==digest(plan_file) and
            row['previous_sha256']==previous and hashlib.sha256(canonical(value)).hexdigest()==claimed,'touch ledger chain differs')
        run=row['run']
        if row['event']=='register':
            board=row['board'];prior=[r for r in registered.values() if r['board']==board]
            require(board in BOARDS and run not in registered and type(row['attempt']) is int and
                row['attempt']==len(prior)+1 and row['attempt']<=2 and
                run==str(Path(plan['root'])/board/f"attempt-{row['attempt']:02d}"),'invalid attempt/cap/path')
            if prior:
                end=finished.get(prior[-1]['run']);require(end and end['disposition']=='unverified','first attempt not closed unverified')
                verify_sources(end);require(retry_eligible(plan,prior[-1]['run']),'retry needs a complete zero-contact post-GO window')
            registered[run]=row
        else:
            require(row['event']=='finish' and run in registered and run not in finished and
                row['disposition'] in ('passed','unverified','failed','incomplete'),'invalid touch ledger disposition')
            finished[run]=row
        rows.append(row);previous=claimed
    return rows,registered,finished


@contextmanager
def journal(plan_file):
    plan=checked_plan(plan_file)
    with (Path(plan['root'])/'attempts.jsonl').open('a+') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        try:yield plan,stream,*entries(stream,plan_file,plan)
        finally:fcntl.flock(stream,fcntl.LOCK_UN)


def append(stream,rows,plan_file,**fields):
    row=dict(sequence=len(rows)+1,plan_sha256=digest(plan_file),previous_sha256=rows[-1]['entry_sha256'] if rows else '0'*64,
        monotonic_ns=time.monotonic_ns(),**fields)
    row['entry_sha256']=hashlib.sha256(canonical(row)).hexdigest()
    stream.seek(0,2);stream.write(json.dumps(row,sort_keys=True)+'\n');stream.flush();os.fsync(stream.fileno());return row


def register(plan_file,board,out):
    require(board in BOARDS,'unknown board')
    with journal(plan_file) as (plan,stream,rows,registered,finished):
        path=Path(out).resolve();require(not path.exists(),'retain attempts; fresh output required')
        prior=[r for r in registered.values() if r['board']==board];number=len(prior)+1
        require(number<=2 and path==Path(plan['root'])/board/f'attempt-{number:02d}','attempt order/cap/path differs')
        actual={str(p.resolve()) for p in Path(plan['root']).rglob('attempt-*') if p.is_dir()}
        require(actual==set(registered),'unregistered retained attempt or missing original')
        if prior:
            first=prior[-1]['run'];end=finished.get(first)
            require(end and end['disposition']=='unverified','first attempt not closed unverified')
            verify_sources(end);require(retry_eligible(plan,first),'retry needs a complete zero-contact post-GO window')
        return append(stream,rows,plan_file,event='register',board=board,run=str(path),attempt=number)


def finish(plan_file,out,error=None):
    out=Path(out).resolve()
    with journal(plan_file) as (plan,stream,rows,registered,finished):
        require(str(out) in registered and str(out) not in finished,'attempt not registered or already closed')
        out.mkdir(parents=True,exist_ok=True);path=out/'touch.json';disposition='incomplete'
        try:
            row=load(path)
            disposition='failed' if error or row.get('error') or row.get('cleanup_errors') or not row.get('acquisition_passed') else (
                'passed' if row.get('touch_verified') else 'unverified')
        except (OSError,ValueError,TypeError,AttributeError,UnicodeError):pass
        if error:write_new(out/'attempt-error.json',dict(error=str(error)))
        files=sorted(p for p in out.rglob('*') if p.is_file());require(not any(p.is_symlink() for p in files),'symlink in attempt evidence')
        return append(stream,rows,plan_file,event='finish',run=str(out),disposition=disposition,
            sources={str(p):digest(p) for p in files})


def live(args,plan):
    import board_smoke as smoke
    from p4_product import candidate_storage
    from p4_step14 import restoring
    from p4_parity_serial import candidate_serial
    bundle=Path(plan['images'][args.board]['bundle']);manifest=pinned_bundle(bundle,args.board,'demo')
    args.out.mkdir(parents=True,exist_ok=False);write_new(args.out/'identity.json',plan['product_identity'])
    result=dict(kind='user-paced-f11-result',board=args.board,variant='demo',bundle=str(bundle),elf_sha256=manifest['elf_sha256'],
        plan_file=str(args.plan.resolve()),plan_sha256=digest(args.plan),identity=plan['product_identity'],
        acquisition_passed=False,touch_verified=False,retryable=False,qualification=False,cleanup_errors=[])
    guard=None
    try:
        with restoring(plan['restore_record'],plan['restore_record_sha256']),candidate_storage(args.board,bundle,args.out):
            io=smoke.LiveIO(args,args.out,manifest,plan['product_identity']);io.preflight_tools();io.preflight_restore();guard=io.hold()
            with guard:
                guard.touch();io.flash(bundle,manifest,'candidate')
                with io.open() as board:
                    try:
                        io.boot(board);io.provision(board);io.begin(board,'resume');board.command('touch-enable 1')
                        def status():
                            require(smoke.process_token(io.pid)==plan['product_identity']['bot_instance'],'product bot exited/restarted')
                            cursor=board.cursor;board.command('touch-status')
                            return json.loads(board.wait_for(r'^DEMO_TOUCH_STATUS \{',5,since=cursor).split(' ',1)[1])
                        def mark():return dict(cursor=board.cursor,byte_offset=(args.out/'serial.log').stat().st_size)
                        def serial(start):
                            with (args.out/'serial.log').open('rb') as stream:
                                stream.seek(start['byte_offset']);return stream.read().decode(errors='replace').splitlines()
                        def restart():
                            # begin() takes a new peer/profile witness after intentional teardown.
                            io.stopping=True;board.command('demo hold',45);io.active=False
                            io.begin(board,'resume');board.command('touch-enable 1')
                        result.update(observe(args.out,args.board,status,mark,serial,restart=restart))
                    finally:
                        io.stopping=True
                        for command,timeout in (('touch-enable 0',5),('demo hold',45)):
                            try:board.command(command,timeout)
                            except BaseException as exc:result['cleanup_errors'].append(type(exc).__name__+': '+str(exc))
                        require(not result['cleanup_errors'],'touch disable/hold cleanup failed')
                        io.active=False
                guard.success=True
            result['restore']=guard.record
            require(guard.record.get('clean') is True and not result['cleanup_errors'],'touch cleanup/restore failed')
        candidate_serial(args.out);result['acquisition_passed']=True
    except BaseException as exc:
        result.update(error=type(exc).__name__+': '+str(exc),status='acquisition-error',touch_verified=False,retryable=False)
        if guard is not None:result['restore']=guard.record
    finally:
        try:
            try:
                timeline=args.out/'status-timeline.jsonl'
                if timeline.is_file():
                    rows=[json.loads(line) for line in timeline.read_text().splitlines()]
                    if rows and not (args.out/'after.json').exists():write_new(args.out/'after.json',rows[-1]['status'])
                    for key in ('before','after'):
                        if (args.out/(key+'.json')).exists():result[key]=load(args.out/(key+'.json'))
            except (ValueError,OSError,TypeError) as exc:
                result.update(acquisition_passed=False,touch_verified=False,retryable=False,
                    error='retained status incomplete: '+str(exc))
            write_new(args.out/'touch.json',result)
        finally:
            if guard is not None:guard.release_signals()
    return 1 if not result['acquisition_passed'] else 0 if result['touch_verified'] else 2


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('plan')
    for name in ('root','product-identity','out'):p.add_argument('--'+name,type=Path,required=True)
    for verb in ('check','status'):
        p=sub.add_parser(verb);p.add_argument('--plan',type=Path,required=True)
    p=sub.add_parser('run');p.add_argument('--board',choices=BOARDS,required=True)
    for name in ('plan','out','bot-dir','bot-identity'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--bot-repo',type=Path,default=Path.home()/'src/dg-esp32')
    args=parser.parse_args(argv)
    if args.action=='plan':
        row=create_plan(args.root,args.product_identity);require(args.out.resolve()==Path(row['root'])/'plan.json','use ROOT/plan.json')
        write_new(args.out,row);return 0
    plan=checked_plan(args.plan)
    if args.action=='check':print('F11 plan/source/pins authenticated; no hardware accessed');return 0
    if args.action=='status':
        with journal(args.plan) as (_,_,_,registered,finished):
            for run,row in registered.items():
                if run in finished:verify_sources(finished[run])
                print(json.dumps(dict(board=row['board'],attempt=row['attempt'],run=run,
                    disposition=finished.get(run,{}).get('disposition','unfinished'))))
        return 0
    args.out=args.out.resolve();args.bundle=Path(plan['images'][args.board]['bundle'])
    args.p4_factory_backup=Path.home()/'src/evidence/echoear/p4/factory-20261031T060646Z'
    args.reference_only=args.calibration=args.headless=False;args.p4_restore_bundle=args.p4_restore_elf=None
    require(load(args.bot_identity)==plan['product_identity'],'one planned product bot required')
    from p4_parity_preflight import host_checks
    host_checks(args,plan,pinned_bundle(args.bundle,args.board,'demo'),plan['product_identity'])
    register(args.plan,args.board,args.out)
    try:code=live(args,plan)
    except BaseException as exc:finish(args.plan,args.out,error=type(exc).__name__+': '+str(exc));raise
    finish(args.plan,args.out);return code


if __name__=='__main__':raise SystemExit(main())
