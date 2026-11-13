#!/usr/bin/env python3
"""Step 15d frozen plan, bounded coordinator captures, and offline word-local report."""
import argparse
import json
from pathlib import Path
import random
import re
import shutil
import time
import wave

import numpy as np
import board_smoke as smoke
import p4_aec_measure as measure
import p4_aec_policy as policy
import p4_nearend_analysis as analysis
from atomic_json import atomic_write_json
from make_bundle import digest,verify_bundle
from wire_contract import require

ROOT=Path(__file__).resolve().parents[1]
ELFS={'echoear':'015efd59fe7129cde4031811edb0109f7480cb39064f8de3e5ce48233dba25a0',
      'p4xc':'2831d8bd295eaeea21c603f6aaff11a8d42ffe5a7840c96fd12331cadf7665da'}
ITALY_SHA='cdffc54cc7fbf13551d53a20aa43a98a3a69124596fc2bd42c165ed0e7316b34'
S3_RESTORE='55152f25dcec0e164c8a087d7f5962dd81191e2cdd3f41ac882a58a0a57a626e'
SOURCES=('p4_nearend_capture.py','p4_nearend_analysis.py','p4_aec_measure.py','p4_aec_policy.py',
    'p4_step14.py','p4_product.py','p4_audio.py','p4_audio_fix4.py','board_smoke.py','boards.py',
    'board_serial.py','alsa.py','echoear_aec_capture.py','echoear_aec_timing.py','dump_verify.py',
    'echoear_item2_campaign.py','echoear_restore_verify.py','echoear_aec_bench.py')


