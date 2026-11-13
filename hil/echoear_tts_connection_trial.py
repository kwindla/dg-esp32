"""Declared step-8d websocket-state A/B; board client, raw MIC1, no acoustic gate."""
import json, os, re, signal, subprocess, time, traceback, wave
from pathlib import Path
from echoear_aec_bench import ROOT, NORMAL, NORMAL_HASH, bench_lock, verify_bundle, flash, boot, write, sha
from echoear_aec_bench import HISTORICAL_NORMAL_9936DA45 as NORMAL, HISTORICAL_NORMAL_9936DA45_HASH as NORMAL_HASH   # gap 3: step-9 AEC family stays bound to its declared 9936da45 restore
from echoear_aec_resource_trial import command, snapshot
from echoear_aec_accept import rows, control
from speaker_dual_sweep_test import dump_mic
from step7_support import BotServer
from step9a_support import host_address
from step9a_echoear import check_esptool


def validate(plan):
    assert plan['status']=='declared' and plan['noise_gate'] is None
    sessions=plan['sessions']
    assert len(sessions)>=16 and [s['arm'] for s in sessions]==list('AB'*(len(sessions)//2))
    assert all(len(s['texts'])>=4 and s['texts']==sessions[0]['texts'] for s in sessions)
    assert plan['long_state_s']==.8 and plan['trim_leading'] is False
    assert plan['volume_db'] in (0,-6)


def send(directory,number,op,cid):
    requested=time.monotonic()
    control(directory/'command.json',dict(number=number,op=op,case_id=cid))
    expected='script-queued' if op=='speak' else 'case-sealed'
    deadline=requested+20
    while time.monotonic()<deadline:
        events=rows(directory/'events.jsonl')
        if any(e.get('event')=='evaluation-error' for e in events):raise RuntimeError('Evaluation error')
        if any(e.get('number')==number and e['event']==expected and e['t']>=requested for e in events):return requested
        time.sleep(.02)
    raise TimeoutError('Evaluation command completion')


def run(plan_path,out):
    plan=json.loads(Path(plan_path).read_text());validate(plan)
    out=Path(out);out.mkdir(exist_ok=False);write(out/'declaration.json',plan)
    write(out/'identity.json',dict(commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),runner_sha256=sha(__file__)))
    image=Path(plan['image']);h=plan['elf_sha256'];verify_bundle(image,h,0);verify_bundle(NORMAL,NORMAL_HASH);check_esptool()
    result=dict(completed=False,sessions=[],cleanup_errors=[]);board=bot=None
    def interrupted(sig,frame):raise KeyboardInterrupt('Hold stopped')
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
    with bench_lock():
        subprocess.run(['alsactl','-f',str(out/'mixer-before.state'),'store','0'],check=True,capture_output=True)
        try:
            flash(image,h,out,'candidate',0);board=boot(out,'candidate',h)
            for c in ('demo hold','audio 0','quiet 1','gate auto','aec-ref-gain 15','aec-guard-ms 400',f"playback-volume {plan['volume_db']}",'peer-option arrival-trace 0'):
                command(board,c,40)
            command(board,'server-url http://'+host_address()+':7861')
            for spec in plan['sessions']:
                folder=out/spec['id'];folder.mkdir();evaluation=folder/'evaluation';evaluation.mkdir()
                scripts=[dict(id=f'g{i+1}',text=text) for i,text in enumerate(spec['texts'])]
                write(evaluation/'protocol.json',dict(echo_replies=scripts,barge_ids=[],barge_bot_text='unused'))
                os.environ.pop('ECHOEAR_AEC_FULL_DUPLEX',None)
                os.environ.update(ECHOEAR_LIVENESS_SECONDS='0.5',AUDIO_OUT_10MS_CHUNKS='4',ECHOEAR_AEC_EVALUATION_DIR=str(evaluation),ECHOEAR_AEC_SESSION_DIR=str(folder/'sessions'),RTP_EVIDENCE_DIR=str(folder/'rtp'),RTP_EVIDENCE_MAX_PACKETS='200000',DEMO_LLM='local',OFFLINE_SKIP_GREETING='1',DEMO_TTS_PROACTIVE_RECONNECT='0',DEMO_TTS_TRIM_LEADING='0',DEMO_TTS_RECONNECT_EACH_GENERATION='1' if spec['arm']=='B' else '0',DEEPGRAM_TTS_VOICE='flux-kelsey-en',DEEPGRAM_TTS_SPEED='1.5',DEEPGRAM_TTS_EXPRESSIVITY='2')
                bot=BotServer(folder/'bot',entrypoint='demo_bot.py');bot.start()
                at,_=command(board,'connect',90);board.wait_for('STATE Ready',20,since=at)
                command(board,'audio 1');time.sleep(3)
                session=dict(id=spec['id'],arm=spec['arm'],generations=[],completed=False)
                result['sessions'].append(session);write(out/'result.json',result)
                snapshot(board,folder,'before')
                for i,script in enumerate(scripts):
                    cid=script['id'];command(board,'mic-record 1');capture_start=time.monotonic();time.sleep(.3)
                    requested=send(evaluation,i*2+1,'speak',cid)
                    deadline=capture_start+25
                    while time.monotonic()<deadline:
                        events=[e for e in rows(evaluation/'events.jsonl') if e.get('case_id')==cid]
                        if any(e.get('frame') in ('ErrorFrame','FatalErrorFrame') or e['event']=='evaluation-error' for e in events):raise RuntimeError('Generation error')
                        if any(e.get('frame')=='BotStoppedSpeakingFrame' and e['t']>requested for e in events):break
                        time.sleep(.05)
                    else:raise TimeoutError('Generation did not finish inside raw capture window')
                    time.sleep(1);command(board,'mic-record 0');capture_end=time.monotonic()
                    send(evaluation,i*2+2,'seal',cid)
                    dump_mic(board,folder/(cid+'-raw-mic1.wav'))
                    with wave.open(str(folder/(cid+'-raw-mic1.wav'))) as w:duration=w.getnframes()/w.getframerate()
                    assert duration<30 and abs(duration-(capture_end-capture_start))<.3,'Incomplete raw MIC1 window'
                    entry=dict(id=cid,request_s=requested,raw_capture_start_s=capture_start,raw_capture_end_s=capture_end,raw_duration_s=duration)
                    session['generations'].append(entry);write(out/'result.json',result)
                    print('GENERATION',spec['id'],cid,'complete',flush=True)
                snapshot(board,folder,'after');command(board,'audio 0');command(board,'disconnect',40)
                time.sleep(1);bot.stop();bot=None;session['completed']=True;write(out/'result.json',result)
                print('SESSION',spec['id'],spec['arm'],'complete',flush=True)
            result['completed']=True
        except BaseException as exc:
            result['error']=type(exc).__name__+': '+str(exc);(out/'exception.txt').write_text(traceback.format_exc());print('ERROR',result['error'],flush=True)
        finally:
            for label,fn in [('bot',lambda:bot.stop() if bot else None),('serial',lambda:board.close() if board else None)]:
                try:fn()
                except Exception as exc:result['cleanup_errors'].append(label+': '+str(exc))
            try:
                flash(NORMAL,NORMAL_HASH,out,'restore');restored=boot(out,'restore',NORMAL_HASH);restored.close();result['restored_elf_sha256']=NORMAL_HASH
            except Exception as exc:result['cleanup_errors'].append('RESTORE: '+str(exc))
            try:
                subprocess.run(['alsactl','-f',str(out/'mixer-before.state'),'restore','0'],check=True,capture_output=True)
                subprocess.run(['alsactl','-f',str(out/'mixer-after.state'),'store','0'],check=True,capture_output=True)
                result['mixer_equal']=(out/'mixer-before.state').read_bytes()==(out/'mixer-after.state').read_bytes()
            except Exception as exc:result['cleanup_errors'].append('mixer: '+str(exc))
            write(out/'result.json',result)
            if result.get('restored_elf_sha256')!=NORMAL_HASH:
                print('RESTORE FAILED: retaining lock',flush=True)
                while True:time.sleep(30)
    result['lock_released']=True;write(out/'result.json',result);print('DONE',result['completed'],flush=True)


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('plan');p.add_argument('output');a=p.parse_args();run(a.plan,a.output)
