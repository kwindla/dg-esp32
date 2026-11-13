"""Separate candidate link experiment; validate/freeze is host-only.

Reuses the campaign's single BenchHold owner and restoration path. No acoustic
acceptance credit and no modification of the environmental device_alive gate.
"""
import argparse
import json
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from echoear_aec_bench import ROOT, NORMAL, NORMAL_HASH, sha, write, verify_bundle
from echoear_aec_bench import HISTORICAL_NORMAL_9936DA45 as NORMAL, HISTORICAL_NORMAL_9936DA45_HASH as NORMAL_HASH   # gap 3: step-9 AEC family stays bound to its declared 9936da45 restore
from echoear_aec_transport_environment import uplink_excursion, uplink_loss

ELF='76e29175ec089e47cedbfba8911ac8ba8fabec8ab975b3a50f1ce2f752c223e2'
RULE='missing-run>=4 slots; dump-rate>=2*no-dump-rate and pooled-events>=5'
TEXT='The little blue bicycle rolls beside the green meadow and over the wooden bridge.'
TRACE_POLICY=dict(enabled=True,drain_interval_s=15,max_drain_gap_s=25,
    basis='Same drain policy both arms; no-dump means no AEC PCM export. Drain before and after each synchronous export, otherwise every15s. Any overwritten/missing trace invalidates inference.',
    burst='Count every consecutive Render PLC/Zero missing run of at least4 slots once at the fourth slot. Later Ingress witnesses classify late versus unresolved diagnostically; neither attribution nor genuine loss invalidates covered runs.')
WIFI_POLICY=dict(command='wifi-observe',boundaries=['before','after'],arms=['no-dump','dump'],
    fields=['bssid','rssi_dbm','primary_channel','ps_mode'],
    coverage='Last approximately256s per300s block; retain both boundary rings and report observed BSSID changes and uncovered gaps. A roam and return entirely in a gap can remain unseen.')


def prompts():
    return [dict(id=f'link-block-{b+1}-reply-{i+1}',kind='echo',text=TEXT,
                 block=b+1,offset_s=i*60,volume_db=-6) for b in range(6) for i in range(5)]


def validate(p, output=None):
    """No ports, lock, NIC commands, bot start or output creation."""
    if p.get('experiment')!='candidate-link-soak-01' or p.get('qualification') is not False or p.get('status')!='declared':
        raise ValueError('Separate declared non-acceptance experiment required')
    if (p.get('elf_sha256'),p.get('gain_db'),p.get('boot_ref_gain_db'),p.get('volume_db'))!=(ELF,15,15,-6):
        raise ValueError('Frozen candidate/gain/volume changed')
    if p.get('blocks')!=[dict(id=i+1,mode='no-dump' if i%2==0 else 'dump',duration_s=300) for i in range(6)]:
        raise ValueError('Six alternating five-minute blocks required')
    if p.get('prompts')!=prompts() or p.get('trace_policy')!=TRACE_POLICY or p.get('decision_rule')!=RULE:
        raise ValueError('Frozen cadence, measurement or decision rule changed')
    if p.get('wifi_observation_policy')!=WIFI_POLICY or p.get('request_lateness_max_s')!=1:
        raise ValueError('Frozen WiFi observation or lateness policy changed')
    if 'host_wifi_diagnostics' in p:
        from host_wifi_diagnostics import POLICY
        if p['host_wifi_diagnostics']!=POLICY or not p.get('run_id'):
            raise ValueError('Host diagnostics policy and declared run_id required')
    if p.get('launch_status')!='NOT EXECUTED' or p.get('normal_elf_sha256')!=NORMAL_HASH:
        raise ValueError('Review-only launch/restoration contract required')
    if output is not None and Path(output).resolve()!=Path(p['output']).resolve():raise ValueError('Output differs from declaration')
    if Path(p['output']).exists():raise ValueError('Output already exists; never resume or overwrite')
    from echoear_aec_noise_gate import validate_noise_gate
    validate_noise_gate(p['noise_gate'])
    manifest=p['code_sha256']
    required={str(f.relative_to(ROOT)) for pattern in ('hil/echoear_aec_*.py','hil/arrival_trace.py','hil/wifi_observation.py','hil/host_nic.py','hil/esp_serial.py','hil/echoear_aec_resources.py','hil/sctp_recovery.py','server/*.py') for f in ROOT.glob(pattern)}
    if not required.issubset(manifest):raise ValueError('Incomplete code manifest')
    if 'host_wifi_diagnostics' in p and 'hil/host_wifi_diagnostics.py' not in manifest:
        raise ValueError('Missing host WiFi diagnostics binding')
    for name,digest in manifest.items():
        if sha(ROOT/name)!=digest:raise ValueError('Code hash mismatch: '+name)
    for bound in p['inputs']:
        if sha(bound['path'])!=bound['sha256']:raise ValueError('Input hash mismatch: '+bound['path'])
    if sha(Path(p['image'])/'manifest.json')!=p['image_bundle_sha256']:raise ValueError('Candidate manifest changed')
    verify_bundle(Path(p['image']),ELF,15);verify_bundle(NORMAL,NORMAL_HASH)
    from tts_connection_probe import supported,ProactiveConnectionTTS,SOURCE_HASHES
    import pipecat
    if not supported(ProactiveConnectionTTS,pipecat.__version__,SOURCE_HASHES):raise ValueError('Unsupported TTS runtime')
    if p['bot_config'].get('DEMO_TTS_PROACTIVE_RECONNECT')!='1':raise ValueError('Proactive reconnect required')
    return dict(passed=True,hardware_access=False,qualification=False,launch_status='NOT EXECUTED')


