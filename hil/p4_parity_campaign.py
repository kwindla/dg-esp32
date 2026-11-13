"""Append-only, hash-chained attempt inventory. No device access or bench lock."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time

from p4_parity import checked_plan,load,digest,require,write_new,output_path

FAMILIES=('smoke','display','liveness','touch','erle')


def canonical(row):return json.dumps(row,sort_keys=True,separators=(',',':')).encode()


def read_entries(stream,plan_path):
    stream.seek(0);data=stream.read();require(not data or data.endswith('\n'),'truncated attempt ledger')
    entries=[];previous='0'*64;registered={};finished={}
    for line in data.splitlines():
        row=json.loads(line);value=dict(row);claimed=value.pop('entry_sha256')
        require(value.get('previous_sha256')==previous and claimed==hashlib.sha256(canonical(value)).hexdigest(),
            'attempt ledger hash chain changed')
        require(row.get('plan_sha256')==digest(plan_path) and row.get('sequence')==len(entries)+1,'ledger plan/sequence differs')
        run=row['run']
        if row['event']=='register':
            require(run not in registered and row['family'] in FAMILIES,'duplicate or invalid attempt registration')
            prior=[e for e in registered.values() if e['unit']==row['unit']]
            require(row['attempt']==len(prior)+1 and row['attempt']<=2,'attempt order/retry cap differs')
            if prior:
                require(prior[-1]['run'] in finished and finished[prior[-1]['run']]['disposition']=='untouched-noisy',
                    'a scored/unfinished attempt cannot be replaced')
                require(Path(prior[-1]['run']).parent==Path(run).parent,'attempt siblings must share their registered parent')
            registered[run]=row
        else:
            require(row['event']=='finish' and run in registered and run not in finished,'invalid attempt disposition')
            require(row['disposition'] in ('completed','failed','untouched-noisy','incomplete','inconclusive'),'invalid attempt disposition')
            finished[run]=row
        entries.append(row);previous=claimed
    return entries,registered,finished


def append(stream,entries,plan_path,**fields):
    row=dict(sequence=len(entries)+1,plan_sha256=digest(plan_path),previous_sha256=entries[-1]['entry_sha256'] if entries else '0'*64,
        monotonic_ns=time.monotonic_ns(),**fields)
    row['entry_sha256']=hashlib.sha256(canonical(row)).hexdigest()
    stream.seek(0,2);stream.write(json.dumps(row,sort_keys=True,separators=(',',':'))+'\n');stream.flush();os.fsync(stream.fileno())
    return row


@contextmanager
def journal(plan_path):
    plan=checked_plan(plan_path);path=Path(plan['root'])/'attempts.jsonl';path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        try:yield plan,stream,*read_entries(stream,plan_path)
        finally:fcntl.flock(stream,fcntl.LOCK_UN)


def untouched_noise(run):
    path=Path(run)/'result.json'
    if not path.exists():return False
    try:row=load(path)
    except (ValueError,UnicodeError):return False
    if not isinstance(row,dict) or not isinstance(row.get('room'),dict):return False
    return bool(row.get('device_untouched') is True and row.get('acquisition_passed') is False and
        row.get('status')=='preflight-noisy' and row.get('retryable') is True and row.get('score_produced') is False and
        row.get('room',{}).get('noise_contaminated') is True and
        not row.get('cleanup_error') and not row.get('cleanup_errors') and not row.get('restore') and
        not (Path(run)/'restore.json').exists() and
        not any(Path(run).glob('*flash*')) and not any(Path(run).glob('turn-*.json')) and
        not (Path(run)/'boot.log').exists() and not (Path(run)/'serial.log').exists())


def retained_directories(plan,registered):
    actual=set()
    for path in Path(plan['root']).rglob('attempt-*'):
        if not path.is_dir():continue
        if re.fullmatch(r'attempt-\d{2}-room',path.name):
            # Frozen AEC smoke puts its room baseline beside the original.
            require(str(path.with_name(path.name[:-5]).resolve()) in registered,
                'room preflight has no registered original')
        else:actual.add(str(path.resolve()))
    return actual


def register(plan_path,family,board,variant,run,part=None):
    require(family in FAMILIES and board in ('echoear','p4xc') and variant in ('demo','aec-demo','normal'),'invalid attempt unit')
    with journal(plan_path) as (plan,stream,entries,registered,finished):
        path=output_path(plan,run);match=re.fullmatch(r'attempt-(\d{2})',path.name)
        require(match is not None,'attempt directory must be attempt-NN')
        unit='/'.join((family,board,variant,str(part)))
        # Validate a prospective row by replaying the same reader policy.
        prior=[e for e in registered.values() if e['unit']==unit]
        require(int(match[1])==len(prior)+1 and len(prior)<2,'attempt order/retry cap differs')
        if prior:
            previous=finished.get(prior[-1]['run'])
            require(previous and previous['disposition']=='untouched-noisy' and untouched_noise(prior[-1]['run']),
                'a scored/unfinished attempt cannot be replaced')
            require(Path(prior[-1]['run']).parent==path.parent,'attempt siblings changed')
        # An unregistered failed sibling must not be hidden by a fresh run.
        actual=retained_directories(plan,registered)
        require(actual<=set(registered),'unregistered retained attempt directory; campaign inventory incomplete')
        return append(stream,entries,plan_path,event='register',run=str(path),unit=unit,family=family,
            board=board,variant=variant,part=part,attempt=int(match[1]))


def finish(plan_path,run,*,error=None,abandon=False):
    run=Path(run).resolve()
    with journal(plan_path) as (plan,stream,entries,registered,finished):
        require(str(run) in registered and str(run) not in finished,'attempt not registered or already closed')
        record=registered[str(run)];run.mkdir(parents=True,exist_ok=True)
        primary=next((p for p in (run/'result.json',run/'acquisition.json',run/'touch.json') if p.is_file()),None)
        malformed=None
        try:
            row=load(primary) if primary else {}
            if not isinstance(row,dict):raise ValueError('primary result is not an object')
        except (ValueError,UnicodeError) as exc:row={};malformed=type(exc).__name__+': '+str(exc)
        if error or primary is None or abandon or malformed:
            marker=run/'attempt-error.json'
            if not marker.exists():write_new(marker,dict(board=record['board'],variant=record['variant'],
                error=str(error or malformed or 'acquisition did not produce a result'),acquisition_passed=False))
        from p4_parity import validated_unavailable
        unavailable=validated_unavailable(record['family'],record['board'],record['variant'],run,row) if not malformed and not abandon and not error else None
        disposition='incomplete' if abandon or primary is None or malformed else 'untouched-noisy' if unavailable and unavailable['kind']=='untouched-noisy' else 'inconclusive' if unavailable else (
            'completed' if row.get('acquisition_passed') is True and not row.get('error') and
                not row.get('cleanup_error') and not row.get('cleanup_errors') and error is None and
                not (run/'analysis-error.json').exists() else 'failed')
        paths=[p for p in (primary,run/'attempt-error.json',run/'analysis-error.json') if p is not None and p.is_file()]
        room=run.with_name(run.name+'-room')
        if room.exists():paths.extend(p for p in room.rglob('*') if p.is_file())
        return append(stream,entries,plan_path,event='finish',run=str(run),disposition=disposition,
            sources={str(p):digest(p) for p in paths})


@contextmanager
def attempt(plan_path,family,board,variant,run,part=None):
    register(plan_path,family,board,variant,run,part)
    try:yield
    except BaseException as exc:
        finish(plan_path,run,error=type(exc).__name__+': '+str(exc));raise
    else:finish(plan_path,run)


def inventory(plan_path):
    with journal(plan_path) as (plan,stream,entries,registered,finished):
        actual=retained_directories(plan,registered)
        require(actual==set(registered),'retained attempt directories and ledger disagree')
        require(set(finished)==set(registered),'unfinished registered attempt; retain and close as incomplete before reporting')
        for row in finished.values():
            for path,sha in row['sources'].items():require(digest(path)==sha,'attempt result changed after ledger disposition')
            if row['disposition']=='untouched-noisy':require(untouched_noise(row['run']),'retry eligibility witness changed')
        return plan,registered,finished


def binding(plan_path,category,board,variant,run):
    _,registered,finished=inventory(plan_path);run=str(Path(run).resolve())
    require(run in registered,'receipt run is absent from attempt ledger')
    row=registered[run];family={'product-pair':'smoke','display-pair':'display'}.get(category,category)
    require((row['family'],row['board'],row['variant'])==(family,board,variant),'receipt/attempt unit mismatch')
    return dict(registration_sha256=row['entry_sha256'],disposition_sha256=finished[run]['entry_sha256'],
        disposition=finished[run]['disposition'],sources=finished[run]['sources'])


def reconcile(plan_path,receipts):
    _,registered,finished=inventory(plan_path)
    supplied={(str(Path(r['run']).resolve()),r['category']) for r in receipts}
    require(all((run,row['family']) in supplied for run,row in registered.items()),
        'table omitted a registered acquisition receipt, including failed/unscored attempts')
    return [dict(registered[run],disposition=finished[run]['disposition']) for run in registered]
