#!/usr/bin/env python3
"""Read-only close-out replay; acquisition tools and evidence stay frozen."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
from pathlib import Path
import sys

DECISION=Path.home()/'src/dg-esp32/proj-2026-10-30-2143/codex-jobs/decision-2026-11-03-step16-prereq-retry.md'
ACQUISITION_DECISION='7efc7e537caa816d9f2bb525f052486f73dff7e387f65fdb516dac48b4b0cf49'
CLOSE_DECISION='28033c92ef8f2b9baaab79a8e14b276af353d6e417861c4ddf1797baa7ff644a'


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def approved_decision():
    raw=DECISION.read_bytes();prefix=raw.split(b'\n## Addendum 2026-11-03 17:05:',1)[0]
    if digest(DECISION)!=CLOSE_DECISION or hashlib.sha256(prefix).hexdigest()!=ACQUISITION_DECISION:
        raise ValueError('close-out decision or immutable acquisition decision prefix changed')


@contextmanager
def decision_runtime():
    """Authenticate the appended decision without rewriting any captured plan.

    Only this exact approved append maps to the original decision digest during
    frozen replay. All other files retain their real hashes. CLI subprocesses
    also inherit the adapter when they import digest from the core module.
    """
    import p4_parity as core
    approved_decision()
    names=('p4_parity','p4_parity_followup','p4_parity_followup_product','p4_parity_followup_merge')
    saved={name:sys.modules[name].digest for name in names if name in sys.modules}
    def acquisition_digest(path):
        if Path(path).resolve()==DECISION.resolve():approved_decision();return ACQUISITION_DECISION
        return digest(path)
    for name in saved:sys.modules[name].digest=acquisition_digest
    try:yield
    finally:
        for name in names:
            module=sys.modules.get(name)
            if module is not None and module.digest is acquisition_digest:module.digest=saved.get(name,saved['p4_parity'])


@contextmanager
def replay_runtime():
    import p4_parity as core
    import p4_parity_campaign as campaign
    import p4_parity_followup as follow
    with decision_runtime(),follow.adapters():
        original_bound,original_journal=core.bound_receipt,campaign.journal
        @contextmanager
        def readonly_journal(path):
            plan=follow.checked_plan(path);ledger=Path(plan['root'])/'attempts.jsonl'
            with ledger.open('r') as stream:
                fcntl.flock(stream,fcntl.LOCK_SH)
                try:yield plan,stream,*campaign.read_entries(stream,path)
                finally:fcntl.flock(stream,fcntl.LOCK_UN)
        def bound(plan_file,category,board,variant,run,peer=None):
            if category!='erle':return original_bound(plan_file,category,board,variant,run,peer)
            run=Path(run).resolve();identity_file=run/'identity.json';original_load=core.load
            def retained_identity(path):
                if Path(path).resolve()==identity_file and not identity_file.exists():
                    return original_load(run/'result.json').get('identity')
                return original_load(path)
            core.load=retained_identity
            try:row=original_bound(plan_file,category,board,variant,run,peer)
            finally:core.load=original_load
            if row['status'] in ('pass','report-only'):
                try:
                    plan=follow.checked_plan(plan_file);result=core.load(run/'result.json')
                    _,registered,_=campaign.inventory(plan_file);registration=registered[str(run)]
                    core.require(result['identity']==plan['product_identity'],'ERLE retained identity differs from planned product bot')
                    core.require(core.load(run/'scored-original.json')==result,'ERLE scored original differs')
                    core.require(result['kind']=='p4-aec-gain' and result['board']=='p4xc' and
                        result['elf_sha256']==plan['images']['p4xc/aec-demo']['elf_sha256'] and
                        type(result['part']) is int and result['part']==registration['part'] and result['gain_db']==30,
                        'ERLE retained image/part/gain differs')
                    binding=registration['input_plan']
                    core.require(digest(binding['file'])==binding['sha256']==result['plan_sha256'],'ERLE measurement-plan binding differs')
                except (ValueError,OSError,KeyError,TypeError) as exc:row.update(status='fail',reason=str(exc))
            row['collection_policy']=dict(kind='step16-close-erle-retained-identity',collector_sha256=digest(__file__),
                decision_sha256=CLOSE_DECISION,identity_source=str(run/'result.json'))
            return row
        core.bound_receipt=bound;campaign.journal=readonly_journal
        try:yield
        finally:core.bound_receipt=original_bound;campaign.journal=original_journal


def close_notes(main_file,follow_file,main_rows,follow_rows):
    import p4_parity as core
    from board_smoke import bounds_for
    from wire_contract import measured,parse_capture
    follow=core.load(follow_file);lines=['','Step 16 close-out notes ([17:05 decision addendum]('+str(DECISION)+')):','']
    pairs=[r for r in follow_rows if r['category']=='product-pair' and r['variant']=='demo' and r['board']=='echoear']
    for pair in pairs:
        if pair['status']!='fail':continue
        core.require('paired p99 exceeds bound' in pair.get('reason',''),'unexpected demo-pair failure; inspect before explaining')
        bounds=bounds_for(Path(__file__).parent/'fixtures/wire_contract_bounds.json','demo');values=[]
        for run in (pair['run'],pair['peer']):
            rows=parse_capture((Path(run)/'resume-serial.log').read_text().splitlines()).intervals
            core.require(len(rows)==1,'one resume interval required for pair explanation')
            values.append(measured(rows[0],bounds)['p99_us']/1000)
        lines+=['- Demo product pair **FAIL**: resume uplink p99 S3 '+f'{values[0]:.0f} ms vs P4 {values[1]:.0f} ms'+
            '; P4 was better. S3 jitter exceeds the unchanged two-sided '+f'{bounds.p99_delta_us/1000:g} ms'+
            ' pair bound. No rerun and no bound change; AEC-specific pair bounds remain a carry-over.']
    for board in core.BOARDS:
        attempts=[('main',r) for r in main_rows if r['category']=='touch' and r['board']==board]+[
            ('follow-up',r) for r in follow_rows if r['category']=='touch' and r['board']==board]
        if not attempts:continue
        pieces=[]
        for role,row in attempts:
            data=core.load(Path(row['run'])/'touch.json');driver=data['after']['driver']
            pieces.append(role+': '+', '.join(k+'='+str(driver[k]) for k in ('interrupts','reads','taps','double_taps'))+
                ' (`'+row['run']+'/touch.json`)')
        core.require(all(r['status']=='unverified' for _,r in attempts),'F11 notes require unverified outcomes')
        lines+=['- F11 '+board+' **unverified**: '+'; '.join(pieces)+'. User-paced redesign and S3 controller check remain carry-overs.']
    erle=[r for r in follow_rows if r['category']=='erle' and r.get('collection_policy')]
    if erle:
        import p4_aec_close
        from p4_aec_image_measure import checked_report
        report_file=Path(follow['root'])/'aec-demo-report/report.json'
        with p4_aec_close.adapters():report=checked_report(report_file)
        core.require(report['identity']==follow['product_identity'],'close-out ERLE report bot differs')
        counts=[count for point in report['points'] for echo in point['echoes'] for count in echo['erle']['saturation']['counts']]
        core.require(counts and not any(counts),'ERLE close-out report has clips')
        lines+=['- ERLE receipts regenerated host-only: retained `result.json` identity is bound to the planned bot and scored original; '
            'the tool does not write `identity.json`. Original `'+follow['root']+'/receipts` retained; replacements are in `'+
            follow['root']+'/receipts-2`. Same acquisitions: '+f"{report['median_erle_db']:.2f} dB, 0 clips"+
            ' ([report]('+str(report_file)+')). This repairs collector scoring; it adds no hardware trial.']
    return '\n'.join(lines)+'\n'


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0]=='collect-all':
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument('action');parser.add_argument('--plan',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
        args=parser.parse_args(argv)
        import p4_parity as core
        with replay_runtime():core.collect_all(args.plan,args.out)
        return 0
    import p4_parity_followup_merge as merge
    original_adapters,original_merge=merge.adapters,merge.merge_rows
    def merged(main_file,follow_file,main_rows,follow_rows,inventory=()):
        return original_merge(main_file,follow_file,main_rows,follow_rows,inventory)+close_notes(main_file,follow_file,main_rows,follow_rows)
    merge.adapters=replay_runtime;merge.merge_rows=merged
    try:
        with decision_runtime():return merge.main(argv)
    finally:merge.adapters=original_adapters;merge.merge_rows=original_merge


if __name__=='__main__':raise SystemExit(main())
