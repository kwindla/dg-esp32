"""Pure parsers/analysis for CRC-protected spike-B records; opens no hardware."""
import json
import math
import re
import statistics
import zlib


def records(lines):
    result=[]
    for line in lines:
        if line.startswith(b'ERR '): raise ValueError(line.decode('utf-8','replace'))
        if not line.startswith(b'B '): continue
        try:
            payload, checksum=line[2:].rsplit(b' ',1)
            if len(checksum)!=8 or zlib.crc32(payload)!=int(checksum,16): raise ValueError('CRC mismatch')
            row=json.loads(payload)
            if not isinstance(row,dict) or not isinstance(row.get('kind'),str): raise ValueError('record kind')
            result.append(row)
        except (ValueError,TypeError) as exc:
            raise ValueError('Corrupt B measurement record') from exc
    return result


def one(rows, kind):
    matches=[row for row in rows if row['kind']==kind]
    if len(matches)!=1: raise ValueError(f'Expected one {kind}, got {len(matches)}')
    return matches[0]


def sequence(rows, kind, count):
    matches=[row for row in rows if row['kind']==kind]
    if len(matches)!=count or [row['seq'] for row in matches]!=list(range(count)):
        raise ValueError(f'Missing, duplicate or reordered {kind}')
    return matches


def distribution(values):
    values=sorted(values)
    if not values: raise ValueError('Empty timing distribution')
    if any(not math.isfinite(v) or v<0 for v in values): raise ValueError('Invalid timing value')
    return dict(count=len(values),p50=statistics.median(values),p99=values[max(0,math.ceil(.99*len(values))-1)],max=values[-1])


def slots(rows):
    end=one(rows,'slots_end')
    selected=[row for row in rows if row['kind']=='slot']
    if sorted(row['channel'] for row in selected)!=[1,2,3,4]: raise ValueError('Missing/duplicate slot')
    if end['overflows'] or any(row['samples']!=end['frames'] for row in selected): raise ValueError('Incomplete slot capture')
    return dict(channels={str(row['channel']):row for row in selected},summary=end)


def gain_identity(baseline, changed, channel, stimulus='floor-based: muted-DAC noise floor'):
    delta={key:baseline['channels'][key]['rms_dbfs']-changed['channels'][key]['rms_dbfs'] for key in baseline['channels']}
    observed=max(delta,key=delta.get)
    if delta[observed]<6: observed=None
    return dict(gain_reduced_channel=channel,drop_db=delta,observed_slot=observed,
                matches_expected=observed==str(channel),stimulus=stimulus,
                criterion='largest RMS drop, at least 6 dB; ambiguous/insufficient floor is not a match')


