#!/usr/bin/env python3
"""Existing20-minute qualification voice soak, cat throughout, forced360s reconnect/180s tail."""
import argparse,json,os
from pathlib import Path
from types import SimpleNamespace
from agent_conversation_test import run
from esp_serial import ROOT
from ethernet_bench import ethernet_bench
from netem import Netem
from quiet_nic import wait_for_quiet,QuietNic
from step9a_assets import asset_server
from step8e2b_analysis import lcd_memory_gate,perf_history,device_send_cadence,owner_gate,console_windows
from uplink_continuity import link_tail,recorded_audio_idle_windows


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    os.environ['STEP8E_TRACE']='1';os.environ['STEP8E_EVENT_LOOP_PROBE']='1';wait_for_quiet(a.out/'quiet-preflight.json')
    (a.out/'plan.json').write_text(json.dumps({'minutes':20,'forced_reconnect_s':360,'tail_s':180,'concurrent_persist':'306917-byte RAM-only fixture prepared before call, typed persist at timed120s under cat/voice; owned cleanup only','cat':'started before warmup/timed interval, never reissued','idle_probe':'audio0 at tail+30s, audio1 at tail+60s, same connected peer; explicit ACK windows alongside host link','providers':'existing qualification defaults unchanged','retakes':False},indent=2)+'\n')
    with asset_server(),Netem('192.168.4.29',['loss','0%'],'both',state=a.out/'tc-state.json'),QuietNic(a.out/'quiet-nic.json'),ethernet_bench(a.out/'ethernet'):
        code=run(SimpleNamespace(out=a.out/'turns',port='/dev/ttyACM0',runs=1,spl=65,elf=ROOT/'build/dg-esp32.elf',turn_limit=None,mic_gain=1600,fault_free=True,forced_reconnect=True,pause_case=False,calibration_from=None,soak_minutes=20,interruptions=False,restart_regression=False,playback_target=None,display='cat',display_idle_probe=True,display_concurrent_persist=True))
    d=json.loads((a.out/'turns/result.json').read_text());summary={'exit_code':code,'passed':d['passed'],'turns':len(d['turns']),'acoustic_hold':d.get('acoustic_hold'),'display_final':d.get('display_final'),'faults':d.get('faults'),'soak_checks':d.get('soak_checks'),'memory':lcd_memory_gate(perf_history(a.out/'turns/serial.log') if (a.out/'turns/serial.log').exists() else [],reconnects=sum(f.get('kind')=='reconnect' for f in d.get('faults',[])))}
    if d.get('acoustic_hold'):
        (a.out/'result.json').write_text(json.dumps(summary,indent=2)+'\n');print('ACOUSTIC HOLD: noise',flush=True);return 2
    if not d.get('acoustic_hold'):
        packets=[json.loads(line) for line in (a.out/'turns/bot/rtp/rtp.jsonl').read_text().splitlines()];observations=d.get('observations',[])
        if observations:
            start=observations[0]['observed_monotonic_s'];end=observations[-1]['observed_monotonic_s'];idle=recorded_audio_idle_windows(a.out/'turns/serial.log',end=end);summary['link']=link_tail(packets,start,end,excluded_windows=idle)
            serial=a.out/'turns/serial.log';samples=perf_history(serial);trace=[json.loads(line) for p in (a.out/'turns').rglob('*arrivals.jsonl') for line in p.read_text().splitlines()];trace=list({tuple(sorted(r.items())):r for r in trace}.values());first=observations[0]['perf']['device_us'];last=observations[-1]['perf']['device_us'];windows=console_windows(serial)+recorded_audio_idle_windows(serial,clock='device_us',end=last)
            turns=d['turns']
            for before,after in zip(turns,turns[1:]):
                b,c=before['after_perf'],after['before_perf']
                if b['generation']!=c['generation']:windows.append({'start_us':b['device_us'],'end_us':c['device_us'],'category':'reconnect'})
            summary['device_cadence']=device_send_cadence(trace,first,last,windows,target_p99_ms=45,snapshots=samples)
            slow=[r for r in trace if r['kind']==8 and first<=r['device_us']<=last];selected=[p for p in samples if first<=p['device_us']<=last];calls=sum(b['peer_profile']['loop_iteration']['calls']-a['peer_profile']['loop_iteration']['calls'] for a,b in zip(selected,selected[1:]) if a['generation']==b['generation']);overwritten=max((p['peer_profile']['trace']['overwritten'] for p in selected),default=1);sends=[r['device_us'] for r in trace if r['kind']==12 and first<=r['device_us']<=last];covered=bool(sends) and min(sends)-first<=1000000 and last-max(sends)<=1000000 and not overwritten
            summary['owner']=owner_gate(slow,calls,sustained=False,covered=covered,trace=trace,snapshots=samples,excluded_windows=[w for w in windows if w['category']=='reconnect']);summary['device_window_basis']='first through final pooled observation, including entire180-second tail and explicit audio-idle probe; all retained traces; generation transitions excluded'
            summary['idle_probe']={'host_windows':idle,'device_windows':[w for w in windows if w['category']=='idle'],'device_scoring':summary['device_cadence']['idle'],'host_link_basis':summary['link'].get('basis')}

    if d.get('display_timed_before') and d.get('display_final'):
        before,after=d['display_timed_before'],d['display_final'];summary['display_rate']={'interval_s':d['elapsed_s'],'requested':after['updates'][0]-before['updates'][0],'rendered':after['updates'][2]-before['updates'][2],'coalesced':after['updates'][3]-before['updates'][3],'bench_enabled_final':after['bench'][0],'dma':after['dma']};summary['display_rate']['achieved_fps']=summary['display_rate']['rendered']/summary['display_rate']['interval_s']
    (a.out/'result.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps({'passed':summary['passed'],'turns':summary['turns'],'acoustic_hold':summary['acoustic_hold'],'display_rate':summary.get('display_rate')},indent=2),flush=True);return code

if __name__=='__main__':raise SystemExit(main())
