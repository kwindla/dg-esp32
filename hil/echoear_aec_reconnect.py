"""One declared reconnect cell; capture thread owns every clock anchor.

No explicit PERF request is made inside the anchored interval. Disconnect's
existing final PERF line retains the retired epoch; the new epoch's first
snapshot is taken only after capture stops. Missing evidence fails closed.
"""
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from echoear_aec_bench import (write, load_capture, start_anchored_capture,
    stop_anchored_capture, clock_anchor, analyze)
from echoear_aec_capture import dump_capture
from echoear_aec_stimulus import schedule_from_tts, schedule_active_window, StimulusPreconditionFailure, TransportEvents, StimulusTimingFailure
from echoear_aec_event_tail import ContextIdentity, EventCache
from echoear_aec_metrics import frame_power, reference_threshold
from echoear_aec_accept_analysis import disconnect_activity, check_render_ledger
from echoear_aec_resources import check_reply_resources
from echoear_aec_checks import admission_counters


def epoch_checks(first,last,samples,*,fresh=False):
    keys=('lost','server_lost','concealed','i2s_underflows','i2s_write_errors','i2s_write_timeouts')
    delta={k:last.get(k, -1)-(0 if fresh and k in ('lost','server_lost','concealed') else first.get(k, -1)) for k in keys}
    present=all(type(p.get(k)) is int for p in (first,last) for k in keys)
    rx=[t['stack_free_bytes'] for p in samples for t in p.get('tasks',[]) if t['name']=='peer-rx']
    drained=all(p.get('data_admission',{}).get('received',-1)==p.get('data_admission',{}).get('dispatched',-2) for p in (first,last))
    return dict(passed=present and all(v==0 for v in delta.values()) and all(last[k]==0 for k in keys if k.startswith('i2s_')) and drained and bool(rx) and min(rx)>=1024,
                delta=delta,drained=drained,peer_rx_min_bytes=min(rx) if rx else None,
                admission=admission_counters(first,last))


def bounded_capture_start(board,command):
    anchor=clock_anchor(board,timeout=.25)
    time.sleep(.2)
    command(board,'aec-record 1',.5)
    return anchor


def bounded_capture_stop(board,command):
    command(board,'aec-record 0',.5)
    return clock_anchor(board,timeout=.25)


def completed_new_reply(events,recorded,case_id,old_pc,now):
    """Requested new context only; both stop and last write precede quiet tail."""
    own=[e for e in events if e.get('case_id')==case_id]
    contexts={(e.get('pc_id'),e.get('context_id')) for e in own if e.get('event')=='tts-generation-metric-start'}
    if len(contexts)>1:raise RuntimeError('Ambiguous new reconnect generation')
    if not contexts:return None
    pc,cid=next(iter(contexts))
    if not pc or not cid or pc==old_pc:raise RuntimeError('Missing/fresh new peer identity')
    writes=[e for e in recorded if e.get('kind')=='tts-transport-write' and
            (e.get('pc_id'),e.get('context_id'))==(pc,cid)]
    starts=[e['t'] for e in own if e.get('frame')=='BotStartedSpeakingFrame']
    stops=[e['t'] for e in own if e.get('frame')=='BotStoppedSpeakingFrame']
    if not writes or not any(e.get('non_silent') for e in writes) or not starts or not stops or max(stops)<max(starts):return None
    last=max(e['monotonic_ns']/1e9 for e in writes);end=max(last,max(stops))
    if now<end+2.3:return None
    return dict(case_id=case_id,pc_id=pc,context_id=cid,bot_stopped_s=max(stops),
                last_write_s=last,quiet_s=now-end,required_quiet_s=2.3)


