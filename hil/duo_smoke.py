#!/usr/bin/env python3
"""Resumable, coordinator-owned duo checks. Offline imports never touch the bench."""
import argparse
from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import time

from duo_identity import ROOT,authenticate,digest,require,write_new
from duo_bundle import verify_bundle

EVIDENCE=Path.home()/'src/evidence/duo/step-7'
PARTS=('install','connect','idle','generating','playback','drop-state-1','drop-state-4','tts-fail-next','quiet','turns','touch')
RULES={
 'install':'Both packs installed, pinned >0; closed fixture origin; reboot restores all hashes, fetched=0; same residency; boot ELF matches.',
 'connect':'Wire hello then state then angel DUO_PROOF; matching session/conn/seq/rev and unchanged app generation.',
 'idle':'One injected toggle while idle; same session/generation/conn; one commit and answer/proof; next turn uses target voice.',
 'generating':'Inject after LLMFullResponseStartFrame and before TTSStartedFrame/audio; same switch/voice gates as idle.',
 'playback':'Inject after TTSStartedFrame and BotStartedSpeakingFrame while still speaking; same switch/voice gates as idle.',
 'drop-state-1':'One dropped state; identical-seq replay, no double commit; exactly one retransmit; matching proof.',
 'drop-state-4':'Four dropped states; three identical-seq retransmits then new-seq hello with stale have; resync retains committed target; one commit/proof.',
 'tts-fail-next':'First synthesis after toggle fails; higher-rev reverted state/proof for old character; next turn uses old voice.',
 'quiet':'More than 300 seconds without user/bot turns or reconnect; then successful STT-strict turn and matching voice.',
 'turns':'One completed STT-strict turn per character; matching real synthesis voice and transport audio; LLM text report-only.',
 'touch':'Post-GO 600 second window; physical +1, injection +0, toggle +1, answered +1, same session/generation, new proof and matching bot commit.'}


class IncompleteRecord(ValueError):pass


def joined_records(lines,prefix):
    """Return complete records and their final-line index; never score fragments."""
    rows=[];pending=None;pieces={}
    child={'DUO_PROOF':'DUO_PROOF_LAYER','DUO_STATUS':'DUO_ERRORS'}.get(prefix)
    for index,line in enumerate(lines):
        kind=next((p for p in (prefix,child) if p and p+' ' in line),None)
        if kind is None:continue
        value=line.split(kind+' ',1)[1]
        if child:
            require(len((kind+' '+value).encode())<=420,'duo console record exceeds wire budget')
            require(value.startswith('{'),'duo console record is not JSON')
        row=json.loads(value) if value.startswith('{') else dict(re.findall(r'(\w+)=([^\s]+)',value))
        if not child:rows.append((row,index));continue
        require(isinstance(row,dict),'duo console record is not an object')
        if kind==prefix:
            if pending is not None:raise IncompleteRecord('incomplete '+prefix+' before next header')
            pending=row;pieces={}
            bindings=('rev','scene_revision') if prefix=='DUO_PROOF' else ('generation','rev')
            require(all(type(row.get(k)) is int and row[k]>=0 for k in bindings),'invalid duo header binding')
            if prefix=='DUO_PROOF':
                require(type(row.get('layer_count')) is int and 1<=row['layer_count']<=4 and 'layers' not in row,
                        'invalid proof layer count/header')
            else:require('errors' not in row,'unsplit status record')
            continue
        require(pending is not None,'orphan '+kind)
        keys=('rev','scene_revision') if prefix=='DUO_PROOF' else ('generation','rev')
        require(all(type(row.get(k)) is int and row[k]==pending.get(k) for k in keys),'mismatched '+kind+' revision/scene')
        if prefix=='DUO_PROOF':
            require(set(row)=={'rev','scene_revision','index','hash','x','y'},'invalid proof layer fields')
            part=row['index'];total=pending['layer_count']
            require(type(part) is int and 0<=part<total and part not in pieces,'duplicate/invalid proof layer')
            require(isinstance(row['hash'],str) and re.fullmatch('[0-9a-f]{64}',row['hash']) and
                    type(row['x']) is int and type(row['y']) is int,'invalid proof layer payload')
            pieces[part]=[part,row['hash'],row['x'],row['y']]
            if len(pieces)==total:pending['layers']=[pieces[i] for i in range(total)]
        else:
            require(set(row)=={'generation','rev','part','parts','errors'} and type(row['parts']) is int and row['parts']==2,'invalid error fragment fields')
            part=row['part'];total=2
            require(type(part) is int and 0<=part<total and part not in pieces and isinstance(row['errors'],dict),
                    'duplicate/invalid error fragment')
            require(all(type(v) is int and v>=0 for v in row['errors'].values()),'invalid error counter')
            require(not any(set(row['errors'])&set(p) for p in pieces.values()),'duplicate error counter')
            pieces[part]=row['errors']
            if len(pieces)==total:
                pending['errors']={k:v for p in pieces.values() for k,v in p.items()}
                require(set(pending['errors'])=={'malformed','session','connection','baseline','stale-revision','mailbox','send',
                    'timeout','render','switch-failed','mismatch','stale','voice','deadline','cancelled','other'},'incomplete error counters')
        if len(pieces)==total:
            rows.append((pending,index));pending=None;pieces={}
    if pending is not None:raise IncompleteRecord('incomplete '+prefix+' fragments')
    return rows


def records(lines,prefix):return [row for row,_ in joined_records(lines,prefix)]