def trace_bursts(records, before, after):
    """Count actual consecutive playout decisions, never total-drop upper bounds.

    A missing slot is a late slot only with a later device Ingress record for the
    same RTP timestamp. Generation, complete render ticks and monotone counters
    are mandatory; mismatched counter coverage fails closed.
    """
    lo,hi=before['device_us'],after['device_us'];gen=before['generation']%65536
    rows=sorted({tuple(sorted(r.items())):r for r in records if r['reserved']==gen}.values(),key=lambda r:r['device_us'])
    renders=[r for r in rows if r['kind']==14]
    ingress={}
    for r in rows:
        if r['kind']==13:ingress.setdefault(r['timestamp'],[]).append(r['device_us'])
    own=[r for r in renders if lo<r['device_us']<=hi]
    trace=[v.get('peer_profile',{}).get('trace',{}) for v in (before,after)]
    same=before['pc_id']==after['pc_id'] and before['generation']==after['generation']
    valid=bool(same and own and all(v.get('enabled') and v.get('allocated') and v.get('overwritten')==0 for v in trace))
    valid &= bool(own and own[0]['device_us']-lo<100000 and hi-own[-1]['device_us']<100000)
    valid &= all((b['value']-a['value'])%2**32==1 and b['aux']==a['aux'] for a,b in zip(own,own[1:]))
    missing=sum(r['flags'] in (1,2) for r in own);zeros=sum(r['flags']==2 for r in own)
    delta={k:after['playout'][k]-before['playout'][k] for k in ('missing_slots','post_plc_zeros','starvation')}
    valid &= min(delta.values())>=0 and missing==delta['missing_slots'] and zeros==delta['post_plc_zeros']
    groups=[];run=[]
    for r in renders:
        consecutive=not run or (r['value']-run[-1]['value'])%2**32==1 and r['aux']==run[-1]['aux']
        if r['flags'] not in (1,2) or not consecutive:
            if run:groups.append(run)
            run=[]
        if r['flags'] in (1,2):run.append(r)
    if run:groups.append(run)
    bursts=[]
    for run in groups:
        if len(run)<4 or not lo<run[3]['device_us']<=hi:continue
        late=[any(t>r['device_us'] for t in ingress.get(r['timestamp'],[])) for r in run]
        longest=current=0
        for yes in late:
            current=current+1 if yes else 0;longest=max(longest,current)
        bursts.append(dict(at_us=run[3]['device_us'],slots=len(run),late_slots=sum(late),
                           late_burst=longest>=4,run_start_us=run[0]['device_us'],run_end_us=run[-1]['device_us']))
    # Genuine loss is an event, not a missing measurement. Attribution is only
    # diagnostic; coverage alone controls the burst count's validity.
    unresolved=sum(not b['late_burst'] for b in bursts)
    return dict(valid=bool(valid),coverage_valid=bool(valid),bursts=bursts,missing_bursts=len(bursts),
                late_bursts=sum(b['late_burst'] for b in bursts),unresolved_bursts=unresolved,
                render_missing=missing,render_zeros=zeros,counter_delta=delta)


