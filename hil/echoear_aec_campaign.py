"""Reviewed, declared step-9 campaign only. Import/validation never opens hardware.

The scheduler owns retry/spare decisions; the bench adapter owns one restorable
hold. Scores never enter scheduling. Every attempt has an immutable directory.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from echoear_aec_accept import (rows, control, boundary as admission_boundary, require_zero_i2s,
    command, snapshot, ROOT, BotServer, read, request, terminal, host_address,
    check_esptool, resolve_jabra, play_wav, set_volumes)
from echoear_aec_bench import (write, sha, verify_bundle, NORMAL, NORMAL_HASH,
    bench_lock, jabra_card_zero, flash, boot, load_capture, dbfs,
    start_anchored_capture, stop_anchored_capture, clock_anchor as _clock_anchor)
from echoear_aec_bench import HISTORICAL_NORMAL_9936DA45 as NORMAL, HISTORICAL_NORMAL_9936DA45_HASH as NORMAL_HASH   # gap 3: step-9 AEC family stays bound to its declared 9936da45 restore
from echoear_aec_capture import dump_capture
from echoear_aec_noise_gate import validate_noise_gate, quiet_gate, baseline_level, NoiseRetryQueue
from echoear_aec_metrics import frame_power, reference_threshold
from echoear_aec_timing import require_bracketed_capture
from echoear_aec_stimulus import schedule_from_tts, TransportEvents, StimulusTimingFailure
from echoear_aec_event_tail import EventCache, ContextIdentity, EventStreamChanged
from echoear_aec_resources import check_reply_resources, check_directory
from echoear_aec_checks import live_admission, outgoing_rtvi_count
from echoear_aec_accept_analysis import main as analyze_block
from sctp_recovery import check_active_recovery
import host_nic


def boundary(board, folder, label, *, admission_window=None):
    """Retain board radio diagnostics; opt-in host environment gates fail closed."""
    guard=getattr(board,'campaign_host_gate',None)
    if guard:
        guard.boundary(folder,label)
        if admission_window is None:guard.periodicity()
    cursor = board.cursor
    started = time.monotonic()
    status = dict(command='wifi-stats', evidence_only=True)
    try:
        command(board, 'wifi-stats', 10)
        status['completed'] = True
    except Exception as exc:
        status.update(completed=False, error=f'{type(exc).__name__}: {exc}')
    finally:
        status.update(start_s=started, end_s=time.monotonic())
        dump = '\n'.join(line for _, _, line in board.lines_since(cursor)) + '\n'
        payload = bool(re.search(r'BUFFER:|LMAC (?:TX|RX):|total_alloc.*fail_fc', dump))
        status.update(payload_present=payload, warning=None if payload else 'wifi-stats returned no driver payload (ACK-only or unavailable)')
        if not payload: print('WARNING', label, status['warning'], flush=True)
        (folder / f'{label}-wifi-stats.log').write_text(dump)
        write(folder / f'{label}-wifi-stats.json', status)
    # Sample after the diagnostic so its cost is inside the resource audit.
    if admission_window is not None:
        sent_ns=time.monotonic_ns()
        admission_window.update(stats_before_send_ns=sent_ns,end_ns=sent_ns-200_000_000,transit_allowance_ns=200_000_000)
        write(folder/(label+'-recorder-window.json'),admission_window)
    result=admission_boundary(board, folder, label)
    if guard:guard.periodicity()
    return result


def retry_admission(before,after,recorded,window):
    if window['end_ns']<window['start_ns']:raise RuntimeError('Empty retry admission interval')
    return live_admission(before,after,recorder_messages=outgoing_rtvi_count(recorded,window['start_ns'],window['end_ns']))


def check_jabra_duration(folder,case,policy):
    j=case['jabra'];error=j['elapsed_seconds']-j['expected_seconds']
    limits=policy['duration_error_bounds_s']
    outcome=dict(jabra=j,error_s=error,limits_s=limits,passed=limits['min']<=error<=limits['max'],stimulus_timer=case.get('stimulus_timer'))
    write(folder/('jabra-outcome-'+case['run_id']+'.json'),outcome)
    if not outcome['passed']:raise HarnessFailure('jabra-duration','Jabra duration outside declaration: '+json.dumps(outcome))


def finalize_analysis(out,original_error=None):
    from echoear_aec_campaign_analysis import analyze_campaign
    try:write(out/'analysis.json',analyze_campaign(out))
    except BaseException as error:
        diagnostic=dict(analysis_error=f'{type(error).__name__}: {error}',original_error=f'{type(original_error).__name__}: {original_error}' if original_error else None)
        try:write(out/'analysis-failure.json',diagnostic)
        except BaseException as recording_error:print('ANALYSIS_FAILURE',diagnostic,'recording_error',str(recording_error),flush=True)
        if original_error is None:raise
        original_error.add_note('Final analysis failed: '+diagnostic['analysis_error'])


def clock_anchor(board):return _clock_anchor(board,timeout=.25)


def capture_start(board):
    first=clock_anchor(board);time.sleep(.2);command(board,'aec-record 1',.5);return first


def capture_stop(board):
    command(board,'aec-record 0',.5);return clock_anchor(board)


class HarnessFailure(RuntimeError):
    """Only these independently witnessed acquisition failures permit a retry."""
    allowed={'rf-pre-gate','capture-deadline','evaluation-ack-timeout','jabra-duration','single-capture-gap','transport-environmental','long-tts','stimulus-timing'}
    def __init__(self,kind,message):
        if kind not in self.allowed:raise ValueError('Unknown harness failure')
        self.kind=kind
        super().__init__(message)


class EnvironmentalPause(RuntimeError):pass


class LinkProbeFailed(EnvironmentalPause):pass


def exception_class(exc):
    """Unknown failures fail closed; only identifiable host/serial failures resume."""
    import serial
    from echoear_aec_stimulus import StimulusPreconditionFailure
    if isinstance(exc,(StimulusTimingFailure,EventStreamChanged)) or (isinstance(exc,HarnessFailure) and exc.kind=='stimulus-timing'):return 'harness'
    if isinstance(exc,StimulusPreconditionFailure):return 'stimulus-precondition'
    if isinstance(exc,EnvironmentalPause):return 'environmental-pause'
    if isinstance(exc,(serial.SerialException,KeyboardInterrupt)):return 'serial-or-host'
    if isinstance(exc,TimeoutError):
        return 'serial-or-host' if str(exc).startswith('Serial timeout waiting for ') else 'device-fault'
    if isinstance(exc,OSError):return 'serial-or-host'
    return 'device-fault'


def finish_after_exception(hold,exc):
    hold.result.update(error=f'{type(exc).__name__}: {exc}',error_class=exception_class(exc),completed=False)
    if isinstance(exc,EnvironmentalPause):hold.result['environmental_pause']=str(exc)
    try:
        hold.finish()
        if hold.result.get('completed') and hold.result['error_class']=='serial-or-host':
            from echoear_aec_campaign_resume import resolve_host_interruption
            resolve_host_interruption(hold)
    except BaseException as audit_error:
        hold.result['completed']=False
        hold.result['finish_error']=f'{type(audit_error).__name__}: {audit_error}'
        if exception_class(audit_error)=='device-fault':hold.result['error_class']='device-fault'
    try:hold.save()
    except OSError as record_error:
        # Restoration must still run if the host evidence volume is unavailable.
        hold.result.setdefault('cleanup_errors',[]).append('finish-record: '+str(record_error))


def export_timeout(p,capture_seconds):
    contract=p['preflight_contract']['console_export']
    return contract['seconds_per_capture_second']*capture_seconds+contract['slack_s']


def play_fixture(path,device):
    try:return play_wav(path,device)
    except RuntimeError as exc:
        if str(exc).startswith('Jabra playback duration mismatch: '):
            raise HarnessFailure('jabra-duration',str(exc)) from exc
        raise


def check_capture_timing(blocks,anchors):
    # Validate every other timing property before classifying the single gap.
    clean=blocks.copy();clean[:,4]=0
    require_bracketed_capture(clean,anchors)
    gaps=sum(bool(b[4]) for b in blocks)
    if gaps==1:raise HarnessFailure('single-capture-gap','One marked capture gap block')
    require_bracketed_capture(blocks,anchors)


def observer_ok(events):
    health=[e for e in events if e['event']=='rtp-observer-health']
    return bool(health) and all(e['passed'] and e['sctp']['supported'] and e['sctp']['flight_fix_enabled'] and e['sctp']['records']>0 and not e['sctp']['truncated'] and not e['sctp']['unavailable'] for e in health)


def replaceable(opportunity):
    return (opportunity in ('no-opportunity', 'not-delivered-no-active-tts') or
            opportunity.startswith('unscorable-'))


def capture_groups(cases):
    groups=[]
    for c in cases:
        key=c.get('capture_group',c['id'])
        if groups and groups[-1]['run_id']==key:groups[-1]['cells'].append(c)
        else:groups.append(dict(run_id=key,cells=[c]))
    return groups


class Schedule:
    """Independent noise/opportunity replacement only; no score argument exists."""
    def __init__(self,cases,spares,policy,prior=None, *, lull_policy=None):
        from echoear_aec_lull import eligible
        eligible(None,lull_policy)
        self.lull_policy=lull_policy
        self.queue=NoiseRetryQueue(capture_groups(cases),policy)
        self.spares={}
        for c in spares:self.spares.setdefault((c['volume_db'],c['kind'],c.get('voice_class','firm')),deque()).append(c)
        self.journal=[];self.unresolved=[]
        if prior:
            groups={g['run_id']:g for g in capture_groups(cases+spares)}
            if prior.get('inflight') or prior['unresolved'] or prior['exhausted']:
                raise ValueError('Prior schedule has a hard failure or exhausted budget')
            replay=Schedule(cases,spares,policy,lull_policy=lull_policy)
            for record in prior['journal']:
                if replay.queue.paused:
                    replay.queue.paused=False;replay.queue.noisy_cells.clear()
                item=replay.next()
                if item is None or (item[0]['run_id'],item[1])!=(record['id'],record['attempt']):
                    raise ValueError('Resume journal attempt/order mismatch')
                replay.finish(*item,record.get('noise_gate_passed',record.get('noise')),record['classifications'],record.get('harness_failure'))
                if replay.journal[-1]['replacements']!=record['replacements']:
                    raise ValueError('Resume spare order mismatch')
            if replay.state()['pending']!=prior['pending']:
                raise ValueError('Resume pending attempts do not match lifetime journal')
            self.journal=copy.deepcopy(prior['journal'])
            used={r['spare'] for j in self.journal for r in j['replacements']}
            for key,pool in self.spares.items():self.spares[key]=deque(c for c in pool if c['id'] not in used)
            self.queue=NoiseRetryQueue([dict(groups[r['id']],start_attempt=r['attempt']) for r in prior['pending']],policy)
            # Explicit new environmental hold: reset the streak, retain attempts.
        self.inflight=None

    def next(self):
        item=self.queue.next()
        self.inflight=dict(id=item[0]['run_id'],attempt=item[1]) if item else None
        return item

    def finish(self,group,attempt,noise,classes,failure=None):
        from echoear_aec_lull import eligible
        if failure and failure.get('kind')=='long-tts':
            from echoear_aec_v85_tts import retry
            action=retry(self.queue,group,attempt)
        else:action=self.queue.finish(group,attempt,noise)
        record=dict(id=group['run_id'],attempt=attempt,noise_gate_passed=noise,action=action,
                    classifications=classes,replacements=[],harness_failure=failure)
        self.inflight=None
        if noise is True:
            for c in group['cells']:
                opportunity=classes.get(c['id'],'unscorable-missing-witness')
                if c.get('expected_class')!='eligible' or c.get('report_only'):continue
                if replaceable(opportunity):
                    pool=self.spares.get((c['volume_db'],c['kind'],c.get('voice_class','firm')),deque())
                    if pool:
                        spare=pool.popleft()
                        self.queue.pending.append((dict(run_id=spare['id'],cells=[spare]),1))
                        record['replacements'].append(dict(original=c['id'],spare=spare['id'],reason=opportunity))
                    else:self.unresolved.append(c['id'])
                elif not eligible(opportunity,self.lull_policy):self.unresolved.append(c['id'])
        elif noise is None:self.unresolved.extend(c['id'] for c in group['cells'])
        self.journal.append(record)
        return action

    def state(self):
        return dict(journal=self.journal,inflight=self.inflight,paused=self.queue.paused,unresolved=self.unresolved,
                    exhausted=self.queue.exhausted,noisy_streak=self.queue.noisy_cells,
                    pending=[dict(id=g['run_id'],attempt=a) for g,a in self.queue.pending])


def validate_spec(p):
    if p.get('v85'):
        from echoear_aec_v85 import validate
        return validate(p)
    from echoear_aec_lull import declared_rule
    declared_rule(p)
    from echoear_aec_transport_policy import validate_policy
    validate_policy(p.get('gates',{}).get('transport',{}))
    from echoear_aec_playback_template import declared_corner
    declared_corner(p)
    validate_noise_gate(p['noise_gate'])
    if p.get('link_probe'):
        probe=p['link_probe']
        if probe['duration_s']!=240 or probe['uplink_loss_max_fraction']!=.005 or probe['reject_interarrival_gap_s_gte']!=1:raise ValueError('Link probe thresholds changed')
        if probe.get('epoch_transport_rule') not in (None,'probe-own-rule-v1'):raise ValueError('Unknown probe epoch rule')
    env=p.get('failure_policy',{}).get('transport_environmental')
    if env and env.get('device_alive_rule') not in (None,'f1b-v1'):raise ValueError('Unknown device-alive rule')
    if p.get('host_environment'):
        from echoear_aec_host_gate import POLICY,CADENCE
        if {k:v for k,v in p['host_environment'].items() if k!='connection'}!=POLICY or not p['host_environment'].get('connection'):raise ValueError('Host environment policy changed')
        if p.get('first_hold_periodicity')!=CADENCE:raise ValueError('Periodicity policy changed')
    if env and (env['max_per_hold'],env['max_per_campaign'],env['clean_recovery_min_s'])!=(3,6,5):raise ValueError('Environmental caps/recovery changed')
    if p['boot_ref_gain_db']!=15 or p['gain_db']!=15:raise ValueError('Candidate gain must be15')
    if [(s['id'],s['volume_db']) for s in p['sessions']]!=[('qualification-01',-6),('qualification-normal',-6),('qualification-02',0),('soak-01',-6)]:raise ValueError('Hold order changed')
    if p['capture_cap_seconds']!=28:raise ValueError('28s capture cap required')
    if p['gated_volume_db']!=-6:raise ValueError('0dB requires separately reviewed appended qualification')
    ids=[c['id'] for key in ('cases','spares','controls','reconnects') for c in p[key]]
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate declared IDs')
    for volume,voice,n in ((-6,'firm',10),(-6,'normal',10),(0,'firm',5)):
        for kind in ('stop','sentence'):
            count=sum(c['volume_db']==volume and c.get('voice_class','firm')==voice and c['kind']==kind and c.get('expected_class')=='eligible' for c in p['cases'])
            spare=sum(c['volume_db']==volume and c.get('voice_class','firm')==voice and c['kind']==kind for c in p['spares'])
            expected_spares=(7 if kind=='stop' else 6) if volume==-6 else 2
            if (count,spare)!=(n,expected_spares):raise ValueError('Minimal denominator or spare budget changed')
    for volume in (-6,0):
        echo=[c for c in p['cases'] if c['volume_db']==volume and c['kind']=='echo']
        if len(echo)!=10 or len(capture_groups(echo))!=8:raise ValueError('Need10 echo/8 captures per volume')
    for c in p['cases']+p['spares']+p['controls']:
        if c.get('voice_class','firm') not in ('firm','normal') or (c.get('voice_class')=='normal' and (c['volume_db']!=-6 or c['session_id']!='qualification-normal')):raise ValueError('Invalid normal-voice block')
        if c['guard_ms']!=400 or c['volume_db'] not in (-6,0):raise ValueError('Guard/volume changed')
        if c['stop_offset_s'] is not None and not 0<=c['stop_offset_s']<=3:raise ValueError('Invalid offset')
    if sum(c.get('expected_class')=='report-actual-guard-overlap' for c in p['cases'])!=2:raise ValueError('Two in-window probes required')
    kinds=[c['kind'] for c in p['controls']]
    if any(kinds.count(k)!=n for k,n in [('probe',2),('near',4),('silent-cancel',2)]):raise ValueError('Control counts changed')
    if len(p['reconnects'])!=3:raise ValueError('Three reconnects required')
    soak=next(b for b in p['blocks'] if b['id']=='soak')
    if soak['duration_s']!=600 or len(soak['prompts'])!=10 or soak['volume_db']!=-6:raise ValueError('Soak changed')
    if any(c['volume_db']!=-6 for c in soak['prompts']):raise ValueError('Soak volume changed')
    from echoear_aec_campaign_budget import validate_budget
    validate_budget(p)
    return True


def validate_campaign(p):
    if p.get('status')!='declared' or p.get('qualification') is not True:
        raise ValueError('Draft specification: acquisition is not authorized')
    if p.get('campaign_authorized') is not True:
        raise ValueError('Reviewed frozen declaration and coordinator campaign authorization required')
    validate_spec(p)
    template=ROOT/('hil/plans/echoear-aec-accept-v8.5.json' if p.get('v85') else 'hil/plans/echoear-aec-accept-v1.json')
    if p.get('reviewed_spec_sha256')!=sha(template):raise ValueError('Reviewed spec hash missing/mismatched')
    frozen=json.loads(template.read_text())
    validate_review_preflight(p,frozen)
    if p['candidate'].get('playback_config')!=frozen['candidate'].get('playback_config'):raise ValueError('Frozen playback config changed')
    for key in ('gates','noise_gate','cases','spares','controls','reconnects','sessions','blocks','bot_config','open_near_end_level','fixture_policy','preflight_contract','review_of_record','failure_policy','rf','source_preflight_contract','diagnostics','time_estimate','link_probe','host_environment','first_hold_periodicity','ongoing_reply_lull','v85'):
        actual,expected=p.get(key),frozen.get(key)
        if key=='host_environment' and actual and expected:
            actual={k:v for k,v in actual.items() if k!='connection'}
            expected={k:v for k,v in expected.items() if k!='connection'}
        if actual!=expected:raise ValueError('Reviewed contract changed: '+key)
    for key in ('firmware_commit','bot_commit','runner_commit'):
        if not re.fullmatch('[0-9a-f]{40}',p['candidate'].get(key) or ''):raise ValueError('Unfrozen '+key)
    if p['candidate']['bot_commit']!=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip():raise ValueError('Paired bot checkout mismatch')
    manifest=p['candidate'].get('code_sha256',{})
    required={str(f.relative_to(ROOT)) for pattern in ('hil/echoear_aec_*.py','hil/host_nic.py','hil/build_echoear_aec_candidate.py','hil/sctp_recovery.py','server/*.py') for f in ROOT.glob(pattern)}
    if p.get('host_environment'):
        required.update(str(f.relative_to(ROOT)) for f in ROOT.glob('hil/*.py'))
        required.update({'server/uv.lock','server/pyproject.toml'})
    if not required.issubset(manifest):raise ValueError('Incomplete runner/bot/analyzer manifest')
    for name,h in manifest.items():
        if sha(ROOT/name)!=h:raise ValueError('Frozen code mismatch: '+name)
    for f in p['fixture_policy']['final_calibrated_paths_and_hashes'].values():
        if sha(f['path'])!=f['sha256']:raise ValueError('Fixture hash mismatch')
    from echoear_aec_fixture_scale import verify_scaled
    for name,f in p['fixture_policy']['final_calibrated_paths_and_hashes'].items():
        if name.endswith('-normal'):
            if f['gain_db']!=(-10 if name=='stop-normal' else -6):raise ValueError('Wrong normal-voice attenuation')
            verify_scaled(f)
    measurement=p['open_near_end_level']['real_talker_measurement']
    if sha(measurement['path'])!=measurement['sha256'] or json.loads(Path(measurement['path']).read_text())!=measurement['data']:raise ValueError('Real-talker measurement mismatch')
    for c in p['cases']+p['spares']+p['controls']:
        if c.get('fixture') and sha(c['fixture'])!=c['fixture_sha256']:raise ValueError('Cell fixture mismatch')
    witness=p.get('source_preflight',{})
    if not witness.get('passed') or witness.get('continuous_active_seconds',0)<4:raise ValueError('Missing >=4s continuous source preflight')
    from echoear_aec_source_preflight import validate_source
    validate_timing_preflight(p,validate_source(p,witness))
    from tts_connection_probe import supported, ProactiveConnectionTTS, SOURCE_HASHES
    import pipecat
    if not supported(ProactiveConnectionTTS,pipecat.__version__,SOURCE_HASHES):raise ValueError('Unsupported pinned Flux private API')
    if p['candidate'].get('elf_sha256')!=p['elf_sha256'] or sha(Path(p['image'])/'manifest.json')!=p['candidate'].get('image_bundle_sha256'):raise ValueError('Candidate bundle not bound to declaration')
    verify_bundle(Path(p['image']),p['elf_sha256'],p['boot_ref_gain_db']);verify_bundle(NORMAL,NORMAL_HASH)


def validate_timing_preflight(p,preflight):
    if preflight.get('timing_contract')!=p['preflight_contract']:
        raise ValueError('Preflight timing/reply bound missing or changed')


def validate_review_preflight(p,frozen):
    review=frozen['review_of_record']
    if p.get('review_approval')!=review or review['reviewer']!='Fable' or sha(review['path'])!=review['sha256']:
        raise ValueError('Fable review approval/hash required')
    decisions=review.get('coordinator_decisions',{})
    if not decisions.get('path') or sha(decisions['path'])!=decisions.get('sha256'):
        raise ValueError('Coordinator decisions hash required')
    for item in review.get('supporting_reviews_and_decisions',[]):
        if sha(item['path'])!=item['sha256']:raise ValueError('Supporting review/decisions hash required')
    evidence=review.get('campaign_02_evidence')
    if evidence:
        if sha(evidence['path'])!=evidence['sha256']:raise ValueError('Campaign-02 manifest mismatch')
        manifest=json.loads(Path(evidence['path']).read_text())
        actual={str(f.relative_to(Path(manifest['root']))):sha(f) for f in Path(manifest['root']).rglob('*') if f.is_file()}
        if actual!=manifest['files_sha256']:raise ValueError('Campaign-02 evidence mismatch')
        if sha(manifest['launch_log']['path'])!=manifest['launch_log']['sha256']:raise ValueError('Campaign-02 launch log mismatch')
    real=p['preflight_contract']['real_reply']
    if sha(ROOT/real['prompt_file'])!=real['prompt_file_sha256']:
        raise ValueError('Real-reply prompt changed; measured bound needs new preflight')


def protocol(cells,reconnects=()):
    scripts=[];real=[];listen=[];host_interrupts={}
    for c in cells:
        for a in range(1,4):
            cid=f"{c['id']}-a{a}";scripts.append(dict(id=cid,text=c.get('text','')))
            if c['kind']=='host-interrupt':
                from echoear_aec_host_interrupt import validate
                validate(c)
                host_interrupts[cid]=dict(next_text=c['next_text'],next_delay_s=c['next_delay_s'])
            if c.get('real_bot_reply'):real.append(cid)
            if c['kind'] in ('near','silent-cancel','soak-near'):listen.append(cid)
    for c in reconnects:
        for a in range(1,4):scripts.append(dict(id=f"{c['id']}-a{a}-old",text=c['text']))
    return dict(dispatch='declared-once',echo_replies=scripts,barge_ids=[],barge_bot_text='unused',real_bot_ids=real,listen_ids=listen,interrupt_ids=list(host_interrupts),host_interrupts=host_interrupts)


class BenchHold:
    """One lock, one candidate boot, one cleanup path, independent of verdict."""
    rf_pre_gate_seconds = 30
    require_clean_cleanup = False

    def export_capture(self, path, *, timeout=150):
        return dump_capture(self.board, path, timeout=timeout)

    def __init__(self,p,s,out):
        self.p,self.s,self.out=p,s,Path(out);self.board=self.bot=None;self.tokens=[];self.number=0
        self.result=dict(completed=False,cases=[],reconnects=[],cleanup_errors=[])

    def save(self):write(self.out/'result.json',self.result)

    def __enter__(self):
        self.out.mkdir(exist_ok=False)
        self.lock=bench_lock();self.lock.__enter__()
        try:
            if self.p.get('host_environment'):
                from echoear_aec_host_gate import HostGate
                self.host_guard=HostGate(self);self.host_guard.start()
            # Gate before any serial, mixer mutation or flash; retain all counters.
            a=host_nic.snapshot();time.sleep(self.rf_pre_gate_seconds);b=host_nic.snapshot()
            gate=host_nic.gate(a,b);write(self.out/'rf-pre-gate.json',gate)
            if not gate['passed']:raise RuntimeError('RF pre-gate failed')
            _,self.device=resolve_jabra();jabra_card_zero()
            subprocess.run(['alsactl','-f',str(self.out/'mixer-before.state'),'store','0'],check=True,capture_output=True)
            set_volumes('0',self.out/'mixer-qualified.log')
            self.touched=True
            flash(Path(self.p['image']),self.p['elf_sha256'],self.out,'candidate',self.p['boot_ref_gain_db'])
            self.board=boot(self.out,'candidate',self.p['elf_sha256'])
            if hasattr(self,'host_guard'):
                self.board.campaign_host_gate=self.host_guard
                self.host_guard.cursor=self.board.cursor
            self.board.wait_for(r'AEC_GUARD_CONFIG guard_ms=400 afe_delay_samples=1024 rearm_ms=2000',5,since=0)
            for c in ('demo hold','audio 0','quiet 1','gate auto',f"aec-ref-gain {self.p['gain_db']}",'aec-guard-ms 400',f"playback-volume {self.s['volume_db']}",'peer-option arrival-trace 0'):
                command(self.board,c,40)
            command(self.board,'disp init',40);heap=snapshot(self.board,self.out,'display-init')['heap']['psram']
            command(self.board,f"disp arena 491520 {heap['free']} 1287760 {heap['largest']}")
            for i,asset in enumerate(re.findall(r'"([0-9a-f]{64})"',(ROOT/'main/demo_assets.h').read_text())):
                rid=f'cat-{i}';request(self.board,'preload',{'hash':asset},rid)
                if terminal(self.board,rid)['error']!='none':raise RuntimeError('Character preload failed')
                command(self.board,f'disp asset {i} {asset}')
            command(self.board,'disp character on',30);write(self.out/'character-start.json',read(self.board,'character-status','DISPLAY_CHARACTER'))
            self.baseline_capture()
            self.evaluation=self.out/'evaluation';self.evaluation.mkdir()
            self.identity=dict(host_boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip())
            write(self.out/'identity.json',self.identity);write(self.out/'declaration.json',self.p)
            return self
        except BaseException:
            self.__exit__(*__import__('sys').exc_info());raise

    def baseline_capture(self):
        policy=self.p['noise_gate']
        for a in range(1,4):
            folder=self.out/f'baseline-{a}';folder.mkdir();time.sleep(5)
            if hasattr(self,'host_guard'):self.host_guard.boundary(folder,'before')
            command(self.board,'aec-record 1');time.sleep(6.1);command(self.board,'aec-record 0');dump_capture(self.board,folder/'capture.npz')
            if hasattr(self,'host_guard'):self.host_guard.boundary(folder,'after')
            raw,afe,ref,blocks=load_capture(folder/'capture.npz');self.baseline=baseline_level(raw,policy)
            gate=quiet_gate(raw,self.baseline,policy,mic3=ref);gate['passed'] &= self.baseline<=-53 and not blocks[:,4].sum()
            write(folder/'noise-gate.json',gate)
            if gate['passed']:
                import shutil
                shutil.copy2(folder/'capture.npz',self.out/'quiet.npz');self.qref=ref;return
        raise RuntimeError('Environmental pause: no clean settled baseline')

    def connect(self,cells,reconnects):
        write(self.evaluation/'protocol.json',protocol(cells,reconnects))
        os.environ.pop('ECHOEAR_AEC_FULL_DUPLEX',None)
        # Declared profile wins over ambient shell state; historical plans are demo.
        from demo_policy import PROFILE_ENV, profile_name
        os.environ[PROFILE_ENV]=profile_name(self.p['bot_config'].get(PROFILE_ENV))
        for key,value in self.p['bot_config'].items():
            if key.isupper() and value is not None:os.environ[key]=str(value)
        retention=self.p.get('time_estimate',{}).get('v85_budget',{}).get('rtp_evidence_max_packets')
        os.environ.update(ECHOEAR_AEC_EVALUATION_DIR=str(self.evaluation),ECHOEAR_AEC_SESSION_DIR=str(self.out/'sessions'),RTP_EVIDENCE_DIR=str(self.out/'rtp'),RTP_EVIDENCE_MAX_PACKETS=str(retention or self.p.get('time_estimate',{}).get('v831_budget',{}).get('sessions',{}).get(self.s['id'],{}).get('rtp_evidence_max_packets',200000)),OFFLINE_SKIP_GREETING='1')
        self.event_cache=EventCache([self.evaluation/'events.jsonl'])
        self.recorded_cache=EventCache(self.paths)
        self.bot=BotServer(self.out/'bot',entrypoint='demo_bot.py');self.bot.start()
        command(self.board,'server-url http://'+host_address()+':7861');at,_=command(self.board,'connect',90);self.board.wait_for('STATE Ready',20,since=at)
        if 'SCTP_FLIGHT_FIX enabled aiortc=1.15.0' not in (self.out/'bot/server-1.log').read_text():raise RuntimeError('SCTP patch not enabled')
        command(self.board,'audio 1');time.sleep(7)
        self.connected=snapshot(self.board,self.out,'connected');require_zero_i2s([self.connected])
        if hasattr(self,'host_guard'):self.host_guard.boundary(self.out,'connected');self.host_guard.periodicity()

    def link_probe(self):
        from echoear_aec_transport_environment import link_probe,observer_healthy
        folder=self.out/'link-probe';folder.mkdir()
        before,pre=boundary(self.board,folder,'before');started=time.monotonic()
        # Bounded sleep steps permit signal-driven restoration; no stimulus.
        deadline=started+self.p['link_probe']['duration_s']
        while time.monotonic()<deadline:time.sleep(max(0,min(1,deadline-time.monotonic())))
        ended=time.monotonic();self.send('health',None)
        after,post=boundary(self.board,folder,'after');require_zero_i2s(pre+post)
        value=link_probe(rows(self.out/'rtp/socket-rtp.jsonl'),before['pc_id'],started,ended)
        own=[e for e in self.events() if started<=e['t']<=time.monotonic()]
        value['observer_passed']=observer_healthy(own)
        value['idle']=not any(e['event'] in ('script-request','real-bot-llm-request','tts-generation-metric-start') for e in own)
        value['resources']=check_reply_resources(before,after,intermediate=pre+post)
        value['passed'] &= value['observer_passed'] and value['idle'] and value['resources']['all_passed']
        value['result']='LINK PROBE PASSED' if value['passed'] else 'LINK PROBE FAILED, retry later'
        write(folder/'result.json',value);self.result['link_probe']=value;self.save()
        print(value['result'],flush=True)
        if not value['resources']['all_passed'] or not value['observer_passed'] or not value['idle']:raise RuntimeError('Link probe device/observer/idle failure')
        if not value['passed']:raise LinkProbeFailed(value['result'])

    def events(self,case_id=None):
        if not hasattr(self,'event_cache'):self.event_cache=EventCache([self.evaluation/'events.jsonl'])
        return self.event_cache.read(case_id)
    def paths(self):return list((self.out/'sessions').glob('*/events.jsonl'))
    def recorded(self):
        if not hasattr(self,'recorded_cache'):self.recorded_cache=EventCache(self.paths)
        return self.recorded_cache.read()

    def send(self,op,cid,*,timeout_s=10,**kw):
        self.number+=1;started=time.monotonic()
        control(self.evaluation/'command.json',dict(number=self.number,op=op,case_id=cid,**kw))
        while time.monotonic()<started+timeout_s:
            if any(e.get('number')==self.number and e['t']>=started for e in self.events()):return
            time.sleep(.01)
        raise HarnessFailure('evaluation-ack-timeout','Evaluation command not acknowledged')

    def capture(self,group,attempt,*,scheduled_s=None,resource_tick=None):
        folder=self.out/f"{group['run_id']}-a{attempt}";folder.mkdir()
        window_start=time.monotonic()
        before,pre=boundary(self.board,folder,'before');nic=host_nic.snapshot()
        self.attempt_before=before
        self.attempt_pre=pre
        self.attempt_started_ns=time.monotonic_ns()
        self.attempt_cases=[]
        self.attempt_command_number=self.number
        command(self.board,f"playback-volume {group['cells'][0]['volume_db']}")
        if scheduled_s is None:time.sleep(5)
        else:
            # Use the existing between-request idle interval, not capture time.
            nic=getattr(self,'soak_nic',nic)
        pre_gate=host_nic.gate(nic,host_nic.snapshot());write(folder/'rf-pre-gate.json',pre_gate)
        if not pre_gate['passed']:raise HarnessFailure('rf-pre-gate','Per-cell RF pre-gate failed')
        nic=host_nic.snapshot()
        began=time.monotonic();anchors=[capture_start(self.board)]
        deadline=began+24 # reserve 4s for stop/anchor within the 28s hard cap
        futures=[];outcomes=[];capture_polls=[];pool=ThreadPoolExecutor(max_workers=2)
        try:
            time.sleep(2.3)
            for index,cell in enumerate(group['cells']):
                c=copy.deepcopy(cell);cid=f"{c['id']}-a{attempt}";c.update(run_id=cid,capture_dir=folder.name,attempt=attempt,start_ns=time.monotonic_ns())
                if scheduled_s is not None:
                    while time.monotonic()<scheduled_s:anchors.append(clock_anchor(self.board));time.sleep(.01)
                    c['request_lateness_s']=time.monotonic()-scheduled_s
                    if c['request_lateness_s']>.1:raise RuntimeError('Soak request deadline missed; no shifting')
                token=threading.Event();self.tokens.append(token)
                if c['stop_offset_s'] is not None:
                    reader=TransportEvents(self.paths,c['start_ns'],at_end=True)
                    identity=ContextIdentity([self.evaluation/'events.jsonl'],cid)
                    future=pool.submit(schedule_from_tts,self.paths,c['start_ns'],c['stop_offset_s'],lambda path=Path(c['fixture']):play_fixture(path,self.device),cancel=token,timeout_s=max(.1,deadline-time.monotonic()-1),require_active=True,active_max_age_s=.12,reader=reader,identity=identity)
                    futures.append((c,future,token))
                if c['kind']=='host-interrupt':
                    host_reader=TransportEvents(self.paths,c['start_ns'],at_end=True)
                    host_identity=ContextIdentity([self.evaluation/'events.jsonl'],cid)
                self.attempt_cases.append(cid)
                remaining=lambda:max(0,min(10,deadline-time.monotonic()))
                if c['kind'] in ('near','silent-cancel','soak-near'):
                    self.send('begin',cid,timeout_s=remaining());future=pool.submit(play_fixture,Path(c['fixture']),self.device);futures.append((c,future,token))
                else:self.send('speak',cid,timeout_s=remaining())
                if c['kind']=='host-interrupt':
                    from echoear_aec_host_interrupt import issue
                    # speak was acknowledged; reserve the next command exclusively
                    # until the timed interruption has completed. seal follows it.
                    self.number+=1
                    number=self.number
                    future=pool.submit(schedule_from_tts,self.paths,c['start_ns'],c['host_interrupt_offset_s'],
                        lambda number=number,cid=cid:issue(self.evaluation/'command.json',number,cid),
                        cancel=token,timeout_s=max(.1,deadline-time.monotonic()-1),require_active=True,
                        active_max_age_s=.12,reader=host_reader,identity=host_identity)
                    futures.append((c,future,token))
                c['requested_s']=time.monotonic();outcomes.append(c)
                # Keep real bot next replies inside the same case. No scripted
                # follow-up and no +7s or cleanup+300ms request masking.
                final=index==len(group['cells'])-1
                last_stop=None
                while time.monotonic()<deadline:
                    anchors.append(clock_anchor(self.board))
                    if resource_tick:resource_tick()
                    own=self.events(cid)
                    capture_polls.append(dict(case_id=cid,**self.event_cache.tail.last_poll))
                    for timed_case,timed_future,_ in futures:
                        if (timed_case['stop_offset_s'] is not None or timed_case['kind']=='host-interrupt') and timed_future.done():
                            timing=timed_future.result()
                            if timing.get('harness_failure'):
                                raise HarnessFailure('stimulus-timing','Timed stimulus exceeded 100 ms invocation error')
                    # Alarms may be case-less. Check the same indexed snapshot
                    # without a second poll or treating global frames as case timing.
                    unscoped=self.event_cache.case_rows(None)
                    if any(e.get('frame') in ('ErrorFrame','FatalErrorFrame') or e['event'] in ('evaluation-error','liveness-alarm','tts-reconnect-refused','tts-reconnect-failed') for group_rows in (own,unscoped) for e in group_rows):raise RuntimeError('Active bot/transport error')
                    stops=[e['t'] for e in own if e.get('frame')=='BotStoppedSpeakingFrame']
                    starts=[e['t'] for e in own if e.get('frame')=='BotStartedSpeakingFrame']
                    requests=[e['t'] for e in own if e['event']=='real-bot-llm-request']
                    if stops and starts and max(stops)>max(starts):last_stop=max(stops)
                    else:last_stop=None
                    # For barge-in, do not confuse end of cancelled old audio with
                    # completion of the real response still in LLM/TTS.
                    if c.get('real_bot_reply') and c['kind'] in ('stop','sentence'):
                        accepted=any(e.get('frame')=='InterruptionFrame' for e in own)
                        if (accepted or requests) and (not requests or not starts or max(starts)<max(requests)):last_stop=None
                    if c['kind']=='near':
                        f=next(f for r,f,t in futures if r is c)
                        if f.done() and time.monotonic()>=c['requested_s']+6:last_stop=c['requested_s']+4
                    if c['kind']=='host-interrupt':
                        next_requests=[e['t'] for e in own if e['event']=='host-next-reply-queued']
                        if not next_requests or not starts or max(starts)<max(next_requests):last_stop=None
                    gap=2.3 if final else group['cells'][index+1]['quick_gap_s']
                    if last_stop and time.monotonic()>=last_stop+gap:break
                    time.sleep(.035)
                else:raise HarnessFailure('capture-deadline','Capture deadline; incomplete reply/post-roll retained')
                c['end_ns']=time.monotonic_ns()
                if c['kind']!='probe':self.send('seal',cid,timeout_s=remaining())
        finally:
            for c,f,token in futures:token.set()
            # Stop recording before joining workers (including slow ALSA).
            anchors.append(capture_stop(self.board))
            capture_stopped=time.monotonic()
            if self.p.get('v85'):
                score_end=snapshot(self.board,folder,'capture-end')
                write(folder/'capture-end-window.json',dict(request_lower_s=capture_stopped,receipt_upper_s=time.monotonic()))
            capture_end=time.monotonic()
            write(folder/'attempt-window.json',dict(window_s=[window_start,None],capture_end_s=capture_end,after_boundary_s=None))
            write(folder/'anchors.json',anchors)
            elapsed=time.monotonic()-began
            write(folder/'capture-duration.json',dict(elapsed_s=elapsed,limit_s=28))
            pool.shutdown(wait=True,cancel_futures=True)
            write(folder/'capture-polls.json',capture_polls)
            for timed_case,timed_future,_ in futures:
                if (timed_case['stop_offset_s'] is not None or timed_case['kind']=='host-interrupt') and not timed_future.cancelled():
                    try:timing=timed_future.result()
                    except Exception as error:timing=dict(error=type(error).__name__+': '+str(error))
                    write(folder/('timer-'+timed_case['run_id']+'.json'),timing)
            if resource_tick:resource_tick(force=True)
            self.export_capture(folder/'capture.npz',timeout=export_timeout(self.p,elapsed) if scheduled_s is not None else 150)
            if resource_tick:resource_tick(force=True)
        if time.monotonic()<began:raise RuntimeError('Clock regressed')
        for c,f,token in futures:
            value=f.result()
            if c['kind']=='host-interrupt':c.update(host_interrupt_timer={**{k:v for k,v in value.items() if k!='jabra'},'command':value.get('jabra')})
            elif c['stop_offset_s'] is not None:c.update(stimulus_timer=value,jabra=value.get('jabra'),stimulus_skipped=value.get('skipped',False))
            elif c['kind']!='probe':c['jabra']=value
            if c.get('stimulus_timer',{}).get('harness_failure') or c.get('host_interrupt_timer',{}).get('harness_failure'):
                raise HarnessFailure('stimulus-timing','Timed stimulus exceeded 100 ms invocation error')
            if c.get('jabra'):
                j=c['jabra'];c['jabra_duration_error_s']=j['elapsed_seconds']-j['expected_seconds']
                check_jabra_duration(folder,c,self.p['fixture_policy'])
            self.tokens.remove(token)
        raw,afe,ref,blocks=load_capture(folder/'capture.npz');check_capture_timing(blocks,anchors)
        gates={name:quiet_gate(raw[sl],self.baseline,self.p['noise_gate'],mic3=ref[sl]) for name,sl in [('pre',slice(0,32000)),('post',slice(-32000,None))]}
        gates['board_silent']=all(not (frame_power(x)>=reference_threshold(self.qref)).any() for x in (ref[:32000],ref[-32000:]))
        valid=bool(gates['board_silent'] and not blocks[:,4].sum())
        noise=bool(all(gates[k]['passed'] for k in ('pre','post')))
        write(folder/'noise-gate.json',gates)
        if not noise:write(folder/'noise-contaminated.json',dict(excluded=True,attempt=attempt,gate=gates))
        after,post=boundary(self.board,folder,'after')
        after_boundary=time.monotonic()
        timing=dict(window_s=[window_start,after_boundary],capture_end_s=capture_end,after_boundary_s=after_boundary)
        write(folder/'attempt-window.json',timing)
        observed=pre+([score_end] if self.p.get('v85') else [])+post
        require_zero_i2s(observed)
        resources=check_reply_resources(before,after,intermediate=observed)
        from echoear_aec_campaign_analysis import delivery_counters, transport
        scored_after=score_end if self.p.get('v85') else after
        delivery=delivery_counters(before,scored_after)
        transport_check=transport(before,scored_after,self.p.get('gates',{}).get('transport',{}).get('rule'))
        rf=host_nic.gate(nic,host_nic.snapshot());write(folder/'rf.json',rf)
        admission=live_admission(before,after,recorder_messages=outgoing_rtvi_count(self.recorded(),outcomes[0]['start_ns'],outcomes[-1]['end_ns']))
        for c in outcomes:
            c.update(noise_gate_passed=noise,quiet_windows_valid=valid,resources=resources,admission=admission,transport=transport_check,events=[e for e in self.events() if e.get('case_id')==c['run_id']])
        checks=dict(resources=resources,admission=admission,rf=rf,delivery=delivery,transport=transport_check)
        if self.p.get('v85'):
            export=dict(transport=transport(score_end,after,self.p['gates']['transport']['rule']),
                        delivery=delivery_counters(score_end,after),report_only=True,
                        window_device_us=[score_end['device_us'],after['device_us']])
            write(folder/'export-event.json',export)
            # Resources/admission/observer/liveness remain hard over the entire
            # attempt. Export-only transport/delivery counters are reported.
            checks['capture_end_present']=dict(passed=True)
        window=timing['window_s']
        write(folder/'result.json',dict(cases=outcomes,checks=checks))
        if not resources['all_passed'] or not all(c['passed'] for c in (admission,rf,delivery,transport_check)):
            if self.p.get('v85'):
                self.result['hard_failure']=dict(capture_dir=folder.name,checks=checks);self.save()
                raise RuntimeError('Capture-interval or integrity failure; v8.5 has no link retake')
            from echoear_aec_transport_environment import classify
            witness=classify(before,after,checks,pre_gate,self.events(),
                rows(self.out/'rtp/downlink.jsonl'),rows(self.out/'rtp/sctp.jsonl'),
                rows(self.out/'rtp/socket-rtp.jsonl'),window,outcomes,timing,
                device_alive_rule=self.p['failure_policy']['transport_environmental'].get('device_alive_rule'),
                recorded=self.recorded())
            write(folder/'transport-environmental-witness.json',witness)
            if witness['passed']:
                exc=HarnessFailure('transport-environmental','All declared transport-environmental witnesses passed')
                exc.witnesses=witness
                raise exc
            self.result['hard_failure']=dict(capture_dir=folder.name,checks=checks)
            self.save()
            raise RuntimeError('Resource/admission/RF/delivery/transport failure')
        if not valid:raise RuntimeError('Invalid capture/quiet windows; acquisition failure')
        for c in outcomes:
            if c['kind']=='probe':continue
            if not observer_ok(c['events']):raise RuntimeError('Observer health failure')
        write(folder/'result.json',dict(cases=outcomes,checks=checks))
        if self.p.get('v85'):
            from echoear_aec_v85_tts import inspect
            vendor=inspect(self.events(),self.recorded(),self.attempt_cases)
            write(folder/'tts-generations.json',vendor)
            if vendor['long_generations']:
                if not vendor['complete']:raise RuntimeError('Incomplete long-TTS generation witnesses')
                raise HarnessFailure('long-tts','Complete long-TTS generation requires whole-cell retake')
        self.result['cases'].extend(outcomes);self.save()
        return noise,outcomes

    def settle_attempt(self):
        # Do not overwrite an unacknowledged command. It may still be executing.
        deadline=time.monotonic()+10
        while self.number>self.attempt_command_number and not any(e.get('number')==self.number for e in self.events()):
            if time.monotonic()>=deadline:raise RuntimeError('Evaluation control remains unacknowledged')
            time.sleep(.02)
        for cid in self.attempt_cases:
            own=[e for e in self.events() if e.get('case_id')==cid]
            if not any(e['event']=='case-sealed' for e in own):
                # Let already-issued work finish naturally; no cancellation that
                # could turn a performance failure into a successful retake.
                end=time.monotonic()+30
                while True:
                    own=[e for e in self.events() if e.get('case_id')==cid]
                    starts=[e['t'] for e in own if e.get('frame')=='BotStartedSpeakingFrame']
                    stops=[e['t'] for e in own if e.get('frame')=='BotStoppedSpeakingFrame']
                    if stops and (not starts or max(stops)>max(starts)) and time.monotonic()>=max(stops)+2.3:break
                    if time.monotonic()>=end:raise RuntimeError('Incomplete bot generation after harness failure')
                    time.sleep(.05)
                self.send('seal',cid)
        self.send('health',None)

    def recover_harness(self,group,attempt,exc):
        """Retain first, then prove the device and observer healthy before retry."""
        folder=self.out/f"{group['run_id']}-a{attempt}"
        failure=dict(id=group['run_id'],attempt=attempt,kind=exc.kind,error=str(exc),retry_safe=False)
        if exc.kind=='transport-environmental':failure['witnesses']=exc.witnesses
        write(folder/'acquisition-failure.json',failure)
        self.result.setdefault('harness_failures',[]).append(failure);self.save()
        if self.p.get('v85') and (folder/'capture-end.json').exists() and not (folder/'after.json').exists():
            boundary(self.board,folder,'after') # export boundary even for a failed acquisition
        self.settle_attempt()
        own=[e for e in self.events() if e['t']>=self.attempt_started_ns/1e9]
        if not observer_ok(own) or any(e.get('frame') in ('ErrorFrame','FatalErrorFrame') or e['event'] in ('evaluation-error','liveness-alarm','tts-reconnect-refused','tts-reconnect-failed') for e in own):raise RuntimeError('Hard observer/bot failure during acquisition')
        # Ensure a steady CPU/liveness interval even after a pre-stimulus failure.
        time.sleep(5)
        window=dict(start_ns=self.attempt_started_ns)
        after,samples=boundary(self.board,folder,'retry-check',admission_window=window);require_zero_i2s([self.attempt_before,*samples])
        retry_first=after
        if exc.kind=='transport-environmental':
            started=time.monotonic();time.sleep(5)
            after,second=boundary(self.board,folder,'retry-check-2',admission_window=window)
            elapsed=time.monotonic()-started
            require_zero_i2s(second);samples+=second
            failure['clean_interval_s']=elapsed
            failure['excluded_transport_interval']=dict(pc_id=after['pc_id'],generation=after['generation'],
                before_device_us=self.attempt_before['device_us'],after_device_us=after['device_us'])
        resources=check_reply_resources(self.attempt_before,after,intermediate=[*getattr(self,'attempt_pre',[]),*samples])
        from echoear_aec_campaign_analysis import delivery_counters,transport
        delivery=delivery_counters(self.attempt_before,after)
        transport_check=transport(retry_first if exc.kind=='transport-environmental' else self.attempt_before,after,self.p.get('gates',{}).get('transport',{}).get('rule'))
        admission=retry_admission(self.attempt_before,after,self.recorded(),window)
        failure['recorder_window']=window
        failure.update(resources=resources,delivery=delivery,transport=transport_check,admission=admission,observer_passed=True)
        failure['retry_safe']=resources['all_passed'] and delivery['passed'] and transport_check['passed'] and admission['passed']
        if exc.kind=='transport-environmental':
            from echoear_aec_transport_environment import observer_healthy
            own=[e for e in self.events() if e['t']>=self.attempt_started_ns/1e9]
            failure['recovery_events']=own
            from echoear_aec_transport_environment import reconnect_witness
            failure['recovery_reconnects']=reconnect_witness(self.events(),after['pc_id'],
                [self.attempt_started_ns/1e9,time.monotonic()],exc.witnesses.get('raw',{}).get('cells',[]))
            bad_sctp=[e for e in rows(self.out/'rtp/sctp.jsonl') if e.get('pc_id')==after['pc_id'] and e['monotonic_ns']>=self.attempt_started_ns and (e['event']=='t3-expired' or e.get('chunk_type')=='AbortChunk')]
            failure['recovery_bad_sctp']=bad_sctp
            failure['retry_safe'] &= failure['recovery_reconnects']['passed'] and not bad_sctp and failure['clean_interval_s']>=5 and (after['device_us']-retry_first['device_us'])>=5000000 and observer_healthy(own) and not any(e.get('frame') in ('ErrorFrame','FatalErrorFrame') or e['event'] in ('evaluation-error','liveness-alarm','tts-reconnect-refused','tts-reconnect-failed') for e in own)
        write(folder/'acquisition-failure.json',failure);self.save()
        if not failure['retry_safe']:raise RuntimeError('Device counters/resources not clean; harness retry forbidden')
        return failure

    def reconnect(self,cell):
        from echoear_aec_reconnect import run_reconnect
        for attempt in range(cell.get('start_attempt',1),4):
            trial=dict(cell,id=f"{cell['id']}-a{attempt}")
            self.result['inflight_aux']=dict(kind='reconnects',cell_id=cell['id'],attempt=attempt);self.save()
            def prepare(new_id):
                for name in ('command.json','session-owner.json'):
                    path=self.evaluation/name
                    if path.exists():path.rename(self.evaluation/(trial['id']+'-old-'+name))
                self.number=0
                self.attempt_cases=[];self.attempt_command_number=0
                remaining=[c for c in self.p['reconnects'] if c['session_id']==self.s['id'] and c['id']>=cell['id']]
                spec=protocol(soak_cells(self.p) if self.p.get('v85') else [],remaining);spec['echo_replies'].insert(0,dict(id=new_id,text=cell['text']))
                write(self.evaluation/'protocol.json',spec)
            def tracked_send(op,cid):
                if op=='speak':
                    self.attempt_cases=[cid]
                    self.attempt_command_number=self.number
                return self.send(op,cid)
            command(self.board,f"playback-volume {cell['volume_db']}")
            value=run_reconnect(self.board,command,boundary,tracked_send,self.paths,prepare,self.out,trial,self.baseline,self.p['noise_gate'],self.out/'quiet.npz',self.identity)
            noise=all(g['passed'] for g in value['noise'].values())
            self.result['reconnects'].append(dict(id=trial['id'],cell_id=cell['id'],attempt=attempt,noise_passed=noise,**value));self.result.pop('inflight_aux',None);self.save()
            self.send('seal',trial['id']+'-new')
            if self.p.get('v85'):
                from echoear_aec_v85_tts import inspect,admit
                vendor=inspect(self.events(),self.recorded(),[trial['id']+'-old',trial['id']+'-new'])
                if vendor['long_generations']:
                    record=admit(self.result,attempt,cell['id'],vendor,value['passed'])
                    self.result['reconnects'][-1]['long_tts_excluded']=record['admitted']
                    self.save()
                    if not record['admitted']:raise RuntimeError('Long-TTS reconnect witness/budget/integrity failure')
                    continue
            if value['passed']:return
            if noise:raise RuntimeError('Reconnect performance/witness failure; no retry')
            write(self.out/trial['id']/'noise-contaminated.json',dict(excluded=True,attempt=attempt,gate=value['noise']))
            hard=all(e['passed'] for e in value['epochs']+value['epoch_liveness']) and all(v['passed'] for v in value['new_resources'].values()) and all(v['passed'] for v in value['old_resources'].values()) and value['new_render_ledger']['passed']
            if not hard:raise RuntimeError('Reconnect hardware/resource failure even in noisy capture')
        raise RuntimeError('Reconnect noise retry budget exhausted')

    def finish(self):
        # Even an RF-paused or probe-only resumed hold needs an observer witness.
        if self.result.get('error_class') and hasattr(self,'attempt_cases'):self.settle_attempt()
        else:self.send('health',None)
        if not observer_ok(self.events()):raise RuntimeError('Observer health failure at hold boundary')
        if any(e.get('frame') in ('ErrorFrame','FatalErrorFrame') or e['event'] in ('evaluation-error','liveness-alarm','tts-reconnect-refused','tts-reconnect-failed') for e in self.events()):raise RuntimeError('Active bot/transport error at final audit')
        settled,samples=boundary(self.board,self.out,'settled-end');require_zero_i2s(samples)
        if self.result.get('error_class'):
            retained=[]
            for path in self.out.rglob('*.json'):
                value=json.loads(path.read_text())
                if isinstance(value,dict) and isinstance(value.get('tasks'),list) and 'audio' in value and value.get('pc_id')==settled.get('pc_id') and value.get('generation')==settled.get('generation'):
                    retained.append(value)
            retained.sort(key=lambda p:p['device_us'])
            audit=check_reply_resources(retained[0],settled,intermediate=retained) if retained else dict(all_passed=False)
            self.result['exception_integrity_audit']=audit
            if not audit['all_passed']:raise RuntimeError('Exception final resource/liveness audit failed')
        write(self.out/'display-end.json',read(self.board,'status','DISPLAY'));write(self.out/'character-end.json',read(self.board,'character-status','DISPLAY_CHARACTER'))
        self.result['resources']=check_directory(self.out)
        self.result['rf_active']=host_nic.gate(json.loads((self.out/'rf-pre-gate.json').read_text())['after'],host_nic.snapshot())
        self.result['active_end_ns']=time.monotonic_ns();self.save()
        command(self.board,'audio 0');command(self.board,'disconnect',40)
        require_zero_i2s([snapshot(self.board,self.out,'disconnected')])
        if hasattr(self,'host_guard'):self.host_guard.boundary(self.out,'disconnected')
        self.bot.stop();self.bot=None
        self.result['sctp_recovery']=check_active_recovery(rows(self.out/'rtp/sctp.jsonl'),self.result['active_end_ns'])
        self.result['completed']=self.result['resources']['all_passed'] and self.result['sctp_recovery']['passed'];self.save()

    def probe(self,cell):
        # Direct canonical excitation is forbidden with active transport media.
        # Run after final disconnect, at unchanged gain/volume, before restore.
        command(self.board,'audio 1');time.sleep(5)
        queue=NoiseRetryQueue([dict(run_id=cell['id'],start_attempt=cell.get('start_attempt',1))],self.p['noise_gate'])
        self.result.setdefault('probes',[])
        while (item:=queue.next()) is not None:
            trial,attempt=item;folder=self.out/f"{cell['id']}-a{attempt}";folder.mkdir()
            self.result['inflight_aux']=dict(kind='probes',cell_id=cell['id'],attempt=attempt);self.save()
            if hasattr(self,'host_guard'):self.host_guard.boundary(folder,'before')
            began=time.monotonic();anchors=[capture_start(self.board)]
            try:
                time.sleep(2.3);anchors.append(clock_anchor(self.board))
                cursor,seq=command(self.board,'aec-probe',8)
                self.board.wait_for(rf'^DONE #{seq} aec-probe$',5,since=cursor)
                anchors.append(clock_anchor(self.board));time.sleep(2.3)
            finally:
                anchors.append(capture_stop(self.board));write(folder/'anchors.json',anchors)
                write(folder/'capture-duration.json',dict(elapsed_s=time.monotonic()-began,limit_s=28))
                dump_capture(self.board,folder/'capture.npz')
            if hasattr(self,'host_guard'):self.host_guard.boundary(folder,'after')
            raw,afe,ref,blocks=load_capture(folder/'capture.npz');require_bracketed_capture(blocks,anchors)
            gates={k:quiet_gate(raw[sl],self.baseline,self.p['noise_gate'],mic3=ref[sl]) for k,sl in [('pre',slice(0,32000)),('post',slice(-32000,None))]}
            noise=all(g['passed'] for g in gates.values());action=queue.finish(trial,attempt,noise)
            from echoear_aec_campaign_analysis import probe_measure
            perf=snapshot(self.board,folder,'probe-perf');require_zero_i2s([perf])
            if any(perf['aec'][key]!=0 for key in ('errors','reference_clips','output_clips')) or any(perf['aec']['capture'][key]!=0 for key in ('adc_clips','gain_clips','read_errors','read_timeouts')):raise RuntimeError('Probe hardware fault')
            value=dict(id=cell['id'],attempt=attempt,noise=noise,noise_action=action,noise_gates=gates,**probe_measure(ref,self.qref,cell['volume_db'],self.p['gain_db']))
            self.result['probes'].append(value);self.result.pop('inflight_aux',None);write(folder/'result.json',value);self.save()
            if not noise:write(folder/'noise-contaminated.json',dict(excluded=True,gate=gates))
        command(self.board,'audio 0')
        if queue.exhausted or not any(v['id']==cell['id'] and v['noise'] and v['passed'] for v in self.result['probes']):raise RuntimeError('Canonical probe failed')

    def __exit__(self,typ,exc,tb):
        for token in self.tokens:token.set()
        if exc:finish_after_exception(self,exc)
        if hasattr(self,'host_guard'):
            # Sticky failure, including during exception audit: restoration must
            # still run and the environment class must not become a device fault.
            if self.host_guard.failed or self.result.get('periodicity',{}).get('passed') is False:
                self.result.update(error_class='environmental-pause',completed=False)
        for name,obj in [('bot',self.bot),('serial',self.board)]:
            try:
                if obj:(obj.stop() if name=='bot' else obj.close())
            except Exception as e:self.result['cleanup_errors'].append(name+': '+str(e))
        if getattr(self,'touched',False):
            try:
                flash(NORMAL,NORMAL_HASH,self.out,'restore');b=boot(self.out,'restore',NORMAL_HASH);b.close();self.result['restored_elf_sha256']=NORMAL_HASH
            except Exception as e:self.result['cleanup_errors'].append('RESTORE: '+str(e))
        if (self.out/'mixer-before.state').exists():
            try:
                for op,label in [('restore','before'),('store','after')]:subprocess.run(['alsactl','-f',str(self.out/f'mixer-{label}.state'),op,'0'],check=True,capture_output=True)
                self.result['mixer_equal']=(self.out/'mixer-before.state').read_bytes()==(self.out/'mixer-after.state').read_bytes()
            except Exception as e:self.result['cleanup_errors'].append('mixer: '+str(e))
        if hasattr(self,'host_guard'):
            try:self.host_guard.boundary(self.out,'hold-end',enforce=False)
            except Exception as e:
                self.host_guard.failed=True;self.result['host_environment']=dict(passed=False,error=str(e))
            self.host_guard.stop()
            if self.host_guard.failed:self.result.update(error_class='environmental-pause',completed=False)
        self.save()
        if ((getattr(self,'touched',False) and self.result.get('restored_elf_sha256')!=NORMAL_HASH)
                or (self.require_clean_cleanup and (self.result['cleanup_errors'] or
                    ((self.out/'mixer-before.state').exists() and self.result.get('mixer_equal') is not True)))):
            self.result.update(completed=False, parked_with_lock=True);self.save()
            print('RESTORE/CLEANUP FAILED: retaining bench lock',flush=True)
            while True:time.sleep(30)
        self.lock.__exit__(typ,exc,tb);self.result['lock_released']=True;self.save()
        if not exc and hasattr(self,'host_guard') and self.host_guard.failed:
            raise EnvironmentalPause('Host environment failed at hold end: no credit; restored and lock released')


def acquire(hold,group,attempt,**kwargs):
    try:
        noise,outcomes=hold.capture(group,attempt,**kwargs)
        return noise,outcomes,None
    except HarnessFailure as exc:
        failure=hold.recover_harness(group,attempt,exc)
        if getattr(hold,'p',{}).get('v85') and exc.kind in ('long-tts','capture-deadline'):
            from echoear_aec_v85_tts import inspect,admit
            vendor=inspect(hold.events(),hold.recorded(),hold.attempt_cases)
            if vendor['long_generations'] or exc.kind=='long-tts':
                record=admit(hold.result,attempt,group['run_id'],vendor,failure['retry_safe'])
                failure.update(kind='long-tts',long_tts=record,retry_safe=record['admitted'])
                write(hold.out/f"{group['run_id']}-a{attempt}"/'acquisition-failure.json',failure);hold.save()
                if not record['admitted']:raise RuntimeError('Long-TTS witness/budget/integrity failure; no retake')
        return False,[],failure


def environmental_cap(hold,failure):
    if not failure or failure.get('kind')!='transport-environmental':return
    counts=getattr(hold,'environmental_counts',None)
    if counts is None:counts={'campaign':0};hold.environmental_counts=counts
    counts['campaign']+=1
    hold.environmental_hold_count=getattr(hold,'environmental_hold_count',0)+1
    caps=hold.p['failure_policy']['transport_environmental']
    if hold.environmental_hold_count>caps['max_per_hold'] or counts['campaign']>caps['max_per_campaign']:
        raise EnvironmentalPause('Transport-environmental attempt cap exceeded; new declared hold required')


def run_schedule(hold,cases,spares,policy,prior=None):
    from echoear_aec_lull import declared_rule
    schedule=Schedule(cases,spares,policy,prior,lull_policy=declared_rule(getattr(hold,'p',{})))
    while (item:=schedule.next()) is not None:
        group,attempt=item
        write(hold.out/'schedule.json',schedule.state()) # durable inflight before acquisition
        noise,outcomes,failure=acquire(hold,group,attempt)
        # Failed acquisitions do not enter acoustic analysis or consume spares.
        classes={}
        if outcomes:
            analysis=analyze_block(hold.out)
            byid={r['id']:r['opportunity'] for r in analysis['cases']}
            classes={c['id']:byid.get(c['run_id'],'unscorable-missing-witness') for c in outcomes}
        schedule.finish(group,attempt,noise,classes,failure)
        write(hold.out/'schedule.json',schedule.state())
        environmental_cap(hold,failure)
        if schedule.queue.paused:raise EnvironmentalPause('Three consecutive noise/harness cells; new declaration required')
    if schedule.unresolved or schedule.queue.exhausted:raise RuntimeError('Insufficient clean/scorable denominator')
    return schedule.state()


def soak_cells(p):
    block=next(b for b in p['blocks'] if b['id']=='soak')
    return [dict(c,session_id='qualification-01' if p.get('v85') else 'soak-01',kind='soak-near' if c['mode']=='real-user-sentence' else 'soak-echo',guard_ms=400,stop_offset_s=None,real_bot_reply=c['mode']=='real-user-sentence') for c in block['prompts']]


def run_soak(hold,*,prior=None,clock=time.monotonic,sleep=time.sleep):
    started=clock();next_sample=started;times=[];exports=[];export_start=None;cells=soak_cells(hold.p)
    def tick(*,force=False):
        nonlocal next_sample,export_start
        now=clock()
        if force or now>=next_sample:
            snapshot(hold.board,hold.out,f'soak-perf-{len(times):03}');times.append(now);next_sample=now+10
            if force:
                if export_start is None:export_start=now
                else:exports.append((export_start,now));export_start=None
    groups=[dict(run_id=c['id'],cells=[c]) for c in cells]
    if prior:
        if prior.get('inflight') or prior.get('exhausted'):raise ValueError('Unresolved hard/exhausted soak attempt')
        byid={g['run_id']:g for g in groups}
        groups=[dict(byid[r['id']],start_attempt=r['attempt']) for r in prior['pending']]
        groups.sort(key=lambda g:g['cells'][0]['offset_s'])
    queue=NoiseRetryQueue(groups,hold.p['noise_gate'])
    attempts=copy.deepcopy(prior['attempts']) if prior else []
    first_attempts={(g['run_id'],g.get('start_attempt',1)) for g in groups}
    def save(inflight=None):
        write(hold.out/'soak-state.json',dict(attempts=attempts,inflight=inflight,exhausted=queue.exhausted,pending=[dict(id=g['run_id'],attempt=a) for g,a in queue.pending]))
    hold.soak_nic=host_nic.snapshot()
    while (item:=queue.next()) is not None:
        group,attempt=item;c=group['cells'][0]
        first=(group['run_id'],attempt) in first_attempts
        if not first:
            while clock()<started+600:tick();sleep(.05)
        due=started+c['offset_s'] if first else clock()+10
        while clock()<due-3:tick();sleep(min(.05,max(0,due-3-clock())))
        if clock()>due-2.3:raise RuntimeError('Soak export exceeded next fixed deadline')
        save(dict(id=group['run_id'],attempt=attempt))
        noise,_,failure=acquire(hold,group,attempt,scheduled_s=due,resource_tick=tick)
        hold.soak_nic=host_nic.snapshot()
        if failure and failure.get('kind')=='long-tts':
            from echoear_aec_v85_tts import retry
            action=retry(queue,group,attempt)
        else:action=queue.finish(group,attempt,noise)
        attempts.append(dict(id=c['id'],attempt=attempt,noise_gate_passed=noise,action=action,harness_failure=failure));save()
        write(hold.out/'soak-attempts.json',attempts)
        environmental_cap(hold,failure)
        if queue.paused:raise EnvironmentalPause('Three consecutive noise/harness soak cells')
        tick()
    if queue.exhausted:raise RuntimeError('Soak noise retry budget exhausted')
    while clock()<started+600:tick();sleep(.05)
    tick();ended=clock()
    gaps=[dict(seconds=b-a,during_export=any(lo==a and hi==b for lo,hi in exports)) for a,b in zip(times,times[1:])]
    soak_contract=next(b for b in hold.p['blocks'] if b['id']=='soak')
    export_limit=soak_contract['resource_max_gap_during_export_s']
    value=dict(duration_s=ended-started,resource_times_s=times,resource_gaps=gaps,export_intervals=exports,
               requests_this_hold=len(attempts)-(len(prior['attempts']) if prior else 0),requests=len(cells),attempts=attempts,
               passed=ended-started>=600 and len(cells)==10 and export_start is None and all(g['seconds']<=(export_limit if g['during_export'] else soak_contract['resource_max_gap_outside_export_s']) for g in gaps))
    write(hold.out/'soak.json',value)
    if not value['passed']:raise RuntimeError('Soak duration/resource coverage failed')
    return value


def run(plan_path,output,*,hold_factory=BenchHold):
    p=json.loads(Path(plan_path).read_text());validate_campaign(p) # before mkdir/lock/serial
    from echoear_aec_campaign_resume import resume_state,remaining
    prior=resume_state(p) # also before mkdir, lock, serial
    from echoear_aec_campaign_resume import run_chain
    environmental_counts={'campaign':sum(f.get('kind')=='transport-environmental' for root in run_chain(p) for path in root.glob('*/result.json') for f in json.loads(path.read_text()).get('harness_failures',[]))}
    out=Path(output);out.mkdir(exist_ok=False);write(out/'declaration.json',p)
    check_esptool()
    def interrupted(signum,frame):raise KeyboardInterrupt('Stop requested')
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,interrupted)
    try:
        for s in p['sessions']:
            cells=[c for c in p['cases'] if c['session_id']==s['id']]
            spare=[c for c in p['spares'] if c['session_id']==s['id']]
            controls=[c for c in p['controls'] if c['session_id']==s['id']]
            reconnects=[c for c in p['reconnects'] if c['session_id']==s['id']]
            state=prior.get(s['id'],{})
            schedule_state=state.get('schedule')
            reconnects=remaining(reconnects,state.get('reconnects',{}),'reconnects')
            probes=remaining([c for c in controls if c['kind']=='probe'],state.get('probes',{}),'probes')
            if s['role']=='soak':
                if state.get('soak_complete'):continue
            elif schedule_state and not schedule_state['pending'] and not reconnects and not probes:
                if schedule_state.get('inflight') or schedule_state['unresolved'] or schedule_state['exhausted']:raise ValueError('Unresolved previous schedule')
                continue
            with hold_factory(p,s,out/s['id']) as h:
                h.environmental_counts=environmental_counts
                h.connect(cells+spare+controls+(soak_cells(p) if s['role'] in ('soak','combined') else []),reconnects)
                h.link_probe()
                if s['role']=='soak':
                    if state.get('soak-state'):run_soak(h,prior=state['soak-state'])
                    else:run_soak(h)
                else:
                    # Cold echo first; canonical probe must not condition it.
                    run_schedule(h,cells+[c for c in controls if c['kind']!='probe'],spare,p['noise_gate'],schedule_state)
                    for c in reconnects:h.reconnect(c)
                    if p.get('v85'):run_soak(h)
                h.finish()
                for c in probes:h.probe(c)
    finally:
        import sys
        finalize_analysis(out,sys.exc_info()[1])


if __name__=='__main__':
    # Helpers import these exception classes. Keep CLI and imported identities
    # identical so an environment stop cannot become a device fault.
    import sys
    sys.modules['echoear_aec_campaign']=sys.modules[__name__]
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('plan',type=Path);parser.add_argument('output',type=Path)
    parser.add_argument('--link-soak',action='store_true',help='Separate declared link experiment; no acceptance credit')
    parser.add_argument('--declaration-sha256',help='Required frozen declaration digest for --link-soak')
    args=parser.parse_args()
    if args.link_soak:
        from echoear_aec_link_soak import run as link_soak
        link_soak(args.plan,args.output,expected_sha256=args.declaration_sha256)
    else:
        if args.declaration_sha256 and sha(args.plan)!=args.declaration_sha256:raise ValueError('Declaration digest mismatch')
        declared=json.loads(args.plan.read_text())
        if declared.get('output') and Path(declared['output']).resolve()!=args.output.resolve():raise ValueError('Frozen campaign output mismatch')
        run(args.plan,args.output)
