#!/usr/bin/env python3
"""Real mid-reply downlink loss, with interruptions on in both mute arms."""
import argparse
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from esp_serial import ROOT
from agent_conversation_test import run
from agent_prompts import EXTRA
from netem import Netem,command as tc


def arm(out,disabled,milliseconds,board_ip,calibration=None,long=False):
    out.mkdir(parents=True,exist_ok=False)
    values=dict(STEP8E_DISABLE_BOT_MUTE=str(int(disabled)),BOT_VOICE_POLICY='smartturn',
        BOT_ICE_INTERFACE='wlp8s0',BOT_ICE_REMOTE_NETWORK_ONLY='0',
        BOT_BENCH_SERVER_URL='http://192.168.7.222:7861')
    previous={k:os.environ.get(k) for k in values};os.environ.update(values)
    stop=threading.Event();faults=[];errors=[];seen=set()
    try:
        with Netem(board_ip,['loss','0%'],'downlink',state=out/'tc-state.json') as fault:
            def inject():
                path=out/'turns/bot/events.jsonl'
                try:
                    while not stop.is_set():
                        try:events=[json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []
                        except json.JSONDecodeError:continue
                        for event in events:
                            key=(event['pc_id'],event.get('response_id'))
                            if event['event']!='bot-audio-start' or event.get('response_id') is None or key in seen:continue
                            seen.add(key)
                            if stop.wait(max(0,event['t']+(3 if long else .3)-time.monotonic())):return
                            if long:
                                # Sentence/chunk boundaries can momentarily stop
                                # speaking. Wait for active output at the chosen
                                # mid-reply time rather than injecting in a gap.
                                while not stop.is_set():
                                    try:latest=[e for e in [json.loads(s) for s in path.read_text().splitlines()]
                                        if e['pc_id']==event['pc_id'] and e.get('response_id')==event['response_id']
                                        and e['event'] in ('bot-audio-start','bot-audio-end')]
                                    except json.JSONDecodeError:continue
                                    if latest and latest[-1]['event']=='bot-audio-start':break
                                    if time.monotonic()>event['t']+25:raise TimeoutError('No active long reply at injection point')
                                    stop.wait(.01)
                                if stop.is_set():return
                            row=dict(bot_start=event['t'],pc_id=event['pc_id'],response_id=event['response_id'],
                                requested_ms=milliseconds,begin_s=time.monotonic(),start_delay_s=3 if long else .3)
                            fault.update(['loss','100%'])
                            row['active_s']=time.monotonic()
                            stop.wait(milliseconds/1000)
                            fault.update(['loss','0%']);row['end_s']=time.monotonic()
                            row['tc_counters']=json.loads(tc('-s','-j','qdisc','show','dev','wlp8s0'))
                            faults.append(row)
                            (out/'faults.json').write_text(json.dumps(faults,indent=2)+'\n')
                        stop.wait(.01)
                except Exception as exc:errors.append(f'{type(exc).__name__}: {exc}')
                finally:fault.update(['loss','0%'])
            thread=threading.Thread(target=inject,daemon=True);thread.start()
            try:
                args=SimpleNamespace(out=out/'turns',port='/dev/ttyACM0',runs=1,spl=65,
                    elf=ROOT/'build/dg-esp32.elf',turn_limit=5,mic_gain=1600,fault_free=True,
                    pause_case=False,calibration_from=calibration,soak_minutes=None,interruptions=False,
                    restart_regression=False,playback_target=None)
                if long:args.turn_definitions=[EXTRA[0]]*5;args.recording_seconds=30
                code=run(args)
            finally:stop.set();thread.join(10)
            assert not thread.is_alive(),'Fault injection thread did not finish'
        result=json.loads((out/'turns/result.json').read_text());turns=result['turns']
        rows=[]
        for turn in turns:
            evidence=turn.get('mute_evidence',{})
            injections=[r for r in faults if r['begin_s']>=turn.get('prompt_start',float('inf'))
                    and r['begin_s']<=turn.get('reply_window_transport',{}).get('end',0)]
            for injection in injections:
                injection['mid_reply_start']=any(begin<injection['active_s'] and (end is None or injection['active_s']<end)
                    for begin,end in evidence.get('bot_intervals',[]))
            rows.append(dict(prompt=turn['prompt'],gate_events=turn.get('gate_events'),
                transport=turn.get('reply_window_transport'),mute=evidence,
                injections=injections,
                acoustic_passed=turn['passed']))
        interruptions=sum(len(r['mute'].get('interruptions_during_bot_speech',[])) for r in rows)
        user_turns=sum(len(r['mute'].get('user_turns_during_bot_speech',[])) for r in rows)
        known=len(turns)>=5 and sum(r.get('mid_reply_start',False) for r in faults)>=5 and all(
            r['mute'].get('bot_intervals') and any(i['mid_reply_start'] for i in r['injections']) for r in rows) and not errors
        summary=dict(mute_enabled=not disabled,burst_ms=milliseconds,turns=len(turns),faults=faults,
            long_replies=long,
            interruptions_during_bot=interruptions,user_turns_during_bot=user_turns,
            coverage_passed=known,policy_passed=known and interruptions==0 and user_turns==0,
            conversation_exit_code=code,conversation_error=result.get('error'),errors=errors,replies=rows)
        (out/'result.json').write_text(json.dumps(summary,indent=2)+'\n')
        print(json.dumps({k:v for k,v in summary.items() if k not in ('faults','replies')}),flush=True)
        return summary
    finally:
        for k,v in previous.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    p.add_argument('--board-ip',default='192.168.4.29');p.add_argument('--long-replies',action='store_true');a=p.parse_args();out=a.out.resolve()
    out.mkdir(parents=True,exist_ok=False);results=[];calibration=None
    for ms in (300,800):
        for disabled in (False,True):
            dest=out/f'{ms}ms-mute-{int(not disabled)}'
            result=arm(dest,disabled,ms,a.board_ip,calibration,a.long_replies);results.append(result)
            if (dest/'turns/result.json').exists():
                prior=json.loads((dest/'turns/result.json').read_text())
                if prior.get('calibration'):calibration=dest/'turns'
    (out/'result.json').write_text(json.dumps(results,indent=2)+'\n')
    return 0 if all(r['coverage_passed'] and (not r['mute_enabled'] or r['policy_passed']) for r in results) else 1


if __name__=='__main__':raise SystemExit(main())