def capture_reconnect(board,command,send,paths,prepare_new,folder,cell,*,
                      anchor=lambda board:clock_anchor(board,timeout=.25),start=bounded_capture_start,
                      stop=bounded_capture_stop,schedule=schedule_from_tts,
                      clock=time.monotonic,sleep=time.sleep,context_identity=None,new_reply=None):
    """Worker issues control commands; caller keeps anchoring through all phases."""
    began=clock()
    anchors=[start(board,command)]
    cancel=threading.Event()
    deadline=began+cell.get("capture_cap_seconds",28)-3
    def remaining():
        left=deadline-clock()
        if cancel.is_set() or left<=0:raise TimeoutError("Reconnect capture cap reached")
        return left
    def bounded_command(text):
        return command(board,text,min(2,remaining()))
    def bounded_sleep(seconds):
        end=clock()+seconds
        while clock()<end:
            remaining();sleep(min(.05,end-clock()))
    def worker():
        # Keep injected sleepers useful in host lifecycle tests.
        remaining();sleep(2.3);remaining()
        start_ns=int(clock()*1e9)
        def disconnect():
            invoked=clock();cursor,_=bounded_command('disconnect')
            finals=[json.loads(line[5:]) for _,_,line in board.lines_since(cursor) if line.startswith('PERF ')]
            if len(finals)!=1:raise RuntimeError('Missing/ambiguous retired-epoch PERF')
            write(folder/'old-final.json',finals[0])
            return dict(request_monotonic_s=invoked,elapsed_seconds=clock()-invoked)
        reader=TransportEvents(paths,start_ns,at_end=True)
        with ThreadPoolExecutor(max_workers=1) as timer:
            if cell.get('active_speech_window_s'):
                future=timer.submit(schedule_active_window,paths,start_ns,disconnect,context_identity,
                    window_s=cell['active_speech_window_s'],timeout_s=min(18,remaining()),cancel=cancel,reader=reader)
            else:
                future=timer.submit(schedule,paths,start_ns,1.0,disconnect,**({'reader':reader} if schedule is schedule_from_tts else {}),require_active=True,active_max_age_s=.12,timeout_s=min(18,remaining()),cancel=cancel)
            send('speak',cell['id']+'-old')
            witness=future.result()
        write(folder/'timer.json',witness)
        if witness.get('harness_failure'):raise StimulusTimingFailure('Reconnect invocation timing failed; see timer.json')
        if witness.get('skipped'):
            if cell.get('active_speech_window_s'):raise StimulusPreconditionFailure(witness['reason'])
            raise RuntimeError('Reconnect deadline has no active TTS')
        prepare_new(cell['id']+'-new')
        cursor,_=bounded_command('connect')
        board.wait_for('STATE Ready',min(remaining(),10),since=cursor)
        remaining()
        ready_s=clock()
        # Do not restate audio 1: that resets the capture framer.
        send('speak',cell['id']+'-new')
        if cell.get('second_reply_completion'):
            while True:
                remaining()
                witness_new=new_reply(clock())
                if witness_new:
                    write(folder/'new-reply-completion.json',witness_new);break
                bounded_sleep(.02)
        else:bounded_sleep(cell['second_reply_window_seconds']) # historical fixed wait
        return dict(timer=witness,start_ns=start_ns,end_ns=int(clock()*1e9),disconnect_ready_interval_s=[witness["jabra"]["request_monotonic_s"],ready_s])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(worker)
        try:
            while not future.done():
                remaining()
                tick=clock();anchors.append(anchor(board));sleep(max(0,.05-(clock()-tick)))
            result=future.result()
        except BaseException as exc:
            if cell.get('active_speech_window_s') and not (folder/'timer.json').exists():
                write(folder/'timer.json',dict(skipped=True,reason='Incomplete reconnect timer',error=type(exc).__name__+': '+str(exc),window_s=cell['active_speech_window_s']))
            raise
        finally:
            cancel.set()
            anchors.append(stop(board,lambda b,t,*a:command(b,t,min(2,a[0]) if a else 2)))
            if cell.get('active_speech_window_s'):
                from echoear_aec_resource_trial import snapshot
                snapshot(board,folder,'capture-end')
            write(folder/'anchors.json',anchors)
            write(folder/'capture-duration.json',dict(elapsed_s=clock()-began,limit_s=cell.get('capture_cap_seconds',28)))
    return result