def validate_status(row):
    for key in ('generation','toggles_sent','answered','retransmitted','resyncs','displayed','taps_injected','taps_physical','taps_ignored_pending','rev'):
        require(type(row.get(key)) is int and row[key]>=0,'missing/invalid DUO_STATUS '+key)
    require(row.get('character') in ('angel','devil') and type(row.get('pending')) is bool and row.get('baseline') is True,'duo baseline missing')
    require(isinstance(row.get('errors'),dict) and all(type(n) is int and n>=0 for n in row['errors'].values()),'invalid error counters')
    return row


def delta(before,after,*,expected_timeout=0):
    validate_status(before);validate_status(after)
    keys=('toggles_sent','answered','retransmitted','resyncs','displayed','taps_injected','taps_physical','taps_ignored_pending')
    result={k:after[k]-before[k] for k in keys}
    require(all(v>=0 for v in result.values()),'duo counters reset')
    require(before['generation']==after['generation'],'app generation changed')
    require(set(before['errors'])==set(after['errors']) and all(after['errors'][k]-before['errors'][k]==(expected_timeout if k=='timeout' else 0) for k in before['errors']),'duo errors changed')
    return result


@contextmanager
def render_busy_reporting(io,report):
    """Observe the part's status bracket without changing any pass/fail rule."""
    report.update(report_only=True,delta=None,rate_per_minute=None,seconds=None,observation='unavailable')
    def sample():
        try:
            row=io.json_command('duo status','DUO_STATUS');at=io.clock()
            if all(type(row.get(k)) is int and row[k]>=0 for k in ('generation','render_busy')):
                return row,at,len(getattr(io,'boots',[]))
        except Exception:pass
        return None
    before=sample()
    try:yield
    finally:
        after=sample()
        if before is not None and after is not None:
            first,start,boot=before;last,end,last_boot=after;seconds=end-start
            if boot==last_boot and first['generation']==last['generation'] and last['render_busy']>=first['render_busy'] and seconds>0:
                count=last['render_busy']-first['render_busy']
                report.update(delta=count,rate_per_minute=count*60/seconds,seconds=seconds,observation='recorded')
            else:report['observation']='reboot, counter reset, generation changed, or empty time bracket'


def proof_matches(proof,state):
    require(state.get('op')=='state' and state.get('type')=='duo' and state.get('v')==1,'not a duo state')
    require(all(proof.get(k)==state.get(k) for k in ('session','conn','seq','rev','character')),'proof does not match state')
    require(type(proof.get('generation')) is int and proof['generation']>0 and
            type(proof.get('scene_revision')) is int and proof['scene_revision']>0,'uncommitted proof')
    descriptor=json.loads((ROOT/'hil/fixtures/echoear360/duo-pack.json').read_text())
    pack=next(c for c in descriptor['characters'] if c['name']==proof['character'])
    require(proof.get('pack')==pack['hash'],'proof pack identity differs')
    layers=proof.get('layers',[])
    # Firmware prints ordered [index, hash, x, y] tuples, at the idle frames.
    frames=[pack['faceplate']]+[layer['frames'][0] for layer in pack['layers']]
    require(len(layers)==len(frames),'proof layer count differs')
    for index,(layer,frame) in enumerate(zip(layers,frames)):
        require(layer==[index,frame['hash'],frame['x'],frame['y']],'proof layer order/hash/position differs')
    require(proof.get('hidden')==list(range(len(frames),8)),'proof hidden layers differ')
    return proof


def status_matches(status,proof):
    validate_status(status)
    require(all(status[k]==proof[k] for k in ('character','rev','generation')),'status contradicts committed proof')


class MissedInjectionWindow(ValueError):
    def __init__(self,witness):
        super().__init__('toggle reached bot outside requested injection phase')
        self.witness=witness


def record_injected_latency(io,wire):
    started=getattr(io,'injection_started_ns',None)
    if started is None:return None
    receipts=[r['monotonic_ns'] for r in wire if r.get('direction')=='received' and
              r.get('data',{}).get('op')=='toggle' and r.get('monotonic_ns',-1)>=started]
    if not receipts:return None
    receipt=min(receipts)
    row=dict(injected_at_ns=started,received_at_ns=receipt,milliseconds=(receipt-started)/1e6,
             basis='host console injection to bot wire receipt')
    if row not in io.tap_latencies:io.tap_latencies.append(row)
    return row


def record_physical_latency(io,wire):
    interval=getattr(io,'touch_tap_interval',None)
    if interval is None:return None
    lower,upper=interval
    receipts=[r['monotonic_ns'] for r in wire if r.get('direction')=='received' and
              r.get('data',{}).get('op')=='toggle' and r.get('monotonic_ns',-1)>=io.touch_mark]
    if not receipts:return None
    receipt=min(receipts)
    row=dict(received_at_ns=receipt,tap_interval_ns=[int(lower*1e9),int(upper*1e9)],
             milliseconds_bounds=[max(0,(receipt-upper*1e9)/1e6),max(0,(receipt-lower*1e9)/1e6)],
             basis='physical recognition bracketed by status polls; bounds only')
    if row not in io.tap_latencies:io.tap_latencies.append(row)
    return row


