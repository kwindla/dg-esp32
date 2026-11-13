"""Step-9 acceptance v9 gate registry (docs/echoear-aec-acceptance-v9.md).

One entry per gate: every v8.7 plan gate key and every runtime raise site inventoried at
69b3d8c, plus the gates v9 adds, each with exactly one class. Host-only data; importing
touches no hardware. What the tests prove (test_echoear_aec_gate_registry.py): each v8.7
plan leaf and each inventoried raise site/check name has one owner, the reviewed inventory
(hil/plans/echoear-aec-v9-gate-inventory.json) equals a fresh scan, so any new raise site
or plan leaf fails until it is classified and the inventory is re-reviewed. Raise sites
with no string literal and gates expressed only as booleans outside the listed check
families are not machine-inventoried; the v9 runner must route them through
echoear_aec_v9_rules. Verdict semantics live in echoear_aec_v9_rules (pure functions).

Classes:
  SCORE           counts toward the acoustic bar (pass/fail/eligibility); hard=True entries
                  are hard events (one valid occurrence -> acoustic FAIL).
  INVALID-CELL    the named endpoints of one attempt are invalid: no credit either way,
                  bounded re-attempt of the logical cell.
  PAUSE-PART      close the part, restore, resumable; the open block earns no acoustic credit.
  CANDIDATE-FAIL  sticky candidate fault: ends the attempt, release blocked.
  MONITOR         recorded only.
Endpoint validity is decided before any action (echoear_aec_v9_rules.classify_attempt).
Unknown cause -> PAUSE-PART.
"""

CLASSES = ('SCORE', 'INVALID-CELL', 'PAUSE-PART', 'CANDIDATE-FAIL', 'MONITOR')
ENDPOINTS = ('acoustic', 'events')   # attenuation/opportunity/outcome; false-event harm
ACTIONS = ('STOP', 'PAUSE-PART', 'RETAKE', 'CONTINUE')   # most to least severe

ECHO = ('echo',)
BARGE = ('barge-firm-stop', 'barge-firm-sentence', 'barge-normal-sentence')
WITNESS = ('near-witness',)
REPORT = ('report-only',)
PART = ('part',)
CAMPAIGN = ('campaign',)
UNSCHEDULED = ('reconnect', 'soak', 'probe')   # v8 kinds with no v9 cells
SCORED = ECHO + BARGE
CELLS = SCORED + WITNESS + REPORT
KINDS = CELLS + PART + CAMPAIGN + UNSCHEDULED

DEFAULTS = {
    'SCORE': dict(
        score_effect='Counts in its stratum: pass, fail or eligibility as defined; no score-driven retake.',
        control_action='Continue; stop early only when the stratum is irreversibly FAIL.',
        release_effect='Acoustic verdict input.',
        reset_scope='none: one scored attempt per logical cell'),
    'INVALID-CELL': dict(
        score_effect='No credit either way; evidence retained; the logical cell is re-attempted.',
        control_action='Queue a re-attempt (later in this part if time permits, else the recovery part). '
                       '<=2 attempts per logical cell, <=8 invalid scored-cell attempts per campaign; '
                       '3 consecutive invalid attempts -> PAUSE-PART.',
        release_effect='None, unless caps exhaust: INCONCLUSIVE naming the missing cells.',
        reset_scope='campaign: counts never reset across parts'),
    'PAUSE-PART': dict(
        score_effect='The open block earns no credit; closed blocks keep theirs; attempts in the open block count.',
        control_action='Close the part: settle, restore the normal image, verify mixer/lock; resumable in the '
                       'next part with the same hashes and a fresh boot.',
        release_effect='None by itself; part count or 150 min ceiling exhausted -> INCONCLUSIVE.',
        reset_scope='part: the streak resets with the new part; attempt counts do not'),
    'CANDIDATE-FAIL': dict(
        score_effect='Acceptance attempt FAIL; acoustic scores already earned are reported, not credited.',
        control_action='Stop the campaign; restore; no further part.',
        release_effect='Integration/resource/control FAIL; release blocked until a stated candidate change.',
        reset_scope='never: sticky across parts and boots'),
    'MONITOR': dict(
        score_effect='None.',
        control_action='Record and report.',
        release_effect='Reported in the result; no gate.',
        reset_scope='none'),
}

HARD = dict(hard=True, score_effect='Hard: any one valid occurrence -> acoustic FAIL regardless of the allowance.',
            control_action='Stop early: irreversible FAIL.')


def S(path, line, anchor):
    """Enforcement site: path relative to the repo, 1-based line, text on that line."""
    return (path, line, anchor)


CA = 'hil/echoear_aec_campaign_analysis.py'
AA = 'hil/echoear_aec_accept_analysis.py'
CP = 'hil/echoear_aec_campaign.py'
NL = 'hil/echoear_aec_nova_live.py'
NP = 'hil/echoear_aec_nova_prestep.py'
RS = 'hil/echoear_aec_resources.py'


def G(id, cls, kinds, description, evidence, *, source=(), plan_keys=(), raises=(), terms=(), new=False,
      hard=False, invalidates=None, room_witness=False, outcome_dependent=False, aggregate=False, **override):
    """hard: hard event. invalidates: endpoints an INVALID-CELL finding removes (default acoustic).
    room_witness: may also invalidate 'events', only when time-relevant to the event.
    outcome_dependent: cannot invalidate an endpoint whose miss is already established.
    aggregate: legacy composite; dispatches to its failing leaves, unresolved -> PAUSE-PART."""
    if invalidates is None: invalidates = ('acoustic',) if cls == 'INVALID-CELL' else ()
    entry = dict(id=id, cls=cls, applicability=tuple(kinds), description=description, evidence=evidence,
                 source=tuple(source), plan_keys=tuple(plan_keys), raises=tuple(raises), terms=tuple(terms),
                 new=new, hard=hard, invalidates=tuple(invalidates), room_witness=room_witness,
                 outcome_dependent=outcome_dependent, aggregate=aggregate, **DEFAULTS[cls])
    entry.update(override)
    return entry


