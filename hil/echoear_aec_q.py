"""AEC-only qualification runner (decisions 88-92, spec rev 2 R1-R10).

One bench hold at a time (lock, flash candidate, boot, mixer, connect: the inherited BenchHold through the v9 hold
type, used for acquisition and cleanup only); one unscored warm-up reply after every connect; the frozen randomized
cell order; near-only alignment witnesses every 8 scored acquisitions; at most 3 acquisitions per logical cell, only
when the outcome is unknown; crash scan after every acquisition (sticky across warm-ups, baselines and discarded
attempts); 150 min acquisition budget with protected cleanup (plan q.cap_s 9000 s); a fresh hold after an acquisition failure (<= 4); normal restore with verification by the
hold's own exit. No parts, no pause/recovery state machine, no part-audit gating.

Verdict-deciding checks, and only these (decision 88). Everything else is report-only; no reused composite has
verdict authority (R10). Required AEC evidence (Q's own echo evaluation, the raw-only near-end delivery and content
measure) is computed separately from the optional v9 report-only analyses, whose exceptions are recorded and never
cause a retake (decision 92 A).
  FAIL         device.reboot_crash (the crash scan; the expected post-flash reset of each hold is exempt) and
               hardware.aec_errors.
  INVESTIGATE  R9 loopback, taking precedence over an ordinary acoustic FAIL (crash stays FAIL): a transcript in the
               cell (any arrival time) with a word run reproducing the bot's own reply that no near-end fixture word
               explains (mixed near+far transcripts count; reply text shared with the near text is reported apart).
  measured     echo: the three frozen echo_measure acoustic clauses (R1) on an independently located playback
               (mapping without the write-to-playback latency deadline); barge: the near-end content bar on the local
               AFE (R2-R4, decision 91/92) against the fixture's single-talk baseline (R5); AFE clipping inside the
               scored interval is a miss (R6).
  validity     (outcome unknown -> retake) the explicit list plan['q']['validity'] on the original (not v9-combined)
               findings, the raw-only near-end delivery acceptance, baseline template support, an unfinished
               acquisition, a failed required evaluation (an evaluator exception is a bounded unknown retake).
               The same required AEC-validity checks (Q.aec_validity: PCM input clipping, AEC PERF counters) run on
               ordinary and recovered-PCM acquisitions; support that cannot be reconstructed is unknown.
Baseline dependency (R5): every near result records the baseline acquisition it used; a reopened baseline drops its
template and returns its near results to waiting (re-evaluated from their retained acquisitions, never reacquired);
results() counts a near result only while that baseline acquisition is final and valid.
Calibration (decision 92): holds A / B, each with two distinct double-talk takes per fixture ('<id>-t2' derived
cells); hold B only with the frozen-rule manifest from A, bound by its sha256.
"""
import sys
if __name__ == '__main__' and sys.pycache_prefix is None:   # Astra d97: fresh bytecode for the WHOLE scorer, set before
    import tempfile                                            # any project module is imported (launchers also export
    sys.pycache_prefix = tempfile.mkdtemp(prefix='aecq-pycache-')   # PYTHONPYCACHEPREFIX)
import copy
import json
import re
import time
import traceback
from pathlib import Path

import echoear_aec_q_near as near
import echoear_aec_q_plan as qplan
import echoear_aec_v9 as v9
import echoear_aec_v9_rules as rules

STRESS_S = 75          # admission: the longest cell window plus scoring
RECOGNITION_DELAY_S = 3.0


def gate_role(gate, q):
    """'fault' | 'validity' | 'report' from the explicit decision-88 lists of the plan."""
    if gate in q['faults']: return 'fault'
    if gate in q['validity']: return 'validity'
    return 'report'


def _words(t): return re.findall(r"[a-z0-9']+", (t or '').lower())


def _grams(r, k): return {tuple(r[i:i+k]) for i in range(len(r)-k+1)}


def _inside(w, r): return len(w) >= 2 and any(r[i:i+len(w)] == w for i in range(len(r)-len(w)+1))


def _cover(w, r, min_run):
    """Transcript positions explained by source words r: inside a >= min_run run of consecutive r words, or all of w
    when the whole >= 2-word transcript lies inside r."""
    if _inside(w, r): return [True]*len(w)
    cov, g = [False]*len(w), _grams(r, min_run)
    for i in range(len(w)-min_run+1):
        if tuple(w[i:i+min_run]) in g: cov[i:i+min_run] = [True]*min_run
    return cov


def _merge(windows, k):
    out = []
    for i in windows:
        if out and i <= out[-1][1]: out[-1][1] = i+k
        else: out.append([i, i+k])
    return out


def loopback_runs(transcripts, reply_text, *, near_text=None, min_run=3):
    """R9 attribution by word runs (decision 92 re-review). In each transcript: a reply window is >= min_run
    consecutive words reproducing the bot's reply (a 2..min_run-1 word transcript wholly inside the reply is one
    window). A reply window touching no word explained by the near-end fixture text is far-specific: a hit (the
    transcript may also contain the near sentence). Reply windows that all overlap near-explained words are shared /
    ambiguous: reported separately, never a hit."""
    hits, shared = [], []
    r, n = _words(reply_text), (_words(near_text) if near_text else [])
    g = _grams(r, min_run)
    for t in transcripts:
        w = _words(t)
        if not w or not r: continue
        nc = _cover(w, n, min_run) if n else [False]*len(w)
        if len(w) < min_run:
            win, k = ([0] if _inside(w, r) else []), len(w)
        else:
            win, k = [i for i in range(len(w)-min_run+1) if tuple(w[i:i+min_run]) in g], min_run
        if not win: continue
        far = [i for i in win if not any(nc[i:i+k])]
        if far: hits.append(dict(text=t, runs=[' '.join(w[x:y]) for x, y in _merge(far, k)]))
        else: shared.append(dict(text=t, runs=[' '.join(w[x:y]) for x, y in _merge(win, k)]))
    return dict(hits=hits, shared=shared)


def loopback(transcripts, reply_text, *, near_text=None, min_run=3):
    """R9 hits: transcripts with a far-specific reply run (loopback_runs)."""
    return [h['text'] for h in loopback_runs(transcripts, reply_text, near_text=near_text, min_run=min_run)['hits']]


class _ScoreProxy:
    """The v9 scorer module with combine() disabled: Q classifies the ORIGINAL clip findings itself (decision 92 A),
    v9's clip -> score / unknown relabelling never reaches the verdict."""
    def __init__(self, module): self._m = module
    def __getattr__(self, k): return getattr(self._m, k)
    @staticmethod
    def combine(entry, integrity_findings, scores, findings, record):
        return scores, list(integrity_findings)+list(findings)


def echo_evaluation(view, a, record):
    """Q's required echo evaluation: playbacks located independently on MIC3 (a late write-to-playback is still a
    mapped exposure), the frozen echo_measure, AFE clipping inside the reference-active extent. Raises on failure
    (outcome unknown)."""
    import numpy as np
    from echoear_aec_campaign_analysis import echo_measure, echo_verdict
    import echoear_aec_v9_score as score
    from echoear_aec_timing import sample_interval
    plays = score.playbacks_of(view, a)
    out = []
    for p in plays:
        src = p.pop('source')
        iv = sample_interval(p['speech_onset_sample'], a['blocks'], a['anchors']) if 'speech_onset_sample' in p else None
        causal = playback_causal(iv, p.get('write_s'))
        mapped = 'source_extent' in p and bool((p.get('match') or {}).get('valid')) and causal
        q = dict(p, passed=mapped, latency_passed=p.get('passed'))
        e = echo_measure(a['raw'], a['afe'], a['ref'], view.qref, src, q, view.serial(), a['blocks']) if mapped else dict(reason=p.get('reason'))
        clipped = 0
        if mapped:
            s0, s1 = p['source_extent']; s0 = max(0, s0)
            clipped = int(np.sum(np.abs(np.asarray(a['afe'][s0+1024:s1+1024])) >= 32767))
        out.append(dict(mapped=mapped, causal=causal, latency=p.get('latency'), latency_passed=p.get('passed'), afe_clipped_samples=clipped,
                        echo={k: e.get(k) for k in ('reason', 'whole', 'first_500ms', 'post_guard', 'source_first_500ms_dbfs',
                                                    'guard_passed', 'gate_unknown_active_frames', 'muted_fraction')}))
    return echo_decide(out)


def playback_causal(interval, write_s):
    """Fable N4: a MIC3 match before the transport write can only be a false match. Only the <= 1 s latency
    deadline is dropped from the inherited mapping test; this physical-causality bound stays."""
    return interval is not None and write_s is not None and interval[0] >= write_s-.1