def fault_bracket(events,voices,proof,start,end):
    require(start<end,'invalid failed-turn bracket')
    events=[r for r in events if start<=r.get('monotonic_ns',-1)<end]
    voices=[r for r in voices if start<=r.get('monotonic_ns',-1)<end]
    starts=[r for r in voices if r.get('kind')=='duo-synthesis']
    require(len(starts)==1,'failed reply was re-spoken in another synthesis')
    first=starts[0];context=first.get('context_id')
    require(context and first['voice']=={'angel':'flux-hannah-en','devil':'flux-rufus-en'}[proof['character']] and
            all(first[k]==proof[k] for k in ('conn','rev','character')),'failed synthesis was not the committed target')
    errors=[r for r in voices if r.get('kind')=='duo-synthesis-error' and r.get('error')=='DUO_FAULT tts-fail-next']
    require(len(errors)==1 and all(errors[0].get(k)==first[k] for k in ('context_id','voice','conn','rev','character')),
            'fault error is not owned by the first target context')
    require(any(r.get('frame')=='ErrorFrame' and r.get('frame_id')==errors[0]['error_id'] for r in events),
            'missing matching recorded synthesis error')
    require(not any(r.get('kind')=='duo-voice-audio' and r.get('bytes',1)>0 for r in voices) and
            not any(r.get('kind')=='tts-transport-write' and (r.get('samples',1)>0 or r.get('non_silent') is True) for r in events),
            'audio or restored-service remainder during failed-turn bracket')
    require(len([r for r in events if r.get('kind')=='aggregator-user-turn-stopped'])==1,
            'failed reply must have one input turn')
    return dict(start=start,end=end,first_context=context,error_id=errors[0]['error_id'],events=events,voices=voices)


def switch_witness(before,after,prior,proof,events,wire,*,physical=False,drops=0):
    status_matches(before,prior);status_matches(after,proof)
    d=delta(before,after,expected_timeout=int(drops==4))
    require(d['toggles_sent']==1 and d['answered']==1 and d['taps_injected']==(0 if physical else 1) and
            d['taps_physical']==(1 if physical else 0) and d['taps_ignored_pending']==0 and d['displayed']==1 and not after['pending'],
            'switch counters differ')
    require(d['retransmitted']==min(drops,3) and d['resyncs']==int(drops==4),'retry/resync counters differ')
    require(proof['session']==prior['session'] and proof['generation']==prior['generation']==before['generation'] and
            proof['conn']==prior['conn'],'switch reconnected')
    target='devil' if prior['character']=='angel' else 'angel'
    require(proof['character']==target and proof['rev']==prior['rev']+1 and proof['scene_revision']>prior['scene_revision'],'switch revision/character differs')
    requests=[r['data'] for r in wire if r.get('direction')=='received' and r.get('data',{}).get('session')==prior['session']]
    toggles=[r for r in requests if r.get('op')=='toggle']
    require(len(toggles)==1+min(drops,3) and all(r==toggles[0] for r in toggles),'missing/changed toggle replay')
    request=toggles[0]
    outcomes=[r for r in events if r.get('kind')=='outcome' and r.get('session')==prior['session'] and r.get('seq')==request['seq']]
    require(outcomes and all(r.get('op')=='state' and r['character']==target and r['rev']==proof['rev'] and r.get('conn')==proof['conn'] for r in outcomes),'bot commit differs')
    verified=[r for r in events if r.get('kind')=='verified' and not r.get('reverting') and r.get('character')==target]
    require(len(verified)==1,'toggle must commit exactly once')
    state=outcomes[-1]
    sent=[r['data'] for r in wire if r.get('direction')=='sent' and r.get('data',{}).get('seq')==proof['seq']]
    require(any(all(r.get(k)==proof.get(k) for k in ('session','conn','seq','rev','character')) for r in sent),'matching state not sent')
    if drops==4:
        hellos=[r for r in requests if r.get('op')=='hello']
        require(hellos and hellos[-1]['seq']>request['seq'] and hellos[-1]['have']==prior['character'] and hellos[-1]['seq']==proof['seq'],'missing stale-have resync')
        state=next(r['data'] for r in reversed(wire) if r.get('direction')=='sent' and r.get('data',{}).get('seq')==proof['seq'])
    else:require(proof['seq']==request['seq'],'proof sequence differs')
    proof_matches(proof,state)
    timings=[r for r in events if r.get('kind')=='phase-timing' and r.get('seq')==request['seq'] and r.get('session')==prior['session']]
    phases=('interrupt','retirement_wait','mutation','publish')
    require(all(any(r.get('phase')==p and r.get('success') is True for r in timings) for p in phases),'missing successful switch phase timing')
    require(all(isinstance(r.get('elapsed_ms'),(int,float)) and math.isfinite(r['elapsed_ms']) and r['elapsed_ms']>=0 for r in timings),'invalid phase timing')
    mutations=[r for r in timings if r.get('phase')=='mutation' and r.get('success') is True and not r.get('reverting')]
    require(len(mutations)==1,'ambiguous request mutation')
    mutation=mutations[0];operation=mutation.get('operation');transaction=mutation.get('transaction')
    require(operation and transaction and verified[0].get('transaction')==transaction,'unrelated verified transaction')
    committed=[r for r in timings if r.get('operation')==operation]
    require(len(committed)==4 and {r['phase'] for r in committed}==set(phases),'incoherent operation phases')
    for row in timings:
        require(row.get('conn')==proof['conn'] and row.get('reverting') is False and row.get('success') is True and
                row.get('rev')==(proof['rev'] if row['phase']=='publish' else prior['rev']),
                'phase connection/revision differs from authoritative outcome')
    require(all(r.get('transaction')==transaction for r in committed if r['phase'] in ('mutation','publish')),
            'mutation/publish transaction differs')
    return dict(delta=d,request=request,state=state,proof=proof,phase_timings=timings,operation=operation,transaction=transaction)


