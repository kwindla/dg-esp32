"""Read-only authentication of main + one product follow-up, with disclosure."""
import argparse
from contextlib import contextmanager
import fcntl
from pathlib import Path

import p4_parity as core
import p4_parity_campaign as campaign
from p4_parity import load,digest,require
from p4_parity_followup import checked_main,checked_plan,main_runtime,adapters,DECISION,DECISION_SHA


@contextmanager
def readonly_main():
    previous=campaign.journal
    @contextmanager
    def journal(path):
        plan=checked_main(path);ledger=Path(plan['root'])/'attempts.jsonl'
        require(ledger.is_file(),'main ledger must already exist; never create or change main evidence')
        with ledger.open('r') as stream:
            fcntl.flock(stream,fcntl.LOCK_SH)
            try:yield plan,stream,*campaign.read_entries(stream,path)
            finally:fcntl.flock(stream,fcntl.LOCK_UN)
    campaign.journal=journal
    try:
        with main_runtime():yield
    finally:campaign.journal=previous


def cell(row):return row['board'],row['variant'],row['category']


def groups(rows):
    result={};seen=set()
    for row in rows:
        identity=(row['category'],row['run'],row.get('peer'))
        require(identity not in seen,'duplicate receipt/original');seen.add(identity)
        if row['category']=='erle':continue  # Prerequisites are retained, not parity cells.
        result.setdefault(cell(row),[]).append(row)
    for key,values in result.items():
        if len(values)>1:
            require(key[2]=='smoke' and len(values)==2 and len({r['run'] for r in values})==2 and
                sum(r['attempt_binding']['disposition']=='untouched-noisy' for r in values)>=1,
                'duplicate/conflicting parity cell')
    return result


def merge_rows(main_file,follow_file,main_rows,follow_rows,inventory=()):
    """Inputs have been authenticated/reconciled by render; no best-of selection."""
    require(digest(DECISION)==DECISION_SHA,'merge decision changed')
    for row in follow_rows:
        require(row['category'] in ('smoke','product-pair','erle','touch') and row['variant'] in core.VARIANTS,
            'follow-up may fill product cells and F11 only')
        if row['category']=='touch':require(row['variant']=='demo','F11 requires demo')
        if row['category']=='erle':require(row['board']=='p4xc' and row['variant']=='aec-demo','unexpected follow-up report')
    left,right=groups(main_rows),groups(follow_rows);disclosed=[];touch_disclosed=[];exclude=set()
    for key in left.keys()&right.keys():
        original=left[key]
        if key[2]=='touch':
            require(key[1]=='demo' and len(original)==1 and original[0]['status']=='unverified' and
                original[0]['attempt_binding']['disposition']=='completed' and len(right[key])==1,
                'only one unverified main F11 attempt per board may be re-run')
            exclude.update(id(r) for r in original);touch_disclosed.extend(original)
            continue
        failures=[r for r in original if r['status']=='fail' and r['attempt_binding']['disposition']=='failed']
        require(key==('echoear','demo','smoke') and len(failures)==1 and
            all(r in failures or r['attempt_binding']['disposition']=='untouched-noisy' for r in original),
            'conflicting main/follow-up product cell; only the disclosed S3 demo failure may be re-run')
        # Preserve FAIL until the authorized one new parity original has an outcome.
        measured=[r for r in right[key] if r['attempt_binding']['disposition'] in ('completed','failed') and r['status']!='unverified']
        require(len(measured)<=1,'more than one scored S3 demo re-run')
        if measured:exclude.update(id(r) for r in original)
        disclosed.extend(failures)
    selected=[r for r in main_rows if id(r) not in exclude]+list(follow_rows)
    stripped=[{k:v for k,v in r.items() if k not in ('plan_file','plan_sha256')} for r in selected]
    text=core.render(stripped)
    plans=[('main',Path(main_file).resolve()),('follow-up',Path(follow_file).resolve())]
    def cite(role,path):return '['+role+' plan '+digest(path)[:12]+']('+str(path)+')'
    heading=['Explicit plan merge: '+', '.join(cite(*p) for p in plans)+'.',
        'Merge rule: the follow-up fills product smoke/pair cells and one F11 touch re-run per board; liveness/display remain from the main plan.',
        'Only one main-plan S3 demo failure may be re-run; the unverified F11 attempts get one re-run per board; every other duplicate/conflicting parity cell is refused.',
        'Prerequisite retries and the S3 demo re-run were authorized **after the failures**, under [the binding decision and addendum]('+str(DECISION)+').',
        'F11 re-runs were authorized after both main attempts were unverified; those originals stay on record.',
        'AEC-off and AEC follow-up smokes use LLM-content-only report mode; STT, completion, timing, resources, interruptions and restore remain strict.','']
    lines=text.splitlines();lines[2:2]=heading
    for i,line in enumerate(lines):
        if not line.startswith('| '):continue
        feature=line.split(' ',2)[1]
        if feature not in {f[0] for f in core.FEATURES}:continue
        citations=[cite(*plans[0])]
        if any(feature in r.get('features',[]) for r in follow_rows):citations.append(cite(*plans[1]))
        lines[i]=line[:-1].rstrip()+'; '+', '.join(citations)+' |'
    a0_failures=[r for r in main_rows if r['category']=='erle' and r['board']=='p4xc' and
        r['variant']=='aec-demo' and r['status']=='fail']
    if a0_failures:
        lines+=['','Disclosed main-plan A0 prerequisite failure:','']
        for row in a0_failures:
            lines+=['- Main **FAIL** retained: `'+row['run']+'`. Reason: '+row.get('reason','unscored prerequisite acquisition failed').replace('|','\\|')]
        lines+=['- The original remains in the main ledger and receipts; the follow-up prerequisite retry policy was authorized after this failure.','']
    if disclosed:
        lines+=['','Disclosed main-plan S3 demo failure and authorized follow-up re-run:','']
        for row in disclosed:
            lines+=['- Main **FAIL** retained: `'+row['run']+'`. Reason: '+row.get('reason','retained failed smoke').replace('|','\\|')]
        lines+=['- The failed original remains in the main ledger and receipts. The follow-up supplies at most one scored re-run; it does not reclassify the original.','']
    if touch_disclosed:
        lines+=['','Disclosed main-plan F11 unverified attempts and one follow-up per board:','']
        for row in touch_disclosed:
            lines+=['- Main **unverified** retained: `'+row['run']+'`. Reason: '+row.get('reason','no complete physical double-tap witness').replace('|','\\|')]
        lines+=['- Each board gets one new attempt, with gesture instructions before the window and an immediate TAP NOW prompt. No main attempt is reclassified.','']
    lines+=['','All originals from both plans, including prerequisite failures and eligible retries:','']
    for role,row in inventory:lines.append('- '+role+' `'+row['run']+'`: '+row['disposition'])
    return '\n'.join(lines)+'\n'