def decision(blocks):
    complete=len(blocks)==6 and all(b.get('valid') for b in blocks)
    arms={}
    for mode in ('no-dump','dump'):
        selected=[b for b in blocks if b['mode']==mode];seconds=sum(b['duration_s'] for b in selected)
        events=sum(b['device_bursts']['missing_bursts'] for b in selected)
        arms[mode]=dict(seconds=seconds,events=events,rate_per_minute=60*events/seconds if seconds else None,
                        late_bursts=sum(b['device_bursts']['late_bursts'] for b in selected),
                        unresolved_bursts=sum(b['device_bursts']['unresolved_bursts'] for b in selected))
    pooled=sum(a['events'] for a in arms.values())
    base,test=arms['no-dump']['rate_per_minute'],arms['dump']['rate_per_minute']
    implicated=complete and pooled>=5 and test>0 and test>=2*base
    return dict(valid=complete,arms=arms,pooled_events=pooled,
                verdict=('export implicated' if implicated else 'export exonerated under declared rule') if complete else 'indeterminate: incomplete measurement',
                qualification=False,rule=RULE,
                limitation='This exploratory decision is not proof of absence; sparse events and trace/serial measurement load limit sensitivity.')


def block_boundary(board, folder, label):
    """Identical observation export followed by wifi-stats/PERF in both arms."""
    from wifi_observation import dump
    from echoear_aec_campaign import boundary
    cursor=board.cursor;started=time.monotonic()
    try:
        dump(board,folder/f'{label}-wifi-observe.json')
    finally:
        (folder/f'{label}-wifi-observe.log').write_text('\n'.join(line for _,_,line in board.lines_since(cursor))+'\n')
        write(folder/f'{label}-wifi-observe-window.json',dict(start_s=started,end_s=time.monotonic()))
    return boundary(board,folder,label)


def wifi_summary(observations, lo=None, hi=None):
    """Deduplicate overlapping history, retain null readbacks and observed changes.

    Ring overwrite is expected at 256s, not proof of uninterrupted coverage.
    Changes across gaps are observations with bounded timing, not exact roam times.
    """
    by_sequence={}
    for observation in observations:
        for row in observation.get('rows',[]):
            sequence=row['sequence']
            if sequence in by_sequence and by_sequence[sequence]!=row:
                raise ValueError('Conflicting WiFi observation sequence')
            by_sequence[sequence]=row
    rows=sorted(by_sequence.values(),key=lambda r:r['sequence'])
    if lo is not None:
        prior=[r for r in rows if r['sample_end_us']<=lo]
        rows=(prior[-1:] if prior else [])+[r for r in rows if lo<r['sample_end_us']<=hi]
    changes=[];gaps=[];previous_ap=None
    for previous,current in zip(rows,rows[1:]):
        if current['sequence']!=previous['sequence']+1:
            gaps.append(dict(after_us=previous['sample_end_us'],before_us=current['sample_begin_us'],
                             missing_samples=current['sequence']-previous['sequence']-1))
    for row in rows:
        if row.get('bssid') is None:continue
        if previous_ap and row['bssid']!=previous_ap['bssid']:
            changes.append(dict(from_bssid=previous_ap['bssid'],to_bssid=row['bssid'],
                                after_us=previous_ap['sample_end_us'],observed_by_us=row['sample_end_us'],
                                unobserved_samples=row['sequence']-previous_ap['sequence']-1))
        previous_ap=row
    distinct=lambda key:sorted({r[key] for r in rows if r.get(key) is not None})
    rssi=distinct('rssi_dbm')
    return dict(available=bool(rows),boundary_exports_available=all(o.get('available') for o in observations) and bool(observations),
                bssids=distinct('bssid'),rssi_dbm_range=[min(rssi),max(rssi)] if rssi else None,
                primary_channels=distinct('primary_channel'),ps_modes=distinct('ps_mode'),
                bssid_changes=changes,uncovered_gaps=gaps,rows=rows,
                prior_context_included=bool(rows and lo is not None and rows[0]['sample_end_us']<=lo),
                limitation=WIFI_POLICY['coverage'])