def turn_verdict(events,voices,proof,fixture='france'):
    specs=json.loads((ROOT/'hil/fixtures/agent_conversation/manifest.json').read_text())['turns']
    spec=next(r for r in specs if r['id']==fixture)
    users=[r.get('text','') for r in events if r.get('kind')=='aggregator-user-turn-stopped']
    # Aggregator recorder uses text; all lexical gates apply only to STT.
    tokens=set(re.findall(r'\w+',' '.join(users).lower()))
    require(len(users)==1 and all(any(word in tokens for word in alternatives) for alternatives in spec['stt']),'strict STT failed')
    require(not any(r.get('frame') in ('ErrorFrame','FatalErrorFrame','CancelFrame') for r in events),'turn service failure')
    starts=[r for r in events if r.get('frame')=='BotStartedSpeakingFrame']
    stops=[r for r in events if r.get('frame')=='BotStoppedSpeakingFrame']
    audio=[r for r in events if r.get('kind')=='tts-transport-write' and r.get('non_silent')]
    require(starts and stops and audio and stops[-1]['monotonic_ns']>starts[0]['monotonic_ns'],'reply audio incomplete')
    voice={'angel':'flux-hannah-en','devil':'flux-rufus-en'}[proof['character']]
    syntheses=[r for r in voices if r.get('kind')=='duo-synthesis']
    require(syntheses and all(r.get('voice')==voice and r.get('character')==proof['character'] and r.get('conn')==proof['conn'] and
                             r.get('rev')==proof['rev'] for r in syntheses),'wrong voice service')
    contexts={v['context_id'] for v in syntheses}
    require(any(r.get('context_id') in contexts for r in audio),'voice has no written audio')
    completed=[r for r in events if r.get('frame')=='TTSStoppedFrame' and r.get('boundary')=='tts-written']
    require(all(any(stop.get('context_id')==context and
                    any(chunk.get('context_id')==context and chunk['monotonic_ns']<=stop['monotonic_ns'] for chunk in audio)
                    for stop in completed) for context in contexts),'synthesis context has no completed written audio')
    return dict(stt=users,voice=voice,llm_content_report_only=[r.get('text') for r in events if r.get('kind')=='aggregator-assistant-turn-stopped'])


def phase_ready(events,phase):
    if phase=='idle':return not any(r.get('frame')=='BotStartedSpeakingFrame' for r in events)
    # Ignore duplicate observer/tap copies of one frame, and consider only
    # the currently unmatched generation, not a historical completed start.
    current=None;audio=False;seen=set()
    for index,row in enumerate(events):
        frame=row.get('frame');identity=row.get('frame_id',('row',index))
        if frame and identity in seen:continue
        if frame:seen.add(identity)
        if frame=='LLMFullResponseStartFrame':current=row;audio=False
        elif frame in ('LLMFullResponseEndFrame','InterruptionFrame','ErrorFrame','FatalErrorFrame','CancelFrame','EndFrame'):
            current=None
        elif frame=='TTSStartedFrame' or row.get('kind')=='tts-transport-write':audio=True
    tts=[r for r in events if r.get('frame')=='TTSStartedFrame']
    speech=[r for r in events if r.get('frame') in ('BotStartedSpeakingFrame','BotStoppedSpeakingFrame')]
    if phase=='generating':return current is not None and not audio
    return bool(tts and speech and speech[-1]['frame']=='BotStartedSpeakingFrame')


