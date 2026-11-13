#!/usr/bin/env python3
"""Retained 8f2 gates, historical board-flow audit and Fable residual comparison."""
import argparse
import json
from pathlib import Path
import subprocess
from esp_serial import ROOT
from uplink_continuity import link_tail,recorded_audio_idle_windows
from step8e2b_analysis import perf_history,voice_device_cadence,owner_gate,lcd_memory_gate,device_send_cadence,console_windows,voice_owner_gate


def load(path):return json.loads(Path(path).read_text())
def write(path,value):Path(path).write_text(json.dumps(value,indent=2)+'\n')


def residual(turns):
    totals=dict(iterations=0,iteration_wall_us=0,main_loop_us=0,control_us=0,wait_us=0,heap_sample_us=0,heap_sample_calls=0)
    for turn in turns:
        a,b=turn['before_perf']['peer_profile'],turn['after_perf']['peer_profile']
        totals['iterations']+=b['loop_iteration']['calls']-a['loop_iteration']['calls']
        for name,key in (('loop_iteration','iteration_wall_us'),('main_loop','main_loop_us'),('control','control_us'),('wait','wait_us'),('heap_sample','heap_sample_us')):
            if name in a and name in b and a[name].get('wall_us') is not None and b[name].get('wall_us') is not None:
                totals[key]+=b[name]['wall_us']-a[name]['wall_us']
        if 'heap_sample' in a:totals['heap_sample_calls']+=b['heap_sample']['calls']-a['heap_sample']['calls']
    remaining=totals['iteration_wall_us']-sum(totals[k] for k in ('main_loop_us','control_us','wait_us'))
    return dict(**totals,residual_us=remaining,residual_per_iteration_us=remaining/totals['iterations'] if totals['iterations'] else None,
        measured_heap_sample_per_iteration_us=totals['heap_sample_us']/totals['iterations'] if totals['iterations'] else None,
        basis='Fable method: sum same-turn PERF deltas, iteration wall minus main_loop/control/wait, divided by iteration count. Inclusive wall times; no CPU inference.')


def traces(path):
    rows=[json.loads(line) for p in Path(path).rglob('*arrivals.jsonl') for line in p.read_text().splitlines()]
    return list({tuple(sorted(r.items())):r for r in rows}.values())


def voice_gates(path):
    path=Path(path);r=load(path/'result.json');turns=r['turns'];trace=traces(path);samples=perf_history(path/'serial.log')
    if not turns:return dict(available=False)
    start,end=turns[0]['before_perf']['device_us'],turns[-1]['after_perf']['device_us']
    windows=[]
    for a,b in zip(turns,turns[1:]):
        if a['after_perf']['generation']!=b['before_perf']['generation']:
            windows.append(dict(start_us=a['after_perf']['device_us'],end_us=b['before_perf']['device_us'],category='reconnect'))
    slow=[x for x in trace if x['kind']==8 and start<=x['device_us']<=end]
    # Counts reset at reconnect. Sum the continuous same-generation PERF brackets.
    selected=[s for s in samples if start<=s['device_us']<=end]
    calls=sum(b['peer_profile']['loop_iteration']['calls']-a['peer_profile']['loop_iteration']['calls'] for a,b in zip(selected,selected[1:]) if a['generation']==b['generation'])
    overwritten=max((s['peer_profile']['trace']['overwritten'] for s in selected),default=1)
    return dict(conversation_passed=r['passed'],passes=sum(t['passed'] for t in turns),turns=len(turns),elapsed_s=r.get('elapsed_s'),
        checks={name:dict(passed=sum(t['checks'].get(name) is True for t in turns),failed=sum(t['checks'].get(name) is False for t in turns),unavailable=sum(t['checks'].get(name) is None for t in turns)) for name in turns[0]['checks']},
        failures=[dict(turn=i+1,prompt=t['prompt'],failures=t['failures']) for i,t in enumerate(turns) if not t['passed']],
        device_send=voice_device_cadence(path),owner=voice_owner_gate(path),
        lcd=lcd_memory_gate(samples,reconnects=sum(f['kind']=='reconnect' for f in r['faults'])),
        soak_checks=r.get('soak_checks'),faults=r.get('faults'),residual=residual(turns))


