"""Authenticate captured fixture paths when replay moves between worktrees."""
import argparse
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import sys


@contextmanager
def report_fixtures(plan_file):
    import p4_aec_policy as policy
    from make_bundle import digest
    from wire_contract import require
    plan=json.loads(Path(plan_file).read_text());captured=plan['fixtures'];current=policy.fixtures()
    require(len(captured)==len(current),'captured fixture definitions differ')
    for old,new in zip(captured,current):
        require({k:v for k,v in old.items() if k!='path'}=={k:v for k,v in new.items() if k!='path'},
            'captured fixture definitions differ')
        require(digest(old['path'])==old['sha256'],'captured fixture source changed')
    original=policy.fixtures
    policy.fixtures=lambda:copy.deepcopy(captured)
    try:yield
    finally:policy.fixtures=original


def table_main(argv=None):
    from p4_step16_close import main
    argv=list(sys.argv[1:] if argv is None else argv)
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('--follow-up-plan',type=Path,action='append',default=[])
    known,_=parser.parse_known_args(argv)
    if len(known.follow_up_plan)==1:
        plan=json.loads(known.follow_up_plan[0].read_text())
        measurement=Path(plan['root'])/'aec-demo-report/plan.json'
        if measurement.is_file():
            with report_fixtures(measurement):return main(argv)
    return main(argv)
