#!/usr/bin/env python3
"""One live reply plus late silence; diagnostic only, under bench.py."""
import argparse
import json
import os
import statistics
import subprocess
from pathlib import Path
from echoear_transport_analysis import rows, gaps
from step8e2a_analysis import receive_audit


def analyze(sample, trace, sent, commands, anchors, export_trace=None):
    snapshots = (sample.get('before_perf'), sample.get('reply_window_transport', {}).get('after'), sample.get('after_perf'))
    if not all(snapshots) or not anchors:
        return dict(available=False, reason='Missing acquisition endpoints or clock anchors')
    offsets = [x['host_s']-x['device_us']/1e6 for x in anchors]
    offset = statistics.median(offsets)
    result = dict(available=True, qualification=False, phases=[],
                  anchor_spread_ms=1000*(max(offsets)-min(offsets)))
    for name, before, after in zip(('reply', 'late-silence'), snapshots, snapshots[1:]):
        lo, hi = before['device_us'], after['device_us']
        start, end = lo/1e6+offset, hi/1e6+offset
        overwrite = after['peer_profile']['trace']['overwritten']-before['peer_profile']['trace']['overwritten']
        # Coverage must exist at both ends, not merely somewhere in the archive.
        covered = all(any(r['kind']==kind and r['device_us']<=lo for r in trace) and
                      any(r['kind']==kind and r['device_us']>=hi for r in trace) for kind in (1,14))
        same_session = before.get('pc_id') is not None and before.get('pc_id')==after.get('pc_id') and before.get('generation')==after.get('generation')
        enabled = before['peer_profile']['trace'].get('enabled') is True and after['peer_profile']['trace'].get('enabled') is True
        export_clean = export_trace is not None and export_trace.get('overwritten')==before['peer_profile']['trace']['overwritten']
        complete = covered and overwrite == 0 and same_session and enabled and export_clean
        result['available'] &= complete
        phase = dict(name=name, before_us=lo, after_us=hi, trace_complete=complete,
                     overwritten_delta=overwrite, export_verified=export_clean, host_window=[start,end],
                     counter_deltas={k:after[k]-before[k] for k in ('lost','concealed','drift_drops','drift_inserts','i2s_underflows','i2s_write_errors')},
                     commands=[c for c in commands if c['start']<end and c['end']>start],
                     host_downlink=gaps(sent,'send_ms',.001,start,end),
                     receive=receive_audit(trace,sent,lo,hi))
        phase['concealment_boundaries']=[]
        phase['receive']['stalls_over_reserve']=[s for s in phase['receive']['stalls_over_reserve'] if s['before_us']<hi and s['after_us']>lo]
        for render in trace:
            if render['kind']!=14 or render['flags']!=1 or not lo<=render['device_us']<=hi:continue
            matches = [r for r in trace if (r['reserved'],r['ssrc'],r['timestamp']) ==
                       (render['reserved'],render['ssrc'],render['timestamp'])]
            phase['concealment_boundaries'].append(dict(render=render,
                boundaries={str(kind):[r['device_us'] for r in matches if r['kind']==kind] for kind in (1,2,13)},
                server=[r for r in sent if (r['ssrc'],r['timestamp'])==(render['ssrc'],render['timestamp'])]))
        result['phases'].append(phase)
    result['limits']='Socket reads follow radio/driver/lwIP buffering; no one-way latency or RF cause inferred. Render timestamps are completion, not DAC/decode deadlines. Diagnostic never changes turn verdict.'
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--budget',type=Path,required=True)
    p.add_argument('--flash',action='store_true');a=p.parse_args()
    if not os.getenv('DG_BENCH_LOCK_FD'):p.error('Run under hil/bench.py after B releases the bench')
    from esp_serial import ROOT
    a.out.mkdir(parents=True,exist_ok=False)
    os.environ.update(DG_BOARD='echoear',ECHOEAR_STRICT_MERGE='1',PIPECAT_SCTP_MAX_CHUNK_SIZE='1100',
        STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'),STEP9A_BUDGET_PATH=str(a.budget.resolve()),
        DISPLAY_HIL_COMMANDS=str(a.out/'display-command.json'),RTP_EVIDENCE_DIR=str(a.out/'rtp'),STEP8E_EVENT_LOOP_PROBE='1')
    # Display modules bind fixture/budget paths when imported.
    from echoear_transport_probe import TimedSerial
    from step7_support import BotServer
    from step9a_assets import asset_server
    from step9a_support import host_address
    from e2e_common import turn
    from alsa import resolve_jabra
    result=dict(qualification=False,purpose='One arrival-traced original reply and late silence, no retakes or soak')
    try:
        if a.flash:
            with (a.out/'flash.log').open('w') as log:
                subprocess.run([str(ROOT/'scripts/idf.sh'),'flash'],check=True,stdout=log,stderr=subprocess.STDOUT,
                               pass_fds=(int(os.environ['DG_BENCH_LOCK_FD']),))
        with asset_server(), BotServer(a.out/'bot','live-test') as bot, TimedSerial(log_path=a.out/'serial.log',display_checks=True) as board:
            board.e2e_display_trace=True
            board.e2e_defer_trace=True # Only export before prompt and after recording.
            _,device=resolve_jabra()
            result['turn']=turn(board,bot,device,a.out/'run1',True,elf=ROOT/'build/dg-esp32.elf',
                options=('arrival-trace=1',),server_url='http://'+host_address()+':7861',display='cat')
            result['export_trace']=getattr(board,'e2e_export_trace',None)
            result['display_monitor']=board.display_monitor.result()
            board.display_cleanup=True;board.command('disp bench off')
        result['analysis']=analyze(result['turn'],rows(a.out/'run1/arrivals.jsonl'),rows(a.out/'rtp/downlink.jsonl'),
                                   rows(a.out/'commands.jsonl'),rows(a.out/'serial.audio.jsonl'),result['export_trace'])
        result['pa_events']=[line for line in (a.out/'serial.log').read_text().splitlines() if 'echoear-audio: PA active=' in line]
    except BaseException as exc:
        result['error']=f'{type(exc).__name__}: {exc}'
        raise
    finally:
        (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(dict(qualification=False,turn_passed=result['turn']['passed'],trace_complete=result['analysis']['available'])))
    return 0 if result['analysis']['available'] else 1


if __name__=='__main__':raise SystemExit(main())