def echo_decide(out):
    """R1 decision over the evaluated playbacks (pure)."""
    from echoear_aec_campaign_analysis import echo_verdict
    if not out or not all(x['mapped'] for x in out): return dict(result='unknown', reason='playback not located on MIC3', plays=out)
    for x in out:
        e = x['echo']
        if e.get('reason') or e.get('gate_unknown_active_frames'):
            return dict(result='unknown', reason=e.get('reason') or 'gate state unknown over active frames', plays=out)
        if e.get('source_first_500ms_dbfs') is None:
            return dict(result='unknown', reason='source first 500 ms not measurable', plays=out)
    if any(x['afe_clipped_samples'] for x in out): return dict(result='miss', reason='AFE output clipping in the extent (R6)', plays=out)
    ok = all(echo_verdict(x['echo']['whole'], x['echo']['first_500ms'], x['echo']['source_first_500ms_dbfs'], x['echo']['post_guard'])['passed']
             for x in out)
    return dict(result='pass' if ok else 'miss', reason=None, plays=out)


def classify(entry, findings, record, q, *, acquired=True, required_error=None):
    """One acquisition -> dict(outcome, result, roles, reason). outcome: fault | invalid | measured. Optional v9
    analysis exceptions are never part of this."""
    roles = {}
    for f in findings: roles.setdefault(gate_role(f['gate'], q), []).append(f['gate'])
    if roles.get('fault'): return dict(outcome='fault', roles=roles)
    if not acquired: return dict(outcome='invalid', roles=roles, reason='acquisition not completed')
    if required_error: return dict(outcome='invalid', roles=roles, reason='required evaluation failed: '+required_error)
    if any(str(f.get('error', '')).startswith('integrity:') for f in findings):
        return dict(outcome='invalid', roles=roles, reason='validity guards not evaluated')
    if roles.get('validity'): return dict(outcome='invalid', roles=roles, reason='validity guard: '+', '.join(roles['validity']))
    kind = entry['kind']
    if kind == 'echo':
        e = record.get('q_echo') or {}
        if e.get('result') not in ('pass', 'miss'): return dict(outcome='invalid', roles=roles, reason=e.get('reason') or 'echo not evaluated')
        return dict(outcome='measured', result=e['result'], roles=roles, reason=e.get('reason'))
    if kind.startswith('barge-'): return dict(outcome='measured', result='deferred', roles=roles)
    return dict(outcome='measured', result='baseline', roles=roles)


def _load(row):
    from echoear_aec_bench import load_capture
    folder = Path(row['folder'])
    raw, afe, ref, blocks = load_capture(folder/'capture.npz')
    return folder, raw, afe, ref, blocks


def default_baseline_eval(cell, row, q):
    """Template support of a single-talk baseline (R5): a raw/dry onset, a complete window, actual single talk (the
    reference inactive over the source span). Returns dict(valid, reason, stats, base)."""
    from echoear_aec_bench import load_capture
    from echoear_aec_metrics import activity
    import numpy as np
    folder, raw, afe, ref, blocks = _load(row)
    src, span = near.read_source(cell['fixture'])
    onset = near.single_talk_onset(raw, src, span)
    base = near.baseline(raw, afe, src, span, onset) if onset is not None else None
    if base is None: return dict(valid=False, reason='no raw/dry onset or incomplete window')
    qref = load_capture(folder.parent/'quiet.npz')[2]
    ra = np.repeat(activity(ref, qref, [(0, len(ref)-1024)]), 320)
    far = float(np.mean(ra[onset:onset+span[1]-span[0]])) if onset+span[1]-span[0] <= len(ra) else 1.0
    stats = dict(path_gain_db=base['path_gain_db'], path_gain_hf_db=base['path_gain_hf_db'], raw_dbfs=base['raw_dbfs'],
                 afe_dbfs=base['afe_dbfs'], far_active_fraction=far, onset=onset, afe_band_floor_dbfs=base['afe_band_floor_dbfs'],
                 afe_corr_band_floor_dbfs=base['afe_corr_band_floor_dbfs'],
                 corr_band_onset_frame=base['corr_band_onset_frame'],
                 band_onset_frame=base['band_onset_frame'])
    anchors = json.loads((folder/'anchors.json').read_text())
    c = json.loads((folder/'result.json').read_text())['cases'][0] if (folder/'result.json').exists() else {}
    pred = near.predicted_onset(c, blocks, anchors, span, q['nearend']['timing']['prior_samples']) if c else None   # the one prior
    stats['jabra_delay_offset_samples'] = None if pred is None else int(onset-pred)
    if far > .05: return dict(valid=False, reason=f'not single talk: reference active over {far:.0%} of the span', stats=stats)
    return dict(valid=True, reason=None, stats=stats, base=base)


def default_near_eval(cell, row, base_eval, q, *, timing=None, arrays=None):
    """Raw-only delivery acceptance (validity) and the content bar for one barge acquisition. timing: the per-hold
    delay estimate (near.hold_delay); None -> the declared prior. The plan's eligibility and local rule are applied."""
    from echoear_aec_bench import load_capture
    from echoear_aec_metrics import activity
    nq = q['nearend']
    folder, raw, afe, ref, blocks = arrays or _load(row)     # arrays: the design tool's constructed-edit AFE (same path)
    anchors = json.loads((folder/'anchors.json').read_text())
    c = json.loads((folder/'result.json').read_text())['cases'][0] if (folder/'result.json').exists() else \
        json.loads((folder/'partial-descriptor.json').read_text())
    src, span = near.read_source(cell['cell']['fixture'])
    timing = timing or dict(delay_samples=nq['timing']['prior_samples'], source='prior')
    pred = near.predicted_onset(c, blocks, anchors, span, timing['delay_samples'])
    al = near.align(raw, base_eval['base'], pred, half=nq['align_half_samples'])
    ok, why = near.delivery_ok(al, nq['delivery'])
    if not ok: return dict(valid=False, reason='near-end delivery not witnessed: '+why, align=al, timing=timing,
                           delivery_failed=[k for k, _ in near.delivery_failures(al, nq['delivery'])])
    qref = load_capture(folder.parent/'quiet.npz')[2]
    m = near.measure(raw, afe, activity(ref, qref, [(0, len(ref)-1024)]), src, span, al['onset'], base_eval['base'],
                     t_c=nq['t_c'], local_rule=near.rule_from(nq.get('local_rule'), near.LOCAL_RULES),
                     level_floor_db=nq['level_floor_db'],
                     eligibility=near.rule_from(qplan.eligibility_spec(nq['eligibility'], cell['kind']) if nq.get('eligibility')
                                                else near.DEFAULT_ELIGIBILITY, near.ELIGIBILITY),
                     level_floor_report_db=nq.get('level_floor_provisional_db'))
    m['align'] = al
    if not m.get('scorable'): return dict(valid=False, reason=m.get('reason'), measure=m, timing=timing)
    return dict(valid=True, eligible=m.get('eligible'), success=m.get('success'), measure=m, timing=timing)


class _Fault(Exception):
    pass


