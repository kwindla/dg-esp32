"""Causal acoustic E2E choreography and unchanged speech/leakage gates."""
import hashlib
import json
import os
import re
import time
from pathlib import Path
import numpy as np
from alsa import resolve_jabra,set_volumes,record_wav,play_wav
from esp_serial import ROOT,EspSerial
from transport_test import perf as base_perf,wait_until
from acoustic_response import calibrate_response,predict
from step7_support import BotServer,deepgram_key,transcribe,acoustic_match,pcm,write_pcm,leakage,prepare_prompt
from speaker_dual_sweep_test import dump_mic
from speech_accuracy import speech_accuracy
from playback_quality import transport_gate,jabra_level_gate,decoded_match,microphone_clip_gate

def quiet_acquisition():
    return os.getenv('ECHOEAR_STRICT_MERGE')=='1' or os.getenv('ECHOEAR_ACOUSTIC_QUALIFICATION')=='1'

def perf(board):
    cursor = board.cursor
    requested=time.monotonic()
    value = base_perf(board)
    if hasattr(board,'e2e_perf_windows'):
        board.e2e_perf_windows.append(dict(start=requested,end=time.monotonic()))
    audio = [dict((k,int(v)) for k,v in re.findall(r'(\w+)=(\d+)', line))
             for _,_,line in board.lines_since(cursor) if 'AUDIO drops=' in line]
    if audio:value['audio'] = audio[-1]
    return value

PRE_PROMPT_DRAIN_ITERATIONS=4
PRE_PROMPT_PENDING_MAX=512


def display_trace(board,out,*,acquisition=False,boundary=False,phase=None):
    # r15 (decision 138a S1): in source-trace mode the trace is read out only outside the scored window (three drains
    # per original: pre-prompt, post-reply, post-turn), never inside it; each drain is followed by a trace-counter
    # snapshot. r16 (Astra r15 Q1, Fable N2): every drain's host duration, rows and counters are recorded per original.
    source=getattr(board,'e2e_source_trace',False)
    if source and acquisition and not boundary:return
    if getattr(board,'e2e_display_trace',False) and not (acquisition and getattr(board,'e2e_defer_trace',False) and not source):
        from arrival_trace import drain
        # r17 (smoke-r17 diagnosis): one dump-arrivals reads at most 8192 rows (512 batches x 16, 4 ms apart, ~2.1 s)
        # while ~300 rows/s keep arriving; after a long inter-original interval the ring is full, so the pre-prompt
        # readout loops until the ring is nearly empty (pending <= PRE_PROMPT_PENDING_MAX) before the scored window.
        for iteration in range(PRE_PROMPT_DRAIN_ITERATIONS if source and phase=='pre-prompt' else 1):
            started=time.monotonic();rows=drain(board,out/'arrivals.jsonl');drained=time.monotonic()
            if getattr(board,'e2e_defer_trace',False) or source:
                snapshot=perf(board);board.e2e_export_trace=dict(snapshot['peer_profile']['trace'])
                if source:board.e2e_export_trace['device_us']=snapshot.get('device_us')
            if source:
                board.__dict__.setdefault('e2e_trace_drains',{}).setdefault(str(out),[]).append(dict(
                    phase=phase,iteration=iteration,host_start_s=started,host_end_s=drained,duration_s=drained-started,
                    rows=len(rows) if rows is not None else None,export=dict(board.e2e_export_trace)))
            if not source or phase!='pre-prompt' or (board.e2e_export_trace.get('pending') or 0)<=PRE_PROMPT_PENDING_MAX:break
        else:
            if source and phase=='pre-prompt':   # r18 (Fable r17 N3): visible before the window, recorded as not converged
                print('WARNING: pre-prompt trace readout did not converge (pending %s > %d)'%(board.e2e_export_trace.get('pending'),PRE_PROMPT_PENDING_MAX),flush=True)

def request_offline_play(board,events,prompt_end,delay=.5):
    stop=wait_until(lambda:next((e for e in events() if e['event']=='vad-stop'),None),3)
    assert stop is not None, 'No VAD-stop received before offline playback deadline'
    time.sleep(max(0,stop['t']+delay-time.monotonic()))
    requested_at=time.monotonic()
    board.command('play',10)
    return dict(offline_play_requested_at=requested_at,
                offline_play_basis='VAD-stop + 500 ms; independent of periodic telemetry')

def reply_level(samples,rise,model,stats,policy=None):
    target=-22 if policy is None else policy['target_dbfs']
    return jabra_level_gate(samples,rise,model,target_dbfs=target,board=os.getenv('DG_BOARD','echoear'),volume_relative_db=stats.get('output_volume_db',0))