REGISTRY = (
    # ---- Echo (SCORE unless stated) -------------------------------------------------------------
    G('echo.whole_reply', 'SCORE', ECHO,
      'Integrated reduction over the reference-active whole reply >= 20 dB (unchanged).',
      'Valid playback extent, fixed 1024-sample AFE delay, independent MIC3 activity mask.',
      source=[S(CA, 30, "whole['integrated_reduction_db']>=20")],
      plan_keys=['gates.echo.integrated_reduction_min_db', 'measurement_contract.activity_mask',
                 'measurement_contract.reduction']),
    G('echo.first_500ms', 'SCORE', ECHO,
      'First 500 ms reduction >= 10 dB when source first 500 ms > -30 dBFS (unchanged).',
      'Source first-500 ms level from aligned context PCM; complete frames only.',
      source=[S(CA, 31, "first['integrated_reduction_db']>=10")],
      plan_keys=['gates.echo.first_500ms_reduction_min_db', 'gates.echo.first_500ms_source_min_dbfs_exclusive',
                 'measurement_contract.first500_source']),
    G('echo.post_guard', 'SCORE', ECHO,
      'Post-guard AFE residual <= -50 dBFS full band (unchanged; never relaxed).',
      'Guard-open reference-active frames mapped from the GATE log.',
      source=[S(CA, 32, "post['residual_dbfs']<=-50")],
      plan_keys=['gates.echo.post_guard_afe_residual_max_dbfs']),
    G('echo.guard_closure', 'SCORE', ECHO,
      'Guard closed at speech onset after >=2 s silence; a witnessed wrong state is a scored miss; unknown gate state is echo.unscorable.',
      'GATE log states mapped to every reference-active frame.',
      source=[S(CA, 156, "result['passed'] &= guard_ok and not result['gate_unknown_active_frames']")],
      plan_keys=['gates.guard.source_ms', 'gates.guard.rearm_ms', 'gates.guard.speech_onset', 'gates.guard.rearm_rule',
                 'measurement_contract.reference_onsets']),
    G('echo.hard_floor', 'SCORE', ECHO,
      'Policy limit: whole reply < 15 dB or post-guard > -40 dBFS on any valid echo cell fails acceptance.',
      'Same measurement as echo.whole_reply / echo.post_guard on a valid cell.',
      new=True, **HARD),
    G('echo.false_turns', 'SCORE', ECHO,
      '0 accepted false turns and 0 false interruptions on every echo reply (harm gate).',
      'Positive evidence (accepted UserStartedSpeaking / InterruptionFrame in the reply or its tail); survives alignment, observer or coverage loss; erased only by an independent room witness time-associated with that specific event (per-event ids).',
      source=[S(CA, 419, "row['accepted_false_turns']==0 and row['turn_events']['interruptions']==0")],
      plan_keys=['gates.false_turns.accepted_max', 'gates.false_turns.interruptions_max',
                 'nova_live.event_schema.echo_rule'], **HARD),
    G('barge.false_interruption', 'SCORE', BARGE + WITNESS + REPORT,
      'Unsolicited accepted turn or interruption: not positively associated with the declared near utterance (e.g. any before the mapped near-onset lower bound, or a further turn after the associated one outside [near onset, near-end upper bound + 2 s] whose transcript does not match the near text). A late accepted response associated with the near utterance is an ordinary latency/outcome SCORE miss, not a false event. Intended barge-in is not false.',
      'Event timestamps mapped to the capture timeline with the declared onset uncertainty, plus the event/transcript association to the near utterance; same survival rule as echo.false_turns.',
      new=True, **HARD),
    G('echo.stratum_bar', 'SCORE', ECHO,
      '20 valid echo cells per volume (0 and -6 dB; 10 texts x 2 generations); <=2 failures per volume; no transfer.',
      'Ledger rows for all 40 logical echo cells; first valid attempt of each is the scored one.',
      source=[S(NL, 276, "echo_ok=all(len(rs)==2 and all(r.get('acoustic_passed')")],
      plan_keys=['gates.false_turns.minimum_core_echo_per_volume', 'nova_live.echo_counts', 'volumes_db',
                 'gated_volume_db']),
    G('echo.event_coverage', 'INVALID-CELL', ECHO,
      'Credit needs event coverage from reply start through >= 2 s of declared post-reply quiet, with matching peer/context identity and complete recorder coverage (no gap, truncation or rotation); zero rows alone are not coverage.',
      'Event schema frames present (VAD/UserStarted/Interruption, frame_id, bot_active) for the cell window.',
      source=[S(CA, 418, "row['turn_events']=turn_events(own)")],
      invalidates=('events',), plan_keys=['nova_live.event_schema.version', 'nova_live.event_schema.files', 'nova_live.event_schema.required']),
    G('echo.unscorable', 'INVALID-CELL', ECHO,
      'Applicable echo endpoint unscorable (playback extent, incomplete delayed tail, no active frames, unknown gate frames, no trustworthy playback extent) -> invalid, never a pass; a transport fault alone never makes a measurable echo unscorable.',
      'Analyzer reason string; playback present (else playback.missing).',
      source=[S(CA, 120, "return dict(passed=False,reason='Unscorable playback extent')")]),
    G('echo.zero_db_floor', 'MONITOR', ECHO + PART,
      'Decision 72: the -58 dBFS full-band part-baseline screen is recorded only. Both volumes use the same noise/clip validity gates; the benchmark is room-inclusive (thresholds judged as heard, room included).',
      'Part baseline full-band level.', new=True),
    G('echo.floor_record', 'MONITOR', ECHO + PART,
      'Raw MIC1 and pre-guard AFE full band (and HPF) for each part baseline and each cell pre/post quiet window; drift by volume. Recorded with every verdict.',
      'Baseline and quiet-window records.', new=True),
    G('echo.residual_report', 'MONITOR', ECHO,
      'Median frame reduction, whole/first500/post-guard residuals, max-20 ms residual after every re-onset, guard reports.',
      'Analyzer output.', source=[S(CA, 154, "result['reonsets'].append(dict(source_sample=int(at)*320")],
      plan_keys=['gates.echo.median_frame_reduction', 'gates.echo.afe_residual', 'gates.guard.report']),
    G('echo.median_by_volume', 'MONITOR', ECHO,
      'Median whole-reply reduction and post-guard residual per volume; per-text failures and double-replicate failures.',
      'Scored ledger rows.', new=True),
    G('echo.floor_relative', 'MONITOR', ECHO,
      'Floor-relative echo metrics (report-only module, asymmetric verdict); never a gate before live-noise validation.',
      'Retained captures.', source=[S('hil/echoear_aec_floor_relative.py', 441, 'def proposed_verdict')]),
    G('echo.quick_gap', 'MONITOR', ECHO,
      'Quick-gap witness classification (no v9 quick-gap pairs are scheduled).',
      'Independent MIC3 gap.', source=[S(CA, 531, 'def quick_gap(gap):')], plan_keys=['gates.quick_gap']),
    # ---- Barge-in --------------------------------------------------------------------------------
    G('barge.recognition', 'SCORE', BARGE,
      'Stop: normalized transcript contains the whole word "stop"; sentence: WER <= 0.20 (unchanged). Ordinary STT misses stay scored.',
      'Final transcript events for the cell.',
      source=[S(AA, 150, "row['recognized']=bool(re.search(r'\\bstop\\b',text,re.I))")],
      plan_keys=['gates.barge_in.stop_recognition', 'gates.barge_in.sentence_wer_max', 'measurement_contract.stt']),
    G('barge.credited', 'SCORE', BARGE,
      'Credited barge-in = recognition AND accepted interruption after near onset AND eligible/lull opportunity with verified future speech.',
      'Opportunity class, InterruptionFrame after near onset, future-speech witness.',
      source=[S(AA, 169, "row['credited_barge_in']=bool(opportunity_eligible(row['opportunity'],lull_policy)")],
      plan_keys=['gates.barge_in.credited']),
    G('barge.latency', 'SCORE', BARGE,
      'Near onset to accepted interruption, conservative upper bound: <= 1.2 s Stop, <= 2.0 s sentence (unchanged).',
      'Mapped near onset with +-320 sample uncertainty; missing bound = fail.',
      source=[S(CA, 433, "latency=upper(row.get('near_onset_to_interruption'))")],
      plan_keys=['gates.barge_in.near_onset_to_accepted_interruption_upper_max_s_by_kind', 'timing.latency_verdict',
                 'timing.near_onset_uncertainty_samples']),
    G('barge.group_bar', 'SCORE', BARGE,
      'Three -6 dB groups (firm Stop, firm sentence, normal sentence): 12 scheduled, >=10 eligible, >=9 successes among the first 10 eligible by frozen logical rank (not acquisition order); membership is final only when every lower rank is resolved; early FAIL only when two misses are in the final ten under every legal completion.',
      'Ledger in declared order; opportunity classes per attempt.',
      source=[S(NL, 271, "passed=len(selected)==10 and success>=9")],
      plan_keys=['gates.barge_in.stop_successes_min_at_gated_volume', 'gates.barge_in.sentence_successes_min_at_gated_volume',
                 'gates.barge_in.eligible_min_at_gated_volume_per_kind', 'gates.barge_in.gated_voice_classes',
                 'gates.barge_in.voice_class_rule', 'gates.barge_in.gated_voice_kind_pairs', 'gates.barge_in.gated_volumes_db',
                 'opportunity.required_eligible_per_volume_per_kind', 'opportunity.successes_required_per_volume_per_kind',
                 'opportunity.denominator_rule']),
    G('barge.eligibility', 'SCORE', BARGE,
      'Eligible / ongoing-reply-lull opportunity definitions and near-match rule (abs(r) >= 0.25, >= 1.1x competing peak), unchanged.',
      'Fixed-delay correlation, guard open over onset uncertainty, active MIC3, >=100 ms future speech.',
      source=[S(AA, 23, 'return dict(valid=peak>=.25 and peak>=1.1*alternative')],
      plan_keys=['opportunity.eligible', 'opportunity.near_match', 'opportunity.ongoing_reply_lull',
                 'ongoing_reply_lull.rule', 'ongoing_reply_lull.span_max_frames', 'ongoing_reply_lull.horizon_after_latest_ms',
                 'ongoing_reply_lull.alignment_origin_tolerance_samples']),
    G('barge.no_opportunity', 'SCORE', BARGE,
      'Witnessed MIC3 silence over the onset uncertainty: scheduled but not eligible; no refill (the two extra slots absorb it).',
      'Complete evidence with witnessed silence; partial coverage is barge.unscorable.',
      source=[S(AA, 42, "if mic3 == 'silent':return 'no-opportunity'")],
      plan_keys=['opportunity.no_opportunity', 'opportunity.expected_no_opportunity'],
      score_effect='Not eligible; reduces the eligible count; no refill.'),
    G('barge.in_window', 'INVALID-CELL', BARGE,
      'Guard-closed overlap of the onset uncertainty (in-window) -> invalid with refill (v8.7: unresolved, failed the session).',
      'Gate state at near onset from the GATE log.',
      source=[S(AA, 136, "row['in_window']=gate_at_near(serial,blocks,onset)")], plan_keys=['opportunity.in_window']),
    G('barge.unscorable', 'INVALID-CELL', BARGE,
      'Opportunity unscorable (missing witness, <100 ms verified future speech) -> invalid with refill.',
      'Analyzer opportunity class unscorable-*.',
      source=[S(CP, 181, "opportunity.startswith('unscorable-'))")],
      plan_keys=['opportunity.future_failure', 'opportunity.expected_unscorable', 'opportunity.allowed_unscorable']),
    G('barge.cancelled_lull', 'INVALID-CELL', BARGE,
      'Lull cancelled after the stimulus: if an eligible ongoing reply is independently established (pre-near source and retained future speech), the cell is scored as eligible (barge.eligibility); only when opportunity cannot be established -> invalid with refill. Counts reported separately.',
      'Lull cancellation witness plus independent pre-near/future-speech evidence.',
      source=[S('hil/echoear_aec_lull.py', 54, 'return dict(passed=not ids, expected=0')],
      plan_keys=['ongoing_reply_lull.cancelled_expected']),
    G('barge.undelivered', 'INVALID-CELL', BARGE,
      'Stimulus not delivered: no fresh active TTS at the declared deadline, no non-silent transport anchor, or stimulus precondition failure (v8.7: stopped c09/c10).',
      'Timer record with skip reason; transport-write events.',
      source=[S('hil/echoear_aec_stimulus.py', 107, "value.update(skipped=True, reason='no active TTS at declared deadline')"),
              S(CP, 125, "if isinstance(exc,StimulusPreconditionFailure):return 'stimulus-precondition'")],
      plan_keys=['opportunity.allowed_undelivered', 'require_active_tts', 'active_tts_max_age_s'],
      raises=[('hil/echoear_aec_stimulus.py', 'No non-silent transport anchor before cancellation/deadline')]),
    G('flush.callback', 'SCORE', BARGE,
      'Flush: callback to last old MIC3 <= 100 ms; server interruption to silence <= 250 ms; pre-callback active tail within 300 ms (unchanged).',
      'AUDIO_CANCEL callback, mapped MIC3 blocks, callback witness v2.',
      source=[S(CA, 206, "return dict(passed=bool(witness['passed'] and direct[1]<=.1")],
      plan_keys=['gates.flush.callback_to_last_mic3_upper_max_s', 'gates.flush.server_interruption_to_last_mic3_upper_max_s',
                 'gates.flush.window', 'gates.flush.callback_witness', 'gates.flush.pre_callback_activity_window_ms']),
    G('flush.next_reply', 'SCORE', BARGE,
      'After an accepted interruption the next non-silent TTS reaches MIC3 within 1 s (unchanged).',
      'Second playback witness for the cell.',
      source=[S(CA, 440, "if row.get('interruption_after_stimulus'):row['playback_passed'] &= len(playbacks)>=2")],
      plan_keys=['gates.flush.next_non_silent_tts_to_mic3_upper_max_s']),
    G('flush.interrupt_copies', 'SCORE', BARGE,
      'Both interrupt copies observed in the SCTP ring (part of flush pass). Scored only with healthy observation; observer failure is observer.health, not a missing-copy miss.',
      'SCTP ring with TSNs and first-send ACK.',
      source=[S(CA, 431, "row['flush']['copies_observed']=len(row['interrupt_copies'])==2")],
      plan_keys=['gates.admission_liveness_sctp.interrupt_delivery']),
    G('flush.stale_audio', 'CANDIDATE-FAIL', BARGE,
      'Stale old-reply speech frames after mute: 0.',
      'Blocks of the old reference after control_us + 150 ms.',
      source=[S(CA, 200, "stale=int(sum(b[2]>cb['control_us']+150000")],
      plan_keys=['gates.flush.stale_speech_frames_after_mute_max']),
    G('flush.diagnostics', 'MONITOR', BARGE,
      'Prefix loss, cut-step spike, signed correlation, sentence overlap duration: report-only.',
      'Analyzer output.', source=[S(CA, 211, 'cutoff_step_diagnostic=dict(max_lsb=cut')],
      plan_keys=['gates.flush.new_onset_integrity', 'gates.flush.new_prefix_loss_upper_max_ms', 'gates.flush.cutoff_spike',
                 'gates.flush.diagnostic_only', 'gates.sentence_overlap']),
    G('report_only.exposures', 'MONITOR', REPORT,
      'Normal-voice Stop, host-interrupt, sentence-split regression and 0 dB barge exposures: report-only, no spares and no acoustic credit; candidate faults and false events still apply.',
      'Their rows.', source=[S(NL, 285, "report_only_exposures=[r for r in rows if r.get('report_only')]")],
      plan_keys=['gates.barge_in.reported_volumes_db', 'gates.barge_in.reported_exposure_per_kind_at_0db',
                 'nova_live.normal_stop_report_only', 'nova_live.host_interrupt_report_only',
                 'nova_live.sentence_split_regression', 'nova_live.sentence04_regression']),
    G('near.preservation', 'MONITOR', WITNESS,
      'Near-end preservation (<=1 dB raw MIC1 vs pre-gate AFE): report-only, as in v8.7 Nova.',
      'Near-only cells.', source=[S(CA, 424, "row['near']=near_preservation(c,raw,afe,ref,qref)")],
      plan_keys=['gates.near_preservation', 'nova_live.preservation']),
    G('diagnostics.report', 'MONITOR', CELLS,
      '100 Hz high-pass residual and stimulus-level diagnostics; never gates.', 'Analyzer output.',
      source=[S(CA, 135, "result['residual_diagnostic_filter']=dict(V3_FILTER")], plan_keys=['diagnostics']),
    # ---- Blocks, alignment, parts, caps ---------------------------------------------------------------
    G('alignment.near_witness', 'PAUSE-PART', WITNESS,
      'Near-only alignment witnesses at start and end of each block: each offset within 1024 +- 20 samples and mutual spread <= 20 samples, measured by the v9.8 witness estimator (fixture match, polarity-free envelope delay and relative AFE-vs-raw delay must all agree within +-20 inside 1004-1044; a half-pitch opposite-polarity alias is accepted only with its own checks; the relative estimate carries a declared 1 % ambiguity margin; anything unresolved is ambiguous). The fixed 512+512-sample framer/AFE delay makes 1024 the expectation, not a substitute for the witness. v9.10: an invalid witness (noise-contaminated or ambiguous/out-of-tolerance) is retaken up to 2 more times per position while it fits the part budget and admission; PAUSE-PART after the third. Failure leaves the block unclosed: its acoustic endpoints lose credit, faults and false events stay.',
      'Witness match offsets; boot banner AEC_GUARD_CONFIG.',
      source=[S(CP, 441, 'AEC_GUARD_CONFIG guard_ms=400 afe_delay_samples=1024 rearm_ms=2000')],
      plan_keys=['timing.afe_delay_samples', 'timing.afe_configuration', 'guard_ms', 'rearm_ms']),
    G('parts.block_closure', 'PAUSE-PART', PART,
      'Block = <= 5 acquired scored-cell attempts between two witnesses. A block opens only if all its attempts and the closing witness fit the active deadline and the 210 s start rule; otherwise it stays unattempted. Only closed blocks earn acoustic credit; each invalid attempt is counted once.',
      'Ledger block records.', source=[S(CA, 524, "passed &= saved.get('host_environment',{}).get('passed') is True and not saved.get('error_class')")],
      terms=['hold:error_class']),
    G('caps.invalid_attempts', 'PAUSE-PART', CAMPAIGN,
      'Caps: <=2 acquired attempts per logical cell, <=8 invalid acquired scored-cell attempts per campaign (each counted once, including unclosed-block attempts), 3 consecutive invalid -> PAUSE-PART; unopened blocks are not attempts; exhausted -> INCONCLUSIVE.',
      'Hash-chained attempt ledger.',
      source=[S('hil/echoear_aec_noise_gate.py', 124, "if len(self.noisy_cells) >= self.policy['pause_consecutive_cells']:")],
      plan_keys=['noise_gate.max_retries', 'noise_gate.pause_consecutive_cells', 'failure_policy.retakes',
                 'failure_policy.harness_retries.classes', 'failure_policy.harness_retries.shared_noise_budget',
                 'attempts_per_cell'],
      raises=[(CP, 'Three consecutive noise/harness cells; new declaration required'),
              (CP, 'Insufficient clean/scorable denominator')]),
    G('parts.time_caps', 'PAUSE-PART', PART,
      'At most 3 entered parts (2 planned + 1 recovery), planned parts each <=60 min incl. its own 6 min restore reserve, the recovery (last) part up to the ceiling left (v9.6), 150 min ceiling over part wall time; capacity is the remaining campaign wall time (v9.5/v9.6); >=210 s active time to start a cell.',
      'Part monotonic clocks; restore reserve.',
      source=[S(NL, 295, "if budget.remaining()<self.p['budget']['minimum_cell_remaining_s']:"),
              S(CA, 609, "summary['budget_passed']=bool(elapsed.get('completed')")],
      plan_keys=['budget'],
      raises=[(NL, 'Insufficient active time for next cell; restore'), (NL, 'Total cap expired; no pass'),
              (NP, 'No active time left for export; restore now'), (NP, 'Total cap elapsed; restored, no pass')]),
    G('parts.ledger', 'PAUSE-PART', CAMPAIGN,
      'Resume integrity: hash-chained ledger, same image/profile/plan/runtime hashes (except an approved analyzer-only provenance amendment recorded in the ledger), fresh boot, no attempt reuse. Failure refuses resume -> INCONCLUSIVE.',
      'Ledger chain, prior restore witness.',
      source=[S('hil/echoear_aec_campaign_resume.py', 41, "raise ValueError('Hard prior hold failure cannot be retried')")],
      plan_keys=['failure_policy.serial_host_resume', 'failure_policy.harness_retries.resume', 'v85.resume_allowed']),
    G('outcome.completeness', 'SCORE', CAMPAIGN,
      'All required gates conjunctive; a missing required scored cell or metric at campaign end -> INCONCLUSIVE (not PASS).',
      'Ledger.', source=[S(NL, 279, 'complete=not required-seen and len(unique)==len(set(unique))')],
      plan_keys=['failure_policy.all_required_gates_conjunctive', 'failure_policy.allowed_missing_cells',
                 'failure_policy.allowed_missing_required_metrics', 'failure_policy.performance_failures'],
      score_effect='Missing support (unresolved or exhausted cells) -> INCONCLUSIVE naming them; a measured miss is never missing support.'),
    G('launch.integrity', 'PAUSE-PART', PART,
      'Freeze/launch preconditions: plan, runtime, profile, package, fixture, image and boot-gain hashes; USER-GO; source preflight. Refuse to start or continue a part.',
      'Validation exception text.',
      source=[S(NL, 167, "if {k:v for k,v in p.items() if k not in extras} != plan_from(variant):")],
      plan_keys=['source_preflight_contract', 'preflight_contract.real_reply', 'nova_live.scope',
                 'nova_live.measured_bot_preflight', 'nova_live.implementation_ready', 'nova_live.review_required',
                 'nova_live.launch_authorized', 'nova_live.installed_packages_sha256', 'nova_live.safety_budget',
                 'gain_db', 'boot_ref_gain_db', 'v85.rule'],
      raises=[(CP, 'SCTP patch not enabled'),
              ('hil/echoear_aec_stimulus.py', 'Active TTS freshness must be declared within 200 ms'),
              ('hil/echoear_aec_stimulus.py', 'Declared near-end offset must be 0..3 seconds'),
              ('hil/echoear_aec_stimulus.py', 'Unreviewed active-speech window'),
              ('hil/echoear_aec_stimulus.py', 'Guard must be integer 0..1000 ms')],
      control_action='Refuse to start the part; nothing scored. A fix needing a code/plan change is a new freeze '
                     '(mid-campaign: INCONCLUSIVE).'),
    G('freeze.admission_regression', 'PAUSE-PART', CAMPAIGN,
      'aec-v87 host regressions pass at freeze: 20- and 64-message bursts and byte-budget exhaustion, ordered exactly-once dispatch, admission_overflow observable.',
      'Host test log bound to the candidate commit.', new=True,
      control_action='Withhold launch; not an acoustic or candidate result.'),
    G('freeze.burst_smoke', 'PAUSE-PART', CAMPAIGN,
      'Decision 71 smoke burst: admission_overflow == 0, live quiescent reconciliation, deferred delta > 0 and ordered exactly-once APPLICATION receipt of burst IDs; deferred == 0 is coverage not achieved (bounded, predeclared burst attempts).',
      'Smoke ledger rows and serial/recorder burst records.', new=True,
      control_action='Withhold launch; an unexercised or failed smoke is not acoustic credit or a retrospective candidate failure.'),
    G('freeze.lifecycle_test', 'PAUSE-PART', CAMPAIGN,
      'Host lifecycle test of the frozen schedule through the real hold classes with fault injection and resume.',
      'Test log bound to the runner hash.', new=True, control_action='Withhold launch.'),
    G('freeze.analyzer_dry_run', 'PAUSE-PART', CAMPAIGN,
      'v9 scorer completes over retained campaign-10, nova-live-02 and arms-04 evidence without exception.',
      'Dry-run output bound to the analyzer hash.', new=True, control_action='Withhold launch.'),
    G('freeze.scoring_cost', 'PAUSE-PART', CAMPAIGN,
      'Measured per-attempt scoring + ledger append/checkpoint cost <= 2 s and independent of ledger length.',
      'Benchmark over retained captures and synthetic ledgers of campaign length.', new=True,
      control_action='Withhold launch; re-derive the schedule if exceeded.'),
    G('operator.stop', 'PAUSE-PART', PART, 'Operator Ctrl-C/TERM: restore and close the part.',
      'Signal record.', source=[S(CP, 1052, "def interrupted(signum,frame):raise KeyboardInterrupt('Stop requested')")],
      raises=[(CP, 'Stop requested')]),
    G('unknown.unclassified', 'PAUSE-PART', CELLS + PART,
      'Any exception or failed check not mapped to another entry (v8.7 default: device-fault session stop).',
      'Exception text retained; classification by review before resume.',
      source=[S(CP, 131, "return 'device-fault'")]),
    G('restore.integrity', 'PAUSE-PART', PART,
      'Normal ELF restored and boot-verified, mixer byte-equal, lock released, no cleanup errors; failure parks with the lock.',
      'restore-image.json, mixer states, lock record.',
      source=[S(CP, 927, "print('RESTORE/CLEANUP FAILED: retaining bench lock',flush=True)")],
      plan_keys=['cleanup'], raises=[(NL, 'Cleanup incomplete')],
      terms=['hold:restored_elf_sha256', 'hold:mixer_equal', 'hold:lock_released', 'hold:cleanup_errors'],
      control_action='Park with the lock; operator restores; next part only after a verified restore.'),
    G('host.serial_loss', 'PAUSE-PART', PART,
      'Serial/host failure without device-reboot evidence (USB, host OSError).',
      'No boot banner or reset reason in candidate-serial.log; device_us continuous on reconnect.',
      source=[S(CP, 127, "if isinstance(exc,(serial.SerialException,KeyboardInterrupt)):return 'serial-or-host'")]),
    G('device.reboot_crash', 'CANDIDATE-FAIL', CELLS + PART,
      'Candidate reboot, panic, watchdog or crash during a part, including during setup before connect (v9.7/v9.8: scanned across the candidate boot/setup/live interval before any setup-only exemption; only rst:0x15 USB_UART_CHIP_RESET as the first log line after the ROM banner is the expected host-triggered boot; any other reset reason, a reset after candidate output, or panic text is a crash; AEC_GUARD_CONFIG lines are not boots; the restore boot is in restore-serial.log).',
      'Boot banner/reset reason mid-part, device_us regression, or panic text in candidate-serial.log.', new=True),
    G('bench.audio_device', 'PAUSE-PART', PART, 'Jabra not ALSA card 0 / mixer unavailable.',
      'ALSA query.', source=[S('hil/echoear_aec_bench.py', 281, "raise RuntimeError('Jabra is not ALSA card 0')")],
      raises=[('hil/echoear_aec_bench.py', 'Jabra is not ALSA card 0')]),
    # ---- Room / quiet -----------------------------------------------------------------------------------
    G('noise.cell_quiet_windows', 'INVALID-CELL', CELLS,
      'Noise gate v3 (100 Hz HPF) on the 2 s pre/post quiet windows: <= -55 dBFS, rise <= +6 dB over max(baseline, -61), transient <= -46 dBFS.',
      'Quiet-window gate record per attempt.', room_witness=True, invalidates=('acoustic', 'events'),
      source=[S(CP, 663, "noise=bool(all(gates[k]['passed'] for k in ('pre','post')))")],
      plan_keys=['noise_gate.version', 'noise_gate.level_max_dbfs', 'noise_gate.level_rise_max_db',
                 'noise_gate.transient_max_dbfs', 'noise_gate.frame_ms', 'noise_gate.quiet_seconds', 'noise_gate.policy',
                 'noise_gate.filter', 'noise_gate.rise_reference_min_dbfs']),
    G('noise.part_baseline', 'PAUSE-PART', PART,
      'Settled part baseline <= -53 dBFS (HPF) within 3 attempts, else the part cannot start.',
      'baseline-N/noise-gate.json.', source=[S(CP, 472, "raise RuntimeError('Environmental pause: no clean settled baseline')")],
      plan_keys=['noise_gate.baseline_max_dbfs', 'noise_gate.baseline_seconds', 'noise_gate.settle_seconds',
                 'noise_gate.baseline_max_attempts'],
      raises=[(CP, 'Environmental pause: no clean settled baseline')]),
    G('quiet.board_silent', 'PAUSE-PART', CELLS,
      'MIC3 active in a pre/post quiet window (device playing outside the cell) or a gap block in the quiet window: cause unknown.',
      'MIC3 frame power vs quiet reference.', source=[S(CP, 661, "gates['board_silent']=all(")],
      raises=[(CP, 'Invalid capture/quiet windows; acquisition failure')]),
    G('clip.mic_room', 'INVALID-CELL', CELLS,
      'MIC ADC/gain clip witnessed as room sound (diag-02-r4 precedent).',
      'Time-local: the clipped raw MIC1 samples matched to the counter interval lie outside every declared stimulus extent, or inside a failed quiet window that contains them; a failed quiet window elsewhere does not attribute the clip.',
      source=[S(RS, 59, "('aec', 'capture', k) for k in ('read_errors', 'read_timeouts', 'adc_clips', 'gain_clips')")],
      room_witness=True, invalidates=('acoustic', 'events'),
      plan_keys=['gates.hardware.absolute_zero[aec.capture.adc_clips]', 'gates.hardware.absolute_zero[aec.capture.gain_clips]'],
      terms=['resources:aec.capture.adc_clips', 'resources:aec.capture.gain_clips']),
    G('clip.mic_stimulus', 'CANDIDATE-FAIL', CELLS,
      'MIC clip inside a declared stimulus extent at declared level with clean quiet windows (gain/level configuration fault).',
      'Positive causation: clipped samples coincide with the declared, correctly leveled stimulus waveform (fixture or MIC3-aligned echo) at its calibrated level; clean bookend quiet windows alone are insufficient.', new=True),
    G('clip.mic_unattributed', 'PAUSE-PART', CELLS,
      'MIC clip with neither a room witness nor a stimulus attribution.', 'Neither time-local room attribution nor positive stimulus causation (e.g. a clip inside the stimulus with no independent evidence).', new=True),
    G('clip.afe_output', 'SCORE', SCORED,
      'AFE output clip under a declared stimulus: the cell fails (scored miss).',
      'aec.output_clips delta inside the cell window with a declared stimulus; outside any stimulus -> unknown.unclassified.',
      plan_keys=['gates.hardware.absolute_zero[aec.output_clips]'], terms=['resources:aec.output_clips'],
      source=[S(RS, 58, "('aec', 'reference_clips'), ('aec', 'output_clips')]+[")]),
    G('clip.reference', 'SCORE', SCORED,
      'Verified level/config clip of the actual declared playback with intact reference mapping: the cell fails (ordinary miss). Clipping outside a scored cell, broken mapping or missing integrity evidence -> reference.health / unknown.unclassified.',
      'aec.reference_clips delta inside the cell window.', plan_keys=['gates.hardware.absolute_zero[aec.reference_clips]'],
      terms=['resources:aec.reference_clips'], source=[S(RS, 58, "('aec', 'reference_clips'), ('aec', 'output_clips')]+[")]),
    # ---- Device hardware / resources (CANDIDATE-FAIL) -------------------------------------------------------
    G('hardware.aec_errors', 'CANDIDATE-FAIL', CELLS + PART, 'aec.errors = 0.', 'PERF counter delta (valid observations; missing or malformed data -> resources.coverage).',
      source=[S(RS, 57, "('console_output', 'transport_drops'), ('aec', 'errors'),")],
      plan_keys=['gates.hardware.absolute_zero[aec.errors]'], terms=['resources:aec.errors'],
      raises=[(CP, 'Probe hardware fault')]),
    G('hardware.audio_io', 'CANDIDATE-FAIL', CELLS + PART,
      'I2S/audio/capture faults = 0: capture read errors/timeouts, audio drops/read/write errors/timeouts, PCM queue drops, I2S underflow/write errors/timeouts, live rx_overflows, capture gap blocks beyond one.',
      'PERF counter deltas (valid observations; missing or malformed data -> resources.coverage).', source=[S('hil/echoear_aec_accept.py', 42, "for key in ('i2s_underflows','i2s_write_errors','i2s_write_timeouts'):")],
      plan_keys=['gates.hardware.absolute_zero[aec.capture.read_errors]', 'gates.hardware.absolute_zero[aec.capture.read_timeouts]',
                 'gates.hardware.absolute_zero[audio.drops]', 'gates.hardware.absolute_zero[audio.read_errors]',
                 'gates.hardware.absolute_zero[audio.write_errors]', 'gates.hardware.absolute_zero[audio.write_timeouts]',
                 'gates.hardware.absolute_zero[pcm_queue_drops]', 'gates.hardware.absolute_zero[i2s_underflows]',
                 'gates.hardware.absolute_zero[i2s_write_errors]', 'gates.hardware.absolute_zero[i2s_write_timeouts]',
                 'gates.hardware.live_delta_zero[aec.capture.rx_overflows]', 'gates.hardware.abort'],
      raises=[('hil/echoear_aec_accept.py', 'Whole-session ')],
      terms=['resources:aec.capture.read_errors', 'resources:aec.capture.read_timeouts', 'resources:audio.drops',
             'resources:audio.read_errors', 'resources:audio.write_errors', 'resources:audio.write_timeouts',
             'resources:pcm_queue_drops', 'resources:i2s_underflows', 'resources:i2s_write_errors',
             'resources:i2s_write_timeouts', 'resources:live_rx_overflows']),
    G('hardware.startup_rx', 'MONITOR', PART, 'Startup rx_overflows before Ready: retained separately.',
      'connected.json.', source=[S(RS, 137, "checks['live_rx_overflows'] = dict(value=delta, startup=initial['rx_overflows']")],
      plan_keys=['gates.hardware.startup_rx_overflows']),
    G('hardware.console', 'PAUSE-PART', CELLS + PART,
      'Console output queue full / transport drops (PERF/evidence channel loss; attribution harness load vs firmware unknown).',
      'PERF counters.', source=[S(RS, 57, "('console_output', 'transport_drops'), ('aec', 'errors'),")],
      plan_keys=['gates.hardware.absolute_zero[console_output.full]', 'gates.hardware.absolute_zero[console_output.transport_drops]'],
      terms=['resources:console_output.full', 'resources:console_output.transport_drops']),
    G('hardware.render_ledger', 'CANDIDATE-FAIL', CELLS + PART,
      'Render ledger submitted - completed == 640 - completed % 320 at every active snapshot.',
      'Boundary PERF snapshots (valid observations; missing or malformed data -> resources.coverage).', source=[S(CA, 409, "row['ledger']=check_render_ledger(")],
      plan_keys=['gates.hardware.render_ledger', 'gates.hardware.ledger_basis'],
      terms=['integrity:ledger', 'epoch:render_ledger']),
    G('hardware.uplink_frame', 'CANDIDATE-FAIL', PART, 'Uplink frame 20 ms in every session snapshot (identity).',
      'PERF uplink_frame_ms (valid observations; missing or malformed data -> resources.coverage).', source=[S(RS, 70, "add('uplink_frame_ms', [p.get('uplink_frame_ms') for p in sessions],")],
      plan_keys=['gates.hardware.uplink_frame_ms'], terms=['resources:uplink_frame_ms']),
    G('resources.heap', 'CANDIDATE-FAIL', CELLS + PART, 'Internal heap minimum >= 81920 B and largest block >= 32768 B.',
      'PERF heap (valid observations; missing or malformed data -> resources.coverage).', source=[S(RS, 27, "for key, floor in [('minimum_free', 81920), ('largest', 32768)]:")],
      plan_keys=['gates.resources.internal_minimum_free_bytes', 'gates.resources.internal_largest_block_bytes',
                 'gates.resources.soak_margin'],
      terms=['resources:internal_minimum_free', 'resources:internal_largest']),
    G('resources.failed_alloc', 'CANDIDATE-FAIL', CELLS + PART, 'Failed allocations = 0.', 'PERF failed_allocations.total (valid observations; missing or malformed data -> resources.coverage).',
      source=[S(RS, 53, "for path in [('failed_allocations', 'total'), ('pcm_queue_drops',),")],
      plan_keys=['gates.resources.failed_allocations_max', 'gates.hardware.absolute_zero[failed_allocations.total]'],
      terms=['resources:failed_allocations.total']),
    G('resources.stacks', 'CANDIDATE-FAIL', CELLS + PART,
      'Stack floors: playback max(4096, stack/4) (4288), capture/tx/peer-loop 4096, peer-rx 1024, app-control 2048.',
      'PERF tasks (valid observations; missing or malformed data -> resources.coverage).', source=[S(RS, 8, "STACK_FLOORS = {'audio-playback': 4096, 'audio-capture': 4096,")],
      plan_keys=['gates.resources.stack_free_floor_bytes', 'gates.resources.playback_floor_formula'],
      terms=['resources:stack_audio-playback', 'resources:stack_audio-capture', 'resources:stack_audio-tx',
             'resources:stack_peer-loop', 'resources:stack_peer-rx', 'resources:stack_app-control']),
    G('resources.cpu', 'CANDIDATE-FAIL', CELLS + PART,
      'Measured idle < 25 % per core in a qualifying >= 5 s steady window blocks resource PASS; < 20 % is the immediate stop. No qualifying window -> resources.coverage, not a low-idle result.',
      'PERF steady windows.', source=[S(RS, 50, "add(f'core_{core}_steady_idle_percent', value, valid and value >= 25,")],
      plan_keys=['gates.resources.steady_window_min_us', 'gates.resources.steady_idle_min_percent_per_core',
                 'gates.resources.absolute_idle_min_percent_per_core', 'gates.resources.cpu_rule'],
      terms=['resources:core_0_steady_idle_percent', 'resources:core_1_steady_idle_percent']),
    G('resources.encode', 'CANDIDATE-FAIL', CELLS + PART, 'Opus encode p95 < 20 ms, max < 40 ms.', 'PERF encode stats (valid observations; missing or malformed data -> resources.coverage).',
      source=[S(RS, 37, "for key, limit in [('encode_p95_us', 20000), ('encode_max_us', 40000)]:")],
      plan_keys=['gates.resources.encode_p95_us_strict_max', 'gates.resources.encode_max_us_strict_max'],
      terms=['resources:encode_p95_us', 'resources:encode_max_us']),
    G('resources.display', 'CANDIDATE-FAIL', PART,
      'Character enabled, posts increase, display errors/DMA errors/send failures/gate timeouts = 0; asset preload succeeds.',
      'display-end.json, character-start/end.json (valid observations; missing or malformed data -> resources.coverage).', source=[S(RS, 142, "checks['display'] = dict(details=d, passed=not d['dead'] and not d['failed'] and")],
      plan_keys=['gates.resources.display'], terms=['resources:cat', 'resources:display'],
      raises=[(CP, 'Character preload failed')]),
    G('resources.coverage', 'PAUSE-PART', CELLS + PART,
      'PERF snapshots, fields and steady windows present for the interval; missing, malformed or reset counters are coverage/identity failures, never a measured resource breach and never a pass.',
      'Snapshot files.', source=[S(RS, 18, "add('snapshots_present', len(snapshots), bool(snapshots))")],
      plan_keys=['gates.resources.sampling', 'gates.resources.basis'],
      terms=['resources:snapshots_present', 'resources:session_snapshots_present']),
    G('resources.part_audit', 'PAUSE-PART', PART,
      'Legacy hold-level composite audit (all snapshots, exception audit, final integrity, completed flag). Dispatches to its failing leaf gates (each keeps its own class, e.g. an AFE clip stays SCORE, a room clip INVALID-CELL); an unresolved composite or completed == false without a leaf (operator/host interruption) -> PAUSE-PART. v9.11/v9.12: after a positive (hardened) peer-loss witness the bot-side final stages are not applicable and the unacknowledged evaluation control is attributed to the classified transport exit; the device-side stages must still record their witnesses; device leaves are audited over every observation incl. reconnected generations; PENDING USER CONFIRMATION: only send_cutoff/send_budget_cutoffs in the exit interval of the failed acquisition are exit-associated, every other internal loss stays CANDIDATE-FAIL. v9.7: completed == false after an exception resolves only when every applicable final-audit stage has its completion witness (resources, exception integrity audit, disconnected snapshot, SCTP recovery) and there is no finish_error. v9.8/v9.9: every applicable counter and resource leaf is reconciled through the terminal disconnected snapshot and dispatched to its gate before completion is resolved (absolute-zero counters, heap floors, rx_overflows as a live delta from connected, drop_reasons/admission_overflow deltas -> admission.control_loss, console drops -> hardware.console, clip residuals); session presence, CPU windows, encode activity and uplink-frame identity do not apply after teardown. A setup-only part (no live peer) audits measured, applicable snapshot leaves only (heap, largest block, allocation, stack of existing tasks, hardware counters); session leaves are not applicable.',
      'Leaf check results of check_directory / exception audit.', aggregate=True,
      source=[S(CP, 850, "raise RuntimeError('Exception final resource/liveness audit failed')")],
      raises=[(CP, 'Exception final resource/liveness audit failed'), (NL, 'Final integrity audit failed')],
      terms=['hold:completed', 'hold:resources', 'integrity:resources']),
    # ---- Admission / delivery / transport / liveness --------------------------------------------------
    G('admission.control_loss', 'CANDIDATE-FAIL', CELLS + PART,
      'Proven control-message loss after the aec-v87 fix: admission_overflow delta > 0, admission/rejection/queue drop deltas > 0 (v9.10: the received-but-permanently-undispatched branch is withdrawn: the aec-v87 firmware has no device-side witness of it, so a permanent shortfall is admission.drain_timeout); recorder count must not exceed device deltas once transport delivery is resolved (v9.3: host evidence never resolves it, so a recorder-vs-device shortfall alone is unknown.unclassified / PAUSE-PART; CANDIDATE-FAIL comes only from device-side positive witnesses, incl. a duplicate or misordered application receipt).',
      'data_admission (received, dispatched, deferred, admission_overflow) and drop_reasons.* deltas in one peer epoch; recorder RTVI count.',
      source=[S(CP, 678, 'admission=live_admission(before,after,recorder_messages=outgoing_rtvi_count(')],
      plan_keys=['gates.admission_liveness_sctp.received_equals_dispatched', 'gates.admission_liveness_sctp.admission_budget_delta_max',
                 'gates.admission_liveness_sctp.rejection_deltas_max',
                 'gates.admission_liveness_sctp.per_reply_device_received_and_dispatched_min',
                 'failure_policy.harness_retry_admission'],
      terms=['resources:data_admission_counters_only', 'integrity:admission']),
    G('admission.drain_timeout', 'PAUSE-PART', CELLS + PART,
      'Live quiescent received == dispatched not reached within the drain allowance with zero drop/overflow deltas (cause unresolved). Allowance (v9.9): a fixed declared 4.0 s for smoke and acceptance, from the pooled v9 smoke boundary distribution (90 ordinary attempt boundaries, no-backlog <= 0.76 s, backlog boundaries 1.40 / 1.52 / 1.54 s; 2.6x headroom), within the 5 s receipt timeout, never changed during the campaign; smoke readiness requires every ordinary boundary <= 2.0 s and none unbounded (v9.4-v9.8: 2x the largest smoke boundary, rounded up to 250 ms, <= 3 s). v9.3: the burst last-send-to-sustained-quiescence latency is MONITOR (it must fall within the 5 s receipt timeout) because it is dominated by the device applying 40 test messages that each log two serial lines.',
      'Retained drain observations; smoke calibration record.',
      source=[S(RS, 117, "raise TimeoutError('Admission queue did not drain within polling budget')")],
      plan_keys=['gates.admission_liveness_sctp.boundary_drain_budget_s', 'gates.admission_liveness_sctp.drain_budget_start'],
      raises=[(RS, 'Admission queue did not drain within polling budget')]),
    G('admission.counter_schema', 'PAUSE-PART', CELLS + PART,
      'Admission counters (received, dispatched, deferred, admission_overflow) present with the aec-v87 schema, same peer identity, no reset inside an interval.',
      'PERF schema per snapshot.', new=True),
    G('admission.boundary_identity', 'PAUSE-PART', CELLS + PART,
      'Admission boundary invalid: peer identity changed or malformed counters during the drain poll.',
      'Retained boundary observations.', source=[S(RS, 114, "raise ValueError('Invalid admission boundary; all observations retained by reader')")],
      raises=[(RS, 'Invalid admission boundary; all observations retained by reader')]),
    G('delivery.firmware_drops', 'CANDIDATE-FAIL', CELLS + PART,
      'Proven firmware-internal loss = 0: drop_reasons.* (queues, admission, send_cutoff, uplink_gap, worker_exception), send_budget_cutoffs, live rx_overflows. Never waived by a simultaneous send_errors split. v9.12 (PENDING USER CONFIRMATION): the one exception is send_cutoff / send_budget_cutoffs inside the failed acquisition\'s exit interval after a witnessed peer loss (exit-associated, reported); every other counter stays strict.',
      'Per-counter deltas in one peer epoch.',
      plan_keys=['gates.transport.firmware_uplink_drops_max',
                 'gates.hardware.live_delta_zero[firmware uplink drops/send_cutoff/send-budget-cutoff]'],
      source=[S(CA, 346, "paths=[('send_errors',),('send_budget_cutoffs',),('aec','capture','rx_overflows')]")]),
    G('delivery.send_errors', 'PAUSE-PART', CELLS + PART,
      'send_errors delta without established cause. An independently established network-only failure follows the transport rule (barge overlap INVALID-CELL, echo MONITOR).',
      'Counter delta plus any independent network witness.', new=True),
    G('delivery.counters_dispatch', 'PAUSE-PART', CELLS + PART,
      'Legacy delivery_counters composite (send_errors + internal drops): dispatches per counter to delivery.firmware_drops / delivery.send_errors; unresolved -> PAUSE-PART.',
      'Per-counter deltas.', aggregate=True,
      source=[S(CA, 352, "return dict(passed=bool(before.get('drop_reasons')) and all(v==0 for v in delta.values()),delta=delta)")],
      terms=['integrity:delivery_counters', 'epoch:delivery']),
    G('transport.downlink_barge', 'INVALID-CELL', BARGE,
      'Downlink transport (loss <= 2 %, ge6 ended run 0, post-PLC zeros 0, starvation 0, valid histogram) overlapping the barge-in/flush stimulus window -> invalid; outside the window: no effect.',
      'Interval evidence: ordered PERF snapshots bracketing the stimulus window, same pc_id/generation; a whole-part delta is not assigned to every cell. Starvation/post-PLC zeros are playout counters, not I2S faults; a concurrent local fault keeps its own class.',
      source=[S(CA, 318, 'passed=bool(valid and present and ratio is not None and ratio<=.02 and')],
      plan_keys=['gates.transport.loss_fraction_max', 'gates.transport.loss_denominator', 'gates.transport.scope',
                 'gates.transport.ge6_ended_delta_max', 'gates.transport.post_plc_zeros_delta_max',
                 'gates.transport.starvation_delta_max', 'gates.transport.rule', 'gates.transport.loss_numerator_policy',
                 'gates.transport.histogram_required', 'gates.transport.attribution', 'gates.transport.rationale',
                 'gates.transport.max_consecutive_playout_dropped', 'v85.transport.rule',
                 'v85.transport.piecewise_received_floor', 'v85.transport.cell_ratio_max', 'v85.transport.terminal_short',
                 'v85.transport.interpretation', 'v85.capture_end_perf', 'v85.host_boundary_not_reached', 'v85.no_link_retakes'],
      terms=['integrity:transport', 'epoch:transport']),
    G('transport.downlink_echo', 'MONITOR', ECHO,
      'Downlink transport faults never invalidate echo cells (AEC reference = DAC output incl. PLC); recorded per cell.',
      'Same counters; if no usable stimulus exposure remains the echo is echo.unscorable, never PASS.', new=True),
    G('transport.burst', 'MONITOR', SCORED, 'Burst <= 3 and missing-run histogram: report-only since v8.3.', 'Histogram deltas.',
      source=[S(CA, 324, 'burst_report_only=True,histogram_present=present')],
      plan_keys=['gates.transport.burst_witness', 'gates.transport.burst_report_only']),
    G('transport.export', 'MONITOR', SCORED, 'Export-interval transport/delivery effects: report-only; never suppresses a candidate fault or capture-export evidence failure.', 'export-event.json.',
      source=[S(CP, 683, 'export=dict(transport=transport(score_end,after')],
      plan_keys=['v85.transport.export_transport_delivery']),
    G('transport.sctp_recovery', 'INVALID-CELL', BARGE,
      'Active SCTP T3 without same-pc_id clean SACK recovery overlapping a barge stimulus window -> invalid (echo: recorded only); SCTP flight patch must be enabled.',
      'SCTP ring.', source=[S(CP, 859, "self.result['sctp_recovery']=check_active_recovery(")],
      plan_keys=['gates.admission_liveness_sctp.sctp_patch'], terms=['hold:sctp_recovery']),
    G('transport.environmental_classifier', 'MONITOR', SCORED,
      'v8.4 transport-environmental classifier and caps: disabled since v8.5; superseded by transport.downlink_barge. Witnesses may be retained.',
      'Classifier witness.', source=[S(CP, 959, "raise EnvironmentalPause('Transport-environmental attempt cap exceeded; new declared hold required')")],
      plan_keys=['failure_policy.transport_environmental'],
      raises=[(CP, 'Transport-environmental attempt cap exceeded; new declared hold required')],
      terms=['harness:transport-environmental']),
    G('transport.qualification_bar', 'MONITOR', CAMPAIGN,
      'Transport qualification (separate, network-only, diag-04 form): no >=6 missing run, no zeros, loss <= 2 % over 75 min. Not an AEC gate.',
      'Separate network-only run.', new=True),
    G('capture.interval_checks', 'PAUSE-PART', CELLS,
      'v8.7 composite capture-interval stop (resources/admission/RF/delivery/transport). v9 reclassifies by the failing component; unresolved composite -> PAUSE-PART.',
      'checks record in the attempt result.', aggregate=True, source=[S(CP, 692, "if not resources['all_passed'] or not all(c['passed'] for c in (admission,rf,delivery,transport_check)):")],
      raises=[(CP, 'Capture-interval or integrity failure; v8.5 has no link retake'),
              (CP, 'Resource/admission/RF/delivery/transport failure')]),
    G('observer.health', 'INVALID-CELL', CELLS,
      'SCTP/RTP/arrival observers healthy, supported, untruncated over the attempt.',
      'rtp-observer-health events.', source=[S(CP, 176, "return bool(health) and all(e['passed'] and e['sctp']['supported']")],
      plan_keys=['gates.transport.observers'],
      raises=[(CP, 'Observer health failure'), (CP, 'Observer health failure at hold boundary')]),
    G('observer.event_stream', 'PAUSE-PART', CELLS,
      'Evaluation/recorder event stream rotated, truncated, rewritten, over-backlogged or ambiguous context.',
      'EventStreamChanged text.', source=[S('hil/echoear_aec_event_tail.py', 58, "raise EventStreamChanged('JSONL rotated or truncated')")],
      raises=[('hil/echoear_aec_stimulus.py', 'Ambiguous requested context')]),
    G('liveness.peer', 'PAUSE-PART', CELLS + PART,
      'Liveness ping/pong (0.5 s cadence, 2 s timeout): alarm, device timeout, stopped pongs or peer close -> lost peer, recondition in a new part. Positive reboot, admission-loss or stall evidence takes its CANDIDATE-FAIL gate; absence of a reboot banner is not proof of health.',
      'PERF liveness counters, liveness-alarm events.', source=[S(RS, 82, "passed=(valid and same and all(v>=0 for v in delta.values()) and")],
      plan_keys=['gates.admission_liveness_sctp.liveness_cadence_s', 'gates.admission_liveness_sctp.liveness_timeout_s',
                 'gates.admission_liveness_sctp.liveness_alarm_max_active', 'gates.admission_liveness_sctp.device_timeouts_max_active',
                 'gates.admission_liveness_sctp.per_reply_pongs', 'v85.transport.liveness_peer_close'],
      terms=['resources:liveness', 'epoch:liveness']),
    G('bot.wire_metrics', 'PAUSE-PART', PART, 'No RTVI metrics messages forwarded to the device (bot configuration).',
      'Recorder RTVI output.', source=[S(CA, 509, "wire_metrics=[e for e in recorded if e.get('kind')=='rtvi-output'")],
      plan_keys=['gates.admission_liveness_sctp.wire_metrics_max']),
    G('sctp.cookie_echo', 'MONITOR', PART, 'COOKIE ECHO count per epoch.', 'SCTP ring.',
      plan_keys=['gates.admission_liveness_sctp.cookie_echo']),
    G('bot.pipeline_error', 'PAUSE-PART', CELLS,
      'Bot pipeline errors: FatalErrorFrame, evaluation-error, TTS reconnect refused/failed, unattributed ErrorFrame, unacknowledged control, unfinished generation after a failure.',
      'Evaluation events; bot log.', source=[S(CP, 601, "raise RuntimeError('Active bot/transport error')")],
      plan_keys=['gates.proactive_tts.refusals_max', 'gates.proactive_tts.failures_max', 'gates.proactive_tts.private_guard',
                 'gates.proactive_tts.cold_and_reconnect', 'gates.proactive_tts.reconnect_metric_overlaps_max'],
      raises=[(CP, 'Active bot/transport error'), (CP, 'Active bot/transport error at final audit'),
              (CP, 'Hard observer/bot failure during acquisition'), (CP, 'Evaluation control remains unacknowledged'),
              (CP, 'Incomplete bot generation after harness failure')]),
    G('bot.service_outage', 'INVALID-CELL', CELLS,
      'Independently logged STT/TTS/LLM service outage overlapping the cell. Ordinary misrecognition, late turns and long TTS lead are not outages.',
      'Vendor error frame or connection failure logged by the bot with timestamp inside the cell window.', new=True),
    G('bot.profile_binding', 'PAUSE-PART', PART,
      'Live STT profile and vendor model (general-nova-3 2025-04-17.21547) match the plan; a change mid-campaign refuses resume (INCONCLUSIVE).',
      'stt-profile / stt-model-observed events.', source=[S(CA, 527, "if nova_profile is not None:passed &= nova_profile['passed']")],
      plan_keys=['nova_live.stt_profile', 'nova_live.vendor_model', 'nova_live.vendor_policy'],
      raises=[(NP, 'Unexpected live Nova profile/vendor'), (NP, 'Missing or changed Nova profile/vendor'),
              (NP, 'Incomplete live Nova binding')]),
    G('tts.long_lead', 'MONITOR', CELLS,
      'Proactive TTS leading silence >= 0.8 s: diagnostic when the scored interval is complete (Sol item 4). If it removes the stimulus opportunity the cell is barge.undelivered/unscorable; a late or absent response to a valid near stimulus stays SCORE.',
      'Complete generation audit for the attempt.', source=[S(CP, 721, "raise HarnessFailure('long-tts','Complete long-TTS generation requires whole-cell retake')")],
      plan_keys=['gates.proactive_tts.enabled_by_default', 'gates.proactive_tts.leading_silence_s_strict_max',
                 'gates.proactive_tts.long_generations_max', 'v85.long_tts'],
      raises=[(CP, 'long-tts')], terms=['harness:long-tts']),
    G('tts.generation_witness', 'PAUSE-PART', CELLS,
      'Generation lifecycle audit incomplete (missing metric context, write/metric mismatch, unclosed generation) -> evidence integrity unknown.',
      'generation_audit sessions.', source=[S(CA, 83, "idle_no_generation_requested=idle,passed=(bool(generations) or idle) and not problems")],
      plan_keys=['gates.proactive_tts.metric_coverage'],
      raises=[(CP, 'Incomplete long-TTS generation witnesses'), (CP, 'Long-TTS witness/budget/integrity failure; no retake')]),
    G('tts.diagnostics', 'MONITOR', CELLS, 'After-Stop request/first-write timing: diagnostic.', 'Events.',
      plan_keys=['gates.proactive_tts.after_stop', 'gates.proactive_tts.request_first_write_gate']),
    G('playback.missing', 'CANDIDATE-FAIL', SCORED,
      'Positive device receipt (arrival/decode counters for the context) of a non-silent write but no MIC3 render within 1 s. Clean transport counters support but do not prove delivery; unknown attribution -> PAUSE-PART; no usable echo exposure -> echo.unscorable.',
      'Write event, source/MIC3 match, clean transport over the window.',
      source=[S(CA, 103, 'return dict(passed=interval is not None and interval[0]>=write_s-.1 and interval[1]-write_s<=1,')],
      plan_keys=['gates.missing_playback.anchor', 'gates.missing_playback.window_s', 'gates.missing_playback.missing_max',
                 'gates.missing_playback.coverage', 'gates.missing_playback.diagnostic_anchor',
                 'gates.missing_playback.source_template_rule', 'gates.missing_playback.source_template_basis']),
    G('playback.transport_witnessed', 'INVALID-CELL', BARGE,
      'Barge/flush: missing playback with a witnessed downlink fault overlapping the write window -> invalid, re-attempted. Echo never uses this entry.',
      'Downlink fault in the same window.', new=True),
    G('reference.health', 'CANDIDATE-FAIL', SCORED,
      'Reference health: playback power >= 1024 LSB^2, ratio floor 31.623 ppm at 15 dB, low_windows delta 0 (unexplained reference integrity).',
      'PERF aec.reference_health (valid observations; missing or malformed data -> resources.coverage).', source=[S(CA, 412, "row['reference_health']=after.get('aec',{}).get('reference_health',{}).get('low_windows',-1)")],
      plan_keys=['gates.reference_health.accepted_playback_power_min_lsb2', 'gates.reference_health.ratio_floor_ppm_at_0db',
                 'gates.reference_health.gain_scaling', 'gates.reference_health.ratio_floor_ppm_at_15db',
                 'gates.reference_health.window_samples', 'gates.reference_health.low_windows_delta_max',
                 'gates.reference_health.low_samples', 'gates.reference_health.mask', 'gates.reference_health.basis'],
      terms=['integrity:reference_health']),
    G('reference.probe', 'CANDIDATE-FAIL', ('probe',),
      'Canonical DAC probe calibration (not scheduled in v9 or v8.7 Nova).', 'Probe capture.',
      source=[S(CP, 893, "raise RuntimeError('Canonical probe failed')")],
      plan_keys=['gates.reference_health.calibration', 'gates.reference_health.expected_normalized_ratio_ppm',
                 'gates.reference_health.expected_floor_margin_db'],
      raises=[(CP, 'Canonical probe failed')]),
    # ---- Timing / capture / harness ------------------------------------------------------------------
    G('timing.capture_bracket', 'INVALID-CELL', CELLS,
      'Capture blocks bracketed by clock anchors (interval-bracket-v1), mapped coverage of the scored interval, no timing gap.',
      'anchors.json, capture timing rows.', source=[S('hil/echoear_aec_timing.py', 81, "raise ValueError('Capture timing gap')")],
      plan_keys=['timing.anchor_method', 'timing.anchor_period_s', 'timing.capture_window',
                 'gates.missing_playback.timing_unscorable_max']),
    G('timing.host_clock', 'PAUSE-PART', PART, 'Host monotonic clock regressed.', 'Clock samples.',
      source=[S(CP, 647, "if time.monotonic()<began:raise RuntimeError('Clock regressed')")], raises=[(CP, 'Clock regressed')]),
    G('timing.single_gap', 'PAUSE-PART', CELLS,
      'Firmware-marked capture gap block with unknown attribution (the v8.7 exactly-one-gap retry is withdrawn).',
      'Capture timing rows.', source=[S(CP, 170, "if gaps==1:raise HarnessFailure('single-capture-gap','One marked capture gap block')")],
      raises=[(CP, 'single-capture-gap')], terms=['harness:single-capture-gap']),
    G('capture.gap_host_export', 'INVALID-CELL', CELLS,
      'Capture gap demonstrated to be a host/export measurement defect (device PCM continuous).',
      'Device-side continuity witness plus host export error.', new=True),
    G('capture.gap_firmware', 'CANDIDATE-FAIL', CELLS,
      'Proven firmware PCM discontinuity in a scored capture.', 'Device capture counters/serial attribute the gap to firmware.', new=True),
    G('timing.stimulus_lateness', 'INVALID-CELL', BARGE + WITNESS,
      'Stimulus invocation abs(error) <= 100 ms (equality allowed), the single authoritative threshold: the v9 plan sets timing.timer_lateness_max_s = 0.100 (V9_PLAN_VALUES). Near-onset mapping uncertainty is separate.',
      'Timer record.', source=[S('hil/echoear_aec_stimulus.py', 103, 'if abs(fired-due) > .1:')],
      plan_keys=['timing.timer_lateness_max_s'], raises=[(CP, 'stimulus-timing')], terms=['harness:stimulus-timing']),
    G('capture.cap', 'INVALID-CELL', CELLS, 'Capture <= 28 s (24 s workload deadline): invalidates only still-unresolved endpoints; an already established latency/recognition miss stands.',
      'capture-duration.json.', source=[S(CP, 621, "else:raise HarnessFailure('capture-deadline','Capture deadline; incomplete reply/post-roll retained')")],
      outcome_dependent=True, plan_keys=['capture_cap_seconds', 'reply_window_seconds'], raises=[(CP, 'capture-deadline')],
      terms=['harness:capture-deadline', 'integrity:capture_cap']),
    G('capture.export', 'INVALID-CELL', CELLS,
      'Console capture export within 2.6 s per capture second + 5 s and decodes completely.',
      'Export record; decode error text.', source=[S(CP, 154, "return contract['seconds_per_capture_second']*capture_seconds+contract['slack_s']")],
      plan_keys=['preflight_contract.console_export'],
      raises=[('hil/echoear_aec_bench.py', 'Expected complete EchoEar 16 kHz capture')]),
    G('harness.jabra_duration', 'INVALID-CELL', BARGE + WITNESS, 'Jabra playback duration error within [-0.10, +0.25] s.',
      'jabra-outcome record.', source=[S(CP, 80, "if not outcome['passed']:raise HarnessFailure('jabra-duration'")],
      raises=[(CP, 'jabra-duration')], terms=['harness:jabra-duration']),
    G('harness.evaluation_ack', 'INVALID-CELL', CELLS, 'Evaluation command acknowledged within 10 s.', 'Event log.',
      source=[S(CP, 528, "raise HarnessFailure('evaluation-ack-timeout','Evaluation command not acknowledged')")],
      plan_keys=['preflight_contract.evaluation_ack_timeout_s'], raises=[(CP, 'evaluation-ack-timeout')],
      terms=['harness:evaluation-ack-timeout']),
    G('retry.clean_witness', 'PAUSE-PART', CELLS,
      'A re-attempt needs clean device/observer witnesses over the failed attempt; an unclean composite is reclassified by its failing component, else PAUSE-PART.',
      'acquisition-failure.json.', aggregate=True, source=[S(CP, 791, "if not failure['retry_safe']:raise RuntimeError('Device counters/resources not clean; harness retry forbidden')")],
      plan_keys=['failure_policy.harness_retries.require_clean_device_counters'],
      raises=[(CP, 'Device counters/resources not clean; harness retry forbidden'), (CP, 'Empty retry admission interval')],
      terms=['hold:harness_failures']),
    G('post_seal.errors', 'MONITOR', PART, 'Expected transport teardown effects (T3, liveness alarms) after the active end = the final settled-end boundary of the part, before audio off/disconnect. Heap, crash, I2S, admission-loss and restore faults count whenever observed, through restore.',
      'Events after the last seal.', plan_keys=['failure_policy.post_seal']),
    # ---- Host RF / environment / link -----------------------------------------------------------------
    G('rf.cell_pre_gate', 'INVALID-CELL', CELLS,
      'Per-cell host RF observation over the existing ~5 s pre-cell wait on wlp8s0, limit scaled by actual elapsed time at 20 MB/300 s (about 333 kB per 5 s); missing/reset counters fail. Applies before acquisition at either volume.',
      'rf-pre-gate.json.', source=[S(CP, 545, "if not pre_gate['passed']:raise HarnessFailure('rf-pre-gate','Per-cell RF pre-gate failed')")],
      plan_keys=['rf.implementation', 'rf.interface', 'rf.observation_only', 'rf.pre_gate_window_s', 'rf.limit_bytes_per_300s',
                 'rf.window_total_limit_bytes', 'rf.accounting', 'rf.independent_invalidity'],
      raises=[(CP, 'rf-pre-gate')], terms=['harness:rf-pre-gate']),
    G('rf.part_pre_gate', 'PAUSE-PART', PART, 'Part-start 30 s RF pre-gate (about 2 MB at 20 MB/300 s) before any flash; the only 30 s window.', 'rf-pre-gate.json.',
      source=[S(CP, 431, "if not gate['passed']:raise RuntimeError('RF pre-gate failed')")], raises=[(CP, 'RF pre-gate failed')]),
    G('rf.capture_window', 'INVALID-CELL', BARGE,
      'Host RF byte gate over the capture window: barge INVALID-CELL only when the declared evidence rule places the excess inside the stimulus window (per-second counters); whole-capture totals alone do not; echo recorded only.',
      'rf.json.', source=[S(CA, 411, "row['rf']=read(folder/'rf.json',{}).get('passed',False)")], terms=['integrity:rf']),
    G('rf.hold_active', 'MONITOR', PART, 'Whole-hold RF byte total.', 'rf_active.',
      source=[S(CP, 853, "self.result['rf_active']=host_nic.gate(")], terms=['hold:rf_active']),
    G('rf.diagnostics', 'MONITOR', PART, 'wifi-stats at every boundary: evidence only.', 'wifi-stats logs.',
      source=[S(CP, 46, "status = dict(command='wifi-stats', evidence_only=True)")],
      plan_keys=['rf.diagnostic_boundaries', 'rf.permissions', 'rf.blind_spot']),
    G('link_probe.part', 'PAUSE-PART', PART,
      '240 s idle link probe per part: uplink unique loss <= 0.5 %, no >= 1 s gap, observer healthy, idle, resources clean (resource failure -> its CANDIDATE-FAIL entry).',
      'link-probe/result.json.', source=[S(CP, 512, 'if not value[\'passed\']:raise LinkProbeFailed(value[\'result\'])')],
      plan_keys=['link_probe'], raises=[(CP, 'Link probe device/observer/idle failure'), (CP, 'result')],
      terms=['hold:link_probe']),
    G('host.environment', 'PAUSE-PART', PART,
      'Host BSSID lock b4:20:46:f4:ea:e5 on profile fb56e504, signal >= -60 dBm, 0 supplicant changes; prelaunch refuses, in-run pauses.',
      'host-gate records.', source=[S('hil/echoear_aec_host_gate.py', 123, "if enforce and self.failed:raise EnvironmentalPause('Host environment gate failed: no credit; re-examine host')")],
      plan_keys=['host_environment'],
      raises=[(NL, 'Host UUID/BSSID guard failed'), (NP, 'Active UUID/BSSID prelaunch guard failed'),
              (CP, 'Host environment failed at hold end: no credit; restored and lock released')],
      terms=['hold:host_environment']),
    G('host.periodicity', 'PAUSE-PART', PART,
      'Two adjacent qualifying 304.4 s host-scan lattice cycles in uplink -> pause (host boot id re-derived per freeze).',
      'periodicity.json.', source=[S('hil/echoear_aec_host_gate.py', 156, "if not verdict['passed']:raise EnvironmentalPause(verdict['reason'])")],
      plan_keys=['first_hold_periodicity'], terms=['hold:periodicity']),
    # ---- Unscheduled v8 kinds --------------------------------------------------------------------------
    G('reconnect.cycle', 'CANDIDATE-FAIL', ('reconnect',),
      'Reconnect cycle gates (two epochs, active disconnect, new reply). No reconnect cells in v9; integration result covers reconnect.',
      'Reconnect records.', source=[S(CP, 828, "if noise:raise RuntimeError('Reconnect performance/witness failure; no retry')")],
      plan_keys=['gates.reconnect', 'timing.disconnect_bracket_max_s', 'v85.reconnect_window_s', 'v85.reconnect_new_reply'],
      raises=[(CP, 'Reconnect performance/witness failure; no retry'), (CP, 'Reconnect hardware/resource failure even in noisy capture'),
              (CP, 'Reconnect noise retry budget exhausted'), (CP, 'Long-TTS reconnect witness/budget/integrity failure')]),
    G('soak.block', 'CANDIDATE-FAIL', ('soak',), 'Soak block gates (600 s resource coverage). No soak in v9.',
      'Soak records.', source=[S(CP, 1040, "if not value['passed']:raise RuntimeError('Soak duration/resource coverage failed')")],
      raises=[(CP, 'Soak duration/resource coverage failed'), (CP, 'Soak noise retry budget exhausted'),
              (CP, 'Soak export exceeded next fixed deadline'), (CP, 'Soak request deadline missed; no shifting'),
              (CP, 'Three consecutive noise/harness soak cells'), (CP, 'Unresolved hard/exhausted soak attempt')]),
    G('analyzer.exception', 'MONITOR', CELLS + CAMPAIGN,
      'Analyzer/scorer exception: the attempt score is pending; pending scores withhold credit, the verdict and every score-dependent decision (retry slot, group completion, early stop). Acquisition continues only on the declared schedule while live fault checks stay healthy. Repair offline with an approved provenance amendment; never re-run on hardware, never reopen a finalized FAIL.',
      'analysis-failure.json and retained captures.', source=[S(CP, 88, "try:write(out/'analysis-failure.json',diagnostic)")],
      release_effect='Verdict withheld until the offline repair is reviewed.'),
)