def run_part(io,part):
    """IO-injected driver: independent parts always get fresh acquisition brackets."""
    require(part in PARTS,'unknown part')
    io.require_boot()
    if part=='install':return io.install_offline()
    before,prior=io.snapshot();status_matches(before,prior);mark=io.mark()
    if part=='connect':
        events,wire,voices=io.evidence(0)
        hello_row=next(r for r in wire if r.get('direction')=='received' and r.get('data',{}).get('op')=='hello' and r['data'].get('session')==prior['session'])
        hello=hello_row['data']
        state_row=next(r for r in wire if r.get('direction')=='sent' and r.get('data',{}).get('seq')==hello['seq'] and r['data'].get('session')==hello['session'])
        state=state_row['data'];proof_at=io.proof_received_at(prior)
        require(hello_row['monotonic_ns']<=state_row['monotonic_ns']<=proof_at,'hello/state/proof order differs')
        require(before['generation']==prior['generation'] and prior['character']=='angel','initial angel generation differs')
        proof_matches(prior,state)
        return dict(hello=hello,state=state,proof=prior,hello_at=hello_row['monotonic_ns'],state_at=state_row['monotonic_ns'],proof_at=proof_at)
    if part=='touch':
        from duo_touch_verify import observe
        return observe(io.out,io)
    if part=='quiet':
        started=io.clock();io.pause(301)
        after,proof=io.snapshot();status_matches(after,proof);delta(before,after)
        require(io.clock()-started>300 and proof['session']==prior['session'] and proof['conn']==prior['conn'],'quiet period reconnected')
        events,_,_=io.evidence(mark)
        require(not any(r.get('kind') in ('aggregator-user-turn-stopped','assistant-turn-started') or
                        r.get('frame') in ('UserStartedSpeakingFrame','BotStartedSpeakingFrame') for r in events),'quiet period had speech')
        return dict(quiet_seconds=io.clock()-started,turn=io.turn(proof))
    if part=='turns':
        first=io.turn(prior);toggle=run_part(io,'idle')
        second=toggle.pop('next_turn')
        return dict(first=first,switch=toggle,second=second)
    drops=int(part[-1]) if part.startswith('drop-state-') else 0
    if part in ('generating','playback'):
        io.stimulate('long')
        io.wait(lambda:phase_ready(io.evidence(mark)[0],part),10,'switch scheduling '+part)
    events=io.evidence(mark)[0]
    if part in ('idle','generating','playback'):require(phase_ready(events,part),'missed injection phase')
    injected_at=io.clock();io.injection_started_ns=int(injected_at*1e9);io.command('duo tap')
    def settled():
        record_injected_latency(io,io.evidence(mark)[1])
        state,shown=io.snapshot()
        return state['toggles_sent']==before['toggles_sent']+1 and not state['pending'] and shown['rev']==prior['rev']+1 and shown['character']!=prior['character']
    io.wait(settled,20,'toggle outcome and committed proof')
    after,proof=io.snapshot();events,wire,voices=io.evidence(mark)
    latency=record_injected_latency(io,wire)
    require(latency is not None,'missing toggle receipt for injection latency')
    receipt=latency['received_at_ns']
    witness=switch_witness(before,after,prior,proof,events,wire,drops=drops)
    witness.update(injected_at=injected_at,phase=part,trigger_events=events,tap_latency=latency)
    if part in ('generating','playback'):
        bracket=[r for r in events if mark<=r.get('monotonic_ns',-1)<=receipt]
        require(not any(r.get('frame') in ('ErrorFrame','FatalErrorFrame','CancelFrame','EndFrame','StopFrame') or
                        r.get('kind') in ('coordinator-exit','rollback-failed','voice-retirement-failed','session-end')
                        for r in bracket),'service/pipeline failure before toggle receipt')
        if not phase_ready(bracket,part):
            start='LLMFullResponseStartFrame' if part=='generating' else 'BotStartedSpeakingFrame'
            starts=[r['monotonic_ns'] for r in bracket if r.get('frame')==start]
            require(starts,'missed window has no observed response start')
            tail=[r for r in bracket if r['monotonic_ns']>=starts[-1]]
            require(not any(r.get('frame')=='InterruptionFrame' for r in tail),
                    'response interrupted before toggle receipt')
            endings=('LLMFullResponseEndFrame','TTSStartedFrame') if part=='generating' else ('BotStoppedSpeakingFrame',)
            require(any(r.get('frame') in endings or (part=='generating' and r.get('kind')=='tts-transport-write')
                        for r in tail),'missed window has no clean completion or playback progression')
            raise MissedInjectionWindow(witness)
    if drops:require(len([r for r in events if r.get('kind')=='dropped-state'])==drops,'wrong state drop count')
    if part=='tts-fail-next':
        failed_at=io.mark();io.stimulate('france')
        io.wait(lambda:any(r.get('kind')=='outcome' and r.get('reason')=='reverted' for r in io.evidence(failed_at)[0]),20,'voice rollback')
        io.wait(lambda:io.snapshot()[1]['character']==prior['character'] and io.snapshot()[1]['rev']>proof['rev'],10,'revert proof')
        reverted_status,reverted=io.snapshot();status_matches(reverted_status,reverted);events,wire,voices=io.evidence(failed_at)
        states=[r for r in events if r.get('kind')=='outcome' and r.get('reason')=='reverted']
        proof_matches(reverted,states[-1]);require(reverted['session']==prior['session'] and reverted['generation']==prior['generation'] and reverted['conn']==prior['conn'] and reverted['rev']==proof['rev']+1,'rollback reconnected/revision differs')
        def check_failed_turn(next_stimulus_ns):
            bracket_events,_,bracket_voices=io.evidence(failed_at)
            witness['no_respeak']=fault_bracket(bracket_events,bracket_voices,proof,failed_at,next_stimulus_ns)
        next_turn=io.turn(reverted,prior_bracket=check_failed_turn)
        require('no_respeak' in witness,'missing next stimulated-turn boundary')
        witness.update(reverted=reverted,rollback_events=events,next_turn=next_turn)
    else:witness['next_turn']=io.turn(proof)
    return witness


def duo_hashes():
    descriptor=json.loads((ROOT/'hil/fixtures/echoear360/duo-pack.json').read_text())
    hashes={c['faceplate']['hash'] for c in descriptor['characters']}
    hashes.update(f['hash'] for c in descriptor['characters'] for layer in c['layers'] for f in layer['frames'])
    require(len(hashes)==26,'expected exactly 26 duo-only assets')
    return hashes


def check_origin(url,*,socket_factory=None):
    from urllib.parse import urlsplit
    import socket
    parsed=urlsplit(url)
    require(parsed.scheme=='http' and parsed.hostname and parsed.port==7874 and not parsed.username and
            not parsed.password and not parsed.path and not parsed.query and not parsed.fragment,
            'origin must be http://LOCAL-INTERFACE:7874 with no path or credentials')
    # A successful local bind proves this advertised host belongs to this host;
    # no request is sent to the configured URL or any external service.
    factory=socket.socket if socket_factory is None else socket_factory
    with factory(socket.AF_INET,socket.SOCK_DGRAM) as probe:probe.bind((parsed.hostname,0))
    return parsed.port


@contextmanager
def owned_origin(url,out):
    import step9a_assets
    from unittest.mock import patch
    port=check_origin(url);row=dict(configured_origin=url,local_bind_verified=True,port=port,requests=[],closed=False)
    original=step9a_assets.SimpleHTTPRequestHandler.do_GET
    def observed(handler):
        if handler.server.server_address[1]==port:
            row['requests'].append(dict(path=handler.path,client=handler.client_address[0],monotonic_ns=time.monotonic_ns()))
        return original(handler)
    try:
        with patch.object(step9a_assets,'ROOT',ROOT/'hil/fixtures/echoear360'),patch.object(step9a_assets.SimpleHTTPRequestHandler,'do_GET',observed):
            with step9a_assets.asset_server(port=port):yield row
        row['closed']=True
    finally:write_new(Path(out)/'owned-origin.json',row)