def historical_board_flow(base):
    result={}
    for name in ('step8e2a','step8e2a2','step8e2b','step8f'):
        directory=base/name;arms=[]
        for file in sorted(directory.rglob('tc-state.json')):
            # Ignore copied source/images; retained acquisition arms have result files.
            folder=file.parent
            if not (folder/'result.json').exists():continue
            r=load(folder/'result.json');tc=load(file)
            counters=r.get('tc_counters',{})
            owned={dev:next((q for q in rows if q.get('handle')=='8e03:' and q.get('parent')=='8e01:3'),None) for dev,rows in counters.items()}
            quiet=load(folder/'quiet-nic.json') if (folder/'quiet-nic.json').exists() else {}
            pairs=[]
            for p in folder.rglob('ice-pairs.jsonl'):
                pairs.extend(json.loads(l) for l in p.read_text().splitlines())
            # Nested campaign parents belong to their child records.
            if not owned and not quiet and not pairs:continue
            deltas=r.get('transport_deltas')
            if deltas is None and 'before' in r and 'after' in r:
                a,b=r['before'].get('perf',r['before']),r['after'].get('perf',r['after'])
                deltas={k:b[k]-a[k] if k in a and k in b else None for k in ('lost','late','concealed','rebuffers')}
            if deltas is not None and 'before' in r and 'after' in r:
                a,b=r['before'].get('perf',r['before']),r['after'].get('perf',r['after'])
                deltas=dict(deltas,late=b['late']-a['late'] if 'late' in a and 'late' in b else None)
            arms.append(dict(path=str(folder.relative_to(directory)),netem=r.get('netem'),outage_s=r.get('outage_s'),
                sessions=[dict(session_ordinal=i,pc_id=pc) for i,pc in enumerate(dict.fromkeys(p['pc_id'] for p in pairs),1)],
                tc_owned={dev:{k:q.get(k) for k in ('bytes','packets','drops')} if q else None for dev,q in owned.items()},
                board_flow_tx_bytes=quiet.get('board_flow_tx_bytes'),board_flow_rx_bytes=quiet.get('board_flow_rx_bytes'),
                ice_pairs=pairs,device_deltas=deltas,restoration=tc.get('status')))
        result[name]=arms
    return result




def rescore_voice_content(path,*,matcher=None,level_centers=None):
    """Re-evaluate retained PCM; original acquisition verdicts remain immutable."""
    import copy
    from playback_quality import decoded_match,transport_gate,acoustic_content_verdict,REPLY_SPL_CENTERS
    matcher=matcher or decoded_match
    level_centers=REPLY_SPL_CENTERS if level_centers is None else level_centers
    from step7_support import pcm
    from agent_conversation_test import playback_counter_deltas,one_reply_matches,reply_gate_window,CHECKS
    path=Path(path);original=load(path/'result.json');results=[]
    for index,raw in enumerate(original['turns'],1):
        segments=copy.deepcopy(raw.get('tts_segments',[]));decoded_path=path/f'turn{index:03}'/'rx.wav'
        if not decoded_path.exists() or not all(k in raw for k in ('before_perf','after_perf','reply_window_transport','events')):
            results.append(dict(turn=index,prompt=raw['prompt'],original_passed=raw['passed'],original_checks=raw['checks'],
                corrected_passed=raw['passed'],corrected_checks=raw['checks'],decoded_matches=[],
                retained_other_failures=raw['failures'],transport=raw.get('reply_transport_gate'),
                rescore_available=False,reason='Incomplete retained PCM or turn evidence; original verdict retained'))
            continue
        decoded=pcm(decoded_path)
        for segment in segments:
            if segment['cancelled']:continue
            level=segment.get('jabra_level',{})
            spl=level.get('board_mic_tone_transfer_spl');rise=level.get('rms_rise_db')
            if spl is not None and rise is not None:
                target=level.get('target_dbfs',original.get('playback_target_dbfs',-22))
                center=level_centers.get(target,73.+(target+25));window=[center-7,center+7]
                level.update(spl_window_db=window,passed=rise>=10 and window[0]<=spl<=window[1])
            segment['decoded_content']=matcher(decoded,pcm(segment['source_path']))
            verified=(segment.get('correlation',0)>=.6 and segment.get('board',{}).get('correlation',0)>=.6
                and segment.get('jabra_level',{}).get('passed') is True and segment['decoded_content']['passed'])
            segment.update(verified=verified,complete=verified)
        checks=dict(raw['checks']);complete=[s for s in segments if not s['cancelled']]
        counters=playback_counter_deltas(raw['before_perf'],raw['after_perf'])
        checks['playback']=bool(complete) and all(s['verified'] for s in complete) and all(v>0 for v in counters.values())
        checks['one_reply']=one_reply_matches(raw['events'],segments)
        window=raw['reply_window_transport'];transport=transport_gate(window['before'],window['after'])
        closed,opened=reply_gate_window(raw.get('gate_events',[]))
        if 'overlap' in raw:
            # This qualification has no overlap prompts. Other campaigns retain
            # their original gate verdict instead of guessing overlap evidence.
            checks['gate']=raw['checks']['gate']
        elif closed is None or opened is None or not checks['playback'] or not transport['passed']:
            checks['gate']=False
        elif not complete[0].get('acoustic_timing_available',True):checks['gate']=None
        else:checks['gate']=closed<=complete[0]['onset']+.1 and opened>=max(s['end'] for s in complete)-.1
        stt=raw.get('acoustic_stt_passed',raw['checks'].get('acoustic_content') is True)
        checks['acoustic_content']=acoustic_content_verdict(stt,segments)
        extra_failures=[f for f in raw['failures'] if f['check'] not in checks]
        passed=len(checks)==len(CHECKS) and all(v is not False for v in checks.values()) and not extra_failures
        results.append(dict(turn=index,prompt=raw['prompt'],original_passed=raw['passed'],original_checks=raw['checks'],
            corrected_passed=passed,corrected_checks=checks,decoded_matches=[s['decoded_content'] for s in complete],
            level_gates=[s.get('jabra_level') for s in complete],
            retained_other_failures=extra_failures,transport=transport))
    return dict(original_passes=sum(r['original_passed'] for r in results),corrected_passes=sum(r['corrected_passed'] for r in results),
        turns=len(results),rows=results,
        basis='Same retained PCM, board/Jabra correlations, transferred levels, counters, gate timestamps and events. Recompute matcher, predeclared level window and dependent checks; VAD/STT/LLM/session and raw evidence unchanged. No retake.')