# Plan leaves that carry no gate (descriptive or superseded); each still classified once.
NON_GATE = {
    'gates.echo.scope': 'descriptive', 'gates.echo.basis': 'descriptive',
    'gates.false_turns.scope': 'descriptive', 'gates.false_turns.basis': 'descriptive',
    'gates.false_turns.proposed': 'descriptive (superseded label)',
    'gates.barge_in.basis': 'descriptive', 'gates.flush.basis': 'descriptive',
    'gates.missing_playback.basis': 'descriptive', 'gates.guard.basis': 'descriptive',
    'gates.proactive_tts.basis': 'descriptive',
    'failure_policy.acquisition_errors': 'v8.7 summary, superseded by per-gate classes',
    'failure_policy.abort_immediately': 'v8.7 summary, superseded by per-gate classes',
    'timing.sample_rate_hz': 'constant', 'timing.frame_ms': 'constant',
    'v85.rationale': 'descriptive',
}

# Raise sites resolved by enclosing function rather than literal. (file, qualname prefix): gate id or
# 'EXCLUDED: reason'. '' is module level; '*' is the whole file. Longest prefix wins.
SCOPES = {
    ('hil/echoear_aec_campaign.py', 'validate_spec'): 'launch.integrity',
    ('hil/echoear_aec_campaign.py', 'validate_campaign'): 'launch.integrity',
    ('hil/echoear_aec_campaign.py', 'validate_review_preflight'): 'launch.integrity',
    ('hil/echoear_aec_campaign.py', 'validate_timing_preflight'): 'launch.integrity',
    ('hil/echoear_aec_campaign.py', 'HarnessFailure.__init__'): 'launch.integrity',
    ('hil/echoear_aec_campaign.py', 'Schedule.__init__'): 'parts.ledger',
    ('hil/echoear_aec_campaign.py', 'run'): 'parts.ledger',
    ('hil/echoear_aec_campaign.py', ''): 'launch.integrity',
    ('hil/echoear_aec_nova_live.py', 'variant_of'): 'launch.integrity',
    ('hil/echoear_aec_nova_live.py', 'candidate_override'): 'launch.integrity',
    ('hil/echoear_aec_nova_live.py', 'plan_from'): 'launch.integrity',
    ('hil/echoear_aec_nova_live.py', 'input_manifest'): 'launch.integrity',
    ('hil/echoear_aec_nova_live.py', 'validate'): 'launch.integrity',
    ('hil/echoear_aec_nova_live.py', 'prepare'): 'launch.integrity',
    ('hil/echoear_aec_nova_live.py', 'run'): 'launch.integrity',
    ('hil/echoear_aec_nova_prestep.py', 'prepare'): 'launch.integrity',
    ('hil/echoear_aec_nova_prestep.py', 'validate'): 'launch.integrity',
    ('hil/echoear_aec_nova_prestep.py', 'validate_bindings'): 'launch.integrity',
    ('hil/echoear_aec_nova_prestep.py', 'run'): 'launch.integrity',
    ('hil/echoear_aec_v85.py', 'validate'): 'launch.integrity',
    ('hil/echoear_aec_bench.py', 'transcribe_afe'): 'EXCLUDED: legacy bench STT helper, not on the acceptance path',
    ('hil/echoear_aec_bench.py', 'acquire'): 'EXCLUDED: legacy bench acquisition, not on the acceptance path',
    ('hil/echoear_aec_host_gate.py', 'HostGate.boundary'): 'host.environment',
    ('hil/echoear_aec_host_gate.py', 'HostGate.periodicity'): 'host.periodicity',
    ('hil/echoear_aec_host_gate.py', 'lattice'): 'launch.integrity',
    ('hil/echoear_aec_host_gate.py', ''): 'EXCLUDED: CLI exit status',
    ('hil/echoear_aec_resources.py', ''): 'EXCLUDED: CLI exit status',
    ('hil/echoear_aec_accept.py', 'validate_plan'): 'EXCLUDED: legacy pre-v8 accept runner, not on the acceptance path',
    ('hil/echoear_aec_accept.py', 'run'): 'EXCLUDED: legacy pre-v8 accept runner, not on the acceptance path',
    ('hil/echoear_aec_campaign_resume.py', '*'): 'parts.ledger',
    ('hil/echoear_aec_v85_tts.py', 'exclusions'): 'tts.generation_witness',
    ('hil/echoear_aec_reconnect.py', '*'): 'reconnect.cycle',
    ('hil/echoear_aec_event_tail.py', '*'): 'observer.event_stream',
    ('hil/echoear_aec_timing.py', '*'): 'timing.capture_bracket',
    ('hil/echoear_aec_capture.py', '*'): 'capture.export',
    ('hil/host_nic.py', '*'): 'rf.cell_pre_gate',
    ('hil/echoear_aec_noise_gate.py', 'validate_noise_gate'): 'launch.integrity',
    ('hil/echoear_aec_noise_gate.py', 'NoiseRetryQueue'): 'parts.ledger',
    ('hil/echoear_aec_noise_gate.py', '*'): 'noise.cell_quiet_windows',
    ('hil/echoear_aec_echo_dropout.py', 'ActivityBudget'): 'parts.time_caps',
    ('hil/echoear_aec_echo_dropout.py', 'diagnostic_hold'): 'noise.part_baseline',
    ('hil/echoear_aec_echo_dropout.py', '*'): 'EXCLUDED: offline echo-dropout diagnostic runner, not on the acceptance path',
    ('hil/echoear_aec_bench.py', 'verify_bundle'): 'launch.integrity',
    ('hil/echoear_aec_bench.py', 'declared_boot_gain'): 'launch.integrity',
    ('hil/echoear_aec_bench.py', 'boot'): 'launch.integrity',
    ('hil/echoear_aec_bench.py', 'read_wav'): 'launch.integrity',
    ('hil/echoear_aec_bench.py', 'json_scalar'): 'unknown.unclassified',
    ('hil/echoear_aec_campaign_analysis.py', '*'): 'analyzer.exception',
    ('hil/echoear_aec_accept_analysis.py', '*'): 'analyzer.exception',
    ('hil/echoear_aec_lull.py', '*'): 'analyzer.exception',
    ('hil/echoear_aec_metrics.py', '*'): 'analyzer.exception',
    ('hil/echoear_aec_v85_transport.py', '*'): 'analyzer.exception',
    ('hil/echoear_aec_transport_environment.py', '*'): 'analyzer.exception',
    ('hil/echoear_aec_floor_relative.py', '*'): 'analyzer.exception',
    ('hil/echoear_aec_playback_template.py', '*'): 'launch.integrity',
    ('hil/echoear_aec_transport_policy.py', '*'): 'launch.integrity',
    ('hil/echoear_aec_host_interrupt.py', '*'): 'launch.integrity',
}

