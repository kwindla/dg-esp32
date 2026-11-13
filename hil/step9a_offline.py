#!/usr/bin/env python3
"""Unchanged offline acoustic acceptance with the production cat bench enabled."""
import argparse,json,os
from pathlib import Path
from types import SimpleNamespace
from e2e_common import run
from esp_serial import ROOT
from ethernet_bench import ethernet_bench
from netem import Netem
from quiet_nic import wait_for_quiet,QuietNic
from step9a_assets import asset_server
from step8e2b_analysis import lcd_memory_gate,perf_history,device_send_cadence,owner_gate,console_windows
from uplink_continuity import link_tail,recorded_audio_idle_windows


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    wait_for_quiet(a.out/'quiet-preflight.json')
    with asset_server(),Netem('192.168.4.29',['loss','0%'],'both',state=a.out/'tc-state.json'),QuietNic(a.out/'quiet-nic.json'),ethernet_bench(a.out/'ethernet'):
        code=run(SimpleNamespace(out=a.out/'test',port='/dev/ttyACM0',runs=5,spl=65,no_retakes=True,warm=True,elf=ROOT/'build/dg-esp32.elf',server_url=os.environ['BOT_BENCH_SERVER_URL'],ice_servers=[os.environ['BOT_BENCH_STUN_URL']],option=['arrival-trace=1','call-timing=1'],display='cat'),False)
    d=json.loads((a.out/'test/result.json').read_text());summary={'exit_code':code,'passed':d['passed'],'pass_count':d.get('pass_count'),'acoustic_hold':d.get('acoustic_hold'),'display_final':d.get('display_final'),'memory':lcd_memory_gate(perf_history(a.out/'test/serial.log'),reconnects=None)}
    summary['link']=d.get('link_tail');summary['turn_audio_gates']=[]
    for index,row in enumerate(d.get('runs',[]),1):
        folder=a.out/'test'/('run'+str(index));trace_file=folder/'arrivals.jsonl'
        if not trace_file.exists() or 'before_perf' not in row or 'after_perf' not in row:
            summary['turn_audio_gates'].append({'run':index,'passed':None,'reason':'incomplete turn/trace'});continue
        trace=[json.loads(line) for line in trace_file.read_text().splitlines()];first,last=row['before_perf'],row['after_perf'];samples=perf_history(a.out/'test/serial.log');start,end=first['device_us'],last['device_us'];windows=console_windows(a.out/'test/serial.log')+recorded_audio_idle_windows(a.out/'test/serial.log',clock='device_us',end=end)
        cadence=device_send_cadence(trace,start,end,windows,target_p99_ms=45,snapshots=samples);slow=[r for r in trace if r['kind']==8 and start<=r['device_us']<=end];calls=last['peer_profile']['loop_iteration']['calls']-first['peer_profile']['loop_iteration']['calls'];owner=owner_gate(slow,calls,covered=last['peer_profile']['trace']['overwritten']==0,trace=trace,snapshots=samples)
        summary['turn_audio_gates'].append({'run':index,'device_cadence':cadence,'owner':owner,'passed':cadence['target_passed'] and cadence['hard_ceiling_passed'] and owner['passed']})
    (a.out/'result.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps({'passed':summary['passed'],'pass_count':summary['pass_count'],'acoustic_hold':summary['acoustic_hold']},indent=2),flush=True);return code

if __name__=='__main__':raise SystemExit(main())