def retained_content_reanalysis(path):
    from playback_quality import decoded_match,transport_gate,acoustic_content_verdict
    from step7_support import pcm
    results=[]
    for index in (5,6,14,50):
        directory=Path(path)/f'turn{index:03}';r=load(directory/'result.json')
        matches=[decoded_match(pcm(directory/'rx.wav'),pcm(s['source_path'])) for s in r['tts_segments'] if not s['cancelled']]
        window=r['reply_window_transport']
        results.append(dict(turn=index,original_checks=r['checks'],decoded_matches=matches,
            transport_gate=transport_gate(window['before'],window['after']),
            acoustic_content=acoustic_content_verdict(r['checks']['acoustic_content'],r['tts_segments'])))
    return results

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--qualification',type=Path);a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=True)
    base=ROOT.parent/'evidence'
    write(a.out/'historical-board-flow.json',historical_board_flow(base))
    comparison={}
    for name,path in [('8f-approved',base/'step8f/voice-soak-approved01/turns'),('8e2b-candidate',base/'step8e2b/candidate-01-soak/turns')]:
        if (path/'result.json').exists():comparison[name]=residual(load(path/'result.json')['turns'])
    write(a.out/'residual-comparison.json',comparison)
    write(a.out/'8f-content-reanalysis.json',retained_content_reanalysis(base/'step8f/voice-soak-approved01/turns'))
    write(a.out/'8f-approved-new-gates.json',voice_gates(base/'step8f/voice-soak-approved01/turns'))
    if a.qualification:
        voice=a.qualification/'voice-soak/turns'
        if (voice/'result.json').exists():
            gates=voice_gates(voice);write(a.out/'voice-gates.json',gates)
            if gates.get('available') is not False:write(a.out/'voice-content-rescore.json',rescore_voice_content(voice))
            if 'residual' in gates:
                comparison['8f2']=gates['residual'];write(a.out/'residual-comparison.json',comparison)
        quiet=a.qualification/'quiet0'
        if (quiet/'result.json').exists():
            r=load(quiet/'result.json');samples=perf_history(quiet/'serial.log');trace=traces(quiet)
            start,end=r['before']['device_us'],r['after']['device_us']
            calls=r['after']['peer_profile']['loop_iteration']['calls']-r['before']['peer_profile']['loop_iteration']['calls']
            slow=[x for x in trace if x['kind']==8 and start<=x['device_us']<=end]
            write(a.out/'quiet0-gates.json',dict(passed=r['passed'],room_baseline=r['room_baseline'],
                cadence=r['cadence'],diagnostic_log_drops=r['diagnostic_log_drops'],lcd=lcd_memory_gate(samples),
                owner=owner_gate(slow,calls,covered=r['after']['peer_profile']['trace']['overwritten']==0,trace=trace,snapshots=samples)))
        offline=a.qualification/'offline/test'
        if (offline/'result.json').exists():
            r=load(offline/'result.json');samples=perf_history(offline/'serial.log');trace=traces(offline)
            packets=[json.loads(l) for l in (offline/'rtp/rtp.jsonl').read_text().splitlines()]
            starts=[t['prompt_start'] for t in r['runs'] if 'prompt_start' in t]
            ends=[t['reply_window_transport']['end'] for t in r['runs'] if 'reply_window_transport' in t]
            link=link_tail(packets,min(starts),max(ends),excluded_windows=recorded_audio_idle_windows(offline/'serial.log',end=max(ends))) if starts and ends else dict(passed=None)
            write(a.out/'offline-gates.json',dict(passes=r.get('pass_count'),passed=r['passed'],reruns=len(r['reruns']),
                link_basis='Device-acknowledged audio idle windows only; legacy captures without acknowledgements remain raw',link=link,lcd=lcd_memory_gate(samples),cadence=device_send_cadence(trace,samples[0]['device_us'],samples[-1]['device_us'],console_windows(offline/'serial.log')+recorded_audio_idle_windows(offline/'serial.log',clock='device_us',end=samples[-1]['device_us']),snapshots=samples)))
    print(json.dumps(comparison,indent=2))


if __name__=='__main__':main()
