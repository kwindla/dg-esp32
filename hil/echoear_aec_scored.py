#!/usr/bin/env python3
"""HISTORICAL/UNUSED by the demo plan; see docs/echoear-aec-bench.md.

One predeclared scored AEC campaign. --prepare is entirely off bench.

Execution requires an Astra-reviewed binding and a NEW explicit B grant.
No threshold edits, retakes, services on7860, mixer writes or network changes.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import platform
import signal
import subprocess
import time

import numpy as np
from alsa_stream_capture import StreamCapture
from echoear_aec_capture import dump_capture
from echoear_aec_resource_trial import command, guard
from echoear_aec_scoring import (
    RATE, FRAME, rms, dbfs, reference_threshold, saturation, reply_erle,
    known_match, quiet_stop, native_stop, future_speech, final_transcript,
    session_integrity, rtp_coverage, adjudicate_transcripts,
)
from echoear_aec_stimuli import sha, playback48
from host_nic import snapshot as nic_snapshot, gate as nic_gate
from step7_support import pcm, write_pcm

ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'hil/plans/echoear-aec-scored-v1.json'
EVIDENCE=Path('/home/khkramer/src/evidence/echoear/A')
FATAL=r'abort\(\)|Mem alloc fail|Guru Meditation|panic.ed|Stack canary|stack overflow|assert failed|CORRUPT HEAP'


def write(path,data):
    path=Path(path);temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(data,indent=2,allow_nan=False)+'\n');temp.replace(path)


def jsonl(path):
    path=Path(path)
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def source_inputs(protocol):
    image=Path(protocol['firmware_image'])
    from demo_session import firmware_artifacts
    version=json.loads((image/'project_description.json').read_text())['project_version']
    if firmware_artifacts(image,version)!=protocol['firmware_elf_sha256']:
        raise ValueError('Frozen experimental image identity mismatch')
    flash=json.loads((image/'flasher_args.json').read_text())
    paths={p for directory in ('hil','server') for p in (ROOT/directory).glob('*.py')}
    paths|={PROTOCOL,ROOT/'docs/echoear-aec-scored.md'}
    paths|={image/name for name in ('manifest.json','dg-esp32.elf','flasher_args.json','project_description.json',*flash['flash_files'].values())}
    stimuli=Path(protocol['stimulus_prepare_path'])
    manifest=json.loads((stimuli/'manifest.json').read_text())
    for name,wanted in manifest['files'].items():
        if sha(stimuli/name)!=wanted:raise ValueError('Stimulus changed: '+name)
    if sha(stimuli/'stop-16k.wav')!=protocol['barge_word_sha256'] or sha(stimuli/'marker-16k.wav')!=protocol['barge_marker_sha256']:
        raise ValueError('Protocol/stimulus mismatch')
    paths|={stimuli/name for name in (*manifest['files'],'manifest.json')}
    # Independent long calibrated speech used only for AFE delay, not ERLE.
    prompt=Path(protocol['independent_delay_prompt'])
    paths.add(prompt)
    return {str(p):sha(p) for p in sorted(paths)}


def verify_binding(binding):
    for name,wanted in binding['inputs'].items():
        if sha(name)!=wanted:raise ValueError('Frozen input changed: '+name)
    for name,wanted in binding.get('dependencies',{}).items():
        if importlib.metadata.version(name)!=wanted:raise ValueError('Frozen dependency changed: '+name)
    if binding.get('python_version')!=platform.python_version():raise ValueError('Python version changed')


def prepare(out,approval=None):
    out=out.resolve()
    if not out.is_relative_to(EVIDENCE):raise ValueError('Evidence must be under worker A evidence')
    out.mkdir(parents=True,exist_ok=False)
    protocol=json.loads(PROTOCOL.read_text())
    write(out/'protocol.json',protocol)
    binding=dict(schema=1,state='PREPARED_NOT_STARTED',source_inputs_immutable=True,
                 python_version=platform.python_version(),
                 inputs=source_inputs(protocol),
                 dependencies={name:importlib.metadata.version(name) for name in
                               ('numpy','scipy','pipecat-ai','aiortc','aioice','pyserial')},
                 new_B_grant_required=True,astra_approval_required=True)
    binding['inputs'][str(out/'protocol.json')]=sha(out/'protocol.json')
    binding['source_commit']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    if approval:
        text=approval.read_text()
        if 'APPROVE-FOR-SCORED-TRIAL' not in text or binding['source_commit'] not in text:
            raise ValueError('Explicit Astra approval of this exact source commit required')
        approval_copy=out/'astra-approval.md';approval_copy.write_bytes(approval.read_bytes())
        binding['inputs'][str(approval_copy)]=sha(approval_copy)
        binding['astra_approval_sha256']=sha(approval_copy)
        binding['state']='PREPARED_REVIEWED'
    write(out/'binding.json',binding)
    print(json.dumps(dict(prepared=str(out),binding_sha256=sha(out/'binding.json'),
                          files=len(binding['inputs']),hardware=False)),flush=True)


def source_prefix(source):
    powers=np.mean(np.asarray(source[:len(source)//FRAME*FRAME]).reshape(-1,FRAME)**2,axis=1)
    active=np.flatnonzero(powers>(32768*10**(-45/20))**2)
    if not len(active):raise ValueError('No generated source speech')
    first=int(active[0])*FRAME
    if first+6400>len(source):raise ValueError('Missing 400ms source anchor')
    return first,source[first:first+6400]


def analyze_original(row, capture, jabra, receipts, source, word, marker, quiet, delay, events):
    raw=capture['pcm'];row['saturation']=saturation(raw)
    row['jabra_saturation_samples']=int(np.count_nonzero((jabra<=-32768)|(jabra>=32767)))
    first,prefix=source_prefix(source)
    bot=known_match(prefix,raw[:,2]);row['native_bot_source']=bot
    jabra_bot=known_match(prefix,jabra);row['jabra_bot_source']=jabra_bot
    if row['kind']=='echo':
        whole=known_match(source,raw[:,2]);row['native_complete_source']=whole
        if whole.get('available'):
            row['erle']=reply_erle(raw,quiet_reference=quiet['reference'],delay_samples=delay,
                                  reply_start=whole['start'],reply_end=whole['end'])
        else:
            row['erle']=dict(passed=False,available=False,reason='Complete emitted source not identified')
        row['microphone_echo_rise_db']=(20*np.log10(max(rms(raw[whole['start']:whole['end'],0]),1e-12)/max(quiet['microphone_rms'],1e-12))
                                        if whole.get('available') else None)
        row['passed']=bool(row['erle']['available'] and row['saturation']['passed']
                           and whole.get('available') and jabra_bot.get('available')
                           and row['jabra_saturation_samples']==0 and row['microphone_echo_rise_db']>=10)
        return row
    matches=dict(word_jabra=known_match(word,jabra),marker_jabra=known_match(marker,jabra),
                 word_native=known_match(word,raw[:,0]),marker_native=known_match(marker,raw[:,0]))
    row['witnesses']=matches
    valid=all(m.get('available') is True for m in matches.values())
    ordered=valid and all(matches['word_'+clock]['end']<matches['marker_'+clock]['start']
                          for clock in ('jabra','native'))
    marker_late=row['marker_request_s']>=row['user_request_s']+2.1
    if ordered and marker_late and bot.get('available') and jabra_bot.get('available'):
        row['physical_stop']=quiet_stop(jabra,receipts,request_s=row['user_request_s'],
            word_end=matches['word_jabra']['end'],marker_start=matches['marker_jabra']['start'],
            quiet_rms=quiet['jabra_rms'])
        word_power=np.convolve(word*word,np.ones(320)/320,mode='same')
        word_active=int(np.flatnonzero(word_power>max(1.,float(word_power.max())*.001))[0])
        row['native_stop']=native_stop(raw,word_match=matches['word_native'],
            marker_match=matches['marker_native'],bot_match=bot,quiet_reference=quiet['reference'],
            word_active_offset=word_active)
        offset=matches['word_native']['start']+word_active-bot['start']+first
        row['future_speech']=future_speech(source,source_offset=offset,
            generated_before_trigger=row['generated_before_trigger'])
        # The bot anchor must be wholly before the independently located user
        # word, in BOTH physical streams. No self-echo or stale silence credit.
        row['bot_active_before_word']=all(b['end']<matches['word_'+clock]['start']
            for b,clock in ((bot,'native'),(jabra_bot,'jabra')))
    else:
        row['physical_stop']=dict(passed=False,available=False,reason='Missing ordered post-trigger witnesses or bot source')
    row['transcript']=final_transcript(events)
    interrupted=any(e.get('event')=='frame' and e.get('frame')=='InterruptionFrame'
                    and row['user_request_s']<=e['t']<=row['marker_request_s'] for e in events)
    row['real_interruption']=interrupted
    row['passed']=all(row.get(k,{}).get('passed') is True for k in
        ('physical_stop','native_stop','future_speech','saturation')) and row.get('bot_active_before_word') is True and interrupted and row['jabra_saturation_samples']==0
    return row


def cleanup_owned(board,bot,ready,recording,path,result):
    """Independent best-effort cleanup. One failure cannot skip another owner."""
    from step9a_r2_checks import audit
    def attempt(label,fn):
        try:return fn()
        except Exception as exc:
            result['cleanup_errors'].append(label+': '+type(exc).__name__+': '+str(exc))
            result['passed']=False
    if board:
        board.display_cleanup=True
        if recording:attempt('capture stop',lambda:command(board,'aec-record-slots 0',3))
        monitor=attempt('monitor',board.display_monitor.result)
        if monitor is not None:result['monitor']=monitor
        if ready:
            commands=[('demo hold',10),('audio 0',3),('disconnect',12)]
            if result.get('display_setup'):
                commands += [('disp character off',3),('disp bench off',3)]
            commands.append(('playback-volume -6',3))
            for text,timeout in commands:
                attempt(text,lambda text=text,timeout=timeout:board.command(text,timeout))
        attempt('serial close',board.close)
        raw=attempt('serial read',lambda:(path/'serial.log').read_bytes())
        if raw is not None:
            result['boot_count']=raw.count(b'ESP-ROM:')
            value=attempt('raw audit',lambda:audit(raw))
            if value is not None:result['raw_audit']=value
            result['passed'] &= result.get('raw_audit',{}).get('passed') is True and result['boot_count']==1 and result.get('monitor',{}).get('passed') is True
    if bot:attempt('bot stop',bot.stop)


def run(args):
    from alsa import resolve_jabra, play_wav
    from demo_session import prepare_board
    from e2e_common import perf
    from echoear_aec_alignment import analyze,load_nearend_source
    from echoear_uplink_ab import mixer
    from esp_serial import EspSerial
    from step7_support import BotServer
    from step9a_echoear import verify,check_esptool,esptool_python
    out=args.out.resolve()
    protocol=json.loads((out/'protocol.json').read_text())
    binding=json.loads((out/'binding.json').read_text());verify_binding(binding)
    consumption=Path(protocol['scored_consumption_marker'])
    if consumption.exists():raise ValueError('Sole predeclared scored campaign already consumed')
    if (binding.get('state')!='PREPARED_REVIEWED'
            or sha(out/'protocol.json')!=binding['inputs'].get(str(out/'protocol.json'))
            or (out/'protocol.json').read_bytes()!=PROTOCOL.read_bytes()):
        raise ValueError('Reviewed exact consumed protocol binding required')
    if not os.getenv('DG_BENCH_LOCK_FD'):raise ValueError('Use hil/bench.py after explicit new B handoff')
    if not args.approval or not args.approval.is_file():raise ValueError('Exact Astra approval copy required')
    if sha(args.approval)!=binding.get('astra_approval_sha256'):
        raise ValueError('Astra approval differs from frozen copy')
    if not args.grant_note or not args.grant_note.is_file():raise ValueError('Explicit B handoff note required')
    until=datetime.fromisoformat(args.grant_until.replace('Z','+00:00'))
    if until.tzinfo is None or (until-datetime.now(timezone.utc)).total_seconds()<1020:
        raise ValueError('B grant must have >=1020s remaining; do not queue a hold across its deadline')
    # Eligibility attempts have distinct directories; one scoring marker for
    # the entire frozen binding, retained even on fatal setup/connection error.
    path=out/'acquisition';path.mkdir(exist_ok=False)
    result=dict(passed=False,qualification=False,state='ELIGIBILITY',originals=[],samples=[],cleanup_errors=[],
                nic_before=nic_snapshot(),elf_sha256=protocol['firmware_elf_sha256'],
                binding_sha256=sha(out/'binding.json'),approval_sha256=sha(args.approval),
                grant_note=str(args.grant_note),grant_note_sha256=sha(args.grant_note),grant_until=until.isoformat())
    (path/'grant-note.md').write_bytes(args.grant_note.read_bytes())
    board=bot=None;ready=recording=consumed=False
    def save():write(path/'result.json',result)
    def deadline(*_):raise TimeoutError('Predeclared 900s AEC hold deadline')
    signal.signal(signal.SIGALRM,deadline);signal.alarm(900)
    os.environ.update(DG_BOARD='echoear',STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'),
        ECHOEAR_AEC_FULL_DUPLEX='1',ECHOEAR_AEC_EVALUATION_DIR=str(path/'bot'),
        RTP_EVIDENCE_DIR=str(path/'rtp'),RTP_EVIDENCE_MAX_PACKETS='60000',
        PIPECAT_SCTP_MAX_CHUNK_SIZE='1100',PYTHONDONTWRITEBYTECODE='1')
    from step9a_assets import asset_server
    from step9a_support import host_address
    (path/'bot').mkdir();write(path/'bot/protocol.json',protocol)
    try:
        check_esptool();port=verify(path)
        image=Path(protocol['firmware_image']);flash=json.loads((image/'flasher_args.json').read_text())
        cmd=[esptool_python(),'-m','esptool','--chip','esp32s3','--port',port,'--baud','460800',
             '--after','no-reset','write-flash',*flash['write_flash_args']]
        for address,name in flash['flash_files'].items():cmd.extend([address,name])
        with (path/'flash.log').open('w') as log:
            subprocess.run(cmd,cwd=image,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=95)
        card,device=resolve_jabra();result['mixer_before']=mixer(card)
        board=EspSerial(port,log_path=path/'serial.log',reset_on_open=False,display_checks=True)
        board.reset();board.wait_for('READY transport-console',35,since=0)
        board.wait_for('elf_sha256='+protocol['firmware_elf_sha256'],3,since=0);ready=True
        for text in ('demo hold','audio 0','quiet 1','playback-volume 0'):
            command(board,text,38)
        def snap(label,live=False):
            cursor=board.cursor;p=perf(board)
            output=[re.search(r'OUTPUT_PATH volume_relative_db=(-?\d+)',line)
                    for _,_,line in board.lines_since(cursor) if 'OUTPUT_PATH ' in line]
            if len(output)!=1 or output[0] is None:raise ValueError('Missing output volume readback')
            p['output_volume_relative_db']=int(output[0][1])
            write(path/(label+'.json'),p)
            if live:
                result['samples'].append(p)
                save()
                from step9a_merge_checks import memory_gate
                if not memory_gate([p])['passed']:raise RuntimeError('Fatal full-peer memory gate')
            guard(p)
            if p.get('audio',{}).get('write_timeouts')!=0:
                raise RuntimeError('I2S write timeout during AEC campaign')
            save();return p
        def capture(action,name,wait=.4):
            nonlocal recording
            command(board,'aec-record-slots 1');recording=True
            time.sleep(wait);action();time.sleep(.4)
            command(board,'aec-record-slots 0');recording=False
            data=dump_capture(board,path/(name+'.npz'))
            if data['schema']!=2 or not saturation(data['pcm'])['passed']:
                raise ValueError('Invalid/saturated prerequisite capture')
            return data['pcm']
        quiet_raw=capture(lambda:time.sleep(1),'quiet')
        with StreamCapture(path/'quiet-jabra.wav',device,maximum_seconds=3) as q:
            time.sleep(1)
        if q.error:raise ValueError(q.error)
        quiet=dict(reference=quiet_raw[3200:-3200,2],microphone_rms=rms(quiet_raw[3200:-3200,0]),
                   jabra_rms=rms(q.samples()[3200:-3200]))
        result['room']=dict(board_dbfs=dbfs(quiet_raw[3200:-3200,0]),jabra_dbfs=dbfs(q.samples()[3200:-3200]),
                            reference=reference_threshold(quiet['reference']))
        result['eligibility_nic']=nic_gate(result['nic_before'],nic_snapshot())
        if max(result['room']['board_dbfs'],result['room']['jabra_dbfs'])>-55 or not result['eligibility_nic']['passed']:
            result['state']='DEFERRED_BEFORE_CONSUMPTION';save();return 2
        # Remeasure independent AFE delay and electrical reference identity.
        def probe():
            cursor,seq=command(board,'aec-probe',8)
            board.wait_for(rf'^DONE #{seq} aec-probe$',5,since=cursor)
        dac=capture(probe,'reference-probe',wait=1)
        prompt=Path(protocol['independent_delay_prompt'])
        near=capture(lambda:play_wav(prompt,device),'independent-delay')
        result['independent_alignment']=analyze(dac,near,load_nearend_source(prompt))
        alignment=result['independent_alignment']
        if not (alignment['known_source_to_reference']['available'] and
                alignment['reference_silent_for_independent_afe'] and
                alignment['independent_nearend_afe_delay']['available']):
            raise ValueError('Independent reference/AFE delay prerequisites failed')
        delay=alignment['independent_nearend_afe_delay']['lag_samples']
        # DAC-silent bounded word calibration, raw MIC1 only. No gain/mixer
        # changes; freeze the stimulus before scoring. Original source kept.
        stimuli=Path(protocol['stimulus_prepare_path']);word=pcm(stimuli/'stop-16k.wav')
        marker=pcm(stimuli/'marker-16k.wav');scale=.5;calibrations=[]
        for index in range(3):
            playback48(path/'word-calibration.wav',word*scale)
            raw=capture(lambda:play_wav(path/'word-calibration.wav',device),f'word-calibration-{index}')
            m=known_match(word,raw[:,0])
            if not m.get('available'):raise ValueError('Word calibration source identity unavailable')
            # Frozen source-only active mask, not a residual-based selection.
            powers=np.convolve(word*word,np.ones(320)/320,mode='same')
            mask=powers>max(1.,float(powers.max())*.001)
            level=dbfs(raw[m['start']:m['end'],0][mask])
            calibrations.append(dict(scale=scale,active_dbfs=level,source_match=m))
            if abs(level+39)<=1:break
            scale*=10**((-39-level)/20)
        result['calibration']=calibrations
        if abs(calibrations[-1]['active_dbfs']+39)>1:raise ValueError('Word calibration did not reach -39+/-1dBFS')
        playback48(path/'word.wav',word*scale);playback48(path/'marker.wav',marker*scale)
        write(path/'calibrated-stimuli.json',dict(scale=scale,word_sha256=sha(path/'word.wav'),
              marker_sha256=sha(path/'marker.wav'),delay_samples=delay))
        snap('before-peer')
        with asset_server():
            result['display_setup']=prepare_board(board,'http://'+host_address()+':7861',path/'budget.json')
        # Allocate before peer. Repeated capture commands reuse this PSRAM.
        command(board,'aec-record-slots 1');command(board,'aec-record-slots 0')
        from bench_calibration import echoear_gain_path
        from echoear_checks import codec_boot_evidence
        from step9a_merge_checks import census_gate,ready_gdma_record
        result['gain_before']=echoear_gain_path(board)
        if (result['gain_before']['analog_db'],result['gain_before']['digital_percent'])!=(30,125):
            raise ValueError('Frozen microphone gain profile mismatch')
        boot_text='\n'.join(line for _,_,line in board.lines_since(0))
        result['codec_boot']=codec_boot_evidence(boot_text)
        if not result['codec_boot']['passed'] or 'UNQUALIFIED TDM MIC3_GAIN=0x10 analog_db=0' not in boot_text:
            raise ValueError('Codec/reference gain readback failed')
        result['display_census']=census_gate(boot_text.splitlines())
        with consumption.open('x') as consumed_file:
            consumed=True
            json.dump(dict(t=time.monotonic(),binding_sha256=sha(out/'binding.json')),consumed_file)
        (out/'campaign-consumed.json').write_bytes(consumption.read_bytes())
        result['state']='SCORED_STARTED';save()
        bot=BotServer(path/'bot',mode='live-test',entrypoint='demo_bot.py');bot.start()
        cursor=board.cursor;board.command('connect',90);board.wait_for('STATE Ready',5,since=cursor)
        ready_records=[line for _,_,line in board.lines_since(cursor) if line.startswith('DISPLAY_R4_GDMA_READY ')]
        if len(ready_records)!=1:raise ValueError('Missing complete live-peer LCD GDMA proof')
        result['ready_gdma']=ready_gdma_record(ready_records[0])
        result['audio_enable_request_s']=time.monotonic();board.command('audio 1')
        time.sleep(.5);first_perf=snap('live-start',live=True)
        if any(first_perf.get(k)!=0 for k in __import__('playback_quality').TRANSPORT_COUNTERS):
            raise ValueError('Startup transport damage cannot be hidden by first scored snapshot')
        command_number=0
        def control(op,key):
            nonlocal command_number
            command_number+=1;write(path/'bot/command.json',dict(number=command_number,op=op,case_id=key))
            if op in ('seal','health'):
                expected='case-sealed' if op=='seal' else 'health-ack'
                deadline_at=time.monotonic()+3
                while time.monotonic()<deadline_at:
                    if any(e.get('event')==expected and e.get('number')==command_number for e in events()):return
                    if bot.proc.poll() is not None:raise RuntimeError('Owned bot exited before control acknowledgement')
                    time.sleep(.01)
                raise TimeoutError('Unacknowledged evaluation '+op)
        def events():return jsonl(path/'bot/events.jsonl')
        def wait_event(key,frame,since,timeout):
            deadline_at=time.monotonic()+timeout
            while time.monotonic()<deadline_at:
                if bot.proc.poll() is not None:raise RuntimeError('Owned Flux bot exited')
                for e in events():
                    if e.get('case_id')==key and e['t']>=since and e.get('frame')==frame:return e
                time.sleep(.01)
            raise TimeoutError('Missing '+frame+' for '+key)
        result['echo_phase_start_s']=time.monotonic()
        cases=[(r['id'],'echo') for r in protocol['echo_replies']]+[(key,'barge') for key in protocol['barge_ids']]
        for index,(key,kind) in enumerate(cases):
            if index==10:result['echo_phase_end_s']=time.monotonic()
            case=path/key;case.mkdir();row=dict(id=key,kind=kind,passed=False,start_s=time.monotonic())
            result['originals'].append(row);save()
            command(board,'aec-record-slots 1');recording=True
            with StreamCapture(case/'jabra.wav',device,maximum_seconds=28) as recorder:
                control('speak',key)
                started=wait_event(key,'BotStartedSpeakingFrame',row['start_s'],8)
                row['bot_started']=started
                if kind=='echo':
                    row['bot_stopped']=wait_event(key,'BotStoppedSpeakingFrame',started['t'],8)
                    time.sleep(.4)
                else:
                    time.sleep(max(0,started['t']+.8-time.monotonic()))
                    generated=path/'bot'/(key+'.pcm')
                    row['generated_before_trigger']=generated.stat().st_size//2
                    row['user_request_s']=time.monotonic()
                    play_wav(path/'word.wav',device)
                    time.sleep(max(0,row['user_request_s']+2.1-time.monotonic()))
                    row['marker_request_s']=time.monotonic()
                    play_wav(path/'marker.wav',device)
                    time.sleep(3) # fixed final transcript deadline, all events kept
                row['end_s']=time.monotonic()
            command(board,'aec-record-slots 0');recording=False
            if recorder.error:raise ValueError(recorder.error)
            if row['end_s']-row['start_s']>12:raise ValueError('Original exceeded predeclared12s deadline')
            row['after']=snap(key+'-after',live=True)
            captured=dump_capture(board,case/'slots.npz')
            source=np.frombuffer((path/'bot'/(key+'.pcm')).read_bytes(),dtype='<i2').astype(float)
            case_events=[e for e in events() if e.get('case_id')==key]
            analyze_original(row,captured,recorder.samples(),recorder.receipts,source,word,marker,quiet,delay,case_events)
            if kind=='barge':
                row['late_final_events']=[e for e in case_events if e.get('event')=='user-turn-stopped' and e['t']>row['end_s']]
                if row['late_final_events']:row['transcript']['correct']=False
            control('seal',key);row['export_after']=snap(key+'-export',live=True)
            save();print(key,'acquired',row['passed'],flush=True)
        result['gain_after']=echoear_gain_path(board)
        result['gain_unchanged']=all(result['gain_before'][k]==result['gain_after'][k]
                                     for k in ('analog_db','register','digital_percent','protection'))
        # The final PERF is the counter bracket for an endpoint fixed before
        # requesting it. All earlier case PERF/dumps/gain readbacks are inside.
        result['whole_end_s']=time.monotonic();snap('live-end',live=True)
        control('health',None);time.sleep(.2)
        packets=jsonl(path/'rtp/rtp.jsonl');socket_packets=jsonl(path/'rtp/socket-rtp.jsonl')
        if not packets or packets[0]['observed_ms']/1000-result['audio_enable_request_s']>1:
            raise ValueError('Missing session-start RTP coverage')
        result['whole_start_s']=packets[0]['observed_ms']/1000+.0001
        result['integrity']=session_integrity(result['samples'],packets,
            start_s=result['whole_start_s'],end_s=result['whole_end_s'])
        result['rtp_coverage']=rtp_coverage(socket_packets,packets,result['whole_start_s'],result['whole_end_s'])
        health=[e for e in events() if e.get('event')=='rtp-observer-health']
        result['observer_health']=dict(passed=len(health)==16 and all(e.get('passed') is True for e in health)
            and health[-1]['t']>=result['whole_end_s'],samples=health)
        result['rtp_trace_nontruncated']=not any(e.get('truncated') is True for e in jsonl(path/'rtp/socket-status.jsonl'))
        echo_events=[e for e in events() if result['echo_phase_start_s']<=e['t']<=result['echo_phase_end_s']]
        false=[e for e in echo_events if (e.get('event')=='frame' and e.get('frame') in
                    ('UserStartedSpeakingFrame','InterruptionFrame'))
               or (e.get('event')=='user-turn-stopped')
               or (e.get('event')=='frame' and e.get('frame')=='TranscriptionFrame' and e.get('text'))]
        from echoear_aec_campaign_analysis import turn_events
        result['echo_turn_events']=turn_events(echo_events)
        result['false_user_events']=false
        result['events']=dict(single_peer=len({e.get('pc_id') for e in events()})==1,
            no_mute=not any(e.get('frame')=='UserMuteStartedFrame' for e in events()),
            no_errors=not any(e.get('event')=='evaluation-error' or e.get('frame') in
                             ('ErrorFrame','FatalErrorFrame') for e in events()))
        server_log=(path/'bot/server-1.log').read_text()
        result['provider_errors']=re.findall(r'^.*(?:\bERROR\b|Traceback|keepalive.*timeout|timed out).*$'
                                             ,server_log,flags=re.M|re.I)
        # Scoring a complete session must not hide a late final at a seal or
        # after the last original. Keep every real final in the population.
        final_stt=adjudicate_transcripts(result['originals'],events())
        for row in result['originals']:
            if row['kind']=='barge':row['transcript']=final_stt['scores'][row['id']]
        result['unassigned_final_events']=final_stt['unassigned']
        echo=result['originals'][:10];barge=result['originals'][10:]
        medians=[r.get('erle',{}).get('median_db') for r in echo]
        result['median_reply_erle_db']=float(np.median(medians)) if all(type(v) in (int,float) for v in medians) else None
        result['barge_correct']=sum(r.get('transcript',{}).get('correct') is True for r in barge)
        result['passed']=bool(len(echo)==10 and len(barge)==5 and all(r['passed'] for r in result['originals'])
            and result['median_reply_erle_db'] is not None and result['median_reply_erle_db']>=20
            and not false and result['barge_correct']>=4 and result['integrity']['passed']
            and all(result['events'].values()) and result['rtp_trace_nontruncated'] and result['gain_unchanged']
            and not result['unassigned_final_events'] and result['rtp_coverage']['passed']
            and result['observer_health']['passed'] and not result['provider_errors'])
        result['state']='SCORED_COMPLETE'
    except BaseException as exc:
        result.update(error=type(exc).__name__+': '+str(exc),passed=False,
                      state='SCORED_ABORTED_REAL_FAILURE' if consumed else 'PREREQUISITE_FAILURE')
    finally:
        signal.alarm(0)
        cleanup_owned(board,bot,ready,recording,path,result)
        try:
            result['nic_after']=nic_snapshot();result['nic']=nic_gate(result['nic_before'],result['nic_after'])
        except Exception as exc:
            result['nic']=dict(passed=False,error=type(exc).__name__+': '+str(exc));result['passed']=False
        try:
            verify_binding(binding);result['inputs_unchanged']=True
            result['mixer_after']=mixer(card)
        except Exception as exc:
            result.update(passed=False,postcheck_error=str(exc))
        result['passed'] &= not result['cleanup_errors'] and result['nic']['passed'] and result.get('mixer_before')==result.get('mixer_after')
        result['qualification']=bool(result['passed']);result['aec_default_remains_off']=True
        result['bench_released_on_exit']=True;save()
    print(json.dumps(dict(state=result['state'],passed=result['passed'],result=str(path/'result.json'))),flush=True)
    return 0 if result['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--approval',type=Path);parser.add_argument('--grant-note',type=Path)
    parser.add_argument('--grant-until')
    args=parser.parse_args()
    if args.prepare:prepare(args.out,args.approval)
    else:raise SystemExit(run(args))