def analyze(root):
    from echoear_aec_accept import rows
    root=Path(root);read=lambda p:json.loads(p.read_text());p=read(root/'declaration.json')
    packets=rows(root/'rtp/socket-rtp.jsonl');trace=rows(root/'device-trace.jsonl');blocks=[];observations=[]
    host_samples=[];host_errors=[]
    if p.get('host_wifi_diagnostics'):
        from host_wifi_diagnostics import load_samples,block_report
        host_samples,host_errors=load_samples(root)
    for folder in sorted(root.glob('block-*')):
        if not (folder/'window.json').exists() or not (folder/'after.json').exists():continue
        timing=read(folder/'window.json');before=read(folder/'before.json');after=read(folder/'after.json')
        lo,hi=timing['host_window_s'];duration=hi-lo
        up=uplink_excursion(packets,before['pc_id'],lo,hi)
        device=trace_bursts(trace,before,after)
        wifi=[read(folder/f'{label}-wifi-observe.json') if (folder/f'{label}-wifi-observe.json').exists() else {} for label in ('before','after')]
        observations.extend(wifi)
        integrity=read(folder/'integrity.json') if (folder/'integrity.json').exists() else {'passed':False}
        prompts_done=read(folder/'prompts.json') if (folder/'prompts.json').exists() else []
        valid=bool(device['valid'] and up['valid'] and not up['stall'] and integrity['passed']
                   and len(prompts_done)==5 and all('finished_s' in c for c in prompts_done))
        blocks.append(dict(id=timing['id'],mode=timing['mode'],duration_s=duration,valid=valid,
            device_bursts=device,wifi=wifi_summary(wifi,before['device_us'],after['device_us']),
            late_delta=after['late']-before['late'],
            missing_delta=after['lost']-before['lost'],zero_delta=after['post_plc_zeros']-before['post_plc_zeros'],
            spike_seconds=sum(s['max_delay_ms']>=120 for s in up['per_second']),
            spike_seconds_per_minute=60*sum(s['max_delay_ms']>=120 for s in up['per_second'])/duration,
            qualifying_runs=up['excursions'],unique_uplink_loss=uplink_loss(packets,before['pc_id'],lo,hi),
            delay_baseline='5th percentile within each block',integrity=integrity,prompts=prompts_done))
        if p.get('host_wifi_diagnostics'):
            blocks[-1]['host_wifi']=block_report(root,folder,lo,hi,host_samples)
    result=decision(blocks);result['blocks']=blocks;result['wifi']=wifi_summary(observations)
    result['measurement_limit']=p['measurement_limit']
    if p.get('host_wifi_diagnostics'):
        result['host_wifi_diagnostics']=dict(policy=p['host_wifi_diagnostics'],scan_read_errors=host_errors,
            diagnostic_only=True)
    cleanup=read(root/'result.json') if (root/'result.json').exists() else {}
    result['cleanup_passed']=bool(cleanup.get('restored_elf_sha256')==NORMAL_HASH and cleanup.get('mixer_equal') and cleanup.get('lock_released') and not cleanup.get('cleanup_errors'))
    result['final_integrity_passed']=bool(cleanup.get('completed') and cleanup.get('resources',{}).get('all_passed') and cleanup.get('rf_active',{}).get('passed') and cleanup.get('sctp_recovery',{}).get('passed'))
    if not result['cleanup_passed'] or not result['final_integrity_passed'] or cleanup.get('error'):
        result.update(valid=False,verdict='indeterminate: incomplete run or cleanup')
    return result


