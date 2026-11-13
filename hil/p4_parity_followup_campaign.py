"""Additive A0 retry policy over the frozen plan-bound attempt journal."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import re

import p4_parity_campaign as old
from p4_parity import load,digest,require,output_path

INPUT=ContextVar('followup_a0_input',default=None)
OLD_READ=old.read_entries
OLD_REGISTER=old.register


def retryable(plan,registration,finished):
    """A matching raw, unscored A0 error with clean R7, not a parity retake."""
    if registration['family']!='erle' or finished['disposition']!='failed':return False
    run=Path(registration['run']);path=run/'result.json'
    try:
        row=load(path);binding=registration['input_plan']
        require(finished['sources'].get(str(path))==digest(path),'raw A0 result changed')
        require(digest(binding['file'])==binding['sha256']==row['plan_sha256'],'A0 input plan/result differs')
        image=load(binding['file'])
        require(row.get('kind')=='p4-aec-gain' and row.get('board')=='p4xc' and
            type(row.get('part')) is int and row['part']==registration['part'] and row.get('gain_db')==30 and
            row.get('elf_sha256')==image['elf_sha256']==plan['images']['p4xc/aec-demo']['elf_sha256'] and
            row.get('identity')==plan['product_identity'],'A0 raw identity/part differs')
        require(row.get('status')=='acquisition-error' and row.get('acquisition_passed') is False and row.get('device_untouched') is False and
            row.get('score_produced') is False and row.get('retryable') is True and
            isinstance(row.get('error'),str) and bool(row['error']) and
            not row.get('cleanup_error') and not row.get('cleanup_errors'),'not an unscored retryable acquisition error')
        require(not any((run/n).exists() for n in ('scored-original.json','post-body-payload.json')) and
            not any(k in row for k in ('score','cases','post_body_payload_sha256')),'A0 produced a score')
        from p4_parity_restore import restore_proof
        restore_proof(run,dict(row,bundle=plan['images']['p4xc/aec-demo']['bundle']))
        return True
    except (ValueError,OSError,KeyError,TypeError,UnicodeError):return False


def permitted(plan,registration,finished):
    if finished['disposition']=='untouched-noisy' and old.untouched_noise(registration['run']):return 'untouched-noisy'
    if retryable(plan,registration,finished):return 'unscored-prerequisite-error'
    return None


def read_entries(stream,plan_path):
    from p4_parity_followup import scope
    plan=load(plan_path);stream.seek(0);data=stream.read()
    require(not data or data.endswith('\n'),'truncated attempt ledger')
    entries=[];registered={};finished={};previous='0'*64
    for line in data.splitlines():
        row=json.loads(line);value=dict(row);claimed=value.pop('entry_sha256')
        require(value.get('previous_sha256')==previous and claimed==hashlib.sha256(old.canonical(value)).hexdigest(),
            'attempt ledger hash chain changed')
        require(row.get('plan_sha256')==digest(plan_path) and row.get('sequence')==len(entries)+1,'ledger plan/sequence differs')
        run=row['run']
        if row['event']=='register':
            scope(row['family'],row['board'],row['variant'],row['part'])
            require(row['family'] in old.FAMILIES,'pairs must be derived from registered smokes')
            require(run not in registered and Path(run).resolve().is_relative_to(Path(plan['root'])),'invalid/duplicate attempt')
            require(row['unit']=='/'.join((row['family'],row['board'],row['variant'],str(row['part']))),'attempt unit changed')
            prior=[e for e in registered.values() if e['unit']==row['unit']]
            require(row['attempt']==len(prior)+1 and row['attempt']<=(1 if row['family']=='touch' else 2) and Path(run).name==f"attempt-{row['attempt']:02d}",
                'attempt order/retry cap differs')
            if prior:
                first=prior[-1];end=finished.get(first['run'])
                basis=permitted(plan,first,end) if end else None
                require(basis and row.get('retry_basis')==basis and Path(first['run']).parent==Path(run).parent,
                    'scored/unfinished or non-retryable attempt cannot be replaced')
            if row['family']=='erle':
                binding=row['input_plan'];require(digest(binding['file'])==binding['sha256'],'A0 input plan changed')
                if prior:require(binding==prior[-1]['input_plan'],'A0 retry changed input plan')
            registered[run]=row
        else:
            require(row['event']=='finish' and run in registered and run not in finished,'invalid attempt disposition')
            require(row['disposition'] in ('completed','failed','untouched-noisy','incomplete','inconclusive'),'invalid disposition')
            finished[run]=row
        entries.append(row);previous=claimed
    return entries,registered,finished


def register(plan_path,family,board,variant,run,part=None):
    from p4_parity_followup import scope
    scope(family,board,variant,part)
    require(family in old.FAMILIES,'pairs must be derived from registered smokes')
    with old.journal(plan_path) as (plan,stream,entries,registered,finished):
        path=output_path(plan,run);require(re.fullmatch(r'attempt-\d{2}',path.name),'attempt-NN required')
        unit='/'.join((family,board,variant,str(part)));prior=[e for e in registered.values() if e['unit']==unit]
        require(int(path.name[-2:])==len(prior)+1 and len(prior)<(1 if family=='touch' else 2),'attempt order/retry cap differs')
        basis=None
        if prior:
            first=prior[-1];end=finished.get(first['run']);basis=permitted(plan,first,end) if end else None
            require(basis and Path(first['run']).parent==path.parent,'scored/unfinished or non-retryable attempt cannot be replaced')
        require(old.retained_directories(plan,registered)<=set(registered),'unregistered retained attempt directory')
        fields={}
        if family=='erle':
            binding=INPUT.get();require(binding is not None and digest(binding['file'])==binding['sha256'],'A0 input plan binding required')
            image=load(binding['file'])
            require(image['elf_sha256']==plan['images']['p4xc/aec-demo']['elf_sha256'] and image.get('profile')=='aec-demo',
                'same-image A0 input required')
            if prior:require(binding==prior[-1]['input_plan'],'A0 retry changed input plan')
            fields['input_plan']=binding
        return old.append(stream,entries,plan_path,event='register',run=str(path),unit=unit,family=family,board=board,
            variant=variant,part=part,attempt=len(prior)+1,retry_basis=basis,**fields)


def binding(*args,**kwargs):return old.binding(*args,**kwargs)


@contextmanager
def adapters():
    old.read_entries=read_entries;old.register=register
    try:yield
    finally:old.read_entries=OLD_READ;old.register=OLD_REGISTER


def erle_main(argv):
    import argparse
    from p4_parity_followup import checked_plan
    import p4_parity_erle as entry
    import p4_aec_close
    from p4_aec_image_measure import validate_plan
    parser=argparse.ArgumentParser(add_help=False);parser.add_argument('--image-plan',type=Path,required=True)
    parser.add_argument('--plan',type=Path,required=True);known,_=parser.parse_known_args(argv)
    checked_plan(known.plan)
    with p4_aec_close.adapters():validate_plan(load(known.image_plan))
    token=INPUT.set(dict(file=str(known.image_plan.resolve()),sha256=digest(known.image_plan)))
    previous=entry.checked_plan;entry.checked_plan=checked_plan
    try:return entry.main(argv)
    finally:entry.checked_plan=previous;INPUT.reset(token)
