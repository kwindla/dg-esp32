"""Declared barge-in block acquisition for the future step-9 campaign.

No acquisition on import. A reviewed run declaration supplies image/fixture hashes,
counts, offsets and the frozen gate. Preparation does not qualify the full campaign.
"""
import sys,os,json,time,re,subprocess,traceback,shutil,threading,signal
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'hil'),str(ROOT/'server')]
from echoear_aec_bench import *
from echoear_aec_stimulus import schedule_from_tts,set_guard_if_changed,TransportEvents,StimulusTimingFailure
from echoear_aec_event_tail import ContextIdentity
from echoear_aec_capture import dump_capture
from echoear_aec_timing import require_bracketed_capture
from echoear_aec_metrics import frame_power
from echoear_aec_noise_gate import quiet_gate,validate_noise_gate,NoiseRetryQueue
from echoear_aec_resource_trial import command,snapshot
from echoear_aec_resources import check_directory,check_reply_resources,quiescent_admission_snapshot
from echoear_aec_checks import live_admission,outgoing_rtvi_count
from sctp_recovery import check_active_recovery
from step7_support import BotServer
from step9a_support import host_address,read,request,terminal
from step9a_echoear import check_esptool
from alsa import resolve_jabra,play_wav,set_volumes

def rows(path):
 p=Path(path);return [json.loads(x) for x in p.read_text().splitlines(keepends=True) if x.endswith('\n')] if p.exists() else []
def control(path,data):
 tmp=path.with_suffix('.tmp');write(tmp,data);os.replace(tmp,path)

def boundary(board,folder,label):
 def sample(i):
  value=snapshot(board,folder,f'{label}-boundary-{i}')
  print('BOUNDARY',label,i,json.dumps(value.get('data_admission',{})),flush=True)
  return value
 endpoint,observations=quiescent_admission_snapshot(sample)
 write(folder/(label+'.json'),endpoint)
 return endpoint,observations

def require_zero_i2s(snapshots):
 for item in snapshots:
  for key in ('i2s_underflows','i2s_write_errors','i2s_write_timeouts'):
   if item.get(key) != 0:
    raise RuntimeError(f'Whole-session {key} must be zero, got {item.get(key)}')


def validate_plan(plan):
 if plan.get('status') != 'declared':
  raise ValueError('A declared run is required; the step-9 specification is still a draft')
 if plan.get('qualification') and plan.get('review_approval') != 'Astra-approved':
  raise ValueError('Step-9 acquisition requires its reviewed frozen specification')
 if plan.get('attempts_per_cell') != 1:
  raise ValueError('No automatic acceptance/performance retakes')
 if not plan.get('cases'):
  raise ValueError('Declare cells before acquiring')
 for cell in plan['cases']:
  if cell['guard_ms'] != 400 or cell['volume_db'] not in (0,-6):
   raise ValueError('Frozen guard/volume changed')
  offset=cell['stop_offset_s']
  if offset is not None and not 0<=offset<=3:
   raise ValueError('Near-end offsets must be 0..3 seconds')
 for cell in plan.get('reconnects',[]):
  if not 6<=cell['second_reply_window_seconds']<=18 or cell.get('disconnect_offset_s')!=1.0:
   raise ValueError('Reconnect requires +1s active-TTS deadline and declared reply/post-roll window')
 if plan.get('qualification') and not plan.get('require_active_tts'):
  raise ValueError('Acceptance stimuli require active TTS at the declared deadline')


