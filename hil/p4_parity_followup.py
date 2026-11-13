#!/usr/bin/env python3
"""Product follow-up over the byte-frozen step-16 tools; no IO on import."""
import argparse
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
import subprocess
import sys

# CLI entry and imports inside adapters must share the pre-swap originals.
# Otherwise importing this file again after the swap captures checked_plan.
if __name__=='__main__':sys.modules['p4_parity_followup']=sys.modules[__name__]

import p4_parity as core
import p4_parity_campaign as campaign
from p4_parity import load,digest,require,write_new

BASE=Path(__file__).resolve().parent
BASE_COMMIT='42106560112ce5e43dc344c2a1336c51a87f4e65'
DECISION=core.RUN_RULES.with_name('decision-2026-11-03-step16-prereq-retry.md')
DECISION_SHA='7efc7e537caa816d9f2bb525f052486f73dff7e387f65fdb516dac48b4b0cf49'
ORIGINAL_CHECK=core.checked_plan
ORIGINAL_SOURCES=core.source_hashes
ORIGINAL_BOUND=core.bound_receipt


@lru_cache(maxsize=1)
def committed_sources():
    """Read Git objects in s16b, never the running s16 worktree."""
    require(Path(core.__file__).resolve().parent==BASE,'parity imports escaped this worktree')
    names=ORIGINAL_SOURCES()
    tracked=set(subprocess.check_output(['git','ls-tree','-r','--name-only',BASE_COMMIT,'hil'],cwd=BASE.parent,text=True).splitlines())
    import hashlib
    return {name:hashlib.sha256(subprocess.check_output(['git','show',BASE_COMMIT+':hil/'+name],cwd=BASE.parent)).hexdigest()
        for name in names if 'hil/'+name in tracked}


def historical_sources():
    expected=committed_sources()
    for name,sha in expected.items():require(digest(BASE/name)==sha,'frozen main runtime changed: '+name)
    return dict(expected)


@contextmanager
def main_runtime():
    previous=core.source_hashes
    core.source_hashes=historical_sources
    try:yield
    finally:core.source_hashes=previous


def checked_main(path):
    require(load(path).get('source_sha256')==historical_sources(),'main plan is not the frozen step-16 runtime')
    with main_runtime():return ORIGINAL_CHECK(path)


def create_plan(main_file,root,identity_file):
    require(digest(DECISION)==DECISION_SHA,'prerequisite retry decision changed')
    parent=checked_main(main_file);root=Path(root).resolve();old=Path(parent['root']).resolve()
    require(root.is_relative_to(Path.home()/'src/evidence') and not root.is_relative_to(old) and not old.is_relative_to(root),
        'follow-up needs a separate external evidence root')
    identity=load(identity_file)
    from wire_contract import identity_schema
    identity_schema(identity)
    require(all(identity[k]==parent['product_identity'][k] for k in ('bot_source_sha','policy_source_sha','profile_manifest')),
        'follow-up product source/profile must match main; record its current process')
    return dict(schema=1,kind='step16-product-followup-plan',qualification=False,commit=core.COMMIT,
        root=str(root),main_plan_file=str(Path(main_file).resolve()),main_plan_sha256=digest(main_file),
        main_tools_commit=BASE_COMMIT,decision_file=str(DECISION),decision_sha256=DECISION_SHA,
        product_identity_file=str(Path(identity_file).resolve()),product_identity=identity,
        restore_record=parent['restore_record'],restore_record_sha256=parent['restore_record_sha256'],
        pin_verification=parent['pin_verification'],images=parent['images'],variants=list(core.VARIANTS),
        families=['erle','smoke','product-pair','touch'],touch_reruns_per_board=1,llm_content_mode='report-only',
        retry_policy=dict(max_attempts=2,prerequisite='unscored retryable acquisition error; raw result and clean recovery required',
            parity='one untouched noisy preflight retry only; F11 has one attempt per board'),
        rerun_policy='one disclosed main-plan S3 demo failure and one unverified F11 original per board may be re-run; no other duplicate cells',
        source_sha256=ORIGINAL_SOURCES())


def checked_plan(path):
    row=load(path)
    require(row.get('kind')=='step16-product-followup-plan','follow-up plan required')
    require(row==create_plan(row['main_plan_file'],row['root'],row['product_identity_file']),
        'follow-up plan/source/pins/identity selection changed')
    return row


def scope(family,board,variant,part=None):
    require(board in core.BOARDS and variant in core.VARIANTS,'unknown product cell')
    require(family in ('erle','smoke','product-pair','touch'),'follow-up covers product cells and F11 only')
    if family=='touch':require(variant=='demo','F11 uses the pinned demo image')
    if family=='erle':require(board=='p4xc' and variant=='aec-demo' and type(part) is int and part in range(4),'A0 part 0..3 required')
    else:require(part is None,'parity units do not have report parts')


def bound_receipt(plan,category,board,variant,run,peer=None):
    scope(category,board,variant,0 if category=='erle' else None)
    return ORIGINAL_BOUND(plan,category,board,variant,run,peer)


@contextmanager
def adapters():
    """Only scoped function replacement; all frozen files stay byte-identical."""
    from p4_parity_followup_campaign import adapters as ledger
    from p4_parity_followup_product import replay_mode
    previous=core.checked_plan,core.bound_receipt,campaign.checked_plan
    core.checked_plan=checked_plan;core.bound_receipt=bound_receipt;campaign.checked_plan=checked_plan
    try:
        with ledger(),replay_mode():yield
    finally:core.checked_plan,core.bound_receipt,campaign.checked_plan=previous


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    plan=sub.add_parser('plan')
    for name in ('main-plan','root','product-identity','out'):plan.add_argument('--'+name,type=Path,required=True)
    check=sub.add_parser('check');check.add_argument('--plan',type=Path,required=True)
    for action in ('erle','smoke','touch','collect','collect-all','abandon'):
        forward=sub.add_parser(action,add_help=False);forward.add_argument('arguments',nargs=argparse.REMAINDER)
    # REMAINDER subparsers do not consume leading options: split the verb.
    if argv and argv[0] in ('erle','smoke','touch','collect','collect-all','abandon'):
        action=argv.pop(0)
        with adapters():
            if action=='smoke':
                from p4_parity_followup_product import smoke_main
                return smoke_main(argv)
            if action=='touch':
                from p4_parity_followup_product import touch_main
                return touch_main(argv)
            if action=='erle':
                from p4_parity_followup_campaign import erle_main
                return erle_main(argv)
            return core.main([action,*argv])
    a=p.parse_args(argv)
    if a.action=='plan':
        row=create_plan(a.main_plan,a.root,a.product_identity)
        require(a.out.resolve().is_relative_to(Path(row['root'])),'plan output must be in follow-up root')
        write_new(a.out,row)
    else:checked_plan(a.plan);print('Follow-up plan/source/pins authenticated; no hardware accessed')
    return 0


if __name__=='__main__':raise SystemExit(main())
