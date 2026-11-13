#!/usr/bin/env python3
"""Thirty real voice turns on the qualified one-hop path; retain every gate."""
import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace
from esp_serial import ROOT
from ethernet_bench import ethernet_bench
from agent_conversation_test import run
from quiet_nic import wait_for_quiet,QuietNic
from netem import Netem
from uplink_continuity import link_tail


def main():
    p=argparse.ArgumentParser();p.add_argument('--forced-reconnect',action='store_true');p.add_argument('--out',type=Path,required=True);p.add_argument('--minutes',type=int,choices=(20,45),default=20);a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    os.environ['STEP8E_TRACE']='1'
    os.environ['STEP8E_EVENT_LOOP_PROBE']='1'
    wait_for_quiet(out/'quiet-preflight.json')
    with Netem('192.168.4.29',['loss','0%'],'both',state=out/'tc-state.json'),QuietNic(out/'quiet-nic.json'),ethernet_bench(out/'ethernet'):
        code=run(SimpleNamespace(out=out/'turns',port='/dev/ttyACM0',runs=1,spl=65,
            elf=ROOT/'build/dg-esp32.elf',turn_limit=None,mic_gain=1600,fault_free=True,
            forced_reconnect=a.forced_reconnect,pause_case=False,calibration_from=None,soak_minutes=a.minutes,interruptions=False,
            restart_regression=False,playback_target=None))
    result=json.loads((out/'turns/result.json').read_text())
    if result.get('acoustic_hold') == 'noise':
        (out/'result.json').write_text(json.dumps(dict(passed=False,acoustic_hold='noise',
            room_baseline=result['room_baseline']),indent=2)+'\n')
        print('ACOUSTIC HOLD: noise',flush=True)
        return 2
    turns=result['turns'];tokens=[t.get('llm_first_token_uplink',[]) for t in turns]
    packets=[json.loads(line) for line in (out/'turns/bot/rtp/rtp.jsonl').read_text().splitlines()]
    observations=result.get('observations',[])
    gate=link_tail(packets,observations[0]['observed_monotonic_s'],observations[-1]['observed_monotonic_s']) if observations else dict(passed=None)
    qualified=len(turns)>=30 and gate['passed'] is True
    from step8e2b_analysis import lcd_memory_gate,voice_device_cadence,voice_owner_gate,perf_history
    memory=lcd_memory_gate(perf_history(out/'turns/serial.log'),reconnects=sum(f.get('kind')=='reconnect' for f in result.get('faults',[])))
    summary=dict(owner_gate=voice_owner_gate(out/'turns'),room_baseline=result.get('room_baseline'),device_send_cadence=voice_device_cadence(out/'turns'),lcd_memory=memory,turns=len(turns),continuity_passed=qualified,conversation_exit_code=code,
                 conversation_passed=result['passed'],error=result.get('error'),tokens=tokens,link_tail=gate,
                 token_windows_basis='Informational windows; primary link-rate gate uses the entire continuous soak.',
                 strict_transport_column=result.get('strict_transport_column'),soak_checks=result.get('soak_checks'))
    (out/'result.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k!='tokens'}),flush=True)
    return 0 if (summary['owner_gate']['passed'] is True and qualified and result['passed'] and memory['passed'] is True
        and summary['device_send_cadence']['target_passed'] is True
        and summary['device_send_cadence']['hard_ceiling_passed'] is True) else 1


if __name__=='__main__':raise SystemExit(main())