def run_reconnect(board,command,boundary,send,paths,prepare_new,out,cell,baseline,policy,quiet,identity):
    from echoear_aec_noise_gate import quiet_gate
    from echoear_aec_timing import require_bracketed_capture
    folder=out/cell['id'];folder.mkdir()
    before,pre=boundary(board,folder,'before')
    try:
        context_reader=ContextIdentity([out/'evaluation/events.jsonl'],cell['id']+'-old')
        def context_identity():
            owner=context_reader()
            if owner and owner['pc_id']!=before['pc_id']:
                raise StimulusPreconditionFailure('Wrong requested reconnect peer')
            return owner
        context_identity.tail=context_reader.tail
        new_events=EventCache([out/'evaluation/events.jsonl'],at_end=True)
        new_writes=EventCache(paths,at_end=True)
        def new_reply(now):
            return completed_new_reply(new_events.read(),new_writes.read(),
                cell['id']+'-new',before['pc_id'],now)
        result=capture_reconnect(board,command,send,paths,prepare_new,folder,cell,context_identity=context_identity,new_reply=new_reply)
    except BaseException as exc:
        failure=dict(passed=False,error=type(exc).__name__+': '+str(exc))
        if isinstance(exc,StimulusTimingFailure):failure.update(kind='stimulus-timing',error_class='harness',retry_safe=False)
        if cell.get('active_speech_window_s') and isinstance(exc,TimeoutError) and str(exc)=='Reconnect capture cap reached':
            failure.update(kind='capture-deadline',retry_safe=False,reason='Both clean epoch/generation witnesses required before any retry')
        try:dump_capture(board,folder/'capture.npz')
        except Exception as export_error:failure['export_error']=str(export_error)
        write(folder/'acquisition-failure.json',failure)
        if failure.get('kind')=='capture-deadline':
            from echoear_aec_campaign import HarnessFailure
            raise HarnessFailure('capture-deadline',failure['error']) from exc
        raise
    first,post=boundary(board,folder,'new-first' if cell.get('active_speech_window_s') else 'after')
    time.sleep(5.1)
    last,late=boundary(board,folder,'new-steady')
    # This is the first new-epoch snapshot; no racing RTP for a synthetic 0->1.
    old=json.loads((folder/'old-final.json').read_text())
    result['epochs']=[epoch_checks(before,old,pre+[old]),epoch_checks(first,last,post+late,fresh=True)]
    result['new_identity']=bool(first.get('pc_id') and first['pc_id']!=before.get('pc_id') and first.get('generation')!=before.get('generation'))
    result['fresh_prefill']=first.get('prefills')==1
    from echoear_aec_resources import check_snapshots
    result['new_resources']=check_snapshots(post+late)
    result['old_resources']=check_snapshots(pre+[old])
    # Disconnect's autonomous PERF lacks the separate AUDIO line. Those
    # lifetime counters are checked on the surrounding explicit snapshots;
    # every actual old-epoch PERF task/heap/CPU/AEC value remains in its gate.
    for key in list(result['old_resources']):
        if key.startswith('audio.'):
            result['old_resources'][key]=check_snapshots(pre+post+late)[key]
    from echoear_aec_resources import liveness_reply
    result['epoch_liveness']=[liveness_reply(before,old),liveness_reply(first,last)]
    # Resource liveness deltas cannot qualify a single endpoint; whole-hold
    # resources still cover both epochs. Record this endpoint without pretending
    # it is a separately sampled five-second CPU window.
    result['new_render_ledger']=check_render_ledger(post+late)
    dump_capture(board,folder/'capture.npz')
    if cell.get('active_speech_window_s'):
        exported,export_samples=boundary(board,folder,'after')
        from echoear_aec_campaign_analysis import transport,delivery_counters
        end=json.loads((folder/'capture-end.json').read_text())
        result['export_event']=dict(report_only=True,transport=transport(end,exported,'echo-transport-v8.3'),delivery=delivery_counters(end,exported))
        write(folder/'export-event.json',result['export_event'])
        # Fresh epoch strict checks stop at capture-end. Resource/liveness gates
        # still include the export boundary and all subsequent observations.
        result['epochs'][1]=epoch_checks(end,end,[end],fresh=True)
        result['new_resources']=check_reply_resources(first,exported,intermediate=[end]+post+late+export_samples)['checks']
        result['epoch_liveness'][1]=liveness_reply(first,exported)
    raw,afe,ref,blocks=load_capture(folder/'capture.npz')
    anchors=json.loads((folder/'anchors.json').read_text())
    result['edges']=require_bracketed_capture(blocks,anchors)
    qref=load_capture(quiet)[2]
    threshold=reference_threshold(qref)
    result['active_disconnect']=disconnect_activity(blocks,frame_power(ref)>=threshold,anchors,result['timer']['jabra']['request_monotonic_s'])
    result['noise']={name:quiet_gate(raw[sl],baseline,policy,mic3=ref[sl]) for name,sl in [('pre',slice(0,32000)),('post',slice(-32000,None))]}
    result['playback']=[]
    for p in paths():
        events=[json.loads(line) for line in p.read_text().splitlines()]
        events=[e for e in events if e.get('kind')=='session-start' or result['start_ns']<=e['monotonic_ns']<=result['end_ns']]
        if not any(e.get('kind')=='tts-transport-write' for e in events):continue
        result['playback'].append(analyze('live',folder/'capture.npz',quiet,identity,events=events,anchors=anchors))
    playback_ok=len(result['playback'])==2 and all(p['non_silent_tts_counts']['playback']>=1 and not p['non_silent_tts_counts']['missing-playback'] and not p['non_silent_tts_counts']['timing-unscorable'] for p in result['playback'])
    result['passed']=bool(result['active_disconnect']['passed'] and result['new_identity'] and result['fresh_prefill'] and all(e['passed'] for e in result['epochs']) and all(g['passed'] for g in result['noise'].values()) and result['new_render_ledger']['passed'] and playback_ok and all(e['passed'] for e in result['epoch_liveness']) and all(c['passed'] for c in result['new_resources'].values()) and all(c['passed'] for c in result['old_resources'].values()))
    write(folder/'result.json',result)
    return result