# Files whose raise sites are inventoried: the v8.7 Nova runtime path, the resume chain it disables
# (v9 enables it) and the analyzer modules it calls.
SCANNED_FILES = tuple('hil/'+f for f in (
    'echoear_aec_campaign.py', 'echoear_aec_nova_live.py', 'echoear_aec_nova_prestep.py', 'echoear_aec_echo_dropout.py',
    'echoear_aec_host_gate.py', 'echoear_aec_accept.py', 'echoear_aec_resources.py', 'echoear_aec_checks.py',
    'echoear_aec_campaign_resume.py', 'echoear_aec_v85_tts.py', 'echoear_aec_v85.py', 'echoear_aec_stimulus.py',
    'echoear_aec_reconnect.py', 'echoear_aec_event_tail.py', 'echoear_aec_timing.py', 'echoear_aec_capture.py',
    'host_nic.py', 'sctp_recovery.py', 'echoear_aec_noise_gate.py', 'echoear_aec_bench.py',
    'echoear_aec_campaign_analysis.py', 'echoear_aec_accept_analysis.py', 'echoear_aec_lull.py', 'echoear_aec_metrics.py',
    'echoear_aec_v85_transport.py', 'echoear_aec_transport_environment.py', 'echoear_aec_floor_relative.py',
    'echoear_aec_playback_template.py', 'echoear_aec_transport_policy.py', 'echoear_aec_host_interrupt.py'))

