#!/usr/bin/env python3
"""One short exclusive bench hold per bounded EchoEar flood rate."""
import argparse
import json
import os
from pathlib import Path
import time
ROOT=Path(__file__).resolve().parents[1]
os.environ['STEP9A_FIXTURES']=str(ROOT/'hil/fixtures/echoear360')
from esp_serial import EspSerial
from step9a_echoear import bench,verify,flash,check_firmware
from step9a_echoear_wire import bot
from step9a_assets import asset_server
from step9a_echoear_cat import perf
from step9a_support import read,host_address,pack,terminal
from step9a_merge_checks import census_gate,memory_gate,frame_clock,publish_command,rendered_show
from demo_session import prepare_board
from host_nic import snapshot as nic_snapshot,gate as nic_gate


def prepare_flood_board(board,url,budget_path,declared):
    setup=prepare_board(board,url,budget_path)
    if setup['budget']['budget']!=declared['budget']:
        raise RuntimeError('Declared arena differs from demo arena')
    # demo hold can win before the boot worker initializes the LCD. The shared
    # provisioner must establish it before ANY display mode command.
    board.command('disp demo off');board.command('disp character off')
    board.command('disp bench cat 30');board.command('disp defer-persistence 1')
    return setup


def verdict(result):
    samples=result['samples'];clocks=result['clocks'];end=result['completed']
    memory=memory_gate(samples)
    memory['sampled_largest_minimum']=min(s['heap']['internal']['sampled_largest_minimum'] for s in samples)
    memory['passed']=memory['passed'] and memory['sampled_largest_minimum']>=32768
    intervals=[dict(seconds=(b['device_us']-a['device_us'])/1e6,
                    frames=b['display']['rendered']-a['display']['rendered']) for a,b in zip(clocks,clocks[1:])]
    for row in intervals:row['fps']=row['frames']/row['seconds'] if row['seconds']>0 else 0
    start,finish=samples[0],samples[-1]
    counters={k:finish.get('drop_reasons',{}).get(k,0)-start.get('drop_reasons',{}).get(k,0)
              for k in ('admission_budget','admission_shape','admission_heap','invalid_data','message_queue','message_parse')}
    admission={k:finish['data_admission'][k]-start['data_admission'][k] for k in ('received','dispatched','sdk_backpressure')}
    shed=sum(counters.values())
    rate=result['rate'];sent=result['sending_complete']['count'];seconds=result['sending_complete']['elapsed_s']
    rate_ok=rate=='saturating' or abs(sent/seconds-int(rate))<=max(1,int(rate)*.05)
    # Message-plane counters exclude signalling from dispatched, so expose the
    # residual rather than silently claiming exact equality for all RTVI types.
    session_minimum=min(s['heap']['internal']['window_session_minimum_free'] for s in samples if s['heap']['internal'].get('window_session_minimum_free') is not None)
    gates=dict(wire_show=result.get('wire_show',{}).get('accepted') is True and result.get('wire_show_rendered') is True,session_floor=session_minimum>=98304,memory=memory['passed'],alive=bool(intervals) and all(x['frames']>0 and x['seconds']>0 for x in intervals),
               dma=all(s['display']['dma_errors']==0 and not s['display']['dead'] and not s['display']['failed'] for s in samples),
               monitor=result['monitor']['passed'],drained=end['pending_bytes']==0,rate=rate_ok,
               accounted=admission['received']==admission['dispatched']+shed,
               received=admission['received']>=sent,dispatch=rate=='0' or admission['dispatched']>0,
               saturation_shedding=rate!='saturating' or shed>0,
               stack=(result['monitor'].get('app_control_min_free') or 0)>=2048)
    if result.get('prechat_hold_ms'):
        lead=start.get('connect_timings',{}).get('channel_open_us',0)-start.get('first_rtp_before_chat_us',0)
        gates['prechat_media']=(start.get('rtp_before_chat_packets',0)>=50 and lead>=2000000
            and start.get('peer_profile',{}).get('prechat_receive_budget_activations')==1)
    return dict(passed=all(gates.values()),gates=gates,memory=memory,intervals=intervals,
                admission=admission,admission_losses=counters,shed=shed,sent=sent,achieved_rate=sent/seconds,
                session_minimum=session_minimum)


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--rate',choices=['0','25','50','100','saturating'],required=True);p.add_argument('--flash',action='store_true');p.add_argument('--budget',type=Path,required=True);p.add_argument('--chat-delay-ms',type=int,choices=(0,3000),default=0);a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=False)
    import step9a_support
    step9a_support.BUDGET_PATH=a.budget.resolve()
    trigger=a.out/'start';wire=a.out/'display-request.json'
    os.environ.update(OFFLINE_R4_FLOOD_SECONDS='30',OFFLINE_R4_FLOOD_RATE=a.rate,OFFLINE_R4_FLOOD_TRIGGER=str(trigger))
    if a.chat_delay_ms:os.environ['PRECHAT_PROBE_HOLD_MS']=str(a.chat_delay_ms)
    result=dict(passed=False,rate=a.rate,samples=[],clocks=[],prechat_hold_ms=a.chat_delay_ms,
                purpose='Functional B7 board-message stress, not network/acoustic qualification',nic_gate_enforced=False)
    def save():(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    def events():
        path=a.out/'bot/events.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    try:
        with bench():
            if a.flash:flash(a.out)
            with bot(a.out/'bot',wire,entrypoint='prechat_probe_bot.py' if a.chat_delay_ms else 'bot.py'),asset_server(),EspSerial(verify(a.out),log_path=a.out/'serial.log',display_checks=True) as board:
                try:
                    at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at)
                    result['elf_sha256']=check_firmware(board,at)
                    board.command('demo hold',45)
                    declared=json.loads(a.budget.read_text())
                    result['setup']=prepare_flood_board(board,'http://'+host_address()+':7861',a.out/'arena-budget.json',declared)
                    result['census']=census_gate([line for _,_,line in board.lines_since(at)])
                    result['nic_before']=nic_snapshot();board.command('connect',90);time.sleep(2)
                    result['samples'].append(perf(board));result['clocks'].append(frame_clock(board)['perf']);trigger.touch();save()
                    deadline=time.monotonic()+65;next_sample=time.monotonic()+5;show_at=time.monotonic()+10;show_sent=False
                    while time.monotonic()<deadline:
                        time.sleep(1);result['clocks'].append(frame_clock(board)['perf'])
                        if not show_sent and time.monotonic()>=show_at:
                            publish_command(wire,'show',dict(hash=pack()[1]['hash'],layer=7,x=0,y=0),'flood-show')
                            show_sent=True
                            # Cat writes ~30 history records/s: collect the ack
                            # promptly before the 32-entry result ring rolls over.
                            until_ack=time.monotonic()+3
                            while time.monotonic()<until_ack:
                                if any(e['event']=='display-result' and e['result'].get('id')=='flood-show' for e in events()):break
                                time.sleep(.02)
                        rows=events()
                        ack=next((e for e in rows if e['event']=='display-result' and e['result'].get('id')=='flood-show'),None)
                        if ack and 'wire_show' not in result:
                            result['wire_show']=ack['result']
                            completed=terminal(board,'flood-show',timeout=3)
                            status=read(board,'status','DISPLAY');layers=read(board,'status layers','DISPLAY_LAYERS')
                            result['wire_show_proof']=dict(terminal=completed,status=status,layers=layers)
                            result['wire_show_rendered']=rendered_show(completed,status,layers,dict(hash=pack()[1]['hash'],layer=7,x=0,y=0))
                        failed=next((e for e in rows if e['event']=='r4-flood-failed'),None)
                        if failed:raise RuntimeError(str(failed))
                        done=next((e for e in rows if e['event']=='r4-flood-complete'),None)
                        if done:
                            result['completed']=done
                            result['sending_complete']=next(e for e in rows if e['event']=='r4-flood-sending-complete')
                            break
                        if time.monotonic()>=next_sample:
                            result['samples'].append(perf(board));next_sample=time.monotonic()+5;save()
                    else:raise TimeoutError('Flood completion')
                    time.sleep(2);result['clocks'].append(frame_clock(board)['perf']);result['samples'].append(perf(board))
                    result['status']=read(board,'status','DISPLAY');result['monitor']=board.display_monitor.result()
                    result['nic_after']=nic_snapshot();result['nic_observation']=nic_gate(result['nic_before'],result['nic_after'])
                    result['verdict']=verdict(result);result['passed']=result['verdict']['passed'];save()
                finally:
                    board.display_cleanup=True
                    for cmd in ('disp bench off','audio 0','disconnect'):
                        try:board.command(cmd,45)
                        except Exception as exc:result.setdefault('cleanup_errors',[]).append(str(exc))
                    result['monitor']=board.display_monitor.result()
                    result['passed']=result['passed'] and result['monitor']['passed'] and not result.get('cleanup_errors')
    except Exception as exc:
        result['error']=str(exc)
    finally:save()
    print(json.dumps({k:result.get(k) for k in ('passed','rate','error','verdict')}),flush=True)
    return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
