#!/usr/bin/env python3
"""Five final real voice turns, with the same NIC and path qualification as soak."""
import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace
from agent_conversation_test import run
from esp_serial import ROOT
from ethernet_bench import ethernet_bench
from netem import Netem
from quiet_nic import QuietNic, wait_for_quiet
from step8e2b_analysis import voice_device_cadence,lcd_memory_gate,perf_history


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--calibration-from',type=Path,required=True)
    args=parser.parse_args();out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    values={'STEP8E_TRACE':'1','STEP8E_EVENT_LOOP_PROBE':'1'}
    previous={key:os.environ.get(key) for key in values};os.environ.update(values)
    try:
        wait_for_quiet(out/'quiet-preflight.json')
        with Netem('192.168.4.29',['loss','0%'],'both',state=out/'tc-state.json'),QuietNic(out/'quiet-nic.json'),ethernet_bench(out/'ethernet'):
            code=run(SimpleNamespace(out=out/'turns',port='/dev/ttyACM0',runs=1,spl=65,
                elf=ROOT/'build/dg-esp32.elf',turn_limit=5,mic_gain=1600,fault_free=True,
                pause_case=False,calibration_from=args.calibration_from,soak_minutes=None,
                interruptions=False,restart_regression=False,playback_target=None))
        result=json.loads((out/'turns/result.json').read_text())
        summary={key:result.get(key) for key in ('passed','error','elf_sha256','playback_target_dbfs','summary')}
        summary['device_send_cadence']=voice_device_cadence(out/'turns')
        summary['lcd_memory']=lcd_memory_gate(perf_history(out/'turns/serial.log'),reconnects=None)
        summary['turns']=len(result['turns'])
        (out/'result.json').write_text(json.dumps(summary,indent=2)+'\n')
        print(json.dumps(summary),flush=True)
        return code
    finally:
        for key,value in previous.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value


if __name__=='__main__':raise SystemExit(main())