BY_ID = {g['id']: g for g in REGISTRY}

# Values the v9 plan must carry where the v8.7 field was wrong or superseded.
V9_PLAN_VALUES = {'timing.timer_lateness_max_s': 0.100}

# v8.7 plan sections and top-level keys that carry gates; list leaves expand per element.
V87_PLAN_SHA256 = 'bef556271a1acdf8af0cffa75a5738479b65fa9955f50d4fef6334a738747a75'
V87_KEYS_FILE = 'hil/plans/echoear-aec-v87-gate-keys.json'
PLAN_SECTIONS = ('gates', 'noise_gate', 'timing', 'rf', 'failure_policy', 'opportunity', 'link_probe',
                 'host_environment', 'first_hold_periodicity', 'ongoing_reply_lull', 'v85', 'budget',
                 'source_preflight_contract', 'preflight_contract', 'measurement_contract', 'nova_live',
                 'diagnostics', 'cleanup')
PLAN_TOP_LEVEL = ('guard_ms', 'rearm_ms', 'gain_db', 'boot_ref_gain_db', 'require_active_tts', 'active_tts_max_age_s',
                  'capture_cap_seconds', 'reply_window_seconds', 'gated_volume_db', 'volumes_db', 'attempts_per_cell')
EXPANDED_LISTS = ('gates.hardware.absolute_zero', 'gates.hardware.live_delta_zero')


