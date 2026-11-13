#!/usr/bin/env python3
"""Derived per-turn evidence, preserving each campaign's raw result.json."""
import argparse
import copy
import json
from pathlib import Path

import numpy as np
from scipy import signal
from agent_conversation_test import cancelled_response_ids, cancelled_audio_duration, one_reply_matches, summarize, write_json, transport_counter_deltas, TRANSPORT_COUNTERS
from agent_prompts import BANK
from bench_calibration import active_mask, dbfs
from step7_support import pcm
from speech_accuracy import speech_accuracy
from voice_mute_evidence import mute_evidence


def best_alignment(x, source, begin=0, end=None):
    end = len(x) if end is None else min(len(x),end)
    sos=signal.butter(3,[300,3500],btype='bandpass',fs=16000,output='sos')
    ref=signal.sosfilt(sos,source);y=signal.sosfilt(sos,x[begin:end])
    if len(y)<len(ref):raise ValueError('Input recording too short')
    corr=signal.correlate(y,ref,mode='valid',method='fft')
    power=signal.convolve(y*y,np.ones(len(ref)),mode='valid',method='fft')
    norm=corr/np.sqrt(np.maximum(power,1)*np.dot(ref,ref))
    return int(np.argmax(norm))+begin,float(np.max(norm))


def input_metrics(directory, result, row, native_directory, cursors):
    input_path=directory/'bot/input'/f"{row['before_perf']['pc_id']}.wav"
    if not input_path.exists():return dict(available=False,reason='No received PCM')
    if native_directory is None:return dict(available=False,reason='No native turn recording')
    source=pcm(BANK/(row['prompt']+'.wav'));native=pcm(native_directory/'turn-board.wav')
    # The actual native capture is an independent, matching microphone sample
    # stream, unlike clean TTS through the colored Jabra-to-board path. Input
    # WAVs compress capture-off gaps, so a first-frame epoch is not a time map.
    native_at,native_corr=best_alignment(native,source,end=len(source)+24000)
    ref=native[native_at:native_at+len(source)]
    x=pcm(input_path);pc=row['before_perf']['pc_id'];begin=cursors.get(pc,0)
    events=[json.loads(line) for line in (directory/'bot/events.jsonl').read_text().splitlines()]
    anchors=[e for e in events if e['event']=='input-clock' and e['pc_id']==pc and e['t'] <= row['prompt_start']]
    if anchors:
        anchor=anchors[-1]
        expected=anchor['sample_offset']+round((row['prompt_start']-anchor['t'])*16000)
        begin=max(begin,expected-16000)
        limit=expected+len(source)+16000
    else:limit=None
    at,corr=best_alignment(x,ref,begin=begin,end=limit)
    cursors[pc]=at+len(source)
    mask=active_mask(source);speech=x[at:at+len(source)][mask]
    noise=x[max(0,at-4800):max(0,at-800)]
    windows=x[at:at+len(source)//320*320].reshape(-1,320)
    rms=np.sqrt(np.mean(windows**2,axis=1))
    return dict(available=corr>=.6,reason=None if corr>=.6 else 'Native/received alignment below .6; level is diagnostic',path=str(input_path),offset_samples=at,
        alignment_correlation=corr,native_fixture_correlation=native_corr,
        active_rms_dbfs=dbfs(speech),rms_p99_dbfs=dbfs([np.percentile(rms,99)]),
        peak_dbfs=20*np.log10(max(np.max(np.abs(speech))/32768,1e-12)),
        clipped_samples=int(np.count_nonzero(np.abs(speech)>=32767)),
        pre_speech_noise_dbfs=dbfs(noise) if len(noise) else None,
        onset_host_estimate=row['microphone_recording_started']+(native_at+np.flatnonzero(mask)[0])/16000
            if row.get('microphone_recording_started') is not None else None,
        timing_basis='Received PCM aligned to this turn native mic capture; capture-off gaps compress input WAV time. Onset uses native capture epoch when available.')


def restore_reply_window_classification(row):
    """Undo old whole-turn relabelling before applying the tighter bracket."""
    if not row['reply_window_transport_damaged']:
        for failure in row['failures']:
            if failure['cause']=='transport':
                failure['cause']=failure.pop('underlying_cause','unattributed output failure')


def analyze(directory):
    directory=directory.resolve();raw=json.loads((directory/'result.json').read_text())
    rows=[];corrections=[];cursors={};turn_directories={}
    for path in directory.rglob('result.json'):
        if path.parent==directory: continue
        value=json.loads(path.read_text())
        if 'prompt_start' in value and (path.parent/'turn-board.wav').exists():
            turn_directories[value['prompt_start']]=path.parent
    for index,original in enumerate(raw['turns'],1):
        row=copy.deepcopy(original);events=row.get('events',[])
        generation_starts=[e for e in events if e['event']=='llm-start']
        if row.get('prompt_end') is not None:
            row.setdefault('latency_ms',{})['llm_start']=1000*(generation_starts[0]['t']-row['prompt_end']) if generation_starts else None
            row.setdefault('stage_latency_ms',{})['stt_final -> llm_start']=(
                row['latency_ms']['llm_start']-row['latency_ms']['stt_final']
                if row['latency_ms']['llm_start'] is not None and row['latency_ms'].get('stt_final') is not None else None)
            row['stage_latency_ms']['llm_start -> llm_first_token']=(
                row['latency_ms']['llm_first_token']-row['latency_ms']['llm_start']
                if row['latency_ms']['llm_start'] is not None and row['latency_ms'].get('llm_first_token') is not None else None)
        row['live_full_text_accuracy']=speech_accuracy(row['text'],row.get('stt_text',''))
        if row.get('prompt_start') is None:
            row['server_input']=dict(available=False,reason='Turn did not reach prompt playback')
            rows.append(row);continue
        source=pcm(BANK/(row['prompt']+'.wav'));mask=active_mask(source)
        onset=row['prompt_start']+np.flatnonzero(mask)[0]/16000
        row['fixture_speech_onset']=onset
        row['fixture_onset_basis']='Host ALSA command plus clean fixture activity onset; physical I/O delay is not subtracted'
        row['vad_onset_delay_ms']=[1000*(e['t']-onset) for e in events if e['event']=='vad-start']
        row['server_input']=input_metrics(directory,raw,row,turn_directories.get(row['prompt_start']),cursors)
        cancelled=cancelled_response_ids(events)
        segments=row.get('tts_segments',[])
        for segment in segments:
            if segment['response_id'] in cancelled:
                segment['cancelled']=True;segment['complete']=False
        matched=one_reply_matches(events,segments)
        if matched != row['checks'].get('one_reply'):
            corrections.append(dict(index=index,check='one_reply',original=row['checks'].get('one_reply'),
                corrected=matched,basis='Recorded broadcast signal before service End; no fabricated cancellation or audio'))
            row['checks']['one_reply']=matched
            row['failures']=[f for f in row['failures'] if f['check']!='one_reply']
            if not matched:row['failures'].append(dict(check='one_reply',cause='reply count',detail='Audible/cancellation count'))
        row['cancelled_responses']=[dict(response_id=i,
            audible_ms=cancelled_audio_duration(events,segments,i),
            basis='llm-cancelled' if any(e['event']=='llm-cancelled' and e['response_id']==i for e in events)
                  else 'recorded broadcast before generation End') for i in cancelled]
        row['single_correct_audible_reply']=matched and row['checks'].get('acoustic_content',False)
        row['mute_evidence']=mute_evidence(events,row.get('gate_events',[]))
        row['transport_deltas']=transport_counter_deltas(row['before_perf'],row['after_perf'])
        row['transport_observed']=any((row['transport_deltas'].get(k) or 0)!=0 for k in TRANSPORT_COUNTERS)
        row['transport_damaged']=row['transport_observed']
        if 'reply_window_transport' not in row:
            row['reply_window_transport']=dict(deltas=row['transport_deltas'],basis='Retained pre-prompt / post-recording snapshots bracket all replies; includes idle margins, no tighter snapshots in this earlier capture')
        row['reply_window_transport_damaged']=any((row['reply_window_transport']['deltas'].get(k) or 0)!=0 for k in TRANSPORT_COUNTERS)
        row['turn_policy_results']={k:row['checks'].get(k) for k in ('vad','stt','llm_content')}
        complete=[e for e in events if e['event']=='llm-response' and e.get('response_id') not in cancelled]
        row['turn_policy_results']['one_uncancelled_complete_generation']=len(complete)==1 and sum(e['event']=='llm-start' for e in events)==1+len(cancelled)
        restore_reply_window_classification(row)
        if row['reply_window_transport_damaged']:
            for failure in row['failures']:
                if failure['check'] in {'playback','gate','acoustic_content'} or (
                        failure['check']=='one_reply' and not cancelled and row['turn_policy_results']['one_uncancelled_complete_generation']):
                    if failure['cause']!='transport':failure['underlying_cause']=failure['cause']
                    failure['cause']='transport'
        row['passed']=all(row['checks'].values()) and len(row['checks'])==8 and not row['failures']
        rows.append(row)
    result=dict(turns=rows,summary=summarize(rows),original_summary=raw['summary'],checker_corrections=corrections)
    write_json(directory/'analysis-8d2.json',result)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);a=p.parse_args()
    result=analyze(a.directory)
    print(json.dumps(dict(turns=len(result['turns']),passes=result['summary']['passes'],
                         corrections=len(result['checker_corrections']))))