def turn(board,bot,device,out,live,reset=True,disconnect=True,elf=None,options=(),spl=65,server_url=None,after_calibration=None,display=None,diagnostic_noisy=False,preflight=None,jabra_policy=None,image_profile=None):
    from bench_calibration import BoardRoomUnavailable
    out.mkdir(parents=True,exist_ok=False);result={'passed':False};start=board.cursor
    nic_before = None
    if quiet_acquisition() or preflight is not None:
        from host_nic import snapshot as nic_snapshot, gate as nic_gate
        nic_before = nic_snapshot()
    board.e2e_perf_windows=[]
    try:
        from alsa import record_room_baseline
        try:result['room_baseline'] = record_room_baseline(out/'room-baseline', out.name)
        except Exception as exc:
            if not preflight:raise
            result['eligibility']=preflight(out.name,dict(jabra=dict(dbfs=None),board=dict(dbfs=None),
                nic_before=nic_before,nic_after=nic_snapshot()))
            result.update(preflight_deferred=True,scored=False,room_measurement_error=type(exc).__name__+': '+str(exc))
            return result
        if result['room_baseline']['noise_contaminated']:
            result['noise_contaminated']=True
            if not diagnostic_noisy:
                if preflight:
                    result['eligibility']=preflight(out.name,dict(jabra=dict(dbfs=result['room_baseline']['baseline_dbfs'],wav=result['room_baseline']['wav']),
                        board=dict(dbfs=None),nic_before=nic_before,nic_after=nic_snapshot()))
                    result.update(preflight_deferred=True,scored=False)
                result['acoustic_hold']='noise'
                print('ACOUSTIC HOLD: noise',flush=True)
                return result
            print('NOISY DIAGNOSTIC: qualification remains failed',flush=True)
        card,current_device=resolve_jabra()
        assert current_device==device,'Jabra routing changed'
        set_volumes(card,out/'mixer.log')
        if reset:
            board.reset();board.wait_for('READY transport-console',60,since=start)
            identity=board.wait_for(r'elf_sha256=([0-9a-f]{64})',3,since=start)
            assert re.search(r'elf_sha256=([0-9a-f]{64})',identity)[1]==hashlib.sha256((elf or ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest(), 'Running firmware differs from local ELF; reflash under the same bench hold'
            boot_text='\n'.join(line for _,_,line in board.lines_since(start))
            assert 'I2C_If: Fail' not in boot_text, 'Codec I2C failure during startup'
            if os.environ.get('DG_BOARD','echoear') == 'echoear':
                from echoear_checks import codec_boot_evidence
                result['codec_boot']=codec_boot_evidence(boot_text)
                assert result['codec_boot']['passed'], 'Codec/gain readback missing or wrong'
            if os.environ.get('DG_BOARD','echoear') == 'echoear':
                board.command('playback-volume 0')
                result['qualification_volume_relative_db']=0 # fixed -22 dBFS reference gate
            if os.getenv('ECHOEAR_STRICT_MERGE')=='1':
                from step9a_echoear import check_firmware
                # A bound external ELF is checked against itself and the device boot lines (never build/ or its config).
                result['boot_elf_sha256']=check_firmware(board,start,elf=elf,profile=image_profile)
            if server_url:board.command('server-url '+server_url)
            if display:
                from step9a_support import setup
                result['display_setup']=setup(board,mode=display)
                if os.getenv('ECHOEAR_STRICT_MERGE')=='1':
                    from step9a_merge_checks import census_gate
                    result['display_census']=census_gate([line for _,_,line in board.lines_since(start)])
            from bench_calibration import calibrate
            result['calibration']=calibrate(board,device,out/'calibration',spl)
            for option in options:
                name,value=option.split('=');board.command(f'peer-option {name} {int(value)}')
            cursor=board.cursor;connect_started=time.monotonic()
            board.command('connect',90);board.wait_for('STATE Ready',5,since=cursor)
            if os.getenv('ECHOEAR_STRICT_MERGE')=='1':
                from step9a_merge_checks import ready_gdma_record
                result['gdma_ready']=ready_gdma_record(board.wait_for(r'^DISPLAY_R4_GDMA_READY ',5,since=cursor))
            result['console_connect_to_ready_ms']=1000*(time.monotonic()-connect_started)
            board.command('audio 1');board.wait_for('GATE open',15,since=cursor)
        else:
            if os.getenv('DG_BOARD','echoear')=='echoear':
                board.command('playback-volume 0')
                result['qualification_volume_relative_db']=0
            # The previous successful turn checked its gate reopening. Keep
            # the same session and decoder state for warm-onset measurements.
            from bench_calibration import calibrate
            result['calibration']=calibrate(board,device,out/'calibration',spl,output_reference=False)
            cursor=board.cursor;board.command('audio 1')
        if os.getenv('DG_BOARD','echoear')=='echoear':
            from bench_calibration import echoear_gain_path
            result['gain_readback']=echoear_gain_path(board)
            assert result['gain_readback']['analog_db']==result['calibration']['analog_gain_db'],'Analog gain changed'
            assert result['gain_readback']['digital_percent']==result['calibration']['digital_gain_percent'],'Digital gain changed'
        result['calibration']['response']=calibrate_response(board,device,out/'calibration'/'response')
        board.command('audio 1');board.command('rx-clear')
        if quiet_acquisition():
            result['quiet_command_ack']=board.command('quiet 1')
            result['quiet_requested']=1
        if preflight:
            remaining=getattr(preflight,'minimum_nic_seconds',0)-(time.monotonic()-nic_before['monotonic_s'])
            floors=(result['room_baseline']['baseline_dbfs'],result['calibration'].get('room_floor_dbfs'))
            room_limit=getattr(preflight,'room_limit_dbfs',float('-inf'))
            if remaining>0 and all(type(v) in (float,int) and np.isfinite(v) and v<=room_limit for v in floors):time.sleep(remaining)
            result['eligibility']=preflight(out.name,dict(
                jabra=dict(dbfs=result['room_baseline']['baseline_dbfs'],wav=result['room_baseline']['wav']),
                board=dict(dbfs=result['calibration'].get('room_floor_dbfs'),wav=str(out/'calibration/room-board.wav')),
                nic_before=nic_before,nic_after=nic_snapshot()))
            if not result['eligibility']['passed']:
                result.update(preflight_deferred=True,scored=False)
                return result
        result['temperature']='cold' if reset else 'warm'
        stats=perf(board);pc_id=stats['pc_id'];result['pc_id']=pc_id
        result['before_perf']=stats
        result['before_perf_completed_at']=time.monotonic()
        display_trace(board,out,phase='pre-prompt')
        if after_calibration:
            after_calibration()
        if reset:
            setup_text='\n'.join(l for _,_,l in board.lines_since(start))
            def times(pattern):return [int(v) for v in re.findall(pattern,setup_text)]
            starting=times(r'STATE Starting .*t=(\d+)')
            ready=times(r'STATE Ready .*t=(\d+)')
            opened=times(r'PHASE open t=(\d+)');connected=times(r'PHASE connected t=(\d+)')
            duration=times(r'START_READY .*duration_us=(\d+)')
            wifi=times(r'WIFI READY t=(\d+)');cert=times(r'CERT duration_us=(\d+)')
            result['setup_timings_ms']={
                'app_start_to_ready_including_retries':(ready[-1]-starting[0])/1000 if starting and ready else None,
                'successful_start_to_ready':duration[-1]/1000 if duration else None,
                'open_to_connected':(connected[-1]-opened[-1])/1000 if connected and opened else None,
                'dtls':stats.get('dtls_duration_us',0)/1000 if stats.get('dtls_duration_us') is not None else None,
                'wifi_ready_since_boot':wifi[0]/1000 if wifi else None,
                'certificate_generation':cert[0]/1000 if cert else None}
            result['connect_attempts']=len(starting)
            result['connect_failures']=[l for _,_,l in board.lines_since(start) if 'SESSION FAIL' in l]
        with record_wav(out/'turn.wav',device,25,16000) as rec:
            time.sleep(.6);board.command('mic-record 1');mic_started=time.monotonic();time.sleep(.4)
            cursor=board.cursor;prompt_start=time.monotonic();result['scored']=True;play_wav(result['calibration']['prompt']['path'],device);prompt_end=time.monotonic()
            result.update(prompt_start=prompt_start,prompt_end=prompt_end,recording_ready_at=rec.ready_at)
            deadline=prompt_end+20
            def events():return [e for e in bot.events() if e['pc_id']==pc_id and e['t']>=prompt_start]
            def telemetry(value):return [e for e in events() if e['event']=='rtvi-out' and e['message'].get('type')=='server-message' and e['message'].get('data',{}).get('t')=='user-audio' and e['message']['data']['d']['speaking']==value]
            true_reports=wait_until(lambda:[e for e in telemetry(True) if prompt_start<=e['t']<=prompt_end+.3],2)
            if quiet_acquisition():
                # Keep every full PERF dump outside prompt/reply acquisition.
                post_prompt_perf=stats;post_prompt_at=result['before_perf_completed_at']
            else:
                post_prompt_perf=perf(board);post_prompt_at=time.monotonic()
            display_trace(board,out,acquisition=True)
            if not live:
                result.update(request_offline_play(board,events,prompt_end))
            false_reports=wait_until(lambda:[e for e in telemetry(False) if prompt_end-.1<=e['t']<=prompt_end+1],1.2)
            board.wait_for('GATE closed',max(.1,deadline-time.monotonic()),since=cursor)
            if os.getenv('ECHOEAR_STRICT_MERGE')=='1':
                from step9a_merge_checks import wire_show,frame_clock
                first_tts=wait_until(lambda:next((e for e in events() if e['event']=='first-tts-byte'),None),max(.1,deadline-time.monotonic()))
                full_samples=[frame_clock(board)]
                reply_display_before=full_samples[0]['perf'];reply_display_start=full_samples[0]['completed_at']
                result['wire_show']=wire_show(board,bot,pc_id,os.environ['DISPLAY_HIL_COMMANDS'],'voice-show-'+out.name)
                time.sleep(1.1)
                full_samples.append(frame_clock(board))
                reply_display_after=full_samples[-1]['perf'];reply_display_end=full_samples[-1]['completed_at']
                result['reply_display_bracket']=dict(before=reply_display_before,after=reply_display_after,start=reply_display_start,end=reply_display_end,first_tts=first_tts['t'])
                while time.monotonic()<deadline:
                    opened=next((entry for entry in board.lines_since(cursor) if re.search(r'GATE open t=(\d+)',entry[2]) and entry[1]>first_tts['t']),None)
                    if opened:
                        from step9a_merge_checks import full_reply_fps
                        gate_us=int(re.search(r'GATE open t=(\d+)',opened[2])[1])
                        result['full_reply_display']=dict(samples=full_samples,first_tts=first_tts['t'],gate_host=opened[1],gate_device_us=gate_us)
                        result['full_reply_fps']=full_reply_fps(full_samples,first_tts['t'],opened[1],gate_us)
                        break
                    time.sleep(.2)
                    full_samples.append(frame_clock(board))
                assert 'full_reply_fps' in result,'Missing full-reply FPS endpoints'
            board.wait_for('GATE open',max(.1,deadline-time.monotonic()),since=cursor)
            if live:
                board.wait_for(r'RTVI user\[(\d+)\]:.*[Ff]rance',max(.1,deadline-time.monotonic()),since=cursor)
                reference=wait_until(lambda:next((Path(e['path']) for e in events() if e['event']=='tts-reference'),None),max(.1,deadline-time.monotonic()))
            else:reference=ROOT/'server/fixtures/reply_pineapple.wav'
            time.sleep(.4);reply_end_perf=perf(board);reply_end_at=time.monotonic()
            display_trace(board,out,acquisition=True,boundary=True,phase='post-reply')   # after the scored window (reply end)
            if getattr(board,'e2e_source_trace',False):result['source_trace_export']=getattr(board,'e2e_export_trace',None)
            board.command('mic-record 0')
            rec.wait()
        result['after_perf']=perf(board)
        result['mic_clip_gate']=microphone_clip_gate(result['before_perf'],result['after_perf'])
        display_trace(board,out,phase='post-turn')
        if getattr(board,'e2e_source_trace',False):
            result['source_trace_drains']=getattr(board,'e2e_trace_drains',{}).pop(str(out),[])
        sent=[e['t'] for e in events() if e['event']==('first-tts-byte' if live else 'first-fixture-byte')]
        reply_before=post_prompt_perf if sent and post_prompt_at<min(sent) else stats
        result['reply_window_transport']=dict(before=reply_before,after=reply_end_perf,
            start=post_prompt_at if reply_before is post_prompt_perf else prompt_start,end=reply_end_at,
            basis='Last completed pre-send snapshot (pre-prompt fallback) through gate reopen +400 ms; whole-turn snapshots retained separately')
        result['reply_transport_gate']=transport_gate(reply_before,reply_end_perf)
        result['acoustic_scoring_eligible']=result['reply_transport_gate']['passed'] and not result.get('noise_contaminated',False)
        dump_mic(board,out/'turn-board.wav')
        if os.getenv('DG_BOARD','echoear')=='echoear':
            from mic_headroom import microphone_headroom
            result['mic_headroom']=microphone_headroom(pcm(out/'turn-board.wav'),result['gain_readback']['digital_percent'],result['gain_readback'].get('protection'))
        from transport_test import dump
        decoded=dump(board,out)
        lines=board.lines_since(cursor);text='\n'.join(l for _,_,l in lines)
        assert not re.search(r'Guru Meditation|assert failed|abort\(\)|Task watchdog|SESSION FAIL|I2C_If: Fail',text),'Session/device failure during turn'
        prompt=acoustic_match(out/'turn.wav',ROOT/'hil/fixtures/prompt_speech.wav',prompt_start-rec.ready_at,rec.ready_at,prompt_start-rec.ready_at-.1,last=prompt_end-rec.ready_at+.1)
        # Only audio after the played prompt can satisfy the reply assertion.
        first_bytes=[e for e in events() if e['event']==('first-tts-byte' if live else 'first-fixture-byte') and (live or e.get('name')=='reply_pineapple.wav')]
        assert first_bytes,'Missing server send evidence'
        send_at=first_bytes[0]['t']
        raw_match=acoustic_match(out/'turn.wav',reference,prompt_end-rec.ready_at,rec.ready_at,prompt_start-rec.ready_at-.1,server_send_at=send_at)
        matched=acoustic_match(out/'turn.wav',reference,prompt_end-rec.ready_at,rec.ready_at,prompt_start-rec.ready_at-.1,reference_pcm=predict(pcm(reference),result['calibration']['response'],'jabra'),server_send_at=send_at)
        result['reply_raw']=raw_match
        board_match=acoustic_match(out/'turn-board.wav',reference,prompt_end-mic_started,mic_started,
            prompt_start-mic_started,reference_pcm=predict(pcm(reference),result['calibration']['response'],'board'),server_send_at=send_at)
        result['reply_board']=board_match
        result.update(prompt_match_diagnostic=prompt,reply=matched)
        at=round(matched['offset_s']*16000)
        result['acoustic_timing_available']=matched.get('acoustic_timing_available',True)
        result['jabra_level']=reply_level(pcm(out/'turn.wav')[at:at+len(pcm(reference))],matched['rms_rise_db'] if result['acoustic_timing_available'] else None,result['calibration']['response'],stats,jabra_policy)
        if jabra_policy is not None:result['jabra_rebaseline_policy']=jabra_policy
        if not result['acoustic_timing_available']:
            result['jabra_level']['rms_rise_db']=None
            result['jabra_level']['baseline_rise_available']=False
        result['decoded_content']=decoded_match(decoded,pcm(reference))
        result['leakage']=leakage(lines,matched['onset'] if result['acoustic_timing_available'] else None,matched['end'] if result['acoustic_timing_available'] else None)
        gates=result['leakage']['gates'];closed_at=next(t for t,s,_ in gates if s=='closed');open_at=next(t for t,s,_ in gates if s=='open' and t>closed_at)
        if os.getenv('ECHOEAR_ACOUSTIC_QUALIFICATION')=='1':
            from step9a_merge_checks import perf_window_gate
            result['perf_acquisition_gate']=perf_window_gate(board.e2e_perf_windows,prompt_start,open_at)
            assert result['perf_acquisition_gate']['passed'],'Full PERF query during acoustic acquisition'
        if os.getenv('ECHOEAR_STRICT_MERGE')=='1':
            from step9a_merge_checks import memory_gate,audio_gate,reply_fps,perf_window_gate
            from step9a_support import read
            result['wire_status']=read(board,'wire-status','DISPLAY_WIRE_STATUS')
            ack_clean=all(result['wire_status'].get(k)==0 for k in ('ack_queue_full','ack_send_failed','ack_stale'))
            result['perf_acquisition_gate']=perf_window_gate(board.e2e_perf_windows,prompt_start,open_at)
            bracket=result['reply_display_bracket']
            fps=reply_fps(bracket['before'],bracket['after'])
            memory=memory_gate([stats,reply_end_perf,result['after_perf']])
            audio=audio_gate(stats,result['after_perf'])
            whole_transport=transport_gate(stats,result['after_perf'])
            during_reply=bracket['first_tts']<=bracket['start']<bracket['end']<open_at and bracket['first_tts']<=result['wire_show']['started']<result['wire_show']['completed_at']<open_at
            result['display_merge']=dict(fps=fps,full_reply_fps=result['full_reply_fps'],perf_acquisition_gate=result['perf_acquisition_gate'],ack_clean=ack_clean,memory=memory,audio=audio,whole_turn_transport=whole_transport,during_reply=during_reply,
                passed=result['perf_acquisition_gate']['passed'] and ack_clean and fps['passed'] and result['full_reply_fps']['passed'] and memory['passed'] and audio['passed'] and during_reply and whole_transport['passed'] and result['reply_transport_gate']['passed'])
            assert result['display_merge']['passed'], 'Strict display merge: '+str(result['display_merge'])
        result.update(prompt_match_diagnostic=prompt,reply=matched,speaking_reports=len(true_reports),stop_report_delay_s=false_reports[0]['t']-prompt_end)
        ev=events();stops=[e for e in ev if e['event']=='vad-stop'];assert stops,'Missing VAD stop'
        turn_id=stops[-1]['turn'];result['turn']=turn_id
        transcripts=[e for e in ev if e['event']=='final-transcript' and e.get('turn')==turn_id]
        tts=[e for e in ev if e['event']=='first-tts-byte' and e.get('turn')==turn_id]
        fixture_bytes=[e for e in ev if e['event']=='first-fixture-byte' and e.get('turn')==turn_id and e.get('name')=='reply_pineapple.wav']
        segment=pcm(out/'turn-board.wav')[round(board_match['offset_s']*16000):round(board_match['offset_s']*16000)+len(pcm(reference))]
        write_pcm(out/'reply-board-segment.wav',segment)
        transcript=None
        if live or deepgram_key():
            transcript=transcribe(out/'reply-board-segment.wav');result['reply_transcript']=transcript
            result['board_stt']=dict(available=True,passed='pineapple' in transcript.lower())
            assert result['board_stt']['passed'],'Board-mic STT did not recognize pineapple'
        else:
            # Preserve credential-free offline transport/acoustic acceptance.
            # Full 8b3 bench qualification requires this extension to be known.
            result['board_stt']=dict(available=False,passed=None,reason='SKIPPED: no credentials; decoded content and board correlation remain verified')
        assert result['mic_clip_gate']['passed'],'In-turn microphone clips missing or nonzero (gate-open and gate-closed both required)'
        if os.getenv('DG_BOARD','echoear')=='echoear':
            assert result['mic_headroom']['passed'],'Microphone pre-mute echo headroom below 3 dB'
        assert result['reply_transport_gate']['passed'], 'Reply transport gate: '+str(result['reply_transport_gate'])
        assert result['jabra_level']['passed'] or (diagnostic_noisy and result['jabra_level'].get('available') is False),'Reply Jabra RMS level/rise missing or outside limits'
        assert result['decoded_content']['passed'],'Reply decoded PCM content'
        assert board_match['correlation']>=.6,'Reply board-mic modeled correlation'
        assert not result['acoustic_timing_available'] or matched['rms_rise_db']>=3,'Reply RMS rise over room noise'
        assert matched['onset']>prompt_end,'Reply preceded prompt completion'
        assert not result['acoustic_timing_available'] or (closed_at<=matched['onset']+.1 and open_at>=matched['end']-.1 and open_at-matched['end']<=2.3),'Reply gate timing'
        if live:
            final=[e for e in ev if e['event']=='turn-transcript' and e['turn']==turn_id and 'france' in e['text'].lower()]
            assert final,'Missing matching server turn transcript'
            result['user_transcript']=final[0]['text'] # Preserve failed prompt evidence before asserting accuracy.
            result['prompt_word_accuracy']=speech_accuracy('Please tell me the capital of France.',final[0]['text'])
            assert result['prompt_word_accuracy']==1,'Full prompt transcript accuracy failed'
            assert any(re.search(rf'RTVI user\[{turn_id}\]:.*[Ff]rance',l) for _,_,l in lines),'Device/server turn IDs differ'
            replies=[e for e in ev if e['event']=='fixed-reply' and e['turn']==turn_id]
            assert len(replies)==1,'Expected exactly one reply per user turn'
            assert tts and transcripts,'Missing first-byte/transcript latency evidence'
            result.update(user_transcript=final[0]['text'],reply_transcript=transcript)
        result['prompt_end_basis']='Host monotonic ALSA playback completion. Jabra AEC suppresses its own prompt; the low-correlation prompt match is diagnostic only.'
        result['latency_ms']={
            'vad_stop':1000*(stops[-1]['t']-prompt_end),
            'transcript':1000*(transcripts[-1]['t']-prompt_end) if transcripts else None,
            'first_tts_byte':1000*(tts[0]['t']-prompt_end) if tts else None,
            'first_fixture_byte':1000*(fixture_bytes[0]['t']-prompt_end) if fixture_bytes else None,
            'audible_reply':1000*(matched['onset']-prompt_end) if result['acoustic_timing_available'] else None,
            'offline_playout':1000*(matched['onset']-fixture_bytes[0]['t']) if not live and fixture_bytes and result['acoustic_timing_available'] else None}
        result['latency_timestamps']={'prompt_end':prompt_end,'vad_stop':stops[-1]['t'],'transcript':transcripts[-1]['t'] if transcripts else None,'first_tts_byte':tts[0]['t'] if tts else None,'first_fixture_byte':fixture_bytes[0]['t'] if fixture_bytes else None,'audible_reply':matched['onset'] if result['acoustic_timing_available'] else None}
        result['diagnostic_turn_passed']=True
        result['noise_contaminated']=result.get('noise_contaminated',False) or not result['acoustic_timing_available']
        result['passed']=not result['noise_contaminated'] and result['jabra_level']['passed']
        if not result['passed']: result['error']='Diagnostic only: room contamination or failed Jabra reply-level gate'
    except BoardRoomUnavailable as exc:
        if preflight:
            result['eligibility']=preflight(out.name,dict(
                jabra=dict(dbfs=result.get('room_baseline',{}).get('baseline_dbfs'),wav=result.get('room_baseline',{}).get('wav')),
                board=dict(dbfs=None,wav=str(out/'calibration/room-board.wav')),nic_before=nic_before,nic_after=nic_snapshot()))
            result.update(preflight_deferred=True,scored=False,board_measurement_error=str(exc))
        else:result['error']=f'{type(exc).__name__}: {exc}'
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:
        result['perf_windows']=list(board.e2e_perf_windows)
        if display:board.display_cleanup=True
        if 'board_stt' not in result and (out/'turn-board.wav').exists() and 'prompt_end' in result:
            try:
                audio=pcm(out/'turn-board.wav');begin=max(0,round((result['prompt_end']-mic_started)*16000))
                write_pcm(out/'reply-board-fallback.wav',audio[begin:])
                text=transcribe(out/'reply-board-fallback.wav')
                result['board_stt']=dict(available=True,passed='pineapple' in text.lower(),
                    transcript=text,basis='Retained raw post-prompt window; timing matcher unavailable')
            except Exception as exc:result['board_stt']=dict(available=False,passed=None,reason=str(exc))
        if not result['passed'] and not result.get('preflight_deferred'):
            from failure_snapshot import capture_failure
            result['failure_snapshot']=capture_failure(board,out,result.get('error','gate failure'))
        try:
            board.command('mic-record 0')
            if os.getenv('DG_BOARD','echoear')=='echoear':
                board.command('playback-volume -6')
            if disconnect or not result['passed']:
                board.command('audio 0');board.command('disconnect',40)
        except Exception as exc:result['cleanup_error']=f'{type(exc).__name__}: {exc}';result['passed']=False
        if nic_before is not None:
            result['host_nic'] = nic_gate(nic_before, nic_snapshot())
            if not result['host_nic']['passed']:
                result['passed'] = False
                result.setdefault('error', 'Board-path wlp8s0 NIC quiet bound exceeded or unavailable')
        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result

def verify_session_continuity(sample, previous_pc=None):
    """A warm observation must retain the successful preceding peer session."""
    temperature=sample.get('temperature')
    before=sample.get('before_perf',{}).get('pc_id')
    after=sample.get('after_perf',{}).get('pc_id')
    valid=bool(before and after==before and
        (temperature!='warm' or previous_pc and before==previous_pc))
    sample['session_continuity']=dict(passed=valid,previous_pc=previous_pc,
        before_pc=before,after_pc=after)
    if not valid:
        sample['turn_assertions_passed']=sample['passed']
        sample['passed']=False
        sample.setdefault('error','Turn did not retain the required peer session')
        # Retain its timestamps, but exclude an unverified temperature from
        # the actual cold/warm coverage and percentile populations.
        sample['temperature']='unverified-'+str(temperature)
    return valid

def run(a,live):
    if live and not deepgram_key():print('SKIPPED: no credentials');return 0
    first=getattr(a,'original_start',1)
    if type(first) is not int or not 1<=first<=a.runs:raise ValueError('Invalid original slot range')
    originals=list(range(first,a.runs+1))
    a.out.mkdir(parents=True,exist_ok=False);results=[];reruns=[]
    result={'mode':'live-test' if live else 'offline-test','runs':results,'reruns':reruns,'passed':False}
    result['requested_original_indices']=originals
    if getattr(a,'jabra_policy',None) is not None:result['jabra_rebaseline_policy']=a.jabra_policy
    previous_evidence=os.environ.get('RTP_EVIDENCE_DIR')
    os.environ['RTP_EVIDENCE_DIR']=str(a.out/'rtp')
    board=None
    try:
        card,device=resolve_jabra();set_volumes(card,a.out/'mixer.log')
        with BotServer(a.out/'bot',result['mode'],ice_servers=getattr(a,'ice_servers',())) as bot,EspSerial(a.port,log_path=a.out/'serial.log',display_checks=bool(getattr(a,'display',None))) as board:
            board.e2e_display_trace=bool(getattr(a,'display',None) and 'arrival-trace=1' in getattr(a,'option',()))
            board.e2e_source_trace=bool(getattr(a,'source_trace',False))   # r15 (decision 138a S1, item2 only)
            if board.e2e_source_trace and not board.e2e_display_trace:raise ValueError('source trace needs display and arrival-trace=1')
            active=False;previous_pc=None
            for index in originals:
                warm=getattr(a,'warm',False)
                sample=turn(board,bot,device,a.out/f'run{index}',live,reset=not (warm and active),disconnect=not warm,elf=getattr(a,'elf',None),spl=getattr(a,'spl',65),options=getattr(a,'option',()),server_url=getattr(a,'server_url',None),display=getattr(a,'display',None),diagnostic_noisy=getattr(a,'diagnostic_noisy',False),preflight=getattr(a,'preflight',None),jabra_policy=getattr(a,'jabra_policy',None),image_profile=getattr(a,'image_profile',None));sample['run']=index
                if sample.get('preflight_deferred'):
                    result.update(deferred=True,next_original_slot=index,scored_runs=len(results))
                    result.setdefault('deferrals',[]).append(sample)
                    (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
                    return 2
                if sample.get('acoustic_hold')=='noise':
                    results.append(sample);result['acoustic_hold']='noise'
                    (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
                    return 2
                verify_session_continuity(sample,previous_pc);results.append(sample)
                if getattr(a,'display',None):
                    board.display_cleanup=False
                    try:board.display_monitor.check()
                    except Exception as exc:
                        sample.update(passed=False,fatal_monitor_error=type(exc).__name__+': '+str(exc))
                        result['fatal_stop']='Raw/display monitor failure after original acquisition'
                        (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
                        raise
                active=sample['passed']
                if active:previous_pc=sample['pc_id']
                print(f'run {index}: {"PASS" if sample["passed"] else sample.get("error")}',flush=True)
                if not sample['passed'] and not getattr(a,'no_retakes',False):
                    if sample.get('turn_assertions_passed'):
                        board.command('audio 0');board.command('disconnect',40)
                    repeat=turn(board,bot,device,a.out/f'run{index}-rerun',live,disconnect=not warm,elf=getattr(a,'elf',None),spl=getattr(a,'spl',65),options=getattr(a,'option',()),server_url=getattr(a,'server_url',None),diagnostic_noisy=getattr(a,'diagnostic_noisy',False));repeat['original_run']=index
                    if repeat.get('acoustic_hold')=='noise':
                        reruns.append(repeat);result['acoustic_hold']='noise'
                        (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
                        return 2
                    verify_session_continuity(repeat);reruns.append(repeat)
                    active=repeat['passed']
                    if active:previous_pc=repeat['pc_id']
                (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
                if getattr(a,'display',None) and not sample['passed']:
                    if not getattr(a,'all_originals',False):break
                    if not sample.get('scored') or sample.get('cleanup_error'):
                        result['fatal_stop']='Unscored device/setup failure or cleanup failure; remaining originals unrun'
                        break
            if getattr(a,'display',None):
                from step9a_support import read
                result['display_final']=read(board,'status','DISPLAY');board.command('disp bench off')
            if getattr(a,'warm',False):board.command('audio 0');board.command('disconnect',40)
            if getattr(a,'display',None):
                result['display_monitor']=board.display_monitor.result();board.display_monitor.check()
        from uplink_continuity import link_tail,recorded_audio_idle_windows
        packet_file=a.out/'rtp/rtp.jsonl'
        if packet_file.exists():
            packets=[json.loads(line) for line in packet_file.read_text().splitlines()]
            starts=[r['prompt_start'] for r in results if 'prompt_start' in r]
            ends=[r['reply_window_transport']['end'] for r in results if 'reply_window_transport' in r]
            if starts and ends:
                result['link_tail']=link_tail(packets,min(starts),max(ends),
                    excluded_windows=recorded_audio_idle_windows(a.out/'serial.log',end=max(ends)))
        result['pass_count']=sum(r['passed'] for r in results);result['passed']=result['pass_count']==len(originals)
        # Original runs remain the population; missing timestamps remain N/A.
        summary={}
        for key in ('vad_stop','transcript','first_tts_byte','first_fixture_byte','audible_reply','offline_playout'):
            values=[r.get('latency_ms',{}).get(key) for r in results]
            values=[v for v in values if v is not None]
            summary[key]={'n':len(values),
                'p50':float(np.percentile(values,50,method='lower')) if values else None,
                'p90':float(np.percentile(values,90,method='lower')) if values else None}
        result['latency_p50_p90_ms']=summary
        result['latency_by_temperature_ms']={}
        for temperature in ('cold','warm'):
            result['latency_by_temperature_ms'][temperature]={}
            for key in summary:
                values=[r.get('latency_ms',{}).get(key) for r in results if r.get('temperature')==temperature]
                values=[v for v in values if v is not None]
                result['latency_by_temperature_ms'][temperature][key]={'n':len(values),
                    'median':float(np.median(values)) if values else None,
                    'p95':float(np.percentile(values,95,method='lower')) if values else None}
        result['offline_latency_basis']='Offline playout = audible onset minus first fixture byte; prompt-relative timestamps include harness scheduling.'
        print('Prompt end -> VAD stop -> transcript -> first TTS byte -> audible reply (ms)',flush=True)
        for r in results:print(r.get('latency_ms',{}),flush=True)
        print('p50 / p90:',json.dumps(result['latency_p50_p90_ms']),flush=True)
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:
        if previous_evidence is None:os.environ.pop('RTP_EVIDENCE_DIR',None)
        else:os.environ['RTP_EVIDENCE_DIR']=previous_evidence
        if getattr(a,'display',None) and board is not None:
            result['display_monitor']=board.display_monitor.result()
            from step9a_r2_checks import audit
            resets=[b['index'] for b in result['display_monitor'].get('boot_epochs',[]) if b['requested']]
            try:result['raw_display_audit']=audit((a.out/'serial.log').read_bytes(),expected_boot_indices=resets)
            except Exception as exc:
                result['raw_display_audit']=dict(passed=False,error=type(exc).__name__+': '+str(exc))
                result['passed']=False
                result.setdefault('error',result['raw_display_audit']['error'])
        (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return 0 if result['passed'] else 1