def plan_gate_keys(plan):
    def leaves(value, path):
        if isinstance(value, dict) and value:
            for k, v in value.items(): yield from leaves(v, path+'.'+k)
        elif path in EXPANDED_LISTS:
            for v in value: yield f'{path}[{v}]'
        else: yield path
    return sorted([k for s in PLAN_SECTIONS for k in leaves(plan[s], s)] + [k for k in PLAN_TOP_LEVEL if k in plan])


def covers(prefix, key):
    return key == prefix or key.startswith(prefix+'.') or key.startswith(prefix+'[')


def counts():
    return {c: sum(g['cls'] == c for g in REGISTRY) for c in CLASSES}


def markdown_table():
    rows = ['| id | class | applies to | gate | evidence for classification | source today |',
            '|---|---|---|---|---|---|']
    for g in REGISTRY:
        src = '; '.join(f'{p.split("/")[-1]}:{n}' for p, n, _ in g['source']) or ('new in v9' if g['new'] else 'plan only')
        rows.append(f"| `{g['id']}` | {g['cls']} | {', '.join(g['applicability'])} | {g['description']} | "
                    f"{g['evidence']} | {src} |")
    return '\n'.join(rows)


INVENTORY_FILE = 'hil/plans/echoear-aec-v9-gate-inventory.json'