class Q:
    def __init__(self, p, out, *, hold_factory=None, budget_factory=None, acoustic=None, clock=time.monotonic,
                 sleep=time.sleep, allowance_s=4.0, near_eval=None, baseline_eval=None, echo_eval=None, calibration=None,
                 manifest=None, qualify=False):
        p = copy.deepcopy(p)
        self.calibration = calibration
        if calibration:          # unscored bench calibration capture: hold A or B of the declared plan
            plan = qplan.calibration_plan(p)
            cal = plan['supplement'] if calibration == 'A-supp' else plan['holds'][calibration]
            p['q'].update(order=cal['order'], cap_s=cal['estimate_s']+1800, max_holds=2)
            p['q']['cells'] = p['q']['cells']+plan['take_cells']     # the derived second takes (own ids / folders)
        if qualify and calibration: raise ValueError('qualify and calibration are exclusive')
        if calibration == 'B' or qualify:   # prospective validation / qualification: bound to the frozen rules only
            if manifest is None:
                raise ValueError(('the qualification run' if qualify else 'hold B')+' requires the frozen-rule manifest')
            qplan.bind_manifest(p['q'], manifest)
        self.qualify = qualify
        self.expected_digest = ((manifest or {}).get('scorer') or {}).get('digest')
        if qualify and not self.expected_digest: raise ValueError('the qualification manifest carries no scorer digest')
        self.identity_invalid, self.run_end = None, None
        p['v9']['caps']['attempts_per_logical_cell'] = p['q']['retake']['max_acquisitions_per_cell']
        self.p, self.q, self.out = p, p['q'], Path(out)
        self.cells = {c['logical_id']: c for c in self.q['cells']}
        self.hold_factory, self.budget_factory, self.acoustic_fn = hold_factory, budget_factory, acoustic
        self.clock, self.sleep, self.allowance_s = clock, sleep, allowance_s
        self.near_eval = near_eval or (lambda cell, row, b: default_near_eval(cell, row, b, self.q, timing=self.timing()))
        self.baseline_eval = baseline_eval or (lambda cell, row: default_baseline_eval(cell, row, self.q))
        self.echo_eval = echo_eval or echo_evaluation
        self.rows, self.state = [], {}            # lid -> dict(acquisitions, final, attempts=[reasons], near/baseline)
        self.started, self.fault, self.loopbacks, self.loopback_shared = None, None, [], []
        self.witness_n, self.unconfirmed, self.used_folders, self.unhealthy = 0, [], set(), None
        self.identity_start, self.identity_ends = None, []
        self.out.mkdir(parents=True, exist_ok=True)
        self.ledger = self.out/'q-ledger.jsonl'

    def log(self, row):
        row = dict(row, t=self.clock()); self.rows.append(row)
        with self.ledger.open('a') as w: w.write(json.dumps(row, default=str)+'\n')
        return row

    def remaining_s(self):
        return self.q['cap_s']-(self.clock()-self.started)

    def todo(self):
        cap = self.q['retake']['max_acquisitions_per_cell']
        return [lid for lid in self.q['order']
                if (self.state.get(lid) or {}).get('final') is None and (self.state.get(lid) or {}).get('acquisitions', 0) < cap]

    # -- acquisition --
    def entry(self, lid):
        if lid.startswith('q-witness-'):
            return dict(logical_id=lid, kind='near-witness', cell=dict(self.p['v9']['witness'], id=lid, report_only=True))
        if lid.startswith('q-warmup-'):
            base = next(c for c in self.q['cells'] if c['kind'] == 'echo')['cell']
            return dict(logical_id=lid, kind='echo', cell=dict(base, id=lid, report_only=True))
        return self.cells[lid]

    def _acoustic(self, view, plan, entry, a):
        """Required evidence first (loopback transcripts, Q's echo evaluation); the v9 analyses are optional."""
        c = a.get('c') or {}
        own = a.get('own') or []
        lo = (c.get('start_ns') or 0)/1e9; hi = (c.get('end_ns') or 0)/1e9+RECOGNITION_DELAY_S
        texts = [e.get('text') for e in own if e.get('frame') == 'TranscriptionFrame' and e.get('text')
                 and (not c.get('start_ns') or lo <= e.get('t', lo) <= hi)]
        rec = dict(transcripts=texts)
        if entry['kind'] == 'echo' or entry['kind'].startswith('barge-'):
            lr = loopback_runs(texts, c.get('text'), near_text=c.get('expected_text') if entry['kind'].startswith('barge-') else None,
                               min_run=self.q['loopback']['min_run_words'])
            rec['loopback'] = [h['text'] for h in lr['hits']]
            rec['loopback_runs'], rec['loopback_shared'] = lr['hits'], lr['shared']
        if entry['kind'] == 'echo':
            try: rec['q_echo'] = self.echo_eval(view, a, rec)
            except Exception as exc: rec['q_required_error'] = f'{type(exc).__name__}: {exc}'
        import echoear_aec_v9_score as score
        try:
            sc, f, extra = (self.acoustic_fn or score.acoustic)(view, plan, entry, a)
            rec['v9_report'] = dict(scores=sc, findings=[x['gate'] for x in f], **{k: v for k, v in extra.items() if k in ('barge', 'witness')})
            if 'witness' in extra: rec['witness'] = extra['witness']
        except Exception as exc:
            sc, rec['v9_report_error'] = {}, f'{type(exc).__name__}: {exc}'
        return sc, [], rec

    def acquire(self, runner, lid):
        entry = self.entry(lid)
        try:
            row = runner.acquire(lid, 'q')
        except Exception as exc:
            return entry, [dict(gate=v9.gate_of_exception(exc), error=f'{type(exc).__name__}: {exc}')], {}, False, None, exc
        a = runner.ledger.state.attempts[row['attempt_id']]
        rec = row['record']
        folder = runner.out/row['folder']
        acquired = (folder/'capture.npz').exists() and not a['pending']
        findings = list(a['findings'])
        if str(folder) in self.used_folders:                 # hold A: a retake must never re-read an earlier capture
            acquired = False; findings.append(dict(gate='q.support_unknown', reason=f'capture folder reused: {folder.name}'))
        self.used_folders.add(str(folder))
        if acquired and (entry['kind'] in ('single-talk', 'near-witness') or entry['kind'].startswith('barge-')):
            case = self._case(folder)
            if not (case or {}).get('jabra'):                   # the near-end stimulus was never played: nothing acquired
                acquired = False; findings.append(dict(gate='q.support_unknown', reason='near-end stimulus not played'))
        self.unhealthy = self.health(runner, folder)
        aec = self.aec_validity(folder) if folder.exists() else []
        if acquired:
            if row.get('partial'):
                findings += self.partial_validity(runner.hold, folder)  # the inherited capture stopped before its own checks
            findings += aec                                             # the same required checks on both paths
        else:
            # Astra d97 B1: usability and fault extraction are separate. An unplayed stimulus, a reused folder or an
            # unusable capture still has its retained AEC counter evidence read; a positive fault reaches check_crash()
            # (called right after acquire, before any retry or end-of-hold handling). Non-fault findings are moot here.
            findings += [x for x in aec if gate_role(x['gate'], self.q) == 'fault']
        return entry, findings, rec, acquired, folder, row.get('error')

    @staticmethod
    def final_aec_faults(hold_out, result, diag=None):
        """POSITIVE AEC-error evidence retained by a hold's finalisation (also after a finish() exception): the
        candidate session's aec.errors counter in settled-end.json / disconnected.json above its connected.json
        value, and an 'aec.errors' audit leaf whose value is a positive int (Astra qualify r2 R1: check_snapshots
        also marks unknown / missing data passed False with value None; that is not evidence of an error). A null,
        missing, malformed or zero failed leaf is appended to diag (diagnostic, never a fault). Only candidate-session
        observations are read (the restored normal image is never consulted); other resource leaves stay report."""
        def errors(name):
            try: v = json.loads((Path(hold_out)/f'{name}.json').read_text())
            except (OSError, ValueError): return None
            v = ((v or {}).get('aec') or {}).get('errors')
            return v if type(v) is int else None
        out, base = [], errors('connected')
        for name in ('settled-end', 'disconnected'):
            v = errors(name)
            if base is not None and v is not None and v > base: out.append(dict(where=name, delta=v-base))
        for key, val in (result or {}).items():
            leaf = ((val.get('checks') or {}) if isinstance(val, dict) and isinstance(val.get('checks'), dict) else {}).get('aec.errors')
            if not isinstance(leaf, dict) or leaf.get('passed') is not False: continue
            val_ = leaf.get('value')
            if type(val_) is int and val_ > 0: out.append(dict(where=f'{key}.aec.errors', value=val_))
            elif diag is not None: diag.append(dict(where=f'{key}.aec.errors', value=val_, note='failed leaf without positive evidence'))
        return out

    @staticmethod
    def _case(folder):
        try:
            if (folder/'result.json').exists(): return json.loads((folder/'result.json').read_text())['cases'][0]
            if (folder/'partial-descriptor.json').exists(): return json.loads((folder/'partial-descriptor.json').read_text())
        except (OSError, ValueError, KeyError, IndexError): return None
        return None

    def health(self, runner, folder):
        """Session health after an acquisition (hold A): a witnessed peer loss ends the hold (the bot refuses the
        device's reconnect, every later acquisition is dead); an unproven recovery (acquisition-failure.json without
        retry_safe) needs a closing witness before the next cell. None when healthy."""
        try:
            if v9.peer_loss_witness(runner.hold): return 'peer-lost'
        except Exception as exc:
            return f'peer-loss check failed: {type(exc).__name__}'
        af = folder/'acquisition-failure.json' if folder else None
        if af is not None and af.exists():
            try: ok = json.loads(af.read_text()).get('retry_safe')
            except (OSError, ValueError): ok = None
            if ok is not True: return 'recovery-unproven'
        return None

    @staticmethod
    def aec_validity(folder):
        """Required AEC validity of one acquisition, identical on the ordinary and the recovered-PCM (late seal /
        capture deadline) paths: input clipping in the exported PCM (raw MIC1 -> clip.mic_unattributed, reference
        MIC3 -> clip.reference: validity), and the AEC PERF counters between 'before' and the first retained end
        observation (capture-end, after, retry-check): aec.errors > 0 -> hardware.aec_errors (fault); ADC/gain or
        reference clip deltas -> validity. Support that cannot be reconstructed (no end observation, a missing or
        reset counter, an unreadable capture) -> q.support_unknown (validity: outcome unknown). Crash evidence is the
        crash scan run after every acquisition (check_crash). No resource / admission gate is involved."""
        import numpy as np
        from echoear_aec_bench import load_capture
        out = []
        try:
            raw, afe, ref, blocks = load_capture(folder/'capture.npz')
            n_raw, n_ref = int(np.sum(np.abs(raw) >= 32767)), int(np.sum(np.abs(ref) >= 32767))
            if n_raw: out.append(dict(gate='clip.mic_unattributed', source='pcm', clipped_samples=n_raw))
            if n_ref: out.append(dict(gate='clip.reference', source='pcm', clipped_samples=n_ref))
        except Exception as exc:
            out.append(dict(gate='q.support_unknown', reason=f'capture not readable: {type(exc).__name__}: {exc}'))
        def counter(p, path):
            for k in path: p = p.get(k) if isinstance(p, dict) else None
            return p
        try:
            before = json.loads((folder/'before.json').read_text())
            end = next((json.loads((folder/f'{n}.json').read_text()) for n in ('capture-end', 'after', 'retry-check')
                        if (folder/f'{n}.json').exists()), None)
        except (OSError, ValueError) as exc:
            before = end = None; out.append(dict(gate='q.support_unknown', reason=f'PERF observation unreadable: {exc}'))
        if before is not None and end is None:
            out.append(dict(gate='q.support_unknown', reason='no end PERF observation: AEC counters not reconstructable'))
        elif before is not None:
            for path, gate in ((('aec', 'errors'), 'hardware.aec_errors'), (('aec', 'capture', 'adc_clips'), 'clip.mic_unattributed'),
                               (('aec', 'capture', 'gain_clips'), 'clip.mic_unattributed'), (('aec', 'reference_clips'), 'clip.reference')):
                x, y = counter(before, path), counter(end, path)
                if type(x) is not int or type(y) is not int or y < x:
                    out.append(dict(gate='q.support_unknown', reason='.'.join(path)+' missing/reset'))
                elif y > x:
                    out.append(dict(gate=gate, source='perf', counter='.'.join(path), delta=y-x))
        return out

    def partial_validity(self, hold, folder):
        """Local validity of an attempt whose acquisition ended after the export (late seal / capture deadline):
        the frozen pre/post quiet-window witnesses, board-silent windows and capture gap blocks, from the exported
        PCM. Anything not evaluable is a validity finding (unknown), never a pass."""
        from echoear_aec_bench import load_capture
        from echoear_aec_noise_gate import quiet_gate
        from echoear_aec_metrics import frame_power, reference_threshold
        try:
            raw, afe, ref, blocks = load_capture(folder/'capture.npz')
            base, qref = getattr(hold, 'baseline', None), getattr(hold, 'qref', None)
            out = []
            if base is None or qref is None: return [dict(gate='noise.cell_quiet_windows', reason='hold baseline unavailable')]
            for sl in (slice(0, 32000), slice(-32000, None)):
                if not quiet_gate(raw[sl], base, self.p['noise_gate'], mic3=ref[sl])['passed']:
                    out.append(dict(gate='noise.cell_quiet_windows', reason='late-seal capture: quiet window'))
            if any((frame_power(x) >= reference_threshold(qref)).any() for x in (ref[:32000], ref[-32000:])):
                out.append(dict(gate='quiet.board_silent', reason='late-seal capture'))
            if blocks[:, 4].sum(): out.append(dict(gate='timing.single_gap', reason='late-seal capture: gap block'))
            return out
        except Exception as exc:
            return [dict(gate='timing.capture_bracket', reason=f'late-seal capture not evaluable: {type(exc).__name__}: {exc}')]

    def check_crash(self, runner, findings, where):
        crash, _ = v9.candidate_crash_lines(runner.hold.out)
        g = [x['gate'] for x in findings if gate_role(x['gate'], self.q) == 'fault']
        if crash or g:
            self.fault = self.fault or dict(gate=g[0] if g else 'device.reboot_crash', lines=crash[:5], where=where)
            raise _Fault()

    def note_loopback(self, lid, rec):
        if rec.get('loopback'):
            self.loopbacks.append(dict(id=lid, transcripts=rec['loopback'], runs=rec.get('loopback_runs')))
        if rec.get('loopback_shared'):              # reply text explained only together with the near text: report
            self.loopback_shared.append(dict(id=lid, transcripts=rec['loopback_shared']))

    # -- one hold --
    def run_hold(self):
        p, q = self.p, self.q
        total = min(self.remaining_s(), q['cap_s'])
        contract = dict(active_s=total-q['restore_reserve_s'], restore_reserve_s=q['restore_reserve_s'], total_s=total,
                        minimum_cell_remaining_s=p['v9']['parts']['minimum_start_remaining_s'])
        hold_no = sum(1 for r in self.rows if r['type'] == 'hold-start')+1
        self.log(dict(type='hold-start', hold=hold_no, budget=contract))
        import echoear_aec_nova_prestep as n
        budget_factory = self.budget_factory or n.Budget
        ledger = v9.Ledger(self.out/f'hold-{hold_no}-ledger.jsonl', create=True)
        ledger.append(dict(type='campaign', mode='acceptance', plan_sha256='q', runtime_sha256={}, drain_allowance_s=self.allowance_s))
        ledger.append(dict(type='part-start', part=hold_no, slot=hold_no, role='q', mode='acceptance', budget=contract))
        hold, reason, error = None, 'complete', None
        try:
            with v9.patched_campaign(self.allowance_s, None, clock=self.clock, sleep=self.sleep), budget_factory(contract) as budget:
                factory = self.hold_factory(budget) if self.hold_factory else v9.hold_type(budget)
                hold = factory(p, p['sessions'][0], self.out/f'hold-{hold_no}')
                with hold:
                    witnesses = [dict(p['v9']['witness'], id=f'q-witness-{hold_no}-{i:02d}') for i in range(1, qplan.MAX_WITNESSES+1)]
                    warm = [dict(next(c for c in q['cells'] if c['kind'] == 'echo')['cell'], id=f'q-warmup-{hold_no}', report_only=True)]
                    hold.connect([c['cell'] for c in q['cells']]+witnesses+warm, [])
                    runner = v9.Runner(p, self.out, ledger, part=hold_no, role='q', schedule=dict(blocks=[]), budget=budget,
                                       hold=hold, acoustic=self._acoustic, clock=self.clock, slot=hold_no)
                    runner.entry = self.entry
                    runner.score = _ScoreProxy(runner.score)
                    runner.partial_kinds = ('capture-deadline', 'evaluation-ack-timeout')   # late seal: score the exported PCM
                    runner._view()
                    reason = self.cells_loop(runner, hold_no)
                    try: hold.finish()
                    except Exception as exc: self.log(dict(type='finish-error', hold=hold_no, error=f'{type(exc).__name__}: {exc}'))
        except Exception as exc:
            error, reason = exc, 'hold-error'
        r = getattr(hold, 'result', {}) if hold is not None else {}
        restore = dict(restored_elf_sha256=r.get('restored_elf_sha256'), mixer_equal=r.get('mixer_equal'),
                       lock_released=r.get('lock_released'), cleanup_errors=r.get('cleanup_errors', []))
        restore['clean'] = bool(restore['restored_elf_sha256'] == v9.NORMAL_HASH and restore['mixer_equal'] is True
                                and restore['lock_released'] is True and not restore['cleanup_errors'])
        crash, _ = v9.candidate_crash_lines(self.out/f'hold-{hold_no}')
        if crash and not self.fault: self.fault = dict(gate='device.reboot_crash', lines=crash[:5], hold=hold_no)
        diag = []
        af = self.final_aec_faults(self.out/f'hold-{hold_no}', r, diag)   # Astra qualify B1: sticky, like the crash scan
        if diag: self.log(dict(type='final-aec-diagnostic', hold=hold_no, leaves=diag))
        if af:
            self.log(dict(type='final-aec-fault', hold=hold_no, evidence=af))
            if not self.fault: self.fault = dict(gate='hardware.aec_errors', where='hold-end', hold=hold_no, evidence=af)
        ident = self.identity(); self.identity_ends.append(ident)
        if not self.identity_ok(ident) and not self.identity_invalid:
            self.identity_invalid = f"hold {hold_no} end: {ident.get('error') or 'digest '+str(ident.get('digest'))+' != expected'}"
        self.log(dict(type='hold-end', hold=hold_no, reason=reason, restore=restore, identity=ident,
                      identity_changed=not self.identity_ok(ident),
                      error=f'{type(error).__name__}: {error}' if error else None,
                      traceback=traceback.format_exception(error)[-3:] if error else None))
        return reason, restore

    def warmup(self, runner, hold_no):
        e, f, rec, acq, folder, err = self.acquire(runner, f'q-warmup-{hold_no}')
        self.log(dict(type='warmup', hold=hold_no, findings=[x['gate'] for x in f], error=str(err) if err else None))
        self.check_crash(runner, f, 'warm-up'); self.note_loopback(f'q-warmup-{hold_no}', rec)
        return acq

    def witness(self, runner, hold_no):
        for _ in range(self.q['witness']['acquisitions']):
            self.witness_n += 1
            lid = f'q-witness-{hold_no}-{self.witness_n:02d}'
            e, f, rec, acq, folder, err = self.acquire(runner, lid)
            self.check_crash(runner, f, 'witness')
            w = rec.get('witness') or {}
            ok = acq and not any(gate_role(x['gate'], self.q) == 'validity' and x['gate'] != 'alignment.near_witness' for x in f) \
                and w.get('valid') and w.get('offset') is not None and abs(w['offset']-1024) <= 20
            self.log(dict(type='witness', id=lid, valid=bool(ok), offset=w.get('offset'), method=w.get('method')))
            if ok: return True
        return False

    def _reopen(self, lid, reason, **detail):
        st = self.state[lid]; fin = st['final'] or {}; st['final'] = None
        st['attempts'].append(dict(outcome='invalid', reason=reason, reopened=True, folder=fin.get('folder'),
                                   acquisition=fin.get('acquisition'), **detail))
        self.log(dict(type='cell-reopened', id=lid, reason=reason, **detail))
        if self.cells.get(lid, {}).get('kind') == 'single-talk':
            # the template is no longer support: drop it and return every near evaluation that used it to waiting;
            # their retained acquisitions are re-evaluated (not reacquired) once a valid replacement exists
            old = st.get('baseline') or {}
            st['baseline'] = dict(valid=False, reason='baseline reopened: '+reason, invalidated=old.get('acquisition'))
            for other, oc in self.cells.items():
                os_ = self.state.get(other) or {}
                n = os_.get('near') or {}
                if oc['kind'].startswith('barge-') and oc['cell']['fixture'] == self.cells[lid]['fixture'] and n.get('status') == 'evaluated':
                    os_['near'] = dict(status='waiting-for-baseline', superseded=dict(baseline=n.get('baseline'), success=n.get('success'),
                                       eligible=n.get('eligible')), reason='baseline reopened')
                    self.log(dict(type='near-unsupported', id=other, baseline=lid, reason=reason))

    def baseline_ref(self, base_lid):
        """The baseline acquisition currently supporting a fixture: its final row is present and its template valid."""
        st = self.state.get(base_lid) or {}
        b, fin = st.get('baseline') or {}, st.get('final')
        if not fin or not b.get('valid'): return None
        return dict(id=base_lid, acquisition=fin.get('acquisition'), folder=fin.get('folder'))

    def timing(self):
        """Per-hold raw-only delay estimate (plan nearend.timing) from the valid single-talk captures of this run."""
        offs = [(self.state[l].get('baseline') or {}).get('stats', {}).get('jabra_delay_offset_samples')
                for l in self.baseline_of.values() if self.baseline_ref(l)]
        return near.hold_delay(offs, self.q['nearend']['timing'])

    def _evaluate_near(self, lid):
        """Near-end validity of a finalized barge acquisition, as soon as its fixture's baseline exists (decision 92):
        a delivery/scorability failure, or an evaluator exception, reopens the cell while acquisitions remain. The
        result records the baseline acquisition it used."""
        st = self.state[lid]; c = self.cells[lid]
        base_lid = self.baseline_of.get(c['cell']['fixture'])
        ref = self.baseline_ref(base_lid) if base_lid else None
        if ref is None: st['near'] = dict(status='waiting-for-baseline'); return
        try: r = self.near_eval(c, st['final'], self.state[base_lid]['baseline'])
        except Exception as exc: r = dict(valid=False, reason=f'near-end evaluation error: {type(exc).__name__}: {exc}')
        st['near'] = dict(r, status='evaluated', baseline=ref)
        if not r.get('valid'): self._reopen(lid, r.get('reason') or 'near-end measurement invalid', delivery_failed=r.get('delivery_failed'))

    def _after_measured(self, lid):
        c = self.cells[lid]; st = self.state[lid]
        if c['kind'] == 'single-talk':
            try: b = self.baseline_eval(c, st['final'])
            except Exception as exc: b = dict(valid=False, reason=f'evaluation error: {type(exc).__name__}: {exc}')
            st['baseline'] = dict(b, acquisition=dict(id=lid, acquisition=st['final'].get('acquisition'), folder=st['final'].get('folder')))
            if not b.get('valid'): self._reopen(lid, 'baseline: '+(b.get('reason') or 'invalid')); return
            for other, oc in self.cells.items():
                os_ = self.state.get(other) or {}
                if oc['kind'].startswith('barge-') and oc['cell']['fixture'] == c['fixture'] and os_.get('final') \
                        and (os_.get('near') or {}).get('status') == 'waiting-for-baseline':
                    self._evaluate_near(other)
        elif c['kind'].startswith('barge-'):
            self._evaluate_near(lid)

    @property
    def baseline_of(self):
        return {c['fixture']: lid for lid, c in self.cells.items() if c['kind'] == 'single-talk'}

    def cells_loop(self, runner, hold_no):
        try:
            if not self.warmup(runner, hold_no): return 'warmup-failed'
            if not self.witness(runner, hold_no): return 'witness-invalid'
            since = 0
            while True:
                todo = self.todo()
                if not todo or self.decided() or self.remaining_s() < STRESS_S+self.q['restore_reserve_s']: break
                lid = todo[0]
                e, f, rec, acq, folder, err = self.acquire(runner, lid)
                self.check_crash(runner, f, lid); self.note_loopback(lid, rec)
                c = classify(e, f, rec, self.q, acquired=acq, required_error=rec.get('q_required_error'))
                st = self.state.setdefault(lid, dict(acquisitions=0, final=None, attempts=[]))
                st['acquisitions'] += 1
                row = self.log(dict(type='cell', id=lid, kind=e['kind'], hold=hold_no, acquisition=st['acquisitions'],
                                    folder=str(folder) if folder else None, findings=[x['gate'] for x in f],
                                    error=str(err) if err else None, **c,
                                    report=dict(transcripts=rec.get('transcripts'), q_echo=rec.get('q_echo'),
                                                v9_report=rec.get('v9_report'), v9_report_error=rec.get('v9_report_error'))))
                st['attempts'].append(dict(outcome=c['outcome'], reason=c.get('reason'), folder=str(folder) if folder else None,
                                           acquisition=st['acquisitions']))
                if c['outcome'] == 'invalid':
                    if not acq:
                        self.invalidate_unconfirmed(); return 'acquisition-failed'   # link/peer/bot loss: a new hold
                else:
                    st['final'] = row; self.unconfirmed.append(lid)
                    self._after_measured(lid)
                if self.unhealthy == 'peer-lost' or (self.unhealthy or '').startswith('peer-loss check'):
                    self.log(dict(type='session-lost', hold=hold_no, after=lid, reason=self.unhealthy))
                    self.invalidate_unconfirmed(); return 'peer-lost'          # a closing witness cannot run
                if self.unhealthy == 'recovery-unproven':
                    self.log(dict(type='session-check', hold=hold_no, after=lid, reason=self.unhealthy))
                    since = 0
                    if not self.witness(runner, hold_no):
                        self.invalidate_unconfirmed(); return 'witness-invalid'
                    self.unconfirmed = []
                    continue
                since += 1
                if since >= self.q['witness']['every']:
                    since = 0
                    if not self.witness(runner, hold_no):
                        self.invalidate_unconfirmed(); return 'witness-invalid'
                    self.unconfirmed = []
            if self.unconfirmed:
                if self.witness(runner, hold_no): self.unconfirmed = []
                else: self.invalidate_unconfirmed()
        except _Fault:
            return 'fault'
        return 'complete'

    def invalidate_unconfirmed(self):
        for lid in self.unconfirmed:
            if self.state[lid]['final'] is not None: self._reopen(lid, 'closing alignment witness unresolved')
        self.unconfirmed = []

    # -- scoring and verdict --
    def results(self):
        echo, groups, near_rows, baselines = {}, {}, {}, {}
        for lid, c in self.cells.items():
            st = self.state.get(lid) or {}
            fin = st.get('final')
            if c['kind'] == 'single-talk':
                b = st.get('baseline') or {}
                baselines[c['fixture'].split('/')[-1]] = dict(valid=self.baseline_ref(lid) is not None, reason=b.get('reason'),
                                                              acquisition=b.get('acquisition'), **(b.get('stats') or {}))
            elif c['kind'] == 'echo':
                echo.setdefault(str(c['volume_db']), []).append(fin['result'] if fin else 'missing')
            elif c['kind'].startswith('barge-'):
                n = st.get('near') or {}
                ref = self.baseline_ref(self.baseline_of.get(c['cell']['fixture']))
                supported = bool(ref) and n.get('baseline') == ref               # the baseline it used is still final and valid
                if fin is None: state = 'missing'
                elif n.get('status') != 'evaluated' or not supported: state = 'missing'   # no valid baseline (R5): unknown
                elif n.get('eligible') is False: state = 'ineligible'
                elif n.get('success') is None: state = 'missing'                  # local-content rule unset (decision 92)
                else: state = 'success' if n['success'] else 'miss'
                if n.get('status') == 'evaluated':
                    near_rows[lid] = {k: v for k, v in n.items() if k != 'measure'} | dict(
                        measure={k: v for k, v in (n.get('measure') or {}).items() if k not in ('segments', 'segments_from_first_dt')})
                groups.setdefault(c['kind'], []).append((c['rank'], state))
        return dict(echo={v: rules.echo_stratum(r) for v, r in echo.items()}, echo_results=echo,
                    groups={k: rules.barge_group([s for _, s in sorted(v)]) for k, v in groups.items()},
                    group_states={k: [s for _, s in sorted(v)] for k, v in groups.items()},
                    baselines=baselines, nearend=near_rows)

    def decided(self):
        if self.fault: return True
        if self.calibration: return False
        echo = {}
        for lid, c in self.cells.items():
            if c['kind'] == 'echo':
                fin = (self.state.get(lid) or {}).get('final')
                echo.setdefault(c['volume_db'], []).append(fin['result'] if fin else 'pending')
        return any(rules.echo_stratum(r) == 'FAIL' for r in echo.values())

    def verdict(self):
        res = self.results()
        acoustic_fail = 'FAIL' in res['echo'].values() or 'FAIL' in res['groups'].values()
        if self.fault: outcome = 'FAIL'
        elif self.loopbacks: outcome = 'INVESTIGATE'                 # R9 precedes ordinary acoustic FAIL (decision 92)
        elif acoustic_fail: outcome = 'FAIL'
        elif set(res['echo'].values()) == {'PASS'} and len(res['groups']) == 3 and set(res['groups'].values()) == {'PASS'}: outcome = 'PASS'
        else: outcome = 'INCONCLUSIVE'
        retakes = sum(max(0, s['acquisitions']-1) for s in self.state.values())
        return dict(outcome=outcome, fault=self.fault, loopbacks=self.loopbacks, loopback_shared=self.loopback_shared,
                    acoustic_fail=acoustic_fail, retakes=retakes,
                    retake_rate=retakes/max(1, sum(s['acquisitions'] for s in self.state.values())), **res)

    def calibration_set(self):
        """The validation set of one calibration hold: per fixture the single-talk and double-talk acquisitions, the
        echo-only null cells, the per-hold Jabra delay/level record, every attempt with its reason; no verdict."""
        out = {}
        for lid in self.q['order']:
            c = self.cells[lid]
            st = self.state.get(lid) or {}
            fin = st.get('final')
            key = c['fixture'] if c['kind'] == 'single-talk' else (c['cell']['fixture'] if c['kind'].startswith('barge-') else 'echo-null')
            role = 'single_talk' if c['kind'] == 'single-talk' else ('double_talk' if c['kind'].startswith('barge-') else 'null')
            n = st.get('near') or {}
            out.setdefault(key, {}).setdefault(role, []).append(dict(
                id=lid, folder=(fin or {}).get('folder'), outcome=(fin or {}).get('outcome'), acquisitions=st.get('acquisitions', 0),
                attempts=st.get('attempts', []), baseline=(st.get('baseline') or {}).get('stats'),
                baseline_valid=self.baseline_ref(lid) is not None if c['kind'] == 'single-talk' else None,
                take=c.get('take', 1) if role == 'double_talk' else None, take_of=c.get('take_of'), rank=c.get('rank'),
                stop_offset_s=(c.get('cell') or {}).get('stop_offset_s') if role == 'double_talk' else None,
                near=dict(status=n.get('status'), valid=n.get('valid'), baseline=n.get('baseline'), timing=n.get('timing'),
                          align=n.get('align') or (n.get('measure') or {}).get('align'), delivery_failed=n.get('delivery_failed'),
                          eligible=n.get('eligible'), reason=(n.get('measure') or {}).get('reason') or n.get('reason'),
                          coverage=(n.get('measure') or {}).get('coverage'),
                          far_end_end_ms=((n.get('measure') or {}).get('coverage') or {}).get('far_end_end_ms'),
                          report=(n.get('measure') or {}).get('report')) if role == 'double_talk' else None,
                transcripts=((fin or {}).get('report') or {}).get('transcripts')))
        fixtures = {k: v for k, v in out.items() if k != 'echo-null'}

        def supported(v):
            """Fixture support: a final, valid single-talk template and two distinct final double-talk acquisitions
            whose raw delivery was evaluated against that template."""
            st_ = [x for x in v.get('single_talk', []) if x['folder'] and x['baseline_valid']]
            if len(st_) != 1: return False
            ref = self.baseline_ref(st_[0]['id'])
            dt = [x for x in v.get('double_talk', []) if x['folder'] and x['near']['status'] == 'evaluated'
                  and x['near']['valid'] and x['near']['baseline'] == ref]
            return len({x['folder'] for x in dt}) >= 2 and len(dt) == len(v.get('double_talk', []))
        complete = sum(1 for v in fixtures.values() if supported(v))
        if self.calibration == 'A-supp':          # the supplement: every planned acquisition final with its support
            def got(x): return bool(x['folder']) and (x['baseline_valid'] if x['near'] is None else bool(x['near']['valid']))
            ok = {k for k, v in fixtures.items() if all(got(x) for x in v.get('single_talk', [])+v.get('double_talk', []))}
            supported = lambda v, _ok=ok, _f=fixtures: next(k for k, w in _f.items() if w is v) in _ok
            complete = len(ok)
        delays = [b['jabra_delay_offset_samples'] for v in fixtures.values() for x in v.get('single_talk', [])
                  for b in [x.get('baseline') or {}] if b.get('jabra_delay_offset_samples') is not None]
        import statistics
        jabra = dict(delay_offset_samples=delays, median=statistics.median(delays) if delays else None,
                     spread=(max(delays)-min(delays)) if delays else None,
                     raw_dbfs={k.split('/')[-1]: x['baseline']['raw_dbfs'] for k, v in fixtures.items() for x in v.get('single_talk', [])
                               if x.get('baseline')})
        return dict(hold=self.calibration, fixtures=out, complete_fixtures=complete, total_fixtures=len(fixtures),
                    incomplete=sorted(k.split('/')[-1] for k, v in fixtures.items() if not supported(v)),
                    jabra=jabra, timing=self.timing(), delivery_nulls=self.delivery_nulls(), fault=self.fault, loopbacks=self.loopbacks,
                    loopback_shared=self.loopback_shared, manifest_sha256=self.q.get('manifest_sha256'), scored=False,
                    identity=dict(start=self.identity_start, hold_ends=self.identity_ends,
                                  changed=not (self.identity_ok(self.identity_start) and all(self.identity_ok(x) for x in self.identity_ends))
                                  if self.identity_start else None))

    def delivery_nulls(self):
        """Wrong-stimulus delivery nulls from this run's valid single-talk captures (report; hold A sets the delivery
        thresholds from these and the echo-only nulls). Never fatal: an error is recorded."""
        try:
            templates, captures = {}, {}
            for fx, lid in self.baseline_of.items():
                b = (self.state.get(lid) or {}).get('baseline') or {}
                if self.baseline_ref(lid) is None or b.get('base') is None: continue
                templates[fx] = b['base']
                captures[fx] = (self.null_raw(self.state[lid]['final']), b['base']['onset'])
            return dict(rows=near.wrong_stimulus_nulls(templates, captures, half=self.q['nearend']['align_half_samples']),
                        templates=len(templates))
        except Exception as exc:
            return dict(error=f'{type(exc).__name__}: {exc}')

    @staticmethod
    def null_raw(row):
        return _load(row)[1]

    def identity(self):
        """Code identity now (decision 96): the hil/*.py digest (None when it cannot be read; the error is kept
        separately and never counts as a digest), and the bytecode cache prefix in use."""
        import sys
        try: d, err = qplan.scorer_identity()['digest'], None
        except Exception as exc: d, err = None, f'{type(exc).__name__}: {exc}'
        return dict(digest=d, error=err, pycache_prefix=sys.pycache_prefix)

    def identity_ok(self, ident):
        """Astra qualify B2: a successful read, equal to the frozen manifest's scorer digest when one is bound
        (else to the run-start digest)."""
        want = self.expected_digest or (self.identity_start or {}).get('digest')
        return bool(ident and ident.get('digest') and ident['digest'] == want)

    def cell_rows(self):
        """Per logical scored cell (decision 103 verdict JSON): kind, stratum / rank, final state, acquisitions and
        every attempt with its reason; baselines with their template validity."""
        res = self.results(); out = []
        for lid in self.q['order']:
            c = self.cells[lid]; st = self.state.get(lid) or {}; fin = st.get('final') or {}
            row = dict(id=lid, kind=c['kind'], acquisitions=st.get('acquisitions', 0), attempts=st.get('attempts', []),
                       folder=fin.get('folder'))
            if c['kind'] == 'echo':
                row.update(volume_db=c['volume_db'], state=fin.get('result') if fin else 'missing')
            elif c['kind'].startswith('barge-'):
                n = st.get('near') or {}
                ref = self.baseline_ref(self.baseline_of.get(c['cell']['fixture']))
                if not fin: state = 'missing'
                elif n.get('status') != 'evaluated' or not ref or n.get('baseline') != ref: state = 'missing'
                elif n.get('eligible') is False: state = 'ineligible'
                elif n.get('success') is None: state = 'missing'
                else: state = 'success' if n['success'] else 'miss'
                m = n.get('measure') or {}
                row.update(rank=c['rank'], fixture=c['cell']['fixture'].split('/')[-1], state=state,
                           clauses=dict(bwhole=m.get('clause_corr'), floor=m.get('clause_level'), local=m.get('clause_local'),
                                        clipped=m.get('afe_clipped_samples')), reason=m.get('reason') or n.get('reason'),
                           support=self.near_support(lid, n))
            else:
                row.update(fixture=c['fixture'].split('/')[-1], state='baseline-valid' if self.baseline_ref(lid) else 'baseline-missing')
            out.append(row)
        return out

    def near_support(self, lid, n):
        """Astra qualify review (report only): the baseline a near result used -- signed age (cell final time minus
        the baseline's, seconds), hold association, raw path gain and Jabra delay offset -- and the delivery margins
        of the cell's raw alignment to the frozen guards (positive = inside)."""
        fin = (self.state.get(lid) or {}).get('final') or {}
        ref = n.get('baseline') or {}
        bst = self.state.get(ref.get('id')) or {}
        bfin, bstats = bst.get('final') or {}, (bst.get('baseline') or {}).get('stats') or {}
        al = n.get('align') or (n.get('measure') or {}).get('align') or {}
        d = self.q['nearend']['delivery']
        marg = None
        if al.get('valid'):
            marg = dict(corr=al['corr']-d['corr_min'], ambiguity=al['ambiguity']-d['ambiguity_min'],
                        level_low_db=al['near_level_db']-d['level_min_db'], level_high_db=d['level_max_db']-al['near_level_db'],
                        offset_samples=(d['offset_max_samples']-abs(al['offset'])) if 'offset_max_samples' in d else None)
        return dict(baseline=ref.get('id'), baseline_age_s=(fin['t']-bfin['t']) if fin.get('t') is not None and bfin.get('t') is not None else None,
                    same_hold=(fin.get('hold') == bfin.get('hold')) if fin and bfin else None, cell_hold=fin.get('hold'), baseline_hold=bfin.get('hold'),
                    path_gain_db=bstats.get('path_gain_db'), jabra_delay_offset_samples=bstats.get('jabra_delay_offset_samples'),
                    raw_offset_samples=al.get('offset'), delivery_margins=marg)

    def run(self):
        self.started = self.clock()
        self.identity_start = self.identity(); self.identity_ends = []
        self.log(dict(type='run-start', rule=self.q['rule'], order=self.q['order'], cap_s=self.q['cap_s'], calibration=self.calibration,
                      manifest_sha256=self.q.get('manifest_sha256'), identity=self.identity_start, expected_digest=self.expected_digest))
        if not self.identity_ok(self.identity_start):      # before any bench acquisition
            self.identity_invalid = f"run start: {self.identity_start.get('error') or 'digest '+str(self.identity_start.get('digest'))+' != expected'}"
        holds, end = 0, None
        while True:
            if self.identity_invalid: end = 'identity invalid'; break     # restored already if a hold ran
            if self.decided(): end = 'decided'; break
            if not self.todo(): end = 'all cells final or exhausted'; break
            if holds >= self.q['max_holds']: end = 'max holds'; break
            if self.remaining_s() < STRESS_S+self.q['restore_reserve_s']+600: end = 'wall cap'; break
            holds += 1
            reason, restore = self.run_hold()
            if not restore['clean']: end = 'restore not clean'; break
            if reason == 'fault' or self.fault: end = 'fault'; break
            if self.identity_invalid: end = 'identity invalid'; break
            if not self.todo(): end = 'complete'; break
            # Fable qualify B1/B2: any other clean, non-fault hold end (cells reopened by an unresolved closing
            # witness, an acquisition failure, a decided stratum, the remaining time) goes back to the loop head,
            # which decides between another hold, 'decided', 'max holds' and 'wall cap'.
        self.run_end = end
        self.log(dict(type='run-end', reason=end, holds=holds, wall_s=self.clock()-self.started))
        restores = [r['restore'] for r in self.rows if r['type'] == 'hold-end']
        if self.calibration:
            v = self.calibration_set()
            v.update(restores_clean=bool(restores) and all(r['clean'] for r in restores), wall_s=self.clock()-self.started)
            self.log(dict(type='calibration-end', complete_fixtures=v['complete_fixtures'], restores_clean=v['restores_clean']))
            v9.write(self.out/'calibration-set.json', json.loads(json.dumps(v, default=str)))
            return v
        v = self.verdict()
        v['restores_clean'] = bool(restores) and all(r['clean'] for r in restores)
        v['wall_s'] = self.clock()-self.started
        ident_ok = self.identity_ok(self.identity_start) and all(self.identity_ok(x) for x in self.identity_ends) and not self.identity_invalid
        v.update(run_end=self.run_end, cap_s=self.q['cap_s'], manifest_sha256=self.q.get('manifest_sha256'),
                 identity=dict(start=self.identity_start, hold_ends=self.identity_ends, expected=self.expected_digest,
                               changed=not ident_ok, invalid=self.identity_invalid),
                 restores=restores, cells=self.cell_rows(),
                 run_valid=bool(ident_ok and (v['restores_clean'] or not restores)) and bool(restores))
        self.log(dict(type='verdict', **{k: v[k] for k in ('outcome', 'fault', 'echo', 'groups', 'restores_clean', 'wall_s', 'retakes')}))
        v9.write(self.out/'verdict.json', json.loads(json.dumps(v, default=str)))
        return v