def finalize_analysis(root, original_error=None):
    """A partial acquisition or analyzer error must not mask the original stop."""
    root=Path(root)
    if not (root/'declaration.json').exists():return
    try:
        missing=[name for name in ('rtp/socket-rtp.jsonl','device-trace.jsonl') if not (root/name).exists()]
        result=(dict(valid=False,qualification=False,verdict='indeterminate: acquisition ended before evidence was available',missing_inputs=missing)
                if missing else analyze(root))
        write(root/'analysis.json',result)
    except Exception as error:
        if original_error is None:raise
        # Cleanup has already completed. Even an unwritable evidence volume
        # must leave the original acquisition exception as the propagated error.
        try:write(root/'analysis-error.json',dict(error=f'{type(error).__name__}: {error}',original_error=f'{type(original_error).__name__}: {original_error}'))
        except Exception:pass
        print('Final analysis failed; preserving original acquisition exception',file=sys.stderr)


def wait_until(deadline, cancel):
    while time.monotonic()<deadline:
        if cancel.wait(min(.1,max(0,deadline-time.monotonic()))):raise RuntimeError('Soak stopped')


def stimulus_worker(hold, cells, start, cancel, folder):
    """Only host evaluation commands; never touches serial while export runs."""
    done=[]
    for c in cells:
        deadline=start+c['offset_s'];wait_until(deadline,cancel)
        actual=time.monotonic()
        cid=c['id']+'-a1'
        attempt=dict(id=cid,deadline_s=deadline,request_s=actual,lateness_s=actual-deadline)
        write(folder/'prompts.json',done+[attempt])
        if actual-deadline>1:raise RuntimeError('TTS request deadline missed; no shifting')
        hold.send('speak',cid)
        end=deadline+40
        while time.monotonic()<end:
            if cancel.wait(.1):raise RuntimeError('Soak stopped')
            own=[e for e in hold.events() if e.get('case_id')==cid]
            if any(e.get('frame') in ('ErrorFrame','FatalErrorFrame') or e['event'] in ('evaluation-error','tts-reconnect-failed','tts-reconnect-refused','liveness-alarm') for e in own):raise RuntimeError('TTS/transport error')
            if any(e.get('frame')=='BotStoppedSpeakingFrame' for e in own):break
        else:raise RuntimeError('TTS did not finish before next cadence')
        # First BotStopped is sufficient only for the pinned one-sentence TEXT.
        # Recorder writes are context keyed; the case id may only be on metrics.
        contexts={e.get('context_id') for e in own if e['event']=='tts-generation-metric-start'}
        writes=[e for e in hold.recorded() if e.get('kind')=='tts-transport-write' and e.get('context_id') in contexts and e.get('non_silent')]
        if not writes:raise RuntimeError('Missing non-silent TTS delivery witness')
        hold.send('seal',cid)
        done.append(dict(**attempt,finished_s=time.monotonic(),contexts=sorted(contexts)))
        write(folder/'prompts.json',done)
    return done