def raise_sites(root, path):
    """(qualname, line, exception, first string literal or None) for every raise of a call."""
    import ast
    from pathlib import Path
    tree = ast.parse((Path(root)/path).read_text())
    sites = []
    def visit(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, stack+[child.name]); continue
            if isinstance(child, ast.Raise) and isinstance(child.exc, ast.Call):
                func = child.exc.func
                name = getattr(func, 'id', None) or getattr(func, 'attr', None)
                literal = next((x.value for a in child.exc.args for x in ast.walk(a)
                                if isinstance(x, ast.Constant) and isinstance(x.value, str)), None)
                sites.append(('.'.join(stack), child.lineno, name, literal))
            visit(child, stack)
    visit(tree, [])
    return sites


def literal_claims():
    claims = {}
    for g in REGISTRY:
        for claim in g['raises']:
            if claim in claims: raise ValueError('Raise literal claimed twice: '+repr(claim))
            claims[claim] = g['id']
    return claims


def resolve(path, qualname, literal, claims=None):
    claims = literal_claims() if claims is None else claims
    if literal is not None and (path, literal) in claims: return claims[(path, literal)]
    best = None
    for (file, prefix), target in SCOPES.items():
        if file != path: continue
        hit = prefix == '*' or (prefix == '' and qualname == '') or \
            (prefix and (qualname == prefix or qualname.startswith(prefix+'.')))
        rank = 0 if prefix == '*' else 1+len(prefix)
        if hit and (best is None or rank > best[0]): best = (rank, target)
    return best[1] if best else None