def main(argv=None):
    """CLI. 'dry-plan' prints the qualification order and the calibration holds; 'calibration --hold A|B' runs one
    unscored bench calibration hold (smoke standing permission: no USER-GO; the bench lock is taken by the hold;
    normal restore with verification); hold B needs --manifest and --manifest-sha256 (the frozen rules from A,
    'freeze-manifest'). Exit 1 unless every fixture has complete support, no fault and a clean restore. The
    qualification run itself has no launcher yet (freeze and reviews first)."""
    import argparse, hashlib, sys
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('dry-plan')
    fm = sub.add_parser('freeze-manifest', help='host-only: freeze the rules designed on hold A')
    fm.add_argument('--rules', type=Path, required=True)
    fm.add_argument('--hold-a', type=Path)
    fm.add_argument('--out', type=Path, required=True)
    fm.add_argument('--catalog', type=Path, help='the derived development catalog over hold A and its supplement')
    fm.add_argument('--exclude', action='append', default=[], metavar='FIXTURE=REASON',
                    help='a hold-A fixture without support, declared and recorded (all incomplete fixtures, exactly)')
    qa = sub.add_parser('qualify', help='the AEC-only qualification run under a frozen manifest (decision 103)')
    qa.add_argument('--manifest', type=Path)
    qa.add_argument('--manifest-sha256')
    qa.add_argument('--out', type=Path)
    qa.add_argument('--dry-plan', action='store_true', help='print the cell count and expected wall time; no bench')
    qa.add_argument('--confirm-bench', action='store_true', help='required: this flashes the candidate and drives the bench')
    c = sub.add_parser('calibration')
    c.add_argument('--hold', choices=['A', 'B', 'A-supp'], required=True)
    c.add_argument('--out', type=Path)
    c.add_argument('--manifest', type=Path)
    c.add_argument('--manifest-sha256')
    c.add_argument('--confirm-bench', action='store_true', help='required: this flashes the candidate and drives the bench')
    a = ap.parse_args(argv)
    p = qplan.plan_from()
    cal = qplan.calibration_plan(p)
    if a.cmd == 'dry-plan':
        print(json.dumps(dict(order=p['q']['order'], cells=len(p['q']['cells']), nearend=p['q']['nearend'],
                              calibration={k: dict(acquisitions=h['acquisitions'], estimate_s=h['estimate_s'], role=h['role'])
                                           for k, h in cal['holds'].items()}, takes=cal['takes']), indent=1, default=str))
        return 0
    if a.cmd == 'qualify':
        return qualify(a, p)
    if a.cmd == 'freeze-manifest':
        if a.out.exists(): raise SystemExit(f'output exists: {a.out}')
        for x in (a.out, a.catalog):
            if x is not None and not qplan.persistent(x):
                raise SystemExit(f'{x}: the manifest and catalog must live under the evidence tree (B reads them at the recorded path)')
        excl = dict(x.split('=', 1) for x in a.exclude)
        m = qplan.build_manifest(json.loads(a.rules.read_text()), a.hold_a or Path(cal['holds']['A']['output']), excluded=excl,
                                 catalog=a.catalog)
        a.out.write_text(json.dumps(m, indent=1, sort_keys=True))
        print('manifest sha256', qplan.manifest_sha(json.loads(a.out.read_text())))
        return 0
    manifest = None
    if a.hold == 'B':
        if not (a.manifest and a.manifest_sha256): raise SystemExit('hold B is prospective validation: pass --manifest and --manifest-sha256 (frozen on A)')
        try: manifest, _ = qplan.check_manifest(a.manifest, a.manifest_sha256)
        except ValueError as exc: raise SystemExit(f'manifest refused: {exc}')
    if not a.confirm_bench: raise SystemExit('calibration drives the bench: pass --confirm-bench')
    if not sys.stdin.isatty(): raise SystemExit('calibration needs an interactive terminal (operator present)')
    out = a.out or Path((cal['supplement'] if a.hold == 'A-supp' else cal['holds'][a.hold])['output'])
    if out.exists(): raise SystemExit(f'output exists: {out}')
    preflight()
    if sys.pycache_prefix is None:              # imported, not run as a script: still fresh for later imports
        import tempfile
        sys.pycache_prefix = tempfile.mkdtemp(prefix='aecq-pycache-')
    out.mkdir(parents=True)
    plan = json.dumps(p, sort_keys=True, default=str).encode()
    (out/'plan.json').write_bytes(plan)
    (out/'calibration-plan.json').write_text(json.dumps(cal, indent=1, default=str))
    if manifest: (out/'frozen-manifest.json').write_text(json.dumps(manifest, indent=1, sort_keys=True))
    print('plan sha256', hashlib.sha256(plan).hexdigest(), flush=True)
    v = Q(p, out, calibration=a.hold, manifest=manifest).run()
    print(json.dumps(dict(hold=a.hold, complete_fixtures=v['complete_fixtures'], total=v['total_fixtures'], incomplete=v['incomplete'],
                          fault=v['fault'], restores_clean=v['restores_clean'], wall_s=round(v['wall_s']), jabra=v['jabra'],
                          manifest_sha256=v['manifest_sha256']), indent=1, default=str))
    return calibration_exit(v)