def run(plan, output, *, expected_sha256=None, hold_factory=None):
    """Only explicit --link-soak launch reaches this function."""
    from echoear_aec_campaign import BenchHold,command,clock_anchor,observer_ok
    from echoear_aec_capture import dump_capture
    from echoear_aec_accept import rows,check_esptool,require_zero_i2s
    from echoear_aec_resources import check_reply_resources,liveness_reply
    from echoear_aec_campaign_analysis import delivery_counters
    from arrival_trace import drain
    import numpy as np
    import host_nic
    p=json.loads(Path(plan).read_text());validate(p,output)
    if expected_sha256 is None or sha(plan)!=expected_sha256:raise ValueError('Frozen declaration SHA256 required')
    check_esptool()
    hold_factory=hold_factory or BenchHold
    def interrupted(signum,frame):raise KeyboardInterrupt('Stop requested; restore normal image')
    old={s:signal.signal(s,interrupted) for s in (signal.SIGINT,signal.SIGTERM)}
    out=Path(output)
    diagnostics=None
    try:
        if p.get('host_wifi_diagnostics'):
            from host_wifi_diagnostics import HostWifiDiagnostics
            diagnostics=HostWifiDiagnostics(out);diagnostics.start()
        with hold_factory(p,dict(id=p.get('run_id','link-soak-01'),volume_db=-6),out) as h:
            command(h.board,'peer-option arrival-trace 1')
            h.connect(p['prompts'],[])
            baseline=np.load(out/'quiet.npz');baseline_pcm=baseline['pcm'];baseline_timing=baseline['timing']
            last_drain=[time.monotonic()]
            operations=[]
            def trace_drain():
                begin=time.monotonic();drain(h.board,out/'device-trace.jsonl');end=time.monotonic()
                operations.append(dict(kind='trace',start_s=begin,end_s=end,gap_s=begin-last_drain[0]))
                write(out/'operations.json',operations)
                if begin-last_drain[0]>TRACE_POLICY['max_drain_gap_s']:raise RuntimeError('Trace drain deadline exceeded')
                last_drain[0]=end
            trace_drain()
            for block in p['blocks']:
                folder=out/f"block-{block['id']:02d}";folder.mkdir();h.send('health',None)
                if diagnostics:diagnostics.boundary(folder,'before')
                before,pre=block_boundary(h.board,folder,'before');anchor=clock_anchor(h.board)
                origin=(anchor['lower_s']+anchor['upper_s'])/2-anchor['coordinate']/1e6
                nic=host_nic.snapshot();start=time.monotonic();deadline=start+300
                cancel=threading.Event();cells=[c for c in p['prompts'] if c['block']==block['id']]
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future=pool.submit(stimulus_worker,h,cells,start,cancel,folder)
                    try:
                        while time.monotonic()<deadline:
                            if future.done():future.result()
                            if time.monotonic()-last_drain[0]>=TRACE_POLICY['drain_interval_s'] or block['mode']=='dump':trace_drain()
                            if block['mode']=='dump' and time.monotonic()<deadline:
                                begin=time.monotonic();path=folder/f"dump-{len(list(folder.glob('dump-*.npz'))):03d}.npz"
                                # Re-export the unchanged baseline; never aec-record.
                                capture=dump_capture(h.board,path,timeout=25)
                                operations.append(dict(kind='aec-export',block=block['id'],start_s=begin,end_s=time.monotonic()))
                                write(out/'operations.json',operations)
                                if not np.array_equal(capture['pcm'],baseline_pcm) or not np.array_equal(capture['timing'],baseline_timing):raise RuntimeError('Baseline export changed')
                                trace_drain()
                            else:time.sleep(.1)
                        future.result()
                    finally:cancel.set()
                trace_drain();h.send('health',None)
                if diagnostics:diagnostics.boundary(folder,'after')
                after,post=block_boundary(h.board,folder,'after')
                timing=dict(**block,scheduled_start_s=start,target_end_s=deadline,finished_s=time.monotonic(),
                    host_window_s=[origin+before['device_us']/1e6,origin+after['device_us']/1e6],anchor=anchor)
                write(folder/'window.json',timing)
                # Include records generated while obtaining after PERF.
                trace_drain();require_zero_i2s(pre+post)
                resources=check_reply_resources(before,after,intermediate=pre+post)
                delivery=delivery_counters(before,after);live=liveness_reply(before,after)
                rf=host_nic.gate(nic,host_nic.snapshot())
                own=[e for e in h.events() if e['t']>=start]
                healthy=observer_ok(own) and not any(e.get('frame') in ('ErrorFrame','FatalErrorFrame') or e['event'] in ('evaluation-error','liveness-alarm','tts-reconnect-refused','tts-reconnect-failed') for e in own)
                integrity=dict(resources=resources,delivery=delivery,liveness=live,rf=rf,observer=healthy,
                    passed=bool(resources['all_passed'] and delivery['passed'] and live['passed'] and rf['passed'] and healthy))
                write(folder/'integrity.json',integrity)
                if not integrity['passed']:raise RuntimeError('Soak integrity failure')
            h.finish()
    finally:
        for s,handler in old.items():signal.signal(s,handler)
        if diagnostics:
            try:diagnostics.stop()
            except Exception as error:print(f'Host WiFi diagnostic cleanup unavailable: {error}',file=sys.stderr)
        finalize_analysis(out,sys.exc_info()[1])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('declaration',type=Path);parser.add_argument('--validate',action='store_true');parser.add_argument('--analyze',type=Path)
    args=parser.parse_args()
    if args.analyze:print(json.dumps(analyze(args.analyze),indent=2))
    elif args.validate:print(json.dumps(validate(json.loads(args.declaration.read_text())),indent=2))
    else:parser.error('Use campaign runner --link-soak for explicit acquisition')
