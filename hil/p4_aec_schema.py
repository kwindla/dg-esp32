#!/usr/bin/env python3
"""Offline schema replay of retained P4/S3 AEC PERF, bot events and guard logs.

Smoke archives contain no all-slot PCM. A modeled PCM exercises the unchanged
ERLE key path; it is explicitly not a measurement, calibration or qualification.
"""
import argparse
import json
from pathlib import Path
import numpy as np

import p4_aec_policy as policy
import p4_aec_product as product
import p4_contention as contention
import p4_audio_fix4 as fix4
import board_smoke as smoke
from make_bundle import digest
from wire_contract import require,parse_capture,validate_capture


class Reads(dict):
    def __init__(self,value,path,seen,absent):
        self.path,self.seen,self.absent=path,seen,absent
        def wrap(v,p):
            if isinstance(v,dict):return Reads(v,p,seen,absent)
            if isinstance(v,list):return [wrap(x,p+'[]') for x in v]
            return v
        super().__init__((k,wrap(v,path+'.'+k)) for k,v in value.items())
    def __getitem__(self,key):
        self.seen.add(self.path+'.'+key)
        if key not in self:self.absent.add(self.path+'.'+key)
        return super().__getitem__(key)
    def get(self,key,default=None):
        self.seen.add(self.path+'.'+key)
        if key not in self:self.absent.add(self.path+'.'+key)
        return super().get(key,default)


PERF_REQUIRED={
    'gain':('aec.reference_health.gain_db',),
    'clips':('generation','pc_id','mic_in_turn_clips','mic_gate_open_clips','mic_gate_closed_clips',
             'aec.errors','aec.reference_clips','aec.output_clips'),
    'aec_contention':('aec.enabled','aec.errors','aec.reference_clips','aec.output_clips'),
    '13b_resources':('heap.internal.minimum_free','heap.internal.largest','failed_allocations.total',
        'failed_allocations.internal','diagnostic_log_drops','capture.rx_overflows','capture.rx_completions',
        'capture.read_errors','capture.read_timeouts','capture.read_frames','i2s_underflows',
        'dtls_duration_us','per_core_idle_percent','device_us','display.lcd_initialized','display.failed','display.dead'),
    'stacks':('generation','tx_stack','decode_stack','owner_stack','rx_stack','tasks','rx_stack_bytes'),
}


def field(row,path):
    for key in path.split('.'):row=row[key]
    return row