def render(main_file,follow_file,receipt_paths):
    main=checked_main(main_file);follow=checked_plan(follow_file)
    require(Path(follow['main_plan_file']).resolve()==Path(main_file).resolve() and follow['main_plan_sha256']==digest(main_file),
        'follow-up belongs to another main plan')
    require(main['images']==follow['images'],'merge pins differ')
    paths=[Path(p).resolve() for p in receipt_paths];require(len(paths)==len(set(paths)),'duplicate receipt path')
    main_paths=[];follow_paths=[]
    for path in paths:
        row=load(path);planned=Path(row['plan_file']).resolve()
        if planned==Path(main_file).resolve():main_paths.append(path)
        elif planned==Path(follow_file).resolve():follow_paths.append(path)
        else:raise ValueError('receipt belongs to an unselected third plan')
    with readonly_main():
        main_rows=[core.receipt(p) for p in main_paths]
        main_inventory=campaign.reconcile(main_file,main_rows)
    with adapters():
        follow_rows=[core.receipt(p) for p in follow_paths]
        follow_inventory=campaign.reconcile(follow_file,follow_rows)
    return merge_rows(main_file,follow_file,main_rows,follow_rows,
        [('main',r) for r in main_inventory]+[('follow-up',r) for r in follow_inventory])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--follow-up-plan',type=Path,action='append',default=[])
    parser.add_argument('--receipt',type=Path,action='append',default=[])
    parser.add_argument('--out',type=Path,required=True)
    a=parser.parse_args(argv);require(len(a.follow_up_plan)<=1,'at most one follow-up plan may be merged')
    if a.follow_up_plan:content=render(a.plan,a.follow_up_plan[0],a.receipt)
    else:
        with readonly_main():
            checked_main(a.plan);content=core.render([core.receipt(p) for p in a.receipt],a.plan)
    a.out.parent.mkdir(parents=True,exist_ok=True)
    with a.out.open('x') as stream:stream.write(content)
    return 0


if __name__=='__main__':raise SystemExit(main())
