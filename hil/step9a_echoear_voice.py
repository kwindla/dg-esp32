#!/usr/bin/env python3
"""Live voice+cat integration after the EchoEar audio merge; no network changes."""
import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--budget',type=Path,required=True);p.add_argument('--flash',action='store_true');p.add_argument('--runs',type=int,default=3);p.add_argument('--diagnostic-noisy',action='store_true');a=p.parse_args()
    os.environ['DG_BOARD']='echoear';os.environ['STEP9A_FIXTURES']=str(ROOT/'hil/fixtures/echoear360');os.environ['STEP9A_BUDGET_PATH']=str(a.budget.resolve())
    if a.runs<3:p.error('Strict merge requires at least three original runs')
    if os.getenv('ECHOEAR_WIRE_REQUEST'):raise RuntimeError('Legacy wire injector forbidden during voice merge')
    os.environ['PIPECAT_SCTP_MAX_CHUNK_SIZE']='1100'
    os.environ['ECHOEAR_STRICT_MERGE']='1'
    os.environ['DISPLAY_HIL_COMMANDS']=str(a.out.resolve()/'display-command.json')
    from e2e_common import run
    from step9a_echoear import bench,verify,flash,require_echoear_image
    from step9a_assets import asset_server
    from step9a_support import host_address
    require_echoear_image()
    identity=a.out.parent/(a.out.name+'-identity');identity.mkdir(parents=True,exist_ok=False)
    with bench():
        if a.flash:flash(identity)
        port=verify(identity)
        with asset_server():
            code=run(SimpleNamespace(out=a.out,port=port,runs=a.runs,spl=65,warm=True,no_retakes=True,elf=ROOT/'build/dg-esp32.elf',server_url='http://'+host_address()+':7861',ice_servers=[],option=[],display='cat',diagnostic_noisy=a.diagnostic_noisy),True)
    result_path=a.out/'result.json'
    if not result_path.exists():raise RuntimeError('No live integration evidence; a skipped credential check is not a pass')
    result=json.loads(result_path.read_text())
    from step9a_merge_checks import merge_gate
    result['merge_gate']=merge_gate(result,(a.out/'serial.log').read_text(errors='replace'))
    events_path=a.out/'bot/events.jsonl'
    peers=sorted({e['pc_id'] for line in events_path.read_text().splitlines() if (e:=json.loads(line)).get('pc_id')}) if events_path.exists() else []
    result['bot_peer_ids']=peers
    result['merge_gate']['checks']['one_bot_peer']=len(peers)==1
    result['merge_gate']['passed']=result['merge_gate']['passed'] and len(peers)==1
    result['passed']=result.get('passed',False) and result['merge_gate']['passed']
    result_path.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'passed':result.get('passed',False),'acoustic_hold':result.get('acoustic_hold'),'display_monitor':result.get('display_monitor'),'display_final':result.get('display_final')}))
    return code if code else (0 if result.get('passed') else 1)
if __name__=='__main__':raise SystemExit(main())