def aec(rows, expected_gain=15):
    begin=one(rows,'aec_begin');end=one(rows,'aec_end')
    chunks=sequence(rows,'aec_chunk',end['chunks']);chunk=begin['chunk']
    if begin['rate']!=16000 or begin['channels']!=2 or begin['mic']!=1 or begin['ref']!=3 or not 0<chunk<=2048:
        raise ValueError('AFE MR format mismatch')
    if (begin.get('ref_gain_db')!=expected_gain or expected_gain not in range(0,34,3) or
        begin.get('ref_gain_reg')!=0x10|expected_gain//3 or begin.get('dma_descriptors')!=4 or
        begin.get('dma_frames')!=240 or begin.get('stack_memory')!='PSRAM' or
        begin.get('stack_bytes')!=16384 or begin['core']!=1):
        raise ValueError('AFE production operating point mismatch')
    if not chunks or end['samples']!=len(chunks)*chunk or end['output_samples']!=end['samples']:
        raise ValueError('AFE output count mismatch')
    if any(row['bytes']!=2*chunk or row['core']!=begin['core'] for row in chunks): raise ValueError('AFE chunk/core mismatch')
    if sum(row['cycles'] for row in chunks)!=end['cycles'] or sum(row['us'] for row in chunks)!=end['process_us']:
        raise ValueError('AFE timing sum mismatch')
    if end['overflows']: raise ValueError('AFE capture overflow')
    if end['samples']<16000*begin['seconds']: raise ValueError('Short AFE run')
    heaps={row['phase']:row for row in rows if row['kind']=='heap'}
    for phase in ('before_worker','after_worker_create','before_create','after_create','after_buffers','end','after_destroy','after_worker_delete'):
        if phase not in heaps: raise ValueError(f'Missing AFE heap phase {phase}')
    before=heaps['before_create'];after=heaps['after_create']
    return dict(config=begin,summary=end,cycles=distribution([row['cycles'] for row in chunks]),
        process_us=distribution([row['us'] for row in chunks]),heaps=heaps,
        afe_internal_delta=before['internal_free']-after['internal_free'],
        afe_psram_delta=before['psram_free']-after['psram_free'],
        worker_internal_delta=heaps['before_worker']['internal_free']-heaps['after_worker_create']['internal_free'],
        worker_psram_delta=heaps['before_worker']['psram_free']-heaps['after_worker_create']['psram_free'],
        heap_delta_scope='AFE factory delta inside allocated worker; worker stack/TCB separately before/after task creation; DMA already configured',
        heap_floor_met=min(row['internal_min'] for row in heaps.values())>=81920 and
            min(row['internal_largest'] for row in heaps.values())>=32768,
        ten_minute_soak=begin['seconds']>=600 and end['samples']>=600*16000,
        erle_description='Crude MIC1/output power ratio after initial two seconds; not delay-aligned speech ERLE')


def opus(rows):
    unavailable=[row for row in rows if row['kind']=='opus_unavailable']
    if unavailable: return dict(available=False,reason=one(rows,'opus_unavailable')['reason'])
    begin=one(rows,'opus_begin');end=one(rows,'opus_end');frames=sequence(rows,'opus_frame',end['frames'])
    if begin.get('codec_version')!='2.5.0' or begin.get('pcm_bytes')!=640:
        raise ValueError('Opus P4 codec version/frame ABI mismatch')
    if any(begin[key]!=value for key,value in dict(rate=16000,channels=1,frame_ms=20,bitrate=24000,complexity=0,fec=False,dtx=False).items()):
        raise ValueError('Opus production settings mismatch')
    if not frames or any(row['decoded_samples']!=320 or not 0<row['encoded_bytes']<=1276 for row in frames):
        raise ValueError('Opus frame mismatch')
    if (begin.get('stack_memory')!='INTERNAL' or begin.get('stack_bytes',0)<32768 or begin.get('core')!=0 or
        begin.get('input') not in ('uploaded_speech','synthetic_sine') or
        not 0<end.get('stack_high_water_bytes',0)<=begin['stack_bytes']):
        raise ValueError('Opus worker stack/core/input witness mismatch')
    if end.get('plc_frames')!=len(frames) or any(row.get('plc_samples')!=320 or row.get('core')!=0 for row in frames):
        raise ValueError('Opus PLC/core witness mismatch')
    return dict(available=True,config=begin,summary=end,encode_cycles=distribution([row['encode_cycles'] for row in frames]),
                decode_cycles=distribution([row['decode_cycles'] for row in frames]),
                plc_cycles=distribution([row['plc_cycles'] for row in frames]))


def loop_result(lines):
    ends=[line for line in lines if line.startswith(b'LOOP END ')]
    if len(ends)!=1: raise ValueError('Missing/duplicate LOOP END witness')
    match=re.fullmatch(rb'LOOP END reads=(\d+) errors=(\d+)',ends[0])
    if not match or int(match[1])==0 or int(match[2])!=0: raise ValueError('Audio loop read/error failure')
    return dict(reads=int(match[1]),errors=int(match[2]))


