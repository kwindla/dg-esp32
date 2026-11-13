"""Synthetic rehearsal of the production cohort loop; forbids external IO."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def observation(*,jabra=-55,board=-55,wired=180000000,wifi=100000):
    before=dict(monotonic_s=0,interfaces={n:dict(rx_bytes=0,tx_bytes=0) for n in ('enp7s0','wlp8s0')})
    after=deepcopy(before);after['monotonic_s']=30
    after['interfaces']['enp7s0']['rx_bytes']=wired;after['interfaces']['wlp8s0']['tx_bytes']=wifi
    return dict(jabra=dict(dbfs=jabra,wav='synthetic-jabra.wav'),board=dict(dbfs=board,wav='synthetic-board.wav'),nic_before=before,nic_after=after)


def rehearse(plan,out,*,all_originals=False,jabra_policy=None):
    import e2e_common as e2e
    import step9a_support as display
    from playback_quality import TRANSPORT_COUNTERS
    from step9a_r2_checks import DisplayMonitor
    from step9a_merge_checks import merge_gate
    from echoear_acoustic_qualification import gate as acoustic_gate,PROFILE
    from echoear_requalification import preflight_callback,environment_gate,observation_gate
    calls=[];results={}

    class Board:
        cursor=0
        def __init__(self,*a,log_path,**kw):
            self.log=Path(log_path);self.log.write_text('STACK task=app-control free_bytes=9000 priority=4\n');self.display_monitor=DisplayMonitor()
            self.display_monitor.line('STACK task=app-control free_bytes=9000 priority=4')
        def command(self,*args,**kw):return 'SYNTHETIC OK'
        def __enter__(self):return self
        def __exit__(self,*a):pass

    class Bot:
        def __init__(self,path,mode,**kw):
            self.path=Path(path);self.path.mkdir();rtp=self.path.parent/'rtp';rtp.mkdir()
            with (rtp/'rtp.jsonl').open('w') as stream:
                for i in range(3501):stream.write(json.dumps(dict(pc_id='synthetic-peer',ssrc=1,seq=i,timestamp=i*960,observed_ms=i*20))+'\n')
        def __enter__(self):return self
        def __exit__(self,*a):pass

    def cohort(name,count,live,*,failure=None,defer=False,monitor_failure=False,first=1):
        path=out/name;seen=[]
        check=preflight_callback(plan,name,out/'deferrals.jsonl',synthetic=True)
        def turn(board,bot,device,turn_out,is_live,**kw):
            turn_out.mkdir();index=len(seen)+1
            eligibility=kw['preflight'](index,observation(board=-54.99 if defer else -55))
            if not eligibility['passed']:return dict(passed=False,preflight_deferred=True,scored=False,eligibility=eligibility)
            seen.append(dict(index=index,live=is_live,reset=kw['reset'],disconnect=kw['disconnect']))
            sample=dict.fromkeys(TRANSPORT_COUNTERS,0)
            sample.update(playout_schema=2,generation=1,pc_id='synthetic-peer',
                heap=dict(internal=dict(minimum_free=90000,largest=49000)),failed_allocations=dict(internal=0,total=0),
                display=dict(rendered=index*100,animation=False,lock_timeouts=0,lock_failures=0,engine_snapshot=True,
                    lcd_initialized=True,lcd_tx_captured=True,priority_applies=1,last_priority_written=1,tx_pri=1))
            line='PERF '+json.dumps(sample)+'\n'
            with board.log.open('a') as log:log.write(line)
            board.display_monitor.feed(line.encode());board.display_monitor.line(line.strip())
            if monitor_failure:
                raw=b'E (999) spi_master: DMA TX underflow\n'
                with board.log.open('ab') as log:log.write(raw)
                board.display_monitor.feed(raw)
            passed=index!=failure
            # r15 (decision 138a S1): a synthetic, complete 20 ms send trace for the scored window (device clock).
            base=index*10_000_000;sample['device_us']=base;sample['peer_profile']=dict(trace=dict(enabled=True,overwritten=0,capacity=8192))
            reply_end=dict(deepcopy(sample),device_us=base+5_500_000)
            with (turn_out/'arrivals.jsonl').open('w') as stream:
                for k in range(310):
                    stream.write(json.dumps(dict(device_us=base-100_000+20_000*k,timestamp=k*960,ssrc=1,value=0,aux=0,seq=k,bytes=8,kind=12,flags=0,reserved=1))+'\n')
            return dict(passed=passed,scored=True,pc_id='synthetic-peer',temperature='cold' if kw['reset'] else 'warm',
                before_perf=sample,after_perf=deepcopy(sample),prompt_start=index*10+2,before_perf_completed_at=index*10+1.5,
                reply_window_transport=dict(end=index*10+7,after=reply_end),source_trace_export=dict(overwritten=0),
                display_merge=dict(passed=passed,full_reply_fps=dict(passed=passed)),eligibility=eligibility,
                room_baseline=dict(noise_contaminated=False),
                gain_readback=dict(protection=PROFILE,analog_db=30,digital_percent=125,register=0x1a),
                mic_headroom=dict(passed=True,unprotected_headroom_db=4),mic_clip_gate=dict(passed=True),
                qualification_volume_relative_db=0,perf_acquisition_gate=dict(passed=True),host_nic=eligibility['nic'])
        args=SimpleNamespace(out=path,port=None,runs=count,spl=65,warm=True,no_retakes=True,elf=None,server_url=None,
            ice_servers=[],option=[],display='cat',diagnostic_noisy=False,preflight=check,
            all_originals=all_originals,jabra_policy=jabra_policy,original_start=first)
        with patch.object(e2e,'turn',side_effect=turn):code=e2e.run(args,live)
        data=json.loads((path/'result.json').read_text())
        calls.append(dict(scenario=name,original_calls=seen,code=code,runs=len(data['runs']),reruns=len(data['reruns'])))
        if monitor_failure:
            return (code==1 and len(seen)==len(data['runs'])==1 and not data['reruns'] and
                    not data['runs'][0]['passed'] and bool(data['runs'][0].get('fatal_monitor_error')))
        if first>1:
            return (code==0 and [r['run'] for r in data['runs']]==list(range(first,count+1)) and
                    len(seen)==count-first+1 and not data['reruns'])
        if failure:return code==1 and len(seen)==(count if all_originals else failure) and not data['reruns'] and not data['passed']
        if defer:return code==2 and not seen and not data['runs'] and not data['reruns'] and data['next_original_slot']==1
        gate=merge_gate(data,(path/'serial.log').read_text()) if count==3 else acoustic_gate(data,['synthetic-peer'])
        from echoear_item2_campaign import record_source_cadence,source_cadence_enabled,source_cadence_config
        record_source_cadence(path,data,source_cadence_enabled(plan),stage=name,**source_cadence_config(plan))
        observations=observation_gate(plan,name,data,(path/'serial.log').read_text())
        (path/'observation-gate.json').write_text(json.dumps(observations,indent=2)+'\n')
        return code==0 and len(seen)==count and not data['reruns'] and data['passed'] and gate['passed'] and observations['observations_passed'] and [c['reset'] for c in seen]==[True]+[False]*(count-1)

    with ExitStack() as mocks:
        for target in ('subprocess.Popen','subprocess.run','socket.socket','serial.Serial','fcntl.flock'):
            mocks.enter_context(patch(target,side_effect=AssertionError('Host dry-run forbids hardware, bench, network and services')))
        mocks.enter_context(patch.object(e2e,'resolve_jabra',return_value=('synthetic-card','synthetic-device')))
        mocks.enter_context(patch.object(e2e,'set_volumes'))
        mocks.enter_context(patch.object(e2e,'deepgram_key',return_value='synthetic-no-credential-read'))
        mocks.enter_context(patch.object(e2e,'BotServer',Bot));mocks.enter_context(patch.object(e2e,'EspSerial',Board))
        mocks.enter_context(patch.object(display,'read',return_value=dict(dead=False,failed=False,dma=[0,0])))
        for stage in (plan['order'][0],plan['order'][4],plan['order'][5]):
            results[stage['id']]=cohort(stage['id'],stage['runs'],stage['mode']=='live')
        results['failed_original_not_retaken']=cohort('synthetic-failed-original',3,True,failure=2)
        results['preflight_deferral_consumes_no_original']=cohort('synthetic-deferral',3,True,defer=True)
        results['scored_monitor_failure_retained']=cohort('synthetic-monitor-failure',3,True,monitor_failure=True)
        results['resume_only_unscored_slots']=cohort('synthetic-priority-resume',5,True,first=2)
    results['both_room_floors_at_minus55_admitted']=environment_gate(plan,observation())['passed']
    from echoear_item2_campaign import source_cadence_enabled,resume_enabled,aggregate_quiet_live
    if source_cadence_enabled(plan):
        # r15 (decision 138a): one 65 ms eligible send interval fails the original; a missing trace span fails closed.
        from echoear_source_cadence import source_window
        rows=[dict(device_us=-100_000+20_000*k+(45_000 if k>=100 else 0),timestamp=k*960,ssrc=1,value=0,aux=0,seq=k,bytes=8,kind=12,flags=0,reserved=1) for k in range(310)]
        perf=lambda us:dict(device_us=us,pc_id='p',generation=1,peer_profile=dict(trace=dict(enabled=True,overwritten=0)))
        window=dict(before_perf=perf(0),before_perf_completed_at=1.5,prompt_start=2.0,reply_window_transport=dict(after=perf(5_500_000)),
                    source_trace_export=dict(overwritten=0))
        results['source_cadence_rejects_a_65ms_send_gap']=source_window(window,rows)['passed'] is False
        results['source_cadence_incomplete_fails_closed']=source_window(window,rows[:150])['passed'] is None
    if resume_enabled(plan):
        # r15 (decision 138b): a clean deferral after originals 1-2 resumes at original 3 in a fresh session.
        row=lambda i,pc,t:dict(run=i,scored=True,passed=True,pc_id=pc,temperature=t)
        first=dict(runs=[row(1,'a','cold'),row(2,'a','warm')],reruns=[],deferred=True,next_original_slot=3,exit_code=2,error=None,
                   deferral_resources=dict(passed=True),restore=dict(clean=True,attempts=[{}]),
                   image_profile=dict(passed=True),measurement_volume=dict(passed=True),stage_nic=dict(passed=True))
        second=dict(runs=[row(3,'b','cold')],reruns=[])
        results['resume_after_clean_deferral']=aggregate_quiet_live([first,second],3,stage='strict_voice_cat',resume=True)['passed']
        results['resume_refuses_a_failed_prior_hold']=not aggregate_quiet_live([dict(first,image_profile=dict(passed=False)),second],3,
                                                                                stage='strict_voice_cat',resume=True)['passed']
    for name,changes in [('jabra',dict(jabra=-54.999)),('board',dict(board=-54.999)),('missing',dict(board=None)),('wifi',dict(wifi=2000001))]:
        results[name+'_defers']=not preflight_callback(plan,'synthetic-'+name,out/'deferrals.jsonl',synthetic=True)(1,observation(**changes))['passed']
    sources=['hil/e2e_common.py','hil/host_nic.py','hil/step9a_merge_checks.py','hil/echoear_acoustic_qualification.py',
             'hil/mic_headroom.py','hil/playback_quality.py','hil/uplink_continuity.py','hil/bench_calibration.py',
             'hil/speaker_dual_sweep_test.py','hil/echoear_echo_stress.py','hil/echoear_hot_stt.py','hil/echoear_analog_probe.py',
             'hil/echoear_source_cadence.py','hil/echoear_item2_campaign.py']
    sources.extend(['hil/echoear_requalification.py','hil/echoear_requalification_dry_run.py'])
    root=Path(__file__).resolve().parents[1]
    return dict(passed=all(results.values()),checks=results,cohort_calls=calls,
        a6_coordination=[s for s in plan['order'] if s['id'].startswith('a6_')],
        gate_source_sha256={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in sources})