def schedule(board):
    require(board in ELFS,'unsupported board')
    rng=random.Random(151500+list(ELFS).index(board));rows=[]
    for pair in range(20):
        arms=['A','B'];rng.shuffle(arms)
        for arm in arms:rows.append(dict(id=f'{board}-{pair+1:02d}-{arm}',board=board,
            pair=pair+1,arm=arm,part=pair//5,order=len(rows)))
    return rows


def make_plan(s3_bundle,p4_bundle,bot_identity,restore_record,restore_sha):
    from p4_step14 import restore_record as checked_restore
    checked_restore(restore_record,restore_sha)
    identity=json.loads(Path(bot_identity).read_text());smoke.identity_schema(identity)
    bundles={}
    for board,path in (('echoear',s3_bundle),('p4xc',p4_bundle)):
        path=Path(path).resolve();manifest,_=verify_bundle(path,board)
        require(manifest['profile']=='aec-normal' and manifest['aec_enabled'] is True and not manifest['autostart']
            and manifest['elf_sha256']==ELFS[board],'exact normal fix-2 image required')
        bundles[board]=dict(path=str(path),manifest_sha256=digest(path/'manifest.json'),elf_sha256=manifest['elf_sha256'])
    fixtures=policy.fixtures();italy=next(p for p in fixtures if p['id']=='italy')
    # The calibration fixture list is checked against the smoke's identical bytes.
    require(italy['sha256']==ITALY_SHA,'identical frozen smoke Italy required')
    return dict(schema=1,kind='step-15d-word-local-plan',qualification=False,bundles=bundles,
        bot_identity=str(Path(bot_identity).resolve()),bot_identity_sha256=digest(bot_identity),identity=identity,
        restore_record=str(Path(restore_record).resolve()),restore_record_sha256=restore_sha,s3_restore_elf=S3_RESTORE,
        fixtures=fixtures,rules=analysis.RULES,schedule={board:schedule(board) for board in ELFS},
        repetitions_per_arm=20,parts_per_board=4,trials_per_part=10,
        state='fresh boot per part; fresh peer/session per repetition; boot gain/guard unchanged',
        optional_control='C omitted: reference mutation would change the predeclared boot configuration',
        source_sha256={name:digest(ROOT/'hil'/name) for name in SOURCES})


def validate_plan(plan):
    require(plan==make_plan(plan['bundles']['echoear']['path'],plan['bundles']['p4xc']['path'],
        plan['bot_identity'],plan['restore_record'],plan['restore_record_sha256']),'plan or authenticated inputs changed')
    return plan


def read_pcm(path):
    with wave.open(str(path),'rb') as w:
        require((w.getnchannels(),w.getsampwidth(),w.getframerate())==(1,2,16000),'16 kHz mono PCM16 uplink required')
        return np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').copy()


def retain_media(io,folder):
    """Copy after disconnect, when the server has finalized its WAV headers."""
    path,_=io.events();deadline=time.monotonic()+10
    while True:
        rows=[json.loads(line) for line in path.read_text().splitlines() if line]
        ends=[r for r in rows if r.get('kind')=='session-end']
        finalized=False
        if len(ends)==1:
            try:
                finalized=all(len(read_pcm(path.parent/(name+'.wav')))==ends[0]['samples'][name]
                    for name in ('uplink','tts'))
            except (OSError,ValueError,EOFError,wave.Error):pass
        if finalized:break
        require(time.monotonic()<deadline,'recorder did not finalize after disconnect')
        time.sleep(.05)
    require(any(r.get('kind')=='peer-binding' and r.get('pc_id')==io.pc_id for r in rows),'recorder peer binding missing')
    files={}
    for name in ('events.jsonl','uplink.wav','tts.wav'):
        target=folder/name;shutil.copyfile(path.parent/name,target)
        files[name]=dict(path=str(target.resolve()),sha256=digest(target))
    files['source_session']=str(path.parent.resolve())
    files['pc_id']=io.pc_id
    files['rtp_sequences']=dict(available=False,missing_packets_word=None,reason=analysis.RULES['packet_loss'])
    return files


class NearendIO(measure.MeasurementIO):
    def boot(self,board):
        at=super().boot(board)
        lines=[line for _,_,line in board.lines_since(at)]
        guards=[re.search(r'AEC_GUARD_CONFIG guard_ms=(\d+) afe_delay_samples=(\d+) rearm_ms=(\d+)',line) for line in lines]
        guards=[m for m in guards if m]
        require(len(guards)==1 and tuple(map(int,guards[0].groups()))==(400,1024,2000),
            'boot guard/delay/rearm differs from declared unchanged settings')
        (self.out/'boot.log').write_text('\n'.join(lines)+'\n')
        return at

    def prepare_manual(self,board):
        super().prepare_manual(board);board.command('quiet 1')
        at=board.cursor;board.command('mic-gain-status')
        mic=board.wait_for(r'MIC_GAIN_PATH analog_db=\d+ reg=0x[0-9a-f]+ digital_percent=\d+',5,since=at)
        match=re.search(r'analog_db=(\d+) reg=0x([0-9a-f]+) digital_percent=(\d+)',mic)
        require(tuple(int(match[i],16 if i==2 else 10) for i in (1,2,3))==(30,0x1a,125),
            'boot MIC1 analog/digital gain differs from declared unchanged settings')
        at=board.cursor;board.command('aec-ref-gain-status')
        ref=board.wait_for(r'AEC_REF_GAIN db=\d+ reg45=0x[0-9a-f]+',5,since=at)
        gain=self.manifest['boot_ref_gain_db'];match=re.search(r'db=(\d+) reg45=0x([0-9a-f]+)',ref)
        require(int(match[1])==gain and int(match[2],16)==0x10+gain//3,'boot reference gain changed')
        policy.write_new(self.out/'boot-settings.json',dict(mic=mic,reference=ref,guard_ms=400,rearm_ms=2000,
            afe_delay_samples=1024,delay_kind=analysis.RULES['delay_kind']))

    def near_only(self,board,folder,spec):
        folder.mkdir();before=self.snapshot(board);started=time.monotonic_ns();self.start_capture(board)
        self.pause(board,.5);near=self.play(board,Path(spec['path']));self.pause(board,.6)
        captured=self.stop_capture(board,folder/'capture.npz');after=self.snapshot(board)
        # Keep a missing transcript as an observed miss, without retrying to a hit.
        deadline=self.clock()+12
        rows=lambda:[r for r in self.events()[1] if r['monotonic_ns']>=started]
        while self.clock()<deadline and not smoke.turn_finished(rows(),1):self.pause(board,.1)
        result=dict(capture=captured,before=before,after=after,events=rows(),near_playback=near,
            complete=True,injection_status='near-only',bot_started_ns=None,bot_stopped_ns=None)
        policy.write_new(folder/'original.json',result)
        return result


def consumed(part_root,plan,board,part):
    """A started repetition is consumed even after crash, abort, missing STT or bad media."""
    rows={};wanted={r['id']:r for r in plan['schedule'][board] if r['part']==part}
    for path in sorted(Path(part_root).glob('attempt-??/*/trial-start.json')):
        row=json.loads(path.read_text());spec=row['spec']
        require(row['plan_sha256']==digest_plan(plan) and spec==wanted.get(spec.get('id')),'foreign/reordered trial journal')
        require(spec['id'] not in rows,'duplicate scored original; cannot retake consumed repetitions')
        rows[spec['id']]=path.parent
    return rows


def digest_plan(plan):
    import hashlib
    return hashlib.sha256(json.dumps(plan,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def restore_ok(board,record):
    return record.get('clean') is True and (record.get('action')=='p4-product-of-record' if board=='p4xc'
        else record.get('restored_elf_sha256')==S3_RESTORE)


def acquire(args,plan,*,io_factory=NearendIO):
    import boards
    from alsa import record_room_baseline,resolve_jabra,set_volumes
    from p4_step14 import restoring,restore_record
    from p4_product import candidate_storage
    boards.require_bench();validate_plan(plan)
    require(Path(args.bundle).resolve()==Path(plan['bundles'][args.board]['path']),
        'candidate must match planned board bundle')
    require(smoke.NORMAL_HASH==plan['s3_restore_elf'],'S3 restore-of-record changed')
    require(re.fullmatch(r'attempt-\d{2}',args.out.name),'output must end in attempt-NN')
    require(not args.out.exists(),'fresh attempt directory required')
    require(not any(args.out.resolve().is_relative_to(Path(v['path'])) for v in plan['bundles'].values()),'output outside bundles required')
    prior=consumed(args.out.parent,plan,args.board,args.part)
    for earlier in range(args.part):
        require(len(consumed(args.out.parent.parent/f'part-{earlier}',plan,args.board,earlier))==10,
            'earlier declared part must be consumed first')
    ordered=[r for r in plan['schedule'][args.board] if r['part']==args.part]
    require(set(prior)=={r['id'] for r in ordered[:len(prior)]},'consumed originals must form declared order prefix')
    previous=sorted(args.out.parent.glob('attempt-??'))
    require(not previous or args.out.name>previous[-1].name,'attempt number must increase')
    specs=[r for r in plan['schedule'][args.board] if r['part']==args.part and r['id'] not in prior]
    require(specs,'part already consumed; no retakes')
    args.out.mkdir(parents=True)
    result=dict(kind='step-15d-part',board=args.board,part=args.part,plan_sha256=digest_plan(plan),
        acquisition_passed=False,qualification=False,device_untouched=True,status='preflight-error',trials=[])
    guard=io=None
    try:
        restore_record(args.restore_record,args.restore_record_sha256)
        result['room']=record_room_baseline(args.out/'room','Step 15d '+args.board)
        if result['room']['noise_contaminated']:
            result.update(status='preflight-noisy',retryable=True);return result
        manifest,_=verify_bundle(args.bundle,args.board)
        with restoring(args.restore_record,args.restore_record_sha256),candidate_storage(args.board,args.bundle,args.out):
            io=io_factory(args,args.out,manifest,plan['identity']);io.preflight_tools();io.preflight_restore()
            card,io.device=resolve_jabra();set_volumes(card,args.out/'mixer.log');guard=io.hold()
            with guard:
                guard.touch();result['device_untouched']=False;io.flash(args.bundle,manifest,'candidate')
                with io.open() as board:
                    try:
                        io.boot(board);io.prepare_manual(board)
                        # No configure(): measure each image's unchanged boot gain/guard.
                        for spec in specs:
                            folder=args.out/spec['id'];folder.mkdir()
                            policy.write_new(folder/'trial-start.json',dict(plan_sha256=digest_plan(plan),spec=spec))
                            trial=dict(spec=spec,status='unavailable')
                            try:
                                io.begin(board,'manual')
                                italy=next(p for p in plan['fixtures'] if p['id']=='italy')
                                if spec['arm']=='A':original=io.near_only(board,folder/'audio',italy)
                                else:original=io.reply(board,folder/'audio',plan['fixtures'][0],italy)
                                trial['original']=original
                                io.finish(board,spec['id'])
                                trial['media']=retain_media(io,folder)
                                trial['status']='retained'
                            except BaseException as exc:
                                trial['error']=type(exc).__name__+': '+str(exc);raise
                            finally:
                                policy.write_new(folder/'trial.json',trial);result['trials'].append(str(folder.resolve()))
                    finally:io.cleanup(board)
                guard.success=True
            result['restore']=guard.record
            require(restore_ok(args.board,guard.record),'clean unchanged R7 restore required')
        result.update(status='acquired',acquisition_passed=True,retryable=False)
    except BaseException as exc:
        result.update(status='preflight-error' if result['device_untouched'] else 'acquisition-error',error=type(exc).__name__+': '+str(exc),
            retryable=True,retry_scope='only repetitions without trial-start.json; started originals remain consumed')
        if guard is not None:result['restore']=guard.record
    finally:
        try:atomic_write_json(args.out/'result.json',result)
        finally:
            if guard is not None:guard.release_signals()
    return result


def arrival_witnesses(events,start,end):
    rows=sorted([e for e in events if e.get('kind')=='audio' and e.get('stream')=='uplink' and
        e['sample_start']<end and e['sample_end']>start],key=lambda e:e['sample_start'])
    require(rows,'missing word arrival witnesses')
    cursor=start
    for e in rows:
        require(type(e['sample_start']) is int and type(e['sample_end']) is int and
            0<=e['sample_start']<e['sample_end'] and e['sample_start']<=cursor and
            type(e.get('arrival_monotonic_ns')) is int and e['arrival_monotonic_ns']>=0,
            'incomplete/invalid decoded arrival witnesses')
        cursor=max(cursor,e['sample_end'])
    require(cursor>=end,'decoded arrival witnesses end before lexical window')
    return rows


def reference_diagnostics(pcm,onset,masks):
    raw=np.asarray(pcm[:,2],dtype=float);filtered=analysis.band(raw)
    indices=onset+np.r_[masks['phrase'],masks['word']]
    db=lambda x:float(10*np.log10(max(float(np.mean(x*x)),1e-24)/32768**2))
    blocks=[]
    for source_block in sorted(set(np.r_[masks['phrase'],masks['word']]//320)):
        selected=indices[(indices-onset)//320==source_block]
        blocks.append(dict(source_start=int(source_block*320),capture_start=int(selected[0]),samples=len(selected),
            band_dbfs=db(filtered[selected]),full_band_dbfs=db(raw[selected])))
    return dict(band_mean_dbfs=db(filtered[indices]),full_band_mean_dbfs=db(raw[indices]),blocks=blocks,
        band_max_block_dbfs=max(b['band_dbfs'] for b in blocks),
        full_band_max_block_dbfs=max(b['full_band_dbfs'] for b in blocks),
        gate='mean 300-3500 Hz MIC3 over concatenated source-active samples <= -60 dBFS; not per-sample silence')


def pre_onset_windows(timing,anchors,first_bounds,first_sample,bot_started,masks):
    """Conservative source-clock windows: every included sample is before onset-40 ms."""
    from echoear_aec_timing import sample_interval
    cutoff=first_bounds[0]-analysis.RULES['residual']['pre_onset_margin_ms']/1000
    runs=[];run=None
    for block in timing:
        interval=sample_interval(int(block[0]),[block],anchors)
        eligible=interval is not None and interval[0]>=bot_started and interval[1]<=cutoff and block[1]<=first_sample
        if eligible:
            if run is not None and run[1]==int(block[0]):run[1]=int(block[1])
            else:run=[int(block[0]),int(block[1])];runs.append(run)
        else:run=None
    minimum=analysis.RATE*analysis.RULES['residual']['min_window_ms']//1000
    runs=[r for r in runs if r[1]-r[0]>=minimum]
    require(runs,'no bracketed bot-speech residual window of at least 100 ms before physical onset-40 ms')
    begin,end=runs[-1];result={}
    for segment,mask in masks.items():
        n=min(len(mask),end-begin)
        result[segment]=dict(raw_start=end-n,raw_end=end,samples=n,requested_samples=len(mask),
            cutoff_host_s=cutoff,physical_onset_bounds_s=first_bounds,bot_started_s=bot_started,
            clock='raw physical capture bounds; AEC indices shifted by adopted delay')
    return result


def residual_estimates(pcm,uplink,measurement,masks,windows,events):
    """B-only output residuals; corrected power is an estimate under declared assumptions."""
    estimates={stage:{} for stage in ('aec','uplink')}
    filtered_aec=analysis.band(pcm[:,4])
    for segment,window in windows.items():
        left=window['raw_start']+analysis.RULES['afe_delay_samples']
        right=window['raw_end']+analysis.RULES['afe_delay_samples']
        for stage in estimates:
            try:
                require(measurement['stage_validity'].get(stage) is True,'stage unavailable for residual estimate')
                require(0<=left<right<=len(pcm),'residual AEC window outside capture')
                if stage=='aec':
                    prefix=filtered_aec[left:right];alignment=dict(offset_samples=left,kind=analysis.RULES['delay_kind'])
                else:
                    require(uplink is not None,'decoded uplink unavailable')
                    alignment=analysis.locate(pcm[left:right,4],uplink,analysis.RULES['uplink_match_min'])
                    at=alignment['offset_samples'];arrival_witnesses(events,at,at+right-left)
                    require(at+right-left<=measurement['alignment']['uplink']['offset_samples']+int(masks['phrase'][0]),
                        'decoded residual window is not before near onset')
                    prefix=analysis.band(uplink)[at:at+right-left]
                power=float(np.mean(prefix*prefix))
                segment_power=32768**2*10**(measurement['levels'][stage][segment]/10)
                estimate=analysis.corrected_power(segment_power,power)
                estimate.update(window=window,alignment=alignment,stage=stage,segment=segment)
                estimates[stage][segment]=estimate
            except (ValueError,KeyError,TypeError) as exc:
                estimates[stage][segment]=dict(valid=False,reason=type(exc).__name__+': '+str(exc))
    return estimates


def evaluate(folder,plan):
    """Commit each stage only after its own checks; STT observation survives level failure."""
    folder=Path(folder);start=json.loads((folder/'trial-start.json').read_text());spec=start['spec']
    row=dict(board=spec['board'],arm=spec['arm'],pair=spec['pair'],part=spec.get('part'),id=spec['id'],hit=None,levels={},folder=str(folder),
        stage_validity={s:False for s in ('raw','aec','uplink')},stt_observed=False,stt_valid=False,
        control_valid=False,unavailable={},missing_packets_word=None)
    try:
        part=json.loads((folder.parent/'result.json').read_text())
        require(part['plan_sha256']==digest_plan(plan) and restore_ok(spec['board'],part.get('restore',{})),
            'trial lacks authenticated plan and clean R7 restore')
        trial=json.loads((folder/'trial.json').read_text());require(trial['spec']==spec,'trial identity changed')
        require(trial['status']=='retained','started original has unavailable media')
        original=trial['original'];require(original==json.loads((folder/'audio/original.json').read_text()),'original changed')
        media=trial['media'];event_file=media['events.jsonl']
        require(digest(event_file['path'])==event_file['sha256'],'retained events changed')
        events=[json.loads(line) for line in Path(event_file['path']).read_text().splitlines() if line]
        require(any(e.get('kind')=='peer-binding' and e.get('pc_id')==media['pc_id'] for e in events),'media peer differs')
        require(all(e.get('pc_id') in (None,media['pc_id']) for e in events),'foreign recorder peer')
        endings=[e for e in events if e.get('kind')=='session-end']
        require(len(endings)==1,'finalized recorder required for observed STT')
        require(original['before']['pc_id']==original['after']['pc_id']==media['pc_id'],'PERF peer differs')
        near=original.get('near_playback');requested=near.get('request_monotonic_s') if near else None
        texts=smoke.text_rows([e for e in events if requested is None or e['monotonic_ns']>=requested*1e9],'user')
        row.update(transcript=texts,hit=analysis.italy_hit(texts),stt_observed=True,
            perf_before=original['before'],perf_after=original['after'])
        require(requested is not None and np.isfinite(requested),'missed near-end injection')
        pcm=measure.load_capture(original['capture'])
        italy=next(p for p in plan['fixtures'] if p['id']=='italy')
        from echoear_aec_alignment import load_nearend_source
        source=load_nearend_source(Path(italy['path']));uplink=None
        try:
            require(digest(media['uplink.wav']['path'])==media['uplink.wav']['sha256'],'retained uplink changed')
            uplink=read_pcm(media['uplink.wav']['path'])
            require(len(uplink)==endings[0]['samples']['uplink'],'decoded WAV differs from finalized recorder extent')
        except (ValueError,KeyError,OSError,EOFError,wave.Error) as exc:
            uplink=None;row['unavailable']['uplink']=str(exc)
        measurement=analysis.stage_levels(source,pcm,uplink)
        row['unavailable'].update(measurement['unavailable']);row['alignment']=measurement['alignment']
        onset=measurement['alignment']['raw']['offset_samples'];masks=analysis.segments(source)
        indices=onset+np.r_[masks['phrase'],masks['word']]
        from echoear_aec_timing import sample_interval
        with np.load(original['capture']['path'],allow_pickle=False) as data:timing=data['timing']
        anchors=json.loads(Path(original['capture']['anchors']).read_text())
        first=sample_interval(int(indices[0]),timing,anchors);last=sample_interval(int(indices[-1]),timing,anchors)
        require(first is not None and last is not None,'unbracketed near-end capture')
        row['near_host_bounds_s']=[first,last]
        starts=[e['monotonic_ns']/1e9 for e in smoke.downstream_frames(events) if e.get('frame')=='BotStartedSpeakingFrame']
        if spec['arm']=='A':
            row['reference_diagnostics']=reference_diagnostics(pcm,onset,masks)
            reference=row['reference_diagnostics']['band_mean_dbfs'];row['reference_dbfs']=reference
            require(reference<=-60 and not any(s<=last[1] for s in starts),'near-only control contaminated by bot playback/reference')
        else:
            bot=original['bot_started_ns']/1e9 if original.get('bot_started_ns') else None
            require(bot is not None and abs(requested-bot-3)<=.25,'talk-over missed declared +3 s request')
            stops=[e['monotonic_ns']/1e9 for e in smoke.downstream_frames(events) if e.get('frame')=='BotStoppedSpeakingFrame' and e['monotonic_ns']/1e9>bot]
            require(stops and bot<=first[0] and first[1]<=min(stops),'physical near onset not proven within bot speech')
        row.update(control_valid=True,stt_valid=True)
        for stage in ('raw','aec','uplink'):
            if not measurement['stage_validity'][stage]:continue
            try:
                if stage=='uplink':
                    at=measurement['alignment'][stage]['offset_samples'];witnesses={}
                    for segment,mask in masks.items():
                        witnesses[segment]=arrival_witnesses(events,at+int(mask[0]),at+int(mask[-1])+1)
                        require(all(e['sample_end']<=len(uplink) for e in witnesses[segment]),'arrival witness exceeds finalized PCM')
                    row['segment_arrivals']=witnesses;row['word_arrivals']=witnesses['word']
                row['levels'][stage]=measurement['levels'][stage];row['stage_validity'][stage]=True
            except (ValueError,KeyError,TypeError) as exc:row['unavailable'][stage]=type(exc).__name__+': '+str(exc)
        if spec['arm']=='B':
            try:
                windows=pre_onset_windows(timing,anchors,first,int(indices[0]),bot,masks)
                measurement['stage_validity']=row['stage_validity']
                row['residual_estimates']=residual_estimates(pcm,uplink,measurement,masks,windows,events)
            except (ValueError,KeyError,TypeError) as exc:
                row['residual_estimates']={stage:{segment:dict(valid=False,reason=str(exc)) for segment in masks}
                    for stage in ('aec','uplink')}
    except (ValueError,KeyError,OSError,TypeError,EOFError,wave.Error) as exc:
        row['unavailable_reason']=type(exc).__name__+': '+str(exc)
        if not row['control_valid']:row['stt_exclusion_reason']=row['unavailable_reason']
    if row['unavailable'] and 'unavailable_reason' not in row:
        row['unavailable_reason']='; '.join(stage+': '+reason for stage,reason in row['unavailable'].items())
    return row


def report(plan,root):
    validate_plan(plan);rows=[]
    for board in ELFS:
        for part in range(4):
            originals=consumed(Path(root)/board/f'part-{part}',plan,board,part)
            rows.extend(evaluate(path,plan) for path in originals.values())
    return dict(kind='step-15d-word-local-report',plan_sha256=digest_plan(plan),rules=analysis.RULES,
        rows=rows,**analysis.summarize(rows))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('plan')
    for key in ('s3-bundle','p4-bundle','bot-identity','restore-record','out'):p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--restore-record-sha256',required=True)
    p=sub.add_parser('capture')
    p.add_argument('--board',choices=ELFS,required=True);p.add_argument('--part',type=int,choices=range(4),required=True)
    for key in ('plan','out','bot-dir'):p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--bot-repo',type=Path,default=Path.home()/'src/dg-esp32')
    p.add_argument('--factory-backup',dest='p4_factory_backup',type=Path,default=smoke.FACTORY_BACKUP)
    p=sub.add_parser('report')
    for key in ('plan','root','out'):p.add_argument('--'+key,type=Path,required=True)
    args=parser.parse_args(argv)
    if args.action=='plan':
        policy.write_new(args.out,make_plan(args.s3_bundle,args.p4_bundle,args.bot_identity,args.restore_record,args.restore_record_sha256));return 0
    plan=json.loads(args.plan.read_text());validate_plan(plan)
    if args.action=='report':
        row=report(plan,args.root);policy.write_new(args.out,row);return 0 if row['complete'] else 1
    args.bundle=Path(plan['bundles'][args.board]['path']);args.bot_identity=Path(plan['bot_identity'])
    args.restore_record=Path(plan['restore_record']);args.restore_record_sha256=plan['restore_record_sha256']
    args.p4_restore_bundle=args.p4_restore_elf=None;args.reference_only=False;args.calibration=True;args.headless=True
    row=acquire(args,plan)
    return 0 if row['acquisition_passed'] else 2 if row['status']=='preflight-noisy' else 1


if __name__=='__main__':raise SystemExit(main())