def touch(rows, lines, require_loop=False):
    loop=loop_result(lines) if require_loop else None
    if any(row['kind']=='touch_error' for row in rows):
        return dict(available=False,error=one(rows,'touch_error'),audio_loop=loop)
    begin=one(rows,'touch_begin');end=one(rows,'touch_end');polls=sequence(rows,'touch_poll',end['polls'])
    # Product register 0x8140: ASCII "911\0" or "9271". Both use this
    # Goodix register protocol; retain the raw identity and name the controller.
    controller={'39313100':'GT911','39323731':'GT9271'}.get(begin.get('product_id_hex'))
    if (begin['address'] not in (0x5d,0x14) or begin['hz']!=50 or not begin['x_max'] or not begin['y_max'] or
        controller is None): raise ValueError('GT911/GT9271 configuration mismatch')
    points=[]
    for line in lines:
        if line.startswith(b'TOUCH '):
            _,x,y=line.split();points.append((int(x),int(y)))
    if len(points)!=end['events']: raise ValueError('Missing touch event')
    if any(row['rc'] for row in polls) or end['errors'] or end['codec_errors']: raise ValueError('Touch/codec I2C failure')
    if begin['codec_traffic'] and not end['codec_ops']: raise ValueError('No concurrent codec traffic')
    return dict(available=True,controller=controller,config=begin,summary=end,audio_loop=loop,read_us=distribution([row['us'] for row in polls]),
        poll_interval_us=distribution([b['started_us']-a['started_us'] for a,b in zip(polls,polls[1:])]),
        observed_range=dict(x=[min(x for x,y in points),max(x for x,y in points)],
                            y=[min(y for x,y in points),max(y for x,y in points)]) if points else None,
        out_of_range=sum(x>=begin['x_max'] or y>=begin['y_max'] for x,y in points),
        coordinate_validation='observed' if points else 'unmeasured: no touches observed')


def panel(rows):
    begin=one(rows,'panel_begin');end=one(rows,'panel_end')
    if end['overflow']: raise ValueError('Panel callback capture overflow')
    def periods(kind,count):
        events=sequence(rows,kind,count)
        if len(events)<3: raise ValueError('Insufficient panel callbacks')
        return distribution([(b['us32']-a['us32'])%(1<<32) for a,b in zip(events,events[1:])])
    vsync=periods('panel_vsync',end['vsync']);frame=periods('panel_frame',end['frame_complete'])
    if not vsync['p50'] or not frame['p50']: raise ValueError('Zero callback period')
    hz=1e6/vsync['p50']
    return dict(config=begin,summary=end,vsync_period_us=vsync,frame_complete_period_us=frame,refresh_hz=hz,
        effective_pixel_clock_mhz=hz*begin['h_total']*begin['v_total']/1e6,
        timing_meets_50_hz=hz>=50,visual_stability='unmeasured')


def select_panel(runs, visual_stable_clocks=()):
    timing=[run for run in runs if run['timing_meets_50_hz'] and not run.get('driver_underruns_observed',0)]
    visual=[run for run in timing if run['config']['clock_mhz'] in visual_stable_clocks]
    # Preserve selection by lowest tested request, and report the measured PLL
    # result for that same run. Several requests can share a quantized divider.
    lowest=min(timing,key=lambda run:run['config']['clock_mhz']) if timing else None
    stable=min(visual,key=lambda run:run['config']['clock_mhz']) if visual else None
    return dict(lowest_tested_timing_clock_mhz=lowest['config']['clock_mhz'] if lowest else None,
                lowest_tested_timing_effective_pixel_clock_mhz=lowest['effective_pixel_clock_mhz'] if lowest else None,
                lowest_tested_visually_stable_clock_mhz=stable['config']['clock_mhz'] if stable else None,
                lowest_tested_visually_stable_effective_pixel_clock_mhz=stable['effective_pixel_clock_mhz'] if stable else None,
                visual_confirmed_clocks=list(visual_stable_clocks))