def harness_kinds(root):
    """HarnessFailure.allowed by AST: the campaign module imports serial."""
    import ast
    from pathlib import Path
    tree = ast.parse((Path(root)/'hil/echoear_aec_campaign.py').read_text())
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == 'HarnessFailure')
    assign = next(n for n in cls.body if isinstance(n, ast.Assign) and n.targets[0].id == 'allowed')
    return {e.value for e in assign.value.elts}


def code_terms(root):
    """Check names in the legacy check families that a v9 entry must claim."""
    import re
    from pathlib import Path
    from echoear_aec_resources import check_snapshots
    terms = {'resources:'+k for k in check_snapshots([])} | {'resources:liveness'}
    resources = (Path(root)/'hil/echoear_aec_resources.py').read_text()
    body = resources[resources.index('def check_directory'):resources.index("if __name__ == '__main__':")]
    terms |= {'resources:'+k for k in re.findall(r"checks\['(\w+)'\] = dict", body)}
    analysis = (Path(root)/'hil/echoear_aec_campaign_analysis.py').read_text()
    line = next(l for l in analysis.splitlines() if "row['integrity_passed']=all([" in l)
    terms |= {'integrity:'+k for k in re.findall(r"row\['(\w+)'\]", line.split('=all(')[1])}
    epoch = analysis[analysis.index('checks=dict(transport=transport_check'):analysis.index('epoch_checks.append(')]
    terms |= {'epoch:'+k for k in re.findall(r'(?:dict\(|,\s*)(\w+)=', epoch)}
    verdict = analysis[analysis.index("passed=bool(saved.get('completed')"):analysis.index("if nova_profile is not None:passed")]
    terms |= {'hold:'+k for k in re.findall(r"saved\.get\('(\w+)'", verdict)}
    terms |= {'harness:'+k for k in harness_kinds(root)}
    return terms


def owner_of(key):
    owners = [g['id'] for g in REGISTRY for p in g['plan_keys'] if covers(p, key)]
    owners += ['NON_GATE' for p in NON_GATE if covers(p, key)]
    return owners[0] if len(owners) == 1 else None


def scan(root, plan_keys):
    """Fresh inventory: raise sites (no line numbers), plan leaves and check names with owners."""
    claims = literal_claims()
    sites = sorted([path, q, exc, lit, resolve(path, q, lit, claims)]
                   for path in SCANNED_FILES for q, _, exc, lit in raise_sites(root, path))
    terms = {t: next((g['id'] for g in REGISTRY if t in g['terms']), None) for t in sorted(code_terms(root))}
    return dict(raise_sites=sites, plan_leaves={k: owner_of(k) for k in sorted(plan_keys)}, terms=terms)


def check_inventory(fresh, reviewed):
    """Problems that require classification and a reviewed inventory update."""
    problems = []
    for kind in ('raise_sites',):
        a = [tuple(x) for x in fresh[kind]]; b = [tuple(x) for x in reviewed[kind]]
        problems += [('unreviewed raise site', x) for x in sorted(set(a)-set(b))]
        problems += [('stale raise site', x) for x in sorted(set(b)-set(a))]
        problems += [('unclassified raise site', x) for x in a if x[4] is None]
    for kind in ('plan_leaves', 'terms'):
        a, b = fresh[kind], reviewed[kind]
        problems += [('unreviewed '+kind, k) for k in sorted(set(a)-set(b))]
        problems += [('stale '+kind, k) for k in sorted(set(b)-set(a))]
        problems += [('changed owner '+kind, k) for k in sorted(set(a) & set(b)) if a[k] != b[k]]
        problems += [('unclassified '+kind, k) for k, v in a.items() if v is None]
    return problems


if __name__ == '__main__':
    import json
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    if sys.argv[1:] == ['--write-inventory']:
        keys = json.loads((root/V87_KEYS_FILE).read_text())['keys']
        fresh = scan(root, keys)
        if [p for p in check_inventory(fresh, fresh) if p[0].startswith('unclassified')]:
            raise SystemExit('Classify every site/leaf/term before writing the inventory')
        (root/INVENTORY_FILE).write_text(json.dumps(fresh, indent=1)+'\n')
    print(json.dumps(dict(counts=counts(), total=len(REGISTRY)), indent=2))