def audit(root):
    root=Path(root);reports=[]
    rng=np.random.default_rng(15)
    pcm=np.zeros((16000,5));pcm[:,0]=rng.normal(0,200,len(pcm));pcm[:,2]=rng.normal(0,10,len(pcm))
    pcm[1024:,4]=pcm[:-1024,0]/20;quiet=np.ones((8192,5))
    for board in ('p4xc','echoear'):
        for variant in ('normal','demo'):
            directory=root/f'smoke-{board}-aec-{variant}'
            perf_paths=sorted(directory.glob('perf-*.json'));turn_paths=sorted(directory.glob('turn-*.json'))
            require(perf_paths and turn_paths,'missing real PERF/turn archive '+str(directory))
            seen={};missing={};outcomes={};sources={};records=[]
            def run(name,callback):
                reads=seen.setdefault(name,set());absent=missing.setdefault(name,set())
                def track(value,prefix):return Reads(value,prefix,reads,absent)
                try:
                    result=callback(track)
                    outcomes.setdefault(name,[]).append(dict(schema_read_completed=True,
                        acceptance=result.get('passed',result.get('stack_checks_passed')) if isinstance(result,dict) else None))
                except (KeyError,TypeError,IndexError) as exc:
                    raise ValueError(name+' schema mismatch in '+str(directory)+': '+str(exc)) from exc
                except ValueError as exc:
                    # A retained capture can fail a measured gate without a
                    # schema mismatch. Required-field assertions below are hard.
                    outcomes.setdefault(name,[]).append(dict(schema_read_completed=True,gate_error=str(exc)))
            for path in perf_paths:
                row=json.loads(path.read_text());sources[str(path)]=digest(path);records.append(row)
                for paths in PERF_REQUIRED.values():
                    for name in paths:field(row,name)
                gain=field(row,'aec.reference_health.gain_db')
                require(type(gain) is int and gain in policy.GAINS,'real reference gain type/grid')
                run('reference_gain',lambda t:policy.reference_gain(t(row,'PERF'),t(row,'PERF'),gain))
                run('clip_evidence',lambda t:policy.clip_evidence(t(row,'PERF'),t(row,'PERF'),pcm))
                run('13b_resources',lambda t:contention.resources([t(row,'PERF')]*2,board=board,require_cpu=False))
                run('task_stacks',lambda t:fix4.stack_report([t(row,'PERF')]*2,board=board))
                require(row['aec']['enabled'] is True and all(type(row['aec'][k]) is int for k in
                    ('errors','reference_clips','output_clips')),'real AEC contention counter schema')
            # Real same-generation before/after samples, not just single-row checks.
            for before,after in zip(records,records[1:]):
                if (before['generation'],before['pc_id'])==(after['generation'],after['pc_id']):
                    run('clip_evidence_delta',lambda t:policy.clip_evidence(t(before,'PERF'),t(after,'PERF'),pcm))
            long_turns=[];prompt_count=0
            for path in turn_paths:
                row=json.loads(path.read_text());sources[str(path)]=digest(path)
                events=row['events']
                if row['kind']=='long':
                    run('echo_score',lambda t:policy.echo_score(pcm,quiet,1024,[t(e,'EVENT') for e in events],True))
                    try:long_turns.append(smoke.turn_checks(events,'long',True,calibration=True))
                    except ValueError as exc:long_turns.append(dict(passed=False,error=str(exc)))
                if row['kind']=='talkover':
                    require(len(row['playback'])==2,'missing actual near-end playback record')
                    frames=smoke.downstream_frames(events)
                    start=next(e['monotonic_ns'] for e in frames if e['frame']=='BotStartedSpeakingFrame')
                    stop=next(e['monotonic_ns'] for e in frames if e['frame']=='BotStoppedSpeakingFrame')
                    spec=next(s for s in policy.fixtures() if s['id']=='italy')
                    run('prompt_score',lambda t:policy.prompt_score(spec,[t(e,'EVENT') for e in events],t(row['playback'][1],'PLAYBACK'),start,stop))
                    prompt_count+=1
            serial=directory/'serial.log';sources[str(serial)]=digest(serial)
            guards=[line for line in serial.read_text().splitlines() if 'AEC_GUARD onset_upper_us=' in line]
            require(guards and all(policy.GUARD_RE.search(line) for line in guards),'real guard console schema mismatch')
            parsed=[dict(zip(('onset_upper_us','source_deadline_us','guard_ms','rearm_ms','previous_end_us'),
                map(int,policy.GUARD_RE.search(line).groups()))) for line in guards]
            require(all(g['source_deadline_us']==g['onset_upper_us']+1000*g['guard_ms'] for g in parsed),'real guard deadline mismatch')
            run('guard_score',lambda t:policy.guard_score(0,[t(v,'TURN') for v in long_turns],guards,
                t(dict(inside_guard=False,reason='no raw probe in smoke archive'),'INSIDE'),
                t(dict(detected=False,after_rearm=False,reason='no independent after-rearm probe'),'AFTER')))
            for label in ('main','resume','cold') if variant=='demo' else ('main',):
                wire=directory/(label+'-wire.log');sources[str(wire)]=digest(wire)
                capture=parse_capture(wire.read_text().splitlines())
                # The existing early runner stored product capability in bot logs.
                if not capture.capability:
                    bot=directory/(label+'-bot.log');sources[str(bot)]=digest(bot)
                    capture=parse_capture(wire.read_text().splitlines()+[l for l in bot.read_text().splitlines() if 'DEMO_CAPABILITY ' in l])
                validate_capture(capture,profile='product',intended_aec=True,target='esp32p4' if board=='p4xc' else 'esp32s3')
                audio=capture.records['start']['body']['audio']
                product.image_audio(capture,dict(boot_ref_gain_db=records[0]['aec']['reference_health']['gain_db'],
                    p4_aec_calibration=dict(ref_gain_db=audio['ref_gain_db'],guard_ms=parsed[0]['guard_ms'])),board)
            require(not any(missing.values()),'scorer reads absent real fields: '+str({k:sorted(v) for k,v in missing.items() if v}))
            reports.append(dict(board=board,variant='aec-'+variant,perf_records=len(records),turn_records=len(turn_paths),
                nearend_trials=prompt_count,guard_lines=len(guards),sources=sources,required_perf_fields=PERF_REQUIRED,
                read_fields={k:sorted(v) for k,v in seen.items()},optional_absent_reads={k:sorted(v) for k,v in missing.items()},
                outcomes=outcomes,console_guard_fields=sorted(parsed[0]),
                wire_fields=['start.body.audio.aec','start.body.audio.ref_gain_db','start.body.audio.guard_ms',
                    'client_ready','sdp','opus_enc','dc_open','msg=pong','msg=display-ready','UPLINK_INTERVALS','DEMO_CAPABILITY']))
    return dict(kind='p4-aec-real-record-schema-audit',passed=True,qualification=False,hardware_used=False,
        scope='offline schema replay only; modeled PCM is not raw P4/S3 measurement or measured delay/ERLE',reports=reports,
        not_in_smoke_archive=dict(raw_capture=['AEC_DUMP BEGIN/PCM/TIME/CHECK/END','schema','rate','channels','pcm','timing'],
            clock=['AEC_CLOCK.seq','AEC_CLOCK.device_us','coordinate','lower_s','upper_s'],
            runtime_gain=['AEC_REF_GAIN.db','AEC_REF_GAIN.reg45'],
            coverage='These require dedicated acquisition; firmware templates and synthetic capture/readback controls cover their schemas.'))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    args=p.parse_args(argv);policy.write_new(args.out,audit(args.root));return 0


if __name__=='__main__':raise SystemExit(main())