class LiveIO:
    """Compose frozen serial, Jabra and R7 helpers only inside coordinator run."""
    clock=staticmethod(time.monotonic)
    def __init__(self,args,out,manifest,identity):
        import board_smoke as smoke
        from types import SimpleNamespace
        class RestoreIO(smoke.LiveIO):
            def __init__(self):pass
        self.args,self.out,self.manifest,self.identity=args,Path(out),manifest,identity
        self.base=RestoreIO();self.base.args=args;self.base.out=self.out;self.base.manifest=manifest
        self.base.python=__import__('boards').checked_esptool_python()
        self.base.preflight_tools();self.base.preflight_restore()
        self.board=None;self.boots=[];self.candidate_boot=False;self.tap_latencies=[]
    def authenticate_boot(self,since,label,manifest=None):
        from board_smoke import boot_check
        declared=self.manifest if manifest is None else manifest
        self.board.wait_for('READY transport-console',60,since=since)
        lines=[r[2] for r in self.board.lines_since(since)]
        row=dict(label=label,since=since,board=self.args.board,manifest=declared,lines=lines,passed=False)
        try:
            row['elf_sha256']=boot_check(lines,declared,self.args.board);row['passed']=True
            if manifest is None:self.candidate_boot=True
        finally:
            self.boots.append(row);write_new(self.out/f'boot-{len(self.boots):02d}.json',row)
        return row
    def require_boot(self):require(self.candidate_boot,'candidate boot identity was not authenticated')
    def reset_boot(self,label,manifest=None):
        # Match frozen LiveIO.boot: the serial reader/log is active before reset,
        # and the cursor precedes even the first ROM line. Flash leaves no-reset.
        self.candidate_boot=False
        at=self.board.cursor;self.board.reset()
        self.authenticate_boot(at,label,manifest)
        return at
    def alive(self):return authenticate(self.args.bot_dir,fault=self.args.fault)
    def command(self,text):self.alive();return self.board.command(text,90)
    def wait(self,predicate,seconds,label):
        deadline=self.clock()+seconds
        while self.clock()<deadline:
            self.alive();value=predicate()
            if value:return value
            time.sleep(.01)
        raise TimeoutError('duo wait: '+label)
    def pause(self,seconds):
        end=self.clock()+seconds
        self.wait(lambda:self.clock()>=end,seconds+2,'pause')
    def lines(self):
        # The serial logger flushes chunks, not lines. Its last chunk may stop
        # inside JSON even when the eventual wire record arrives intact.
        return [line.decode(errors='replace').rstrip('\r') for line in (self.out/'serial.log').read_bytes().split(b'\n')[:-1]]
    def json_command(self,command,prefix):
        at=self.board.cursor;self.command(command)
        if prefix=='DUO_STATUS':
            return self.wait(lambda:self.complete_records([r[2] for r in self.board.lines_since(at)],prefix),5,'complete duo status')[-1]
        return json.loads(self.board.wait_for('^'+prefix+r' \{',5,since=at).split(prefix+' ',1)[1])
    @staticmethod
    def complete_records(lines,prefix):
        try:return records(lines,prefix)
        except IncompleteRecord:return None
    def snapshot(self):
        status=validate_status(self.json_command('duo status','DUO_STATUS'))
        proofs=self.wait(lambda:self.complete_records(self.lines(),'DUO_PROOF'),5,'complete duo proof')
        require(proofs,'missing DUO_PROOF')
        shown=proofs[-1]
        # A render can lag a received state. Callers wait for the exact proof;
        # snapshot retains this transient without claiming it has been displayed.
        require(shown['generation']==status['generation'],'proof belongs to an old app generation')
        return status,shown
    def proof_received_at(self,proof):
        lines=self.board.lines_since(0)
        matches=[int(lines[index][1]*1e9) for row,index in joined_records([r[2] for r in lines],'DUO_PROOF') if row==proof]
        require(len(matches)==1,'missing/ambiguous serial proof timestamp')
        return matches[0]
    def read_rows(self,path):
        if not path.exists():return []
        # Last partial line remains unscored until the writer flushes it.
        return [json.loads(line) for line in path.read_bytes().split(b'\n')[:-1] if line]
    def evidence(self,mark):
        from board_smoke import recorder_path
        events=self.read_rows(recorder_path(self.args.bot_dir,self.pc_id))
        wire=self.read_rows(self.args.bot_dir/'dc-events.jsonl');voices=self.read_rows(self.args.bot_dir/'voice-events.jsonl')
        return tuple([r for r in rows if r.get('monotonic_ns',0)>=mark] for rows in (events,wire,voices))
    def retain(self):
        if hasattr(self,'pc_id'):
            events,wire,voices=self.evidence(0)
            record_injected_latency(self,wire);record_physical_latency(self,wire)
            write_new(self.out/'bot-evidence.json',dict(events=events,wire=wire,voices=voices))
    def mark(self):return time.monotonic_ns()
    def begin(self):
        at=self.board.cursor;self.command('demo run')
        self.board.wait_for(r'\bSTATE Ready gen=\d+',120,since=at)
        perf=self.json_command('stats','PERF');self.pc_id=perf['pc_id']
        self.board.wait_for(r'^DUO_PROOF \{',20,since=at)
        from board_smoke import recorder_path
        events=self.read_rows(recorder_path(self.args.bot_dir,self.pc_id))
        identities=[r for r in events if r.get('kind')=='duo-identity']
        require(len(identities)==1,'session identity missing')
        require(all(identities[0].get(k)==v for k,v in self.identity['manifest'].items()),'session identity differs')
    def stimulate(self,kind):
        from alsa import play_wav,resolve_jabra,set_volumes
        if not hasattr(self,'device'):
            card,self.device=resolve_jabra();set_volumes(card,self.out/'mixer.log')
        bank=ROOT/'hil/fixtures/agent_conversation';spec=next(r for r in json.loads((bank/'manifest.json').read_text())['turns'] if r['id']==kind)
        require(digest(bank/spec['file'])==spec['sha256'],'stimulus changed')
        return play_wav(bank/spec['file'],self.device)
    def turn(self,proof,*,prior_bracket=None):
        from board_smoke import turn_finished
        mark=self.mark();played=self.stimulate('france')
        if prior_bracket is not None:prior_bracket(int(played['request_monotonic_s']*1e9))
        self.wait(lambda:turn_finished(self.evidence(mark)[0],1),60,'completed turn');self.pause(2)
        events,_,voices=self.evidence(mark)
        write_new(self.out/('turn-'+str(mark)+'.json'),dict(events=events,voices=voices))
        return turn_verdict(events,voices,proof)
    def inventory(self,*,installed=True):
        from step9a_support import read
        rows=[];start=0;revision=0
        for _ in range(44):
            page=read(self.board,'inventory '+str(start)+' '+str(revision),'DISPLAY_INVENTORY')
            if revision:require(page['revision']==revision,'inventory changed during paging')
            revision=page['revision'];rows.extend(page['items']);start=page['next']
            if start>=256:break
        else:raise ValueError('inventory paging exceeded bound')
        descriptor=json.loads((ROOT/'hil/fixtures/echoear360/duo-pack.json').read_text())
        hashes={c['faceplate']['hash'] for c in descriptor['characters']}
        hashes.update(f['hash'] for c in descriptor['characters'] for layer in c['layers'] for f in layer['frames'])
        indexed={r[0]:r for r in rows}
        if not installed:return rows
        require(all(h in indexed and indexed[h][2]>0 and all(indexed[h][i] is True for i in (3,4,5)) and
                    indexed[h][6]=='none' for h in hashes),'duo hashes not pinned, resident and persisted')
        return rows
    def prepare_s3_storage(self):
        # The duo image pins its packs even while held. Boot the pinned normal
        # image first so its idle console can delete just those flash hashes.
        import make_bundle
        from echoear_aec_bench import NORMAL,NORMAL_HASH
        from step9a_support import request,terminal
        legacy,_=make_bundle.verify_bundle(NORMAL,'echoear',reference_only=True)
        require(legacy['elf_sha256']==NORMAL_HASH,'wrong S3 precondition firmware')
        pre=self.out/'fresh-storage';pre.mkdir()
        saved_out,saved_manifest,saved_reference=self.base.out,self.base.manifest,self.args.reference_only
        row=dict(method='delete present duo hashes on pinned normal firmware',bundle=str(NORMAL),
                 elf_sha256=NORMAL_HASH,deleted=[],passed=False)
        try:
            self.base.out=pre;self.base.manifest=legacy;self.args.reference_only=True
            self.base.flash(NORMAL,legacy,'candidate')
            with self.base.open() as board:
                self.board=board
                self.reset_boot('s3-delete-precondition',legacy)
                board.command('demo hold',45);board.command('disp init',90)
                before=self.inventory(installed=False);row['before']=before
                targets=duo_hashes();protected={r[0]:r for r in before if r[0] not in targets}
                present=targets & {r[0] for r in before}
                row['present']=sorted(present);row['absent']=sorted(targets-present)
                write_new(pre/'before.json',before)
                for number,sha in enumerate(sorted(present)):
                    posted=request(board,'delete',dict(hash=sha),f'duo-clean-{number:02d}')
                    result=terminal(board,posted['id'])
                    row['deleted'].append(dict(hash=sha,result=result))
                    require(result['error']=='none','duo asset delete failed')
                after=self.inventory(installed=False);row['after']=after
                require(not targets & {r[0] for r in after},'duo asset remains after delete')
                require({r[0]:r for r in after}==protected,'legacy cat/status or other assets changed')
                write_new(pre/'after.json',after);row['passed']=True
        finally:
            self.board=None;self.base.out=saved_out;self.base.manifest=saved_manifest;self.args.reference_only=saved_reference
            self.storage_precondition=row;write_new(pre/'precondition.json',row)

    def install_offline(self):
        require(self.args.part=='install','install part required')
        # The owned fixture server has already CLOSED before this reboot.
        at=self.reset_boot('offline-reboot')
        line=self.board.wait_for(r'DUO_INSTALL result=ready',60,since=at)
        restored=records([line],'DUO_INSTALL')[0]
        require(int(self.installed['fetched'])>0,'first-boot install requires fresh pack storage (cached-only install cannot pass)')
        require(int(restored['restored'])>0 and int(restored['fetched'])==0 and int(restored['pinned'])==int(self.installed['pinned']),
                'offline residency restore failed')
        mem=self.json_command('duo mem','DUO_MEM');inventory=self.inventory()
        require(mem==self.installed_mem and mem['pinned']==31 and 0<mem['used']<=491520*3//4,'offline memory residency differs')
        return dict(installed=self.installed,restored=restored,installed_mem=self.installed_mem,restored_mem=mem,installed_inventory=self.installed_inventory,restored_inventory=inventory,origin_unavailable=self.origin_record['closed'],origin=self.origin_record,precondition=getattr(self,'storage_precondition',None))
    @contextmanager
    def setup(self):
        from p4_product import candidate_storage
        from duo_nvs import erase_candidate_nvs
        with candidate_storage(self.args.board,self.args.bundle,self.out):
            if self.args.board=='echoear' and self.args.part=='install':self.prepare_s3_storage()
            self.nvs_precondition=erase_candidate_nvs(self.args.bundle,self.out,self.args.board)
            self.base.flash(self.args.bundle,self.manifest,'candidate')
            with self.base.open() as board:
                self.board=board
                try:
                    self.reset_boot('candidate-acquisition')
                    self.command('demo hold')
                    if self.args.board=='p4xc':self.command('disp provision-empty')
                    self.command('demo configure '+json.dumps(dict(url=self.args.server_url,volume_db=-6,enabled=False)))
                    # Explicit fixed fixture root, even if imported previously.
                    with owned_origin(self.args.origin,self.out) as origin:
                        self.origin_record=origin
                        at=board.cursor;self.command('duo origin '+self.args.origin);self.command('demo run')
                        line=board.wait_for(r'DUO_INSTALL result=ready',90,since=0)
                        self.installed=records([line],'DUO_INSTALL')[0]
                        self.command('demo hold');self.installed_mem=self.json_command('duo mem','DUO_MEM')
                        self.installed_inventory=self.inventory()
                    if self.args.part=='install':
                        requested={Path(r['path']).stem for r in origin['requests']}
                        require(duo_hashes()<=requested,'fresh duo fetches were not observed at the configured owned origin')
                    if self.args.part!='install':
                        self.command('demo configure '+json.dumps(dict(url=self.args.server_url,volume_db=-6,enabled=True)))
                        self.begin()
                    yield self
                finally:
                    failures=[]
                    try:self.retain()
                    except BaseException as exc:failures.append('evidence: '+str(exc))
                    for command in ('touch-enable 0','demo hold'):
                        try:self.board.command(command,45)
                        except BaseException as exc:failures.append(type(exc).__name__+': '+str(exc))
                    self.board=None
                    require(not failures,'cleanup failed: '+'; '.join(failures))


def execute(args,*,factory=LiveIO):
    from boards import require_bench
    require_bench()
    require(args.out.resolve().is_relative_to(EVIDENCE) and not args.out.exists(),'fresh step-7 output required')
    args.fault=args.part if args.part in ('drop-state-1','drop-state-4','tts-fail-next') else ''
    from duo_attempts import register,finish
    registration=register(args.out,args.board,args.part)
    result=dict(part=args.part,board=args.board,passed=False,retryable=False,cleanup_errors=[],tap_latencies=[],
                render_busy=dict(report_only=True,delta=None,rate_per_minute=None,seconds=None,observation='part not reached'),**registration)
    guard=None;io=None;miss=None
    from duo_bundle import frozen_adapters
    from p4_step14 import restoring
    from p4_parity import PIN,RESTORE_SHA256
    try:
        identity=authenticate(args.bot_dir,fault=args.fault);manifest,_=verify_bundle(args.bundle,args.board)
        write_new(args.out/'declaration.json',dict(part=args.part,board=args.board,rule=RULES[args.part],
            identity=identity,bundle=str(args.bundle.resolve()),bundle_manifest_sha256=digest(args.bundle/'manifest.json'),**registration))
        with frozen_adapters(),restoring(PIN/'restore-of-record.json',RESTORE_SHA256):
            io=factory(args,args.out,manifest,identity);guard=io.base.hold()
            with guard:
                guard.touch()
                with io.setup():
                    io.require_boot()
                    with render_busy_reporting(io,result['render_busy']):
                        result['evidence']=run_part(io,args.part)
            require(guard.record.get('clean') is True,'restore not clean')
        result['passed']=args.part!='touch' or result['evidence']['touch_verified']
    except MissedInjectionWindow as exc:
        result['error']=str(exc);result['evidence']=exc.witness;miss=exc
    except BaseException as exc:result['error']=type(exc).__name__+': '+str(exc)
    finally:
        try:
            result['restore']=guard.record if guard is not None else None
            result['boots']=getattr(io,'boots',[]);result['tap_latencies']=getattr(io,'tap_latencies',[])
            result['tap_latency_observation']='recorded' if result['tap_latencies'] else 'no measured tap/wire receipt bracket'
            from p4_parity_restore import reject_fatal
            try:
                if (args.out/'serial.log').exists():reject_fatal((args.out/'serial.log').read_text(errors='replace').splitlines(),'candidate')
                require(guard is not None and guard.record.get('clean') is True,'restore not clean or not acquired')
            except BaseException as exc:
                result['passed']=False;result['cleanup_errors'].append(str(exc));miss=None
            result['retryable']=miss is not None and not result['cleanup_errors'] and registration['attempt']==1
            from echoear_item2_campaign import write_final
            write_final(args.out/'result.json',result,guard)
            finish(args.out,args.board,args.part,result)
        finally:
            if guard is not None:guard.release_signals()
    return 0 if result['passed'] else 1


def parser():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('rules','run'))
    p.add_argument('--board',choices=('echoear','p4xc'));p.add_argument('--part',choices=PARTS)
    for key in ('bundle','out','bot-dir'):p.add_argument('--'+key,type=Path)
    p.add_argument('--origin',default='http://192.168.7.222:7874');p.add_argument('--server-url',default='http://192.168.7.222:7861')
    return p


def main(argv=None):
    p=parser();args=p.parse_args(argv)
    if args.action=='rules':print(json.dumps(RULES,indent=2));return 0
    for key in ('board','part','bundle','out','bot_dir'):require(getattr(args,key) is not None,'missing --'+key.replace('_','-'))
    args.reference_only=args.calibration=args.headless=False
    args.p4_restore_bundle=args.p4_restore_elf=None
    import board_smoke
    args.p4_factory_backup=board_smoke.FACTORY_BACKUP
    return execute(args)


if __name__=='__main__':raise SystemExit(main())