def qualify(a, p):
    """The qualification run (decisions 88-90, 103): the declared plan (40 echo cells at -6 / 0 dB, 3 x 12 ranked barge
    cells, 21 single-talk baselines, seeded spread order, warm-up after every connect), bound to a frozen manifest
    given with its canonical sha256 and nothing else. Retakes: at most 3 acquisitions per cell, only for an unknown
    outcome; exhaustion -> missing -> INCONCLUSIVE unless FAIL is certain. Crash / AEC error -> FAIL. Gates are the
    AEC endpoints only; STT / latency / link / resources report-only; loopback -> INVESTIGATE (precedence: fault
    FAIL > INVESTIGATE > acoustic FAIL, decision 92). Wall cap plan q.cap_s (9000 s = 150 min, an acquisition budget with protected cleanup); normal-image restore verified
    by every hold's exit. Exit 0 only for PASS with a valid run (clean restores, unchanged code identity)."""
    import sys
    est = qplan.qualification_estimate(p)
    m = sha = None
    if a.manifest or a.manifest_sha256:
        if not (a.manifest and a.manifest_sha256): raise SystemExit('qualify: pass both --manifest and --manifest-sha256')
        try: m, sha = qplan.check_manifest(a.manifest, a.manifest_sha256)
        except (ValueError, OSError, KeyError) as exc: raise SystemExit(f'manifest refused: {exc}')
    if a.dry_plan:
        print(json.dumps(dict(cells=est['cells'], by_kind=est['by_kind'], witnesses=est['witnesses'],
                              estimate_s=est['estimate_s'], estimate_min=round(est['estimate_s']/60, 1), cap_s=est['cap_s'],
                              headroom_s=est['headroom_s'], note=est['note'], manifest_sha256=sha,
                              rules=(m or {}).get('rules')), indent=1, default=str))
        return 0
    if m is None: raise SystemExit('qualify requires --manifest and --manifest-sha256 (a frozen manifest; nothing else)')
    if not a.confirm_bench: raise SystemExit('qualify drives the bench: pass --confirm-bench')
    if not sys.stdin.isatty():
        raise SystemExit('qualify needs a terminal: an interactive one or a persistent PTY (tmux + script); decision 103 '
                         'authorises the unattended run')
    out = a.out or Path(p['q']['output'])
    if out.exists(): raise SystemExit(f'output exists: {out}')
    if not qplan.persistent(out): raise SystemExit(f'{out}: the qualification output must live under the evidence tree')
    preflight()
    if sys.pycache_prefix is None:
        import tempfile
        sys.pycache_prefix = tempfile.mkdtemp(prefix='aecq-pycache-')
    out.mkdir(parents=True)
    import hashlib
    run = Q(p, out, manifest=m, qualify=True)                       # binds the manifest rules into its own plan copy
    base = json.dumps(p, sort_keys=True, default=str).encode()
    effective = json.dumps(run.p, sort_keys=True, default=str).encode()
    (out/'plan-base.json').write_bytes(base)                       # BASE plan: near-end fields are pre-binding
    (out/'plan-effective.json').write_bytes(effective)             # the plan the evaluator uses (manifest bound)
    (out/'frozen-manifest.json').write_text(json.dumps(m, indent=1, sort_keys=True))
    print('plan-effective sha256', hashlib.sha256(effective).hexdigest(), 'manifest', sha, 'estimate', est['estimate_s'], 's', flush=True)
    v = run.run()
    print(json.dumps(dict(outcome=v['outcome'], echo=v['echo'], groups=v['groups'], fault=v['fault'], loopbacks=v['loopbacks'],
                          run_end=v['run_end'], restores_clean=v['restores_clean'], run_valid=v['run_valid'],
                          wall_s=round(v['wall_s']), retakes=v['retakes']), indent=1, default=str))
    return qualify_exit(v)


def qualify_exit(v):
    """0 only for a PASS from a valid run."""
    return 0 if v['outcome'] == 'PASS' and v.get('run_valid') else 1


def calibration_exit(v):
    """0 only for a usable calibration hold: complete fixture support, no fault, clean restores."""
    return 0 if v['restores_clean'] and not v['fault'] and v['complete_fixtures'] == v['total_fixtures'] \
        and not (v.get('identity') or {}).get('changed') else 1


def preflight():
    """Host-only checks before the calibration path takes the bench (hold A launch-environment lesson): the flashing
    Python has esptool, the bot's pipecat is importable. Raises SystemExit; nothing is created."""
    import importlib.util
    from step9a_echoear import check_esptool
    try: check_esptool()
    except Exception as exc: raise SystemExit(f'preflight: {exc}')
    if importlib.util.find_spec('pipecat') is None:
        raise SystemExit('preflight: pipecat not importable (run under the server project environment)')


if __name__ == '__main__':
    raise SystemExit(main())