def run(plan_path, output):
 plan=json.loads(Path(plan_path).read_text());validate_plan(plan);out=Path(output);out.mkdir(exist_ok=False)
 write(out/'declaration.json',plan);policy=plan['noise_gate'];validate_noise_gate(policy)
 assert sha(Path(plan['stop_fixture']))==plan['stop_sha256']
 for c in plan['cases']:
  if c.get('fixture'):assert sha(Path(c['fixture']))==c['fixture_sha256']
 write(out/'identity.json',dict(commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),runner_sha256=sha(Path(__file__)),host_boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()))
 check_esptool();image=Path(plan['image']);h=plan['elf_sha256'];verify_bundle(image,h,0);verify_bundle(NORMAL,NORMAL_HASH)
 pending_timers=[]
 def stop_hold(signum,frame):
  for token in pending_timers:token.set()
  raise KeyboardInterrupt('Stop requested')
 signal.signal(signal.SIGINT,stop_hold);signal.signal(signal.SIGTERM,stop_hold)
 board=bot=None;result=dict(completed=False,cases=[],cleanup_errors=[])
 with bench_lock():
  _,device=resolve_jabra();jabra_card_zero()
  subprocess.run(['alsactl','-f',str(out/'mixer-before.state'),'store','0'],check=True,capture_output=True)
  try:
   set_volumes('0',out/'mixer-qualified.log')
   flash(image,h,out,'candidate',0);board=boot(out,'candidate',h)
   board.wait_for(r'AEC_GUARD_CONFIG guard_ms=400 afe_delay_samples=1024 rearm_ms=2000',5,since=0)
   for c in ('demo hold','audio 0','quiet 1','gate auto','aec-ref-gain 15','aec-guard-ms 400','playback-volume 0','peer-option arrival-trace 0'):
    command(board,c,40)
   command(board,'disp init',40);heap=snapshot(board,out,'display-init')['heap']['psram']
   command(board,f"disp arena 491520 {heap['free']} 1287760 {heap['largest']}")
   for i,asset in enumerate(re.findall(r'"([0-9a-f]{64})"',(ROOT/'main/demo_assets.h').read_text())):
    rid=f'cat-{i}';request(board,'preload',{'hash':asset},rid);assert terminal(board,rid)['error']=='none'
    command(board,f'disp asset {i} {asset}')
   command(board,'disp character on',30);write(out/'character-start.json',read(board,'character-status','DISPLAY_CHARACTER'))
   for attempt in range(1,4):
    folder=out/f'baseline-{attempt}';folder.mkdir();time.sleep(5)
    command(board,'aec-record 1');time.sleep(6.1);command(board,'aec-record 0');dump_capture(board,folder/'capture.npz')
    raw,afe,ref,timing=load_capture(folder/'capture.npz');baseline=dbfs(raw)
    check=quiet_gate(raw,baseline,policy);check.update(afe_dbfs=dbfs(afe),mic3_dbfs=dbfs(ref),gap_blocks=int(timing[:,4].sum()))
    check['passed'] &= baseline<=policy['baseline_max_dbfs'] and not check['gap_blocks'];write(folder/'noise-gate.json',check)
    print('BASELINE',attempt,json.dumps(check),flush=True)
    if check['passed']:break
   if not check['passed']:raise RuntimeError('No clean settled baseline; environmental pause')
   shutil.copy2(folder/'capture.npz',out/'quiet.npz');result['baseline']=check
   # Every cell is declared once; contamination is retained and stops this hold.
   trials=[dict(c,run_id=f"{c['id']}-a{a}",attempt=a) for a in range(1,plan['attempts_per_cell']+1) for c in plan['cases']]
   evaluation=out/'evaluation';evaluation.mkdir()
   scripts=[]
   for c in trials:
    scripts.append(dict(id=c['run_id'],text=c.get('text',plan['text']),trim_leading=c.get('trim_leading',False)))
    if c['stop_offset_s'] is not None:scripts.append(dict(id=c['run_id']+'-next',text=plan['next_text']))
   scripts.extend(dict(id=c['id']+'-old',text=c['text']) for c in plan.get('reconnects',[]))
   write(evaluation/'protocol.json',dict(echo_replies=scripts,barge_ids=[],barge_bot_text='unused'))
   os.environ.pop('ECHOEAR_AEC_FULL_DUPLEX',None)
   os.environ.update(ECHOEAR_LIVENESS_SECONDS='0.5',AUDIO_OUT_10MS_CHUNKS='4',ECHOEAR_AEC_EVALUATION_DIR=str(evaluation),ECHOEAR_AEC_SESSION_DIR=str(out/'sessions'),RTP_EVIDENCE_DIR=str(out/'rtp'),RTP_EVIDENCE_MAX_PACKETS='200000',DEMO_LLM='local',OFFLINE_SKIP_GREETING='1')
   bot=BotServer(out/'bot',entrypoint='demo_bot.py');bot.start()
   command(board,'server-url http://'+host_address()+':7861');at,_=command(board,'connect',90);board.wait_for('STATE Ready',20,since=at)
   assert 'SCTP_FLIGHT_FIX enabled aiortc=1.15.0' in (out/'bot/server-1.log').read_text()
   command(board,'audio 1');time.sleep(7);connected=snapshot(board,out,'connected');require_zero_i2s([connected])
   number=0
   def send(op,cid,**kw):
    nonlocal number
    requested=time.monotonic();number+=1;control(evaluation/'command.json',dict(number=number,op=op,case_id=cid,**kw))
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
     if any(e.get('number')==number and e.get('t',0)>=requested for e in rows(evaluation/'events.jsonl')):return
     time.sleep(.02)
    raise RuntimeError('Evaluation command not acknowledged')
   if plan.get('silent_stop_control'):
    folder=out/'silent-stop-control';folder.mkdir()
    before,before_samples=boundary(board,folder,'before')
    anchors=[start_anchored_capture(board,command)];time.sleep(2.3);start=time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
     future=pool.submit(play_wav,Path(plan['stop_fixture']),device)
     while time.monotonic()<start+5:
      anchors.append(clock_anchor(board));time.sleep(.05)
     jabra=future.result()
    anchors.append(stop_anchored_capture(board,command))
    after,after_samples=boundary(board,folder,'after')
    write(folder/'anchors.json',anchors);dump_capture(board,folder/'capture.npz')
    raw,afe,ref,timing=load_capture(folder/'capture.npz')
    write(folder/'capture-edge-check.json',require_bracketed_capture(timing,anchors))
    qref=load_capture(out/'quiet.npz')[2]
    gates=dict(pre=quiet_gate(raw[:32000],baseline,policy),post=quiet_gate(raw[-32000:],baseline,policy),reference_silent=not bool((frame_power(ref)>=reference_threshold(qref)).any()))
    write(folder/'noise-gate.json',gates)
    health=check_reply_resources(before,after,intermediate=before_samples+after_samples)
    write(folder/'result.json',dict(jabra=jabra,start_s=start,end_s=time.monotonic(),events=rows(evaluation/'events.jsonl'),resources=health))
    require_zero_i2s(before_samples+after_samples)
    assert health['all_passed'],'Silent Stop resource failure'
    assert gates['pre']['passed'] and gates['post']['passed'] and gates['reference_silent'],'Silent Stop noise/reference failure; no retake'
   current_guard=400
   accepted=set();gate_queue=NoiseRetryQueue([dict(c,run_id=c['id']) for c in plan['cases']],policy)
   with ThreadPoolExecutor(max_workers=1) as pool:
    for c in trials:
     cid=c['run_id'];next_id=cid+'-next'
     if c['id'] in accepted:
      send('skip',cid,reason='retry-not-required')
      if c['stop_offset_s'] is not None:send('skip',next_id,reason='retry-not-required')
      continue
     folder=out/cid;folder.mkdir();command(board,f"playback-volume {c['volume_db']}");current_guard=set_guard_if_changed(board,command,current_guard,c['guard_ms']);time.sleep(5)
     before,before_samples=boundary(board,folder,'before')
     anchors=[start_anchored_capture(board,command)];time.sleep(2.3);start_ns=time.monotonic_ns()
     future=None;token=threading.Event();pending_timers.append(token)
     if c['stop_offset_s'] is not None:
      fixture=Path(c.get('fixture',plan['stop_fixture']))
      paths=lambda:list((out/'sessions').glob('*/events.jsonl'))
      reader=TransportEvents(paths,start_ns,at_end=True)
      identity=ContextIdentity([evaluation/'events.jsonl'],cid)
      future=pool.submit(schedule_from_tts,paths,start_ns,c['stop_offset_s'],lambda:play_wav(fixture,device),cancel=token,timeout_s=18,require_active=plan.get('require_active_tts',False),active_max_age_s=plan.get('active_tts_max_age_s',.12),reader=reader,identity=identity)
     send('speak',cid)
     deadline=time.monotonic()+plan['reply_window_seconds'];next_clock=0;anchor=None;follow=False;sealed=False
     outcome=dict(c,start_ns=start_ns)
     while time.monotonic()<deadline:
      now=time.monotonic()
      if now>=next_clock:anchors.append(clock_anchor(board));next_clock=now+.05
      events=rows(evaluation/'events.jsonl')
      if anchor is None:
       writes=[e for p in (out/'sessions').glob('*/events.jsonl') for e in rows(p) if e.get('kind')=='tts-transport-write' and e.get('non_silent') is True and e['monotonic_ns']>=start_ns]
       if writes:anchor=min(e['monotonic_ns'] for e in writes)/1e9;outcome['non_silent_tts_host_s']=anchor
      if anchor and c['stop_offset_s'] is not None:
       if not follow and now>=anchor+7:
        interrupted=any(e.get('case_id')==cid and e.get('frame')=='InterruptionFrame' and e['t']>=anchor+c['stop_offset_s'] for e in events)
        if interrupted:
         send('seal',cid);sealed=True;send('speak',next_id);outcome['next_request_host_s']=time.monotonic();follow=True
      time.sleep(.01)
     end_ns=time.monotonic_ns();anchors.append(stop_anchored_capture(board,command));after,after_samples=boundary(board,folder,'after')
     write(folder/'anchors.json',anchors)
     if future:
      outcome['stimulus_timer']=future.result();outcome['jabra']=outcome['stimulus_timer'].get('jabra');outcome['stimulus_skipped']=outcome['stimulus_timer'].get('skipped',False)
      write(folder/'timer.json',outcome['stimulus_timer'])
     pending_timers.remove(token)
     if follow:send('seal',next_id)
     elif not sealed:
      send('seal',cid)
      if c['stop_offset_s'] is not None:send('skip',next_id,reason='retry-not-required')
     dump_capture(board,folder/'capture.npz')
     if outcome.get('stimulus_timer',{}).get('harness_failure'):raise StimulusTimingFailure('Invocation timing failed; see timer.json and capture.npz')
     raw,afe,ref,timing=load_capture(folder/'capture.npz');n=32000
     write(folder/'capture-edge-check.json',require_bracketed_capture(timing,anchors))
     gates=dict(pre=quiet_gate(raw[:n],baseline,policy),post=quiet_gate(raw[-n:],baseline,policy),gap_blocks=int(timing[:,4].sum()))
     qref=load_capture(out/'quiet.npz')[2]
     gates['board_silent']=all(not (frame_power(x)>=reference_threshold(qref)).any() for x in (ref[:n],ref[-n:]))
     noise_passed=bool(gates['pre']['passed'] and gates['post']['passed'])
     valid=bool(gates['board_silent'] and not gates['gap_blocks'])
     action=gate_queue.finish(dict(run_id=c['id']),c['attempt'],noise_passed if valid else None)
     if not noise_passed and valid and plan['attempts_per_cell']==1:action='no-retake-declared'
     if noise_passed and valid:accepted.add(c['id'])
     write(folder/'noise-gate.json',gates)
     if not noise_passed:write(folder/'noise-contaminated.json',dict(excluded=True,gate=gates,action=action))
     all_events=[e for p in (out/'sessions').glob('*/events.jsonl') for e in rows(p)]
     count=outgoing_rtvi_count(all_events,start_ns,end_ns)
     outcome.update(end_ns=end_ns,noise_gate_passed=noise_passed,noise_action=action,quiet_windows_valid=bool(gates['board_silent'] and not gates['gap_blocks']),admission=live_admission(before,after,recorder_messages=count),resources=check_reply_resources(before,after,intermediate=before_samples+after_samples))
     outcome['events']=[e for e in rows(evaluation/'events.jsonl') if e.get('case_id') in (cid,next_id)]
     outcome['liveness_alarms']=sum(e['event']=='liveness-alarm' for e in rows(evaluation/'events.jsonl'))
     write(folder/'result.json',outcome);result['cases'].append(outcome);write(out/'result.json',result)
     snapshot(board,out,cid+'-active')
     print('CASE',cid,'noise',noise_passed,'action',action,'resources',outcome['resources']['all_passed'],'admission',outcome['admission']['passed'],flush=True)
     require_zero_i2s(before_samples+after_samples)
     assert valid,'Acquisition failure: incomplete quiet windows; no automatic replacement'
     health=[e for e in outcome['events'] if e.get('event')=='rtp-observer-health']
     assert health and all(e['passed'] and e['sctp']['supported'] and e['sctp']['flight_fix_enabled'] and e['sctp']['records']>0 and not e['sctp']['truncated'] and not e['sctp']['unavailable'] for e in health),'Incomplete SCTP observer'
     if gate_queue.paused:raise RuntimeError('Three consecutive noisy cells; environmental pause')
     assert noise_passed,'Noise-contaminated cell retained; no automatic retake in this declaration'
     assert outcome['admission']['passed'] and outcome['resources']['all_passed'] and not outcome['liveness_alarms'],'Delivery/resource/liveness failure'
   from echoear_aec_reconnect import run_reconnect
   result['reconnects']=[]
   for index,cell in enumerate(plan.get('reconnects',[])):
    def prepare_new(new_id):
     nonlocal number
     for name in ('command.json','session-owner.json'):
      path=evaluation/name
      if path.exists():path.rename(evaluation/(cell['id']+'-old-'+name))
     number=0
     remaining=[dict(id=new_id,text=cell['text'])]+[dict(id=c['id']+'-old',text=c['text']) for c in plan['reconnects'][index+1:]]
     write(evaluation/'protocol.json',dict(echo_replies=remaining,barge_ids=[],barge_bot_text='unused'))
    value=run_reconnect(board,command,boundary,send,lambda:list((out/'sessions').glob('*/events.jsonl')),prepare_new,out,cell,baseline,policy,out/'quiet.npz',json.loads((out/'identity.json').read_text()))
    result['reconnects'].append(value);write(out/'result.json',result)
    assert value['passed'],'Reconnect evidence failed; no standalone retry'
    send('seal',cell['id']+'-new')
   settled,settled_samples=boundary(board,out,'settled-end');require_zero_i2s(settled_samples);write(out/'display-end.json',read(board,'status','DISPLAY'));write(out/'character-end.json',read(board,'character-status','DISPLAY_CHARACTER'))
   result['resources']=check_directory(out);result['active_end_ns']=time.monotonic_ns();write(out/'result.json',result)
   command(board,'audio 0');command(board,'disconnect',40);time.sleep(.5);disconnected=snapshot(board,out,'disconnected');require_zero_i2s([disconnected]);bot.stop();bot=None
   result['sctp_recovery']=check_active_recovery(rows(out/'rtp/sctp.jsonl'),result['active_end_ns'])
   result['completed']=len(accepted)==len(plan['cases']) and result['resources']['all_passed'] and result['sctp_recovery']['passed']
  except BaseException as exc:
   result['error']=type(exc).__name__+': '+str(exc);(out/'exception.txt').write_text(traceback.format_exc());print('ERROR',result['error'],flush=True)
  finally:
   for label,fn in [('bot',lambda:bot.stop() if bot else None),('serial',lambda:board.close() if board else None)]:
    try:fn()
    except Exception as e:result['cleanup_errors'].append(label+': '+str(e))
   try:
    flash(NORMAL,NORMAL_HASH,out,'restore');restored=boot(out,'restore',NORMAL_HASH);restored.close();result['restored_elf_sha256']=NORMAL_HASH
   except Exception as e:result['cleanup_errors'].append('RESTORE: '+str(e))
   try:
    subprocess.run(['alsactl','-f',str(out/'mixer-before.state'),'restore','0'],check=True,capture_output=True)
    subprocess.run(['alsactl','-f',str(out/'mixer-after.state'),'store','0'],check=True,capture_output=True)
    result['mixer_equal']=(out/'mixer-before.state').read_bytes()==(out/'mixer-after.state').read_bytes()
   except Exception as e:result['cleanup_errors'].append('mixer: '+str(e))
   write(out/'result.json',result)
   if result.get('restored_elf_sha256')!=NORMAL_HASH:
    print('RESTORE FAILED: retaining lock',flush=True)
    while True:time.sleep(30)
 result['lock_released']=True;write(out/'result.json',result);print('DONE',result['completed'],result['cleanup_errors'],flush=True)


if __name__=='__main__':
 import argparse
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('plan',type=Path);parser.add_argument('output',type=Path)
 args=parser.parse_args();run(args.plan,args.output)
