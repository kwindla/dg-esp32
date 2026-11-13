#!/usr/bin/env python3
"""Execute one released item2 cohort/arm with the immutable predeclared policy."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
# Step 10 requal-n1 (decision 134): normal (AEC-off, no autostart) image of master 16c45a3. The 9bc2d42/9936da45
# item2 campaign is historical. Gap 3 (after acceptance r20) re-pointed the bench restore to this image (55152f25).
MASTER='16c45a3e26bbb11355c1b949aeb86808b3a5cce8'
NORMAL='55152f25dcec0e164c8a087d7f5962dd81191e2cdd3f41ac882a58a0a57a626e'
RTP_MAX_PACKETS='60000'
STAGES=('strict_voice_cat','a6_echo_stress','a6_hot_stt','a6_analog_probe','quiet_offline','quiet_live')


def _say(text):
    """Best-effort recovery diagnostic: a closed or failing output never decides bench ownership (gap 3)."""
    try:print(text,flush=True)
    except Exception:pass


def restore_bench(out,*,wait=None,verify=None):
    """Re-flash and boot-hash-verify the unchanged restore-of-record bundle inside the runner-owned lock.

    Reuses echoear_aec_bench.flash/boot with its NORMAL/NORMAL_HASH (the gap-3 restore-of-record, 55152f25),
    exactly as echoear_aec_campaign.BenchHold restores. A failed restore or verification is a failed hold: as in
    echoear_rollback, the lock is retained and the operator retries with SIGUSR1 (wait); it is never released
    with the board unverified."""
    from echoear_aec_bench import NORMAL,NORMAL_HASH,flash,boot
    if wait is None:
        from echoear_rollback import Live
        holder=SimpleNamespace(retry=False);wait=lambda:Live.wait_recovery(holder)
    attempts=[]
    while True:
        label='restore-'+str(len(attempts)+1)
        try:
            flash(NORMAL,NORMAL_HASH,out,label);boot(out,label,NORMAL_HASH).close()
            if verify is not None:verify(out,label)
            attempts.append(dict(label=label,restored_elf_sha256=NORMAL_HASH,error=None))
            record=dict(clean=len(attempts)==1,restored_elf_sha256=NORMAL_HASH,image=str(NORMAL),attempts=attempts)
            (out/'restore.json').write_text(json.dumps(record,indent=2)+'\n')
            return record
        except Exception as exc:
            attempts.append(dict(label=label,restored_elf_sha256=None,error=type(exc).__name__+': '+str(exc)))
            try:(out/'restore.json').write_text(json.dumps(dict(clean=False,restored_elf_sha256=None,attempts=attempts),indent=2)+'\n')
            except Exception as journal:_say('RESTORE JOURNAL WRITE FAILED ('+type(journal).__name__+')')
            _say('RESTORE FAILED ('+attempts[-1]['error']+'): bench lock retained')
            try:wait()
            except Exception as waited:   # gap 3: a failing custom wait never ends recovery; fall back to the SIGUSR1 wait
                _say('RESTORE WAIT FAILED ('+type(waited).__name__+'): waiting for SIGUSR1')
                from echoear_rollback import Live
                Live.wait_recovery(SimpleNamespace(retry=False))


class RestoreHold:
    """Invariant: once touch() has run, the lock is never released without a verified restore (or a failed restore
    that keeps the lock, echoear_rollback convention).

    Enter inside bench(), outside every serial/bot context; call touch() immediately before the first flash.
    r5 (Astra r4 #1, Fable r3 N4/r4 N5): one hold-scoped handler for SIGINT and SIGTERM is installed in
    __enter__, before any device touch, and stays until __exit__ has finished restoring. It always records the
    signal. It raises KeyboardInterrupt only when the interrupted frame is NOT inside __exit__ (or the restore it
    runs): it inspects the frame chain it is handed, so no window depends on bytecode position. A signal at
    __exit__ entry, between statements of its prologue, or during the restore is therefore only recorded; the
    restore always runs. A KeyboardInterrupt/SystemExit that still escapes inside the prologue is caught and the
    restore runs anyway. The mask is kept around the restore so exec'd esptool inherits SIGINT/SIGTERM blocked.
    Signals recorded during __exit__ never raise there (Fable r3 N3): callers record them as
    interrupt_after_verdict and keep the verdict exit code (Fable r4 N3)."""
    def __init__(self,out,wait=None,verify=None):
        self.out=out;self.wait=wait;self.verify=verify;self.touched=False;self.record=dict(device_untouched=True)
        self.pending_signals=[];self.signals=[];self.previous={};self.exited=False
        self.phase_signals=dict(during_restore=[],after_restore=[],during_scoring=[]);self.raising=False
    def touch(self):
        self.touched=True
        self.record=dict(device_untouched=False,restore_pending=True)   # truthful from the first flash on
    def _protected(self,frame):
        while frame is not None:
            if frame.f_code is RestoreHold.__exit__.__code__ or frame.f_code is restore_bench.__code__:return True
            frame=frame.f_back
        return False
    def _handler(self,number,frame):
        import signal
        name=signal.Signals(number).name;self.signals.append(name)
        if self.raising:
            # r6 (Fable r5 N1): after the verified restore and unlock, post-hold work (hot_stt scoring) is
            # interruptible again; the signal is labelled by phase and the caller records interrupted.
            self.phase_signals['during_scoring'].append(name);raise KeyboardInterrupt(name+' during scoring')
        if self._protected(frame):self.phase_signals['during_restore'].append(name);self.pending_signals.append(name);return
        if self.exited:self.phase_signals['after_restore'].append(name);self.pending_signals.append(name);return
        raise KeyboardInterrupt(name)
    def __enter__(self):
        import signal
        # SIGTERM first: until this line runs the default SIGTERM ends the process, which is safe only because
        # nothing is touched yet (the kernel releases the flock; the board still runs the restore image).
        try:
            self.previous[signal.SIGTERM]=signal.signal(signal.SIGTERM,self._handler)
            self.previous[signal.SIGINT]=signal.signal(signal.SIGINT,self._handler)
            return self
        except BaseException:
            self.release_signals();raise   # Fable r5 N4: nothing touched; leave no hold handler behind
    def __exit__(self,*exc):
        import signal
        try:
            if self.touched:
                try:
                    signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGINT,signal.SIGTERM})
                    try:self.record=self._restore()
                    finally:signal.pthread_sigmask(signal.SIG_UNBLOCK,{signal.SIGINT,signal.SIGTERM})
                except (KeyboardInterrupt,SystemExit) as error:
                    # Belt and braces: anything that still escapes the prologue never skips the restore.
                    self.pending_signals.append(type(error).__name__);self.phase_signals['during_restore'].append(type(error).__name__)
                    self.record=self._restore()
        finally:
            # Fable r4 N4: the hold handler stays installed (recording only) until the caller has written its
            # verdict and calls release_signals(); a late SIGTERM can no longer kill the process before that write.
            self.exited=True
        if self.phase_signals['during_restore']:self.record=dict(self.record,signals_during_restore=list(self.phase_signals['during_restore']))
        return False
    def _restore(self):
        # gap 3: pass verify only when given, so the existing restore_bench call shape is unchanged for every hold
        if self.verify is None:return restore_bench(self.out,wait=self.wait)
        return restore_bench(self.out,wait=self.wait,verify=self.verify)
    def interruptible(self):
        """Switch to raising mode for post-hold work (lock already released after the verified restore)."""
        self.raising=True
    def release_signals(self):
        import signal
        for number,handler in self.previous.items():signal.signal(number,handler)
        self.previous={}
    @property
    def clean(self):return self.record.get('device_untouched') is True or self.record.get('clean') is True


from atomic_json import atomic_write_json   # r8: shared with echoear_hot_stt.score (Astra r7 #2)


WRITE_FINAL_CAP=100


def write_final(path,result,hold,cap=WRITE_FINAL_CAP):
    """Final verdict write (r6 Astra r5 #3, r7 Astra r6 #2/#3, Fable r6 B1).

    - Raising mode (post-unlock hot_stt scoring) is switched off first: the final persistence is record-only.
    - Every write is atomic (atomic_write_json).
    - Write, re-check the hold's recorded signals, rewrite until stable, at most `cap` rewrites; if signals keep
      arriving the record is written once more with signal_storm_truncated=true and the loop stops.
    - After release_signals() the recorded counts are checked once more: anything recorded between the last
      stability check and the release (the hold handler is still installed until then) is written in one more
      atomic write. Masking was rejected: a signal blocked across the release is delivered to the restored
      default handler on unblock and would be lost or fatal, which is worse.
    Cutoff: a signal that arrives after release_signals() takes the default action (SIGINT KeyboardInterrupt,
    SIGTERM terminate) and is not recorded; the verdict is already on disk, and no exit code is promised then."""
    if hold is not None:hold.raising=False
    def stamp():
        if hold is None:return 0
        seen=list(hold.pending_signals)
        if seen:result['interrupt_after_verdict']=seen
        phases={k:list(v) for k,v in hold.phase_signals.items() if v}
        if phases:result['signals_by_phase']=phases
        return len(seen)+sum(map(len,phases.values()))
    def count():return 0 if hold is None else len(hold.pending_signals)+sum(map(len,hold.phase_signals.values()))
    for attempt in range(cap+1):
        written=stamp();atomic_write_json(path,result)
        if count()==written:break
        if attempt==cap:
            result['signal_storm_truncated']=True;written=stamp();atomic_write_json(path,result);break
    if hold is not None:hold.release_signals()
    if count()!=written:
        stamp();atomic_write_json(path,result)   # recorded between the last check and the release


def binding_provenance(path):
    """r11 (Astra r10 #1, Fable B1): the run-time identity of the binding a record was produced under."""
    path=Path(path).resolve();data=path.read_bytes();binding=json.loads(data)
    return dict(release_binding=str(path),binding_sha256=hashlib.sha256(data).hexdigest(),counted=binding.get('counted'),
                purpose=binding.get('purpose'),binding_revision=binding.get('revision'))


def verified_binding(path):
    from demo_session import firmware_artifacts
    binding=json.loads(path.read_text())
    assert binding['execution_authorized'] is True and binding['master_commit']==MASTER
    assert binding['normal_elf_sha256']==NORMAL
    for name,want in binding['reference_hashes'].items():
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==want,'Frozen reference changed: '+name
    image=Path(binding['normal_image']);manifest=json.loads((image/'manifest.json').read_text())
    assert manifest['profile']=='normal' and manifest['autostart'] is False and manifest['commit']==MASTER
    assert manifest['elf_sha256']==NORMAL and firmware_artifacts(image,MASTER[:7])==NORMAL
    for name,want in manifest['sha256'].items():
        p=(image/name).resolve();assert p.is_relative_to(image.resolve())
        assert hashlib.sha256(p.read_bytes()).hexdigest()==want,'Frozen image changed: '+name
    return binding,image


ITEM1_DEFERRAL_ERROR='RuntimeError: RF pre-gate failed before flash; unscored'
A6_DEFERRAL_ERROR='RuntimeError: Item2 eligibility deferred before scored acoustic arm'


def clean_deferral(slot,record):
    """Astra r11 #1: a deferral counts only with a valid envelope, a clean/untouched restore and no failure.
    r13 (Fable r12 N2): shared by the collector and item2's run-time replay guard."""
    if not isinstance(record,dict) or record.get('deferred') is not True:return False
    restore=record.get('restore') or {}
    restore_ok=restore.get('device_untouched') is True or (restore.get('clean') is True and len(restore.get('attempts') or [])==1)
    if record.get('interrupted') or record.get('restore_failed') or not restore_ok:return False
    if slot=='item1':
        return (record.get('scored') is False and restore.get('device_untouched') is True and
                (record.get('rf_pre_gate') or {}).get('passed') is False and record.get('error')==ITEM1_DEFERRAL_ERROR and
                record.get('exit_code')==1)
    return (not record.get('runs') and not record.get('points') and (record.get('deferral_resources') or {}).get('passed') is True and
            record.get('error') in (None,A6_DEFERRAL_ERROR) and record.get('exit_code')==2)


def source_cadence_enabled(plan):
    """Decision 138a S1 is a rule of the plan revision that declares it."""
    return (plan.get('decision_138') or {}).get('uplink_gate')=='source-cadence'


def source_cadence_config(plan):
    """Declared plan fields (decision 139): excuse_firmware_backpressure (139b: false)."""
    return dict(excuse_backpressure=((plan.get('decision_138') or {}).get('source_cadence') or {}).get('excuse_firmware_backpressure') is True)


def record_source_cadence(out,result,s1,*,stage=None,excuse_backpressure=False):
    """r15 (decision 138a): per scored original, S1 (gate input; only with the trace on) and S2 (report-only in item2).
    Missing or incomplete S1 evidence is recorded as a harness failure, never as a pass."""
    from echoear_source_cadence import source_window,record_s2_window
    from step8e2b_analysis import console_windows
    from uplink_continuity import recorded_audio_idle_windows
    serial=out/'serial.log'
    windows=(console_windows(serial)+recorded_audio_idle_windows(serial,clock='device_us')) if serial.exists() else []
    failures=[]
    for row in result.get('runs',[]):
        if row.get('scored') is not True:continue
        row['s2']=dict(record_s2_window(row.get('before_perf'),(row.get('reply_window_transport') or {}).get('after')),stage=stage)
        if not s1:continue
        path=out/f"run{row.get('run')}"/'arrivals.jsonl'
        rows=[json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
        snapshots=perf_snapshots(serial) if excuse_backpressure else ()
        row['source_cadence']=source_window(row,rows,windows,excuse_backpressure=excuse_backpressure,snapshots=snapshots)
        if not row['source_cadence']['complete']:
            failures.append(dict(kind='source_trace_incomplete',run=row.get('run'),problems=row['source_cadence']['harness_failure']))
    if failures:result['harness_failure']=failures


def perf_snapshots(serial):
    from step8e2b_analysis import perf_history
    return perf_history(serial) if Path(serial).exists() else []


def continuation_outlook(plan,stage,prior,prior_results,binding):
    """r16 (Fable r15 N6): before the lock, say whether the continued priors already fail a segment gate. This is a
    warning and a recorded flag, NOT a refusal: the continuation's final record is still the honest way to close the
    slot (a refused continuation would leave the slot without a final verdict)."""
    problems=[]
    for i,(d,record) in enumerate(zip(prior,prior_results),1):
        if stage.startswith('a6_'):
            problems+=['segment %d %s'%(i,p) for p in prior_hold_problems(stage,record)]
            failed=[a6_point_id(stage,p) for p in a6_own_points(record) if p.get('passed') is False or p.get('acquisition_passed') is False]
            if failed:problems.append('segment %d failed points %s'%(i,failed))
        else:
            text=(d/'serial.log').read_text(errors='replace') if (d/'serial.log').exists() else ''
            gate=segment_observation(plan,record,text,current=False,stage=stage)
            problems+=['segment %d %s'%(i,k) for k,v in (gate.get('required') or {}).items() if v is not True]
            if gate.get('passed') is not True and not gate.get('required'):problems.append('segment %d observation'%i)
            problems+=['segment %d original %s FAIL'%(i,r.get('run')) for r in record.get('runs',[]) if r.get('scored') is True and r.get('passed') is not True]
    return dict(cannot_pass=bool(problems),problems=problems,
                basis='Fable r15 N6: warning only; the continuation still runs and its final record closes the slot')


def resume_enabled(plan):
    """Decision 138b is a rule of the plan revision that declares it (never of the frozen r1 plan)."""
    return (plan.get('decision_138') or {}).get('resume_after_clean_deferral') is True


def continuation_problems(record,result_path,stage_root,provenance,normal_elf=NORMAL,*,stage='quiet_live'):
    """r13 (Astra r12 B1): a quiet_live continuation segment is counted only if it was produced under the SAME
    binding (path, sha256, counted, purpose), is a quiet_live record of the bound normal image, and its result lies
    inside the declared quiet_live slot. Hashing proves which bytes are continued, not that they may be counted.
    Used by item2 execute() before acquisition and by the acceptance collector."""
    problems=[];path=Path(result_path);root=Path(stage_root)
    if (path.is_symlink() or path.parent.is_symlink() or root.is_symlink() or not path.is_file()
            or path.resolve().parent.parent!=root.resolve() or path.name!='result.json'):
        problems.append('continuation result is not inside the declared slot: '+str(path))
    if not isinstance(record,dict):return problems+['continuation record is not an object']
    for key in ('release_binding','binding_sha256','counted','purpose'):
        if record.get(key)!=provenance.get(key):problems.append('continuation %s %r != binding %r'%(key,record.get(key),provenance.get(key)))
    if record.get('stage')!=stage:problems.append('continuation stage %r is not %s'%(record.get('stage'),stage))
    if record.get('normal_elf_sha256')!=normal_elf:problems.append('continuation image differs from the binding')
    # r14 (Fable r13 N1): produced exactly where it now lies (a same-binding record copied from elsewhere is refused).
    if record.get('out_path')!=str(path.resolve().parent):
        problems.append('continuation out_path %r is not its location %s'%(record.get('out_path'),path.resolve().parent))
    return problems


ATTEMPT_NAME=__import__('re').compile(r'attempt-[1-9][0-9]*')


def bound_out(binding,slot,out):
    """r14 (Fable r13 N1): a run's output directory is fixed by its binding: <output_root>/<slot>/attempt-<n>
    (canonical n), compared resolved. Smoke bindings declare smoke-<rev>, acceptance bindings acceptance/run-<rev>.
    Called by item2 execute() and item1 before the lock; the resolved path is recorded in the final record as
    out_path, and the collector requires it to equal the record's actual resolved location."""
    root=binding.get('output_root')
    if not root:raise RuntimeError('Binding declares no output_root; refusing to run')
    out=Path(out).resolve();want=Path(root).resolve()/slot
    if out.parent!=want or not ATTEMPT_NAME.fullmatch(out.name):
        raise RuntimeError('Output %s is not <output_root>/%s/attempt-<n> of the binding (%s)'%(out,slot,want))
    return out


def refuse_replayed_population(stage_root,authorized_prior=None,continued=()):
    paths={directory.with_name(directory.name.removesuffix('-identity'))/'result.json'
        for directory in stage_root.glob('attempt-*') if directory.is_dir()}
    for path in paths:
        if str(path.parent.resolve()) in continued:
            # Validated by aggregate_quiet_live and hash-bound via authorized_prior: never replayed, only continued.
            if hashlib.sha256(path.read_bytes()).hexdigest()!=(authorized_prior or {}).get(str(path.resolve())):
                raise RuntimeError('Continuation source changed: '+str(path))
            continue
        if not path.exists():raise RuntimeError('Prior incomplete population cannot be replayed: '+str(path))
        previous=json.loads(path.read_text())
        authorized=(authorized_prior or {}).get(str(path.resolve()))
        if authorized is not None:
            if hashlib.sha256(path.read_bytes()).hexdigest()!=authorized or previous.get('passed') is not False:
                raise RuntimeError('Authorized earlier FAIL changed: '+str(path))
            continue
        if previous.get('runs') or previous.get('points') or not previous.get('deferred',False) or previous.get('deferral_resources',{}).get('passed') is not True:
            raise RuntimeError('Prior scored/failed/incomplete population cannot be replayed: '+str(path))
        if not clean_deferral(stage_root.name,previous):
            # r13 (Fable r12 N2): aligned with the collector; a failed-restore/interrupted/errored deferral is final.
            raise RuntimeError('Prior deferral is not clean (failed restore, interruption, error or exit code); cannot be replayed: '+str(path))
        if time.monotonic()<previous.get('retry_after_host_s',float('inf')):
            raise RuntimeError('Wait at least30s after releasing the deferred hold')


def flash_image(image,out):
    from step9a_echoear import verify,esptool_python
    port=verify(out);flash=json.loads((image/'flasher_args.json').read_text())
    cmd=[esptool_python(),'-m','esptool','--chip','esp32s3','--port',port,'--baud','460800','write-flash',*flash['write_flash_args']]
    for address,name in flash['flash_files'].items():cmd.extend([address,str(image/name)])
    with (out/'flash.log').open('w') as log:subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=120)
    return verify(out)


def a6_resources(out,*,allow_no_samples=False,requested_boots=None):
    from step9a_r2_checks import audit
    from step9a_merge_checks import memory_gate
    rows=[];raw=[]
    for path in out.rglob('serial.log'):
        data=path.read_bytes()
        try:raw.append(audit(data,expected_boot_indices=(requested_boots or {}).get(str(path),[])))
        except Exception as exc:raw.append(dict(passed=False,error=str(exc)))
        try:rows.extend(json.loads(line[5:]) for line in data.decode(errors='replace').splitlines() if line.startswith('PERF '))
        except ValueError:return dict(passed=False,error='Malformed raw PERF')
    memory=memory_gate(rows)
    health=[row.get('display',{}) if isinstance(row,dict) else None for row in rows]
    display=(bool(rows) or allow_no_samples) and all(isinstance(h,dict) and type(h.get('lcd_initialized')) is bool and
        (not h['lcd_initialized'] or h.get('engine_snapshot') is True) and
        h.get('dead') is False and h.get('failed') is False and
        all(type(h.get(k)) is int and h[k]==0 for k in ('dma_errors','lock_timeouts','lock_failures','send_failures','late_callbacks')) for h in health)
    checks=dict(raw=bool(raw) and all(r['passed'] and
        ((allow_no_samples and not rows) if r.get('app_control_min_free') is None else r['app_control_min_free']>=2048) for r in raw),
        memory=memory['passed'] if rows else allow_no_samples,display=display)
    return dict(passed=all(checks.values()),checks=checks,memory=memory,raw=raw,samples_available=bool(rows))


def validate_deferral(out,result):
    resets=[b['index'] for b in result.get('display_monitor',{}).get('boot_epochs',[]) if b['requested']]
    result['deferral_resources']=a6_resources(out,allow_no_samples=True,requested_boots={str(out/'serial.log'):resets})
    failures=[row.get('cleanup_error') for row in result.get('deferrals',[]) if row.get('cleanup_error')]
    clean=not failures and not result.get('cleanup_errors') and result['deferral_resources']['passed']
    if not clean:result.update(passed=False,deferred=False,deferral_fatal_cleanup=failures)
    else:result['retry_after_host_s']=time.monotonic()+30
    return clean


def score_hot_once(out,result):
    from echoear_hot_stt import score
    atomic_write_json(out/'result.json',result)
    result=score(out)
    result['passed']=bool(result['passed'] and result['item2_resources']['passed'] and result['stage_nic']['passed'])
    return result


def a6_preflight(plan,stage,journal):
    from echoear_requalification import preflight_callback
    from host_nic import snapshot
    from alsa import record_room_baseline
    from bench_calibration import record_board_room,BoardRoomUnavailable
    check=preflight_callback(plan,stage,journal);observations=[]
    def before(board,device,point):
        path=point/'eligibility';path.mkdir();nic=snapshot();jabra={};native={}
        try:
            j=record_room_baseline(path/'jabra',stage);jabra=dict(dbfs=j['baseline_dbfs'],wav=j['wav'])
        except (OSError,ValueError) as exc:jabra=dict(dbfs=None,error=str(exc))
        try:native=dict(dbfs=record_board_room(board,path),wav=str(path/'room-board.wav'))
        except BoardRoomUnavailable as exc:native=dict(dbfs=None,wav=str(path/'room-board.wav'),error=str(exc))
        floors=[jabra.get('dbfs'),native.get('dbfs')]
        if all(type(v) in (int,float) and v<=-55 for v in floors):
            remaining=30-(time.monotonic()-nic['monotonic_s'])
            if remaining>0:time.sleep(remaining)
        verdict=check(point.name,dict(jabra=jabra,board=native,nic_before=nic,nic_after=snapshot()))
        observations.append(verdict);(path/'result.json').write_text(json.dumps(verdict,indent=2)+'\n')
        if not verdict['passed']:raise RuntimeError('Item2 eligibility deferred before scored acoustic arm')
    before.observations=observations
    return before


QUIET_LIVE_MAX_SEGMENTS=3


SEGMENT_COUNT_CHECKS=dict(outer=('exact_originals',),source=('five_originals','three_runs'))
ZERO_SCORED_RESOURCE_CHECKS=('raw_monitor','raw_stack','no_retakes')


def segment_observation(plan,seg,text,*,current=False,stage='quiet_live'):
    """Per-segment validation for the D6 aggregate (Astra r2 #3), independent of the five-original count.

    Scored segment: the real observation_gate on the segment's scored runs; every check is required except the
    count-only checks (outer exact_originals, nested acoustic five_originals). The nested acoustic per-original
    checks (quiet, protection, gain, clips, volume, quiet queries, whole transport) and segment checks (no_retakes,
    one_peer, uplink, display) remain required, as do raw monitor, memory, locks, stack, zero transport,
    eligibility and per-arm NIC.
    Zero-scored setup-failure segment: no measurement exists, so only its resources are validated: raw monitor and
    app-control stack from its serial, no reruns, and memory/locks whenever it produced any PERF sample.
    Prior (already executed) segments additionally need their recorded hold: restore clean or device untouched,
    stage NIC, device image profile and (scored) measurement volume. The current segment's hold is gated by
    execute() itself."""
    from echoear_requalification import observation_gate
    scored=[r for r in seg.get('runs',[]) if r.get('scored') is True]
    gate=observation_gate(plan,stage,dict(seg,runs=scored),text)
    checks=dict(gate['checks']);nested=dict(gate.get('source',{}).get('checks') or {})
    if scored:
        required={k:v for k,v in checks.items() if k not in SEGMENT_COUNT_CHECKS['outer'] and k!='source'}
        required.update({'source.'+k:v for k,v in nested.items() if k not in SEGMENT_COUNT_CHECKS['source']})
        if not nested:required['source.available']=False
    else:
        samples=any(line.startswith('PERF ') for line in text.splitlines())
        required={k:checks.get(k) is True for k in ZERO_SCORED_RESOURCE_CHECKS}
        if samples:required.update(memory=checks.get('memory') is True,locks=checks.get('locks') is True)
    # Astra r3 #4: crash evidence (panic/abort/assert/backtrace/watchdog/brownout/unexpected reset reason) fails any
    # segment, scored or not, even without an E-level companion or a second boot banner.
    from step9a_r2_checks import crash_lines
    crashes=crash_lines(text)
    required['crash_free']=not crashes
    if not current:
        restore=seg.get('restore') or {}
        required['hold.restore']=restore.get('clean') is True or restore.get('device_untouched') is True
        required['hold.stage_nic']=(seg.get('stage_nic') or {}).get('passed') is True
        if scored:
            required['hold.image_profile']=(seg.get('image_profile') or {}).get('passed') is True
            required['hold.measurement_volume']=(seg.get('measurement_volume') or {}).get('passed') is True
    return dict(passed=all(v is True for v in required.values()),scored=len(scored),required=required,crash_lines=crashes[:5],
        excluded=dict(outer=list(SEGMENT_COUNT_CHECKS['outer']),source=list(SEGMENT_COUNT_CHECKS['source'])) if scored else 'measurement checks (no scored original)')


def a6_arm_ids(stage,binding):
    """The declared arm/level order of an A6 stage (stress/analog bank manifest order; hot_stt TARGETS)."""
    if stage=='a6_hot_stt':
        from echoear_hot_stt import TARGETS
        return list(TARGETS)
    bank=Path(binding['analog_bank'] if stage=='a6_analog_probe' else binding['stress_bank'])
    return list(json.loads((bank/'manifest.json').read_text()))


def a6_point_id(stage,point):
    return point.get('target_dbfs') if stage=='a6_hot_stt' else point.get('fixture')


def a6_own_points(record):
    return list(record.get('segment_points',record.get('points')) or [])


def a6_arm_count(stage,binding):
    return len(a6_arm_ids(stage,binding))


def aggregate_a6(stage,segments,arm_ids,*,current=False):
    """r15 (decision 138b) for the A6 arms/levels: every non-final segment is a resumable clean deferral carrying
    acquired points; each segment's own points continue the declared arm order exactly once; no arm repeated.
    r16 (Astra r15 B1): every PRIOR segment (all of them when planning; all but the last when the last is the current
    hold, current=True) must also pass prior_hold_problems(); point failures stay in the merged points."""
    problems=[];done=[]
    if not segments or len(segments)>len(arm_ids):problems.append('segment count outside 1..%d'%len(arm_ids))
    for i,seg in enumerate(segments):
        last=i==len(segments)-1;own=a6_own_points(seg)
        if not (last and current):
            problems+=['segment %d %s'%(i+1,p) for p in prior_hold_problems(stage,seg)]
        if not last:
            if not (seg.get('deferred') is True and own and seg.get('next_original_slot')==len(done)+len(own)+1 and
                    clean_deferral(stage,dict(seg,points=[],runs=[]))):
                problems.append('segment %d is not a clean deferral after acquired points'%(i+1))
        ids=[a6_point_id(stage,p) for p in own]
        if ids!=arm_ids[len(done):len(done)+len(ids)]:problems.append('segment %d arms %s do not continue at %d'%(i+1,ids,len(done)+1))
        done+=ids
    last=segments[-1] if segments else {}
    next_slot=len(done)+1 if last.get('deferred') is True and not problems and len(done)<len(arm_ids) else None
    return dict(problems=problems,complete=done==arm_ids,arms=done,next_original_slot=next_slot,
                rule='decision 138b: a clean deferral after acquired A6 points resumes at the next declared arm/level; '
                     'points are never repeated or replaced')


def prior_hold_problems(stage,record):
    """r16 (Astra r15 B1): the hold gates a PRIOR segment must have passed to be continued, with their declared
    applicability (as segment_observation requires them for e2e priors): restore clean or untouched; stage NIC;
    with scored originals/points the device image profile and the measurement volume (unless declared not applicable,
    decision 136 hot_stt); A6 resources; no cleanup errors; display monitor when recorded. A later clean deferral never
    washes out a failed earlier gate."""
    problems=[];restore=record.get('restore') or {}
    if not (restore.get('clean') is True or restore.get('device_untouched') is True):problems.append('hold.restore')
    if (record.get('stage_nic') or {}).get('passed') is not True:problems.append('hold.stage_nic')
    if record.get('runs') or record.get('points'):
        if (record.get('image_profile') or {}).get('passed') is not True:problems.append('hold.image_profile')
        volume=record.get('measurement_volume') or {}
        if volume.get('applicable',True) is not False and volume.get('passed') is not True:problems.append('hold.measurement_volume')
    if stage.startswith('a6_') and (record.get('item2_resources') or {}).get('passed') is not True:problems.append('hold.item2_resources')
    if record.get('cleanup_errors'):problems.append('hold.cleanup')
    monitor=record.get('display_monitor')
    if isinstance(monitor,dict) and monitor.get('passed') is not True:problems.append('hold.display_monitor')
    return problems


def resumable_deferral(stage,record):
    """r15 (decision 138b): the clean-deferral definition of clean_deferral() (envelope, first-attempt clean restore,
    no error, not interrupted, deferral resources passed, exit 2), except that it carries scored originals: every run
    scored, consecutive, ending exactly before next_original_slot. Such a record is a continuation point."""
    if not isinstance(record,dict) or record.get('deferred') is not True:return False
    runs=record.get('runs') or []
    if not runs or any(r.get('scored') is not True for r in runs) or record.get('reruns')!=[]:return False
    if record.get('next_original_slot')!=runs[-1].get('run',0)+1:return False
    return clean_deferral(stage,dict(record,runs=[],points=[])) and not prior_hold_problems(stage,record)


def aggregate_quiet_live(segments,runs=5,plan=None,serial_texts=None,*,stage='quiet_live',resume=False):
    """Decision 134 D6 (quiet_live setup failures) and r15 decision 138b (clean deferrals, every e2e stage):
    the frozen continuation/aggregation procedure.

    segments are the ordered attempt results (original first). Rules:
    - originals are slots 1..runs; each segment's runs are consecutive, starting at the next unfilled slot;
    - quiet_live only (D6): an unscored run (scored is not True) may only be the LAST run of a segment and ends that
      segment; only such a slot may be filled by the next segment (continuation);
    - decision 138b (resume=True, from the plan): a segment that ended in a clean eligibility/RF deferral after scored
      originals (resumable_deferral) is continued at its next_original_slot;
    - scored runs, including FAILs, are never replaced or repeated; no reruns;
    - each segment is one peer (one pc_id over its scored runs) and every scored run after the first in a segment
      is warm on that peer (verify_session_continuity); a continuation is a new session and starts cold;
    - at most QUIET_LIVE_MAX_SEGMENTS segments end in a setup failure; at most `runs` segments in all;
    - the aggregate passes only with all slots scored, every scored original passed, and (when plan/serial are
      given) each segment's observation gate passed on its scored runs, every check except the per-segment
      exact-original count.
    Returns next_original_slot when exactly one more continuation may run."""
    problems=[];rows=[];segment_rows=[];next_slot=None;setup_failures=0
    if not segments or len(segments)>runs:problems.append('segment count outside 1..%d'%runs)
    for i,seg in enumerate(segments):
        last=i==len(segments)-1;seg_runs=seg.get('runs',[])
        if seg.get('reruns')!=[]:problems.append('segment %d has reruns'%(i+1))
        deferral=seg.get('deferred') is True
        if deferral and not (resume and resumable_deferral(stage,seg)):
            problems.append('segment %d is an eligibility deferral, not a continuation source'%(i+1))
        if any(r.get('preflight_deferred') for r in seg_runs):problems.append('segment %d has a deferred run in its population'%(i+1))
        start=len(rows)+1;indices=[r.get('run') for r in seg_runs]
        if not seg_runs or indices!=list(range(start,start+len(seg_runs))):problems.append('segment %d slots %s do not continue at %d'%(i+1,indices,start))
        unscored=[k for k,r in enumerate(seg_runs) if r.get('scored') is not True]
        if unscored and (stage!='quiet_live' or unscored!=[len(seg_runs)-1]):problems.append('segment %d has an unscored run before its end'%(i+1))
        if unscored:setup_failures+=1
        if not last and not unscored and not deferral:problems.append('segment %d is continued without an unscored setup-failure slot or a clean deferral'%(i+1))
        scored=[r for r in seg_runs if r.get('scored') is True]
        peers={r.get('pc_id') for r in scored}
        if scored and (len(peers)!=1 or None in peers):problems.append('segment %d is not one peer'%(i+1))
        if any(r.get('temperature')!='warm' for r in scored[1:]):problems.append('segment %d has a non-warm later run'%(i+1))
        rows.extend(scored);segment_rows.append([r.get('run') for r in scored])
        if last and unscored:next_slot=start+len(seg_runs)-1
        if last and deferral:next_slot=start+len(seg_runs)
    if setup_failures>QUIET_LIVE_MAX_SEGMENTS-1:problems.append('more than %d setup-failure continuations'%(QUIET_LIVE_MAX_SEGMENTS-1))
    if next_slot is not None and (next_slot>runs or len(segments)>=runs or
                                  (segments[-1].get('deferred') is not True and setup_failures>=QUIET_LIVE_MAX_SEGMENTS)):next_slot=None
    observations=[]
    if plan is not None and serial_texts is not None:
        observations=[segment_observation(plan,seg,text,current=i==len(segments)-1,stage=stage) for i,(seg,text) in enumerate(zip(segments,serial_texts))]
    slots=[r.get('run') for r in rows]
    # All unique scored originals, once, here (never per segment).
    complete=slots==list(range(1,runs+1)) and len(set(slots))==runs
    passed=not problems and complete and all(r.get('passed') is True for r in rows) and (plan is None or (len(observations)==len(segments) and all(o['passed'] for o in observations)))
    return dict(passed=passed,complete=complete,problems=problems,slots=[r.get('run') for r in rows],segment_slots=segment_rows,
        original_failures=[r.get('run') for r in rows if r.get('passed') is not True],next_original_slot=None if problems else next_slot,
        segment_observations=observations,stage=stage,resume_after_clean_deferral=resume,
        rule='decision 134 D6: setup failure unscored, filled by a continuation (quiet_live); decision 138b: a clean '
             'deferral after scored originals resumes at its next original; FAILs preserved')


aggregate_segments=aggregate_quiet_live


def execute(binding_path,stage,out,continue_from=(),restore_wait=None):
    binding,image=verified_binding(binding_path);provenance=binding_provenance(binding_path)
    out=bound_out(binding,stage,out);provenance['out_path']=str(out)   # r14 (Fable r13 N1): before the lock
    from echoear_requalification import load_plan,preflight_callback,observation_gate
    from echoear_image_profile import session_gate_text,measurement_volume_gate
    plan=load_plan(Path(binding['policy_path']))
    spec=next(s for s in plan['order'] if s['id']==stage)
    jabra=None
    if binding.get('geometry_rebaseline_policy'):
        from echoear_jabra_rebaseline import load
        jabra=load(binding['geometry_rebaseline_policy'])
        assert plan['strict_gates']['jabra_reply_target_dbfs']==jabra['target_dbfs']
    if binding.get('authorized_prior_failures') and not jabra:
        raise RuntimeError('Prior failed populations require the explicit geometry rebaseline')
    prior=[Path(d).resolve() for d in continue_from];original_start=1;prior_results=[]
    resume=resume_enabled(plan)
    if prior:
        if stage!='quiet_live' and not resume:raise RuntimeError('Only quiet_live setup-failure slots may be continued (decision 134 D6)')
        if stage=='a6_analog_probe':raise RuntimeError('a6_analog_probe has one arm: nothing to continue')
        if any(d.parent!=out.parent for d in prior):raise RuntimeError('Continuation sources must share the stage root')
        prior_results=[json.loads((d/'result.json').read_text()) for d in prior]
        for d,record in zip(prior,prior_results):
            bad=continuation_problems(record,d/'result.json',out.parent,provenance,stage=stage)
            if bad:raise RuntimeError('Continuation source is not a segment of this binding: '+'; '.join(bad))
        if stage.startswith('a6_'):
            planned=aggregate_a6(stage,prior_results,a6_arm_ids(stage,binding))
        else:
            planned=aggregate_quiet_live(prior_results,spec['runs'],stage=stage,resume=resume)
        if planned['problems'] or planned['next_original_slot'] is None:
            raise RuntimeError('No continuable unscored setup-failure slot or clean deferral: '+json.dumps(planned))
        original_start=planned['next_original_slot']
    try:outlook=continuation_outlook(plan,stage,prior,prior_results,binding) if prior else None
    except Exception as exc:   # r17 (Fable r16 N3): warning-only by construction
        outlook=dict(cannot_pass=None,error=type(exc).__name__+': '+str(exc),problems=[])
        print('WARNING: continuation outlook unavailable: '+outlook['error'],flush=True)
    if outlook and outlook['cannot_pass']:
        print('WARNING: continuation cannot pass: '+'; '.join(outlook['problems'][:8]),flush=True)
    authorized=dict(binding.get('authorized_prior_failures') or {})
    for d in prior:authorized[str((d/'result.json').resolve())]=hashlib.sha256((d/'result.json').read_bytes()).hexdigest()
    # r16 (Astra r15 B2): the validated continuation identity is fixed BEFORE acquisition and carried by every final
    # record (normal, exception/interrupt and scoring failure), so an interrupted continuation still collects.
    identity_fields=dict(continued_from=[dict(path=str(d),result_sha256=authorized[str((d/'result.json').resolve())]) for d in prior],
                         continuation_outlook=outlook) if prior else {}
    refuse_replayed_population(out.parent,authorized,continued={str(d) for d in prior})
    if out.exists():raise FileExistsError('Fresh attempt directory required')
    os.environ.update(DG_BOARD='echoear',STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'),
        STEP9A_BUDGET_PATH=binding['budget'],PIPECAT_SCTP_MAX_CHUNK_SIZE='1100',
        # r13 (Fable r12 N1): every stage's bot retains up to 60000 RTP packets per connection (default 10000 = 200 s of
        # audio, ~27 s of margin on quiet_offline); a truncated capture fails coverage, a false FAIL under no_retakes.
        RTP_EVIDENCE_MAX_PACKETS=RTP_MAX_PACKETS)
    os.environ.setdefault('ESPTOOL_PYTHON',str(Path.home()/'.espressif/python_env/idf6.1_py3.12_env/bin/python'))
    from step9a_echoear import bench,check_esptool
    from host_nic import snapshot,gate as nic_gate
    check_esptool();identity=out.with_name(out.name+'-identity');identity.mkdir(parents=True,exist_ok=False)
    journal=binding_path.parent/'deferrals.jsonl';result={};a6=stage.startswith('a6_');code=1
    hold=None
    try:
        with bench():
            with RestoreHold(identity,wait=restore_wait) as hold:
                print('Bench acquired for '+stage,flush=True);hold.touch();port=flash_image(image,identity);nic=snapshot()
                if not a6:
                    from e2e_common import run
                    from step9a_assets import asset_server
                    from step9a_support import host_address
                    for key in ('OFFLINE_BENCH_FIXTURE_DIR','DISPLAY_HIL_COMMANDS','ECHOEAR_STRICT_MERGE','ECHOEAR_ACOUSTIC_QUALIFICATION'):os.environ.pop(key,None)
                    if jabra:
                        os.environ.pop('OFFLINE_SKIP_GREETING',None)
                        os.environ.update(OFFLINE_USB_ABSENCE_TURNS='0',OFFLINE_RTVI_BURST_SWEEP='0',OFFLINE_BENCH_CONTINUOUS_AUDIO='0')
                    strict=stage=='strict_voice_cat';os.environ['ECHOEAR_STRICT_MERGE' if strict else 'ECHOEAR_ACOUSTIC_QUALIFICATION']='1'
                    if strict:os.environ['DISPLAY_HIL_COMMANDS']=str(out/'display-command.json')
                    s1=source_cadence_enabled(plan)
                    args=SimpleNamespace(out=out,port=port,runs=3 if strict else 5,original_start=original_start,spl=65,warm=True,no_retakes=True,
                        elf=image/'dg-esp32.elf',image_profile='normal',server_url='http://'+host_address()+':7861',ice_servers=[],
                        # r15 (decision 138a S1): the per-packet send trace on for every session, read out only between
                        # scored windows (e2e_common source-trace mode).
                        option=['arrival-trace=1'] if s1 else [],source_trace=s1,
                        display='cat',diagnostic_noisy=False,preflight=preflight_callback(plan,stage,journal),
                        jabra_policy=jabra,all_originals=bool(jabra))
                    with asset_server():code=run(args,stage!='quiet_offline')
                    if not (out/'result.json').exists():raise RuntimeError('Missing acquisition result; credential skip is not a pass')
                    result=json.loads((out/'result.json').read_text())
                    record_source_cadence(out,result,s1,stage=stage,**source_cadence_config(plan))
                    if not result.get('deferred'):
                        texts_now=[path.read_text(errors='replace') for path in sorted(out.rglob('serial.log'))]
                        result['image_profile']=session_gate_text(texts_now,'normal',require_cleanup=True)
                        result['measurement_volume']=measurement_volume_gate(result,stage)
                        if stage=='quiet_live' or prior:
                            segments=prior_results+[result]
                            texts=[(d/'serial.log').read_text(errors='replace') for d in prior]+[(out/'serial.log').read_text(errors='replace')]
                            key='quiet_live_aggregate' if stage=='quiet_live' else 'stage_aggregate'
                            result[key]=aggregate_quiet_live(segments,spec['runs'],plan,texts,stage=stage,resume=resume)
                            result['item2_observations']=observation_gate(plan,stage,result,(out/'serial.log').read_text(errors='replace'))
                            result['passed']=bool(result[key]['passed'])
                        else:
                            result['item2_observations']=observation_gate(plan,stage,result,(out/'serial.log').read_text(errors='replace'))
                            result['passed']=bool(result['passed'] and result['item2_observations']['observations_passed'])
                        if jabra:
                            rows=[r for seg in prior_results for r in seg.get('runs',[]) if r.get('scored') is True]+result.get('runs',[])
                            peers={r.get('pc_id') for r in result.get('runs',[]) if r.get('scored') is True}
                            result['rebaseline_campaign']=dict(policy=jabra,one_peer=len(peers)==1 and None not in peers,
                                level_policy_applied=bool(rows) and all(r.get('jabra_rebaseline_policy')==jabra and
                                    r.get('jabra_level',{}).get('target_dbfs')==jabra['target_dbfs'] for r in rows if r.get('scored') is True),
                                all_originals_required=args.runs,prior_failures_unchanged=True)
                            result['passed']=bool(result['passed'] and result['rebaseline_campaign']['one_peer'] and
                                result['rebaseline_campaign']['level_policy_applied'])
                        code=0 if result['passed'] else 1
                else:
                    before=a6_preflight(plan,stage,journal)
                    # r15 (decision 138b): a continuation acquires only the remaining arms/levels, in order.
                    arm_ids=a6_arm_ids(stage,binding) if prior else None
                    remaining=arm_ids[len([p for seg in prior_results for p in a6_own_points(seg)]):] if prior else None
                    if stage=='a6_hot_stt':
                        from echoear_hot_stt import acquire
                        kwargs=dict(targets=remaining) if prior else {}
                        result=acquire(Path(binding['a6_plan']),out,preflight=before,**kwargs)
                        code=0 if result.get('acquisition_complete') else 1
                    else:
                        from echoear_echo_stress import run
                        analog=stage=='a6_analog_probe'
                        kwargs=dict(arms=remaining) if prior else {}
                        result=run(out,Path(binding['analog_bank'] if analog else binding['stress_bank']),Path(binding['budget']),
                            plan=Path(binding['a6_plan']),calibrate_prompt=not analog,preflight=before,**kwargs)
                        if analog and result.get('points'):
                            from echoear_analog_probe import analyze
                            result=analyze(out,Path(binding['analog_bank']))
                        code=0 if result['passed'] else 1
                    result['eligibility']=before.observations
                    if prior:
                        result['segment_points']=list(result.get('points',[]))
                        result['points']=[p for seg in prior_results for p in a6_own_points(seg)]+result['segment_points']
                        agg=aggregate_a6(stage,prior_results+[result],arm_ids,current=True)
                        result['stage_aggregate']=agg
                        if stage=='a6_hot_stt':
                            result['acquisition_complete']=bool(agg['complete'] and not agg['problems'] and
                                                                all(p.get('acquisition_passed') for p in result['points']))
                            code=0 if result['acquisition_complete'] else 1
                        else:
                            result['passed']=bool(result.get('passed') and agg['complete'] and not agg['problems'] and
                                                  all(p.get('passed') for p in result['points']))
                            code=0 if result['passed'] else 1
                    if before.observations and not before.observations[-1]['passed'] and result.get('error')=='RuntimeError: Item2 eligibility deferred before scored acoustic arm':
                        result['deferred']=True;result['scored_runs']=len(result.get('points',[]));code=2
                        result['next_original_slot']=len(result.get('points',[]))+1
                    result['item2_resources']=a6_resources(out,allow_no_samples=bool(result.get('deferred') and not result.get('points')))
                    if not result['item2_resources']['passed']:
                        result.update(passed=False,deferred=False);code=1
                if not result.get('deferred') or result.get('runs') or result.get('points'):
                    # r15 (decision 138b): a deferral that carries scored originals is a continuation point and needs
                    # its hold's profile/volume evidence too.
                    if 'image_profile' not in result:
                        texts=[path.read_text(errors='replace') for path in sorted(out.rglob('serial.log'))]
                        result['image_profile']=session_gate_text(texts,'normal',require_cleanup=True)
                        result['measurement_volume']=measurement_volume_gate(result,stage)
                    if (not result['image_profile']['passed'] or not result['measurement_volume']['passed']) and not result.get('deferred'):
                        result['passed']=False;code=1
                result['stage_nic']=nic_gate(nic,snapshot())
                if not result['stage_nic']['passed'] and not result.get('deferred'):result['passed']=False;code=1
                if result.get('deferred') and not validate_deferral(out,result):code=1
    except BaseException as exc:
        # Fable r2 N1: every exit writes result.json, with the restore record, before the exception continues.
        result=dict(result if isinstance(result,dict) else {},passed=False,stage=stage,**provenance,**identity_fields,
            normal_elf_sha256=NORMAL,error=type(exc).__name__+': '+str(exc),interrupted=isinstance(exc,KeyboardInterrupt),
            restore=hold.record if hold is not None else dict(device_untouched=True))
        if hold is not None and not hold.clean:result['restore_failed']=True
        # r15 (pre-mortem r1, SIGINT): the exception path writes a FINAL, collectible record: an interrupted (or
        # crashed) acquisition is an honest FAIL, never a deferral (an operator stop is not an eligibility decision and
        # must not let a slot escape a running original). exit_code 1 matches the process exit: KeyboardInterrupt is
        # converted to exit 1 in __main__, any other exception exits 1.
        result.update(exit_code=1,kind='item2-stage')
        aggregate_key='quiet_live_aggregate' if stage=='quiet_live' else 'stage_aggregate' if prior else None
        if aggregate_key and aggregate_key not in result:
            result[aggregate_key]=dict(passed=False,complete=False,problems=['acquisition ended by '+type(exc).__name__])
        write_final(out/'result.json',result,hold)
        raise
    result['restore']=hold.record
    if not hold.clean:result.update(passed=False,restore_failed=True);code=1
    if result.get('deferred'):
        result['retry_after_host_s']=time.monotonic()+30
        if result.get('runs') or result.get('points'):
            # r17 (Fable r16 N4): say at write time whether this scored deferral can be continued.
            result['resumable']=dict(prior_hold_problems=prior_hold_problems(stage,result),
                                     continuable=not prior_hold_problems(stage,result))
            if not result['resumable']['continuable']:
                print('WARNING: this deferral is NOT resumable (failed hold gate: '+', '.join(result['resumable']['prior_hold_problems'])+')',flush=True)
    if stage=='a6_hot_stt' and code==0 and result.get('acquisition_complete'):
        # Actual retained uplink is scored exactly once, after releasing bench.
        restore=result['restore']
        scoring_error=None
        try:
            try:
                hold.interruptible()   # r8 (Astra r7 #1): inside the try whose finally disables raising
                result=score_hot_once(out,result)
            finally:hold.raising=False   # Fable r6 B1: raising mode covers the scoring call only
        except BaseException as exc:scoring_error=exc
        if scoring_error is not None:
            exc=scoring_error
            # Astra r3 #5: the one-shot scoring keeps whatever it already wrote (acquisition record or partial
            # score) and gains the final error fields; it is never retried.
            try:kept=json.loads((out/'result.json').read_text())
            except (OSError,ValueError):kept=dict(result)
            kept.update(passed=False,stage=stage,**provenance,normal_elf_sha256=NORMAL,restore=restore,
                error=type(exc).__name__+': '+str(exc),interrupted=isinstance(exc,KeyboardInterrupt),scoring_failed=True,
                signals_during_scoring=hold.phase_signals['during_scoring'] or None,exit_code=1,kind='item2-stage',**identity_fields)
            if prior:   # r17 (Fable r16 N1): mirror the exception path; a reloaded acquisition record has no aggregate yet
                kept.setdefault('stage_aggregate',dict(passed=False,complete=False,problems=['scoring ended by '+type(exc).__name__]))
            write_final(out/'result.json',kept,hold)
            raise exc
        result['restore']=restore
        code=0 if result['passed'] else 1
    if spec.get('report_only'):
        # Decision 134 D6: the measurement verdict is kept exactly as measured; the stage is excluded from the
        # acceptance pass. Only an invalid acquisition (resources, profile, NIC, restore, deferral) fails the stage.
        result['report_only']=True
        result['acquisition_valid']=bool(not result.get('deferred') and result.get('points') and
            result.get('item2_resources',{}).get('passed') and result.get('image_profile',{}).get('passed') and
            result.get('measurement_volume',{}).get('passed') and
            result.get('stage_nic',{}).get('passed') and hold.clean)
        code=0 if result['acquisition_valid'] else (2 if result.get('deferred') else 1)
    result.update(stage=stage,**provenance,normal_elf_sha256=NORMAL,
        final_item1_passed=binding['final_item1_passed'],overall_firmware_qualified=False,
        **identity_fields)
    result['exit_code']=code   # r9 (Fable r8 N2): the acceptance loader requires the recorded exit code
    result['kind']='item2-stage'   # r10 (Astra r9 #3): record identity for the acceptance loader
    write_final(out/'result.json',result,hold)   # Fable r3 N3 / Astra r5 #3: verdict kept, every recorded signal in it
    print(json.dumps(dict(stage=stage,passed=result.get('passed'),deferred=result.get('deferred'),code=code)),flush=True)
    if hold.phase_signals['during_restore']:
        # Fable r4 N3: a signal recorded during the restore keeps the verdict exit code (0 PASS, 1 FAIL, 2 deferred).
        print('SIGNAL DURING RESTORE recorded ('+','.join(hold.phase_signals['during_restore'])+'); verdict exit code kept: '+str(code),flush=True)
    return code


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--binding',type=Path,required=True)
    p.add_argument('--stage',choices=STAGES,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--continue-from',type=Path,action='append',default=[],
        help='prior attempt directory: a quiet_live unscored setup failure (decision 134 D6) or, under a decision-138 plan, a clean deferral after scored originals (any multi-original/multi-arm stage)')
    a=p.parse_args()
    try:code=execute(a.binding.resolve(),a.stage,a.out.resolve(),continue_from=a.continue_from)
    except KeyboardInterrupt:
        # r15: the final record already says interrupted, exit_code 1 (a traceback-free, uv-independent exit status).
        print('INTERRUPTED: final record written (interrupted FAIL, exit 1)',flush=True);code=1
    raise SystemExit(code)
