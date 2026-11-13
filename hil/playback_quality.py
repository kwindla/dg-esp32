"""Declared playback gates shared by sweep and causal reply harnesses."""
TRANSPORT_COUNTERS=('lost','concealed','plc_frames','drift_drops','rebuffers','playout_dropped',
    'drift_inserts','post_plc_zeros','device_silence_blocks','discontinuity','latency_trim',
    'i2s_write_errors','i2s_underflows')
EDIT_COUNTERS=('drift_inserts','latency_trim')
DAMAGE_COUNTERS=tuple(k for k in TRANSPORT_COUNTERS if k not in EDIT_COUNTERS)

def transport_gate(before,after):
    schema=before.get('playout_schema')==after.get('playout_schema')==2
    deltas={k:after[k]-before[k] if schema and k in before and k in after else None for k in TRANSPORT_COUNTERS}
    available=schema and all(isinstance(v,(int,float)) and v>=0 for v in deltas.values())
    same_session=all(before.get(k)==after.get(k) for k in ('pc_id','generation') if k in before or k in after)
    damaged=available and any(deltas[k]!=0 for k in DAMAGE_COUNTERS)
    edited=available and any(deltas[k]!=0 for k in EDIT_COUNTERS)
    return dict(deltas=deltas,schema=2 if schema else None,available=available,
        passed=same_session and available and not damaged,
        classification='session-changed' if not same_session else 'transport-unavailable' if not available else
            'transport-damaged' if damaged else 'edited' if edited else 'clean')

def microphone_clip_gate(before,after):
    same=all(before.get(k)==after.get(k) for k in ('pc_id','generation'))
    def delta(key):
        a,b=before.get(key),after.get(key)
        return b-a if same and type(a) is int and type(b) is int and b>=a>=0 else None
    total=delta('mic_in_turn_clips')
    opened,closed=delta('mic_gate_open_clips'),delta('mic_gate_closed_clips')
    available=all(v is not None for v in (total,opened,closed))
    consistent=available and total==opened+closed
    return dict(available=available,delta=total,gate_open_delta=opened,
                gate_closed_delta=closed,consistent=consistent,
                passed=consistent and total==opened==closed==0,
                basis='Capture-enabled clips after settling, classified by the PCM block mute decision; both gate states must remain zero')


def classify_turn(row):
    window=row.get('reply_window_transport',{})
    gate=transport_gate(window.get('before',row.get('before_perf',{})),window.get('after',row.get('after_perf',{})))
    row['reply_transport_gate']=gate
    row['acoustic_scoring_eligible']=gate['passed']
    return gate

def acoustic_level_gate(board_gain_db,digital_gain_db,tolerance_db=2,*,control_room_rise_passed=True):
    if not control_room_rise_passed:
        return dict(passed=False,eligible=False,reason="Control failed its own room-rise gate")
    error=board_gain_db-digital_gain_db
    return dict(board_gain_db=board_gain_db,digital_gain_db=digital_gain_db,error_db=error,
                tolerance_db=tolerance_db,passed=abs(error)<=tolerance_db)


# Fixed before board-side rescoring. The Jabra is not an absolute SPL meter:
# its level is transferred through the frozen 1 kHz physical FIR to the board
# microphone's nominal -26 dBFS/94 dB SPL sensitivity, with several dB error.
# -22 centre predeclared for step 9 from the pooled retained 8f/8f2 mean
# (78.1 dB). Keep +/-7 dB transfer uncertainty, independent of each turn.
REPLY_SPL_CENTERS={-25:73.,-22:78.}

ECHOEAR_REPLY_TOLERANCE_DB=3.0

def jabra_level_gate(samples,rise_db,model,*,target_dbfs=-22,board="waveshare_146",volume_relative_db=0):
    import numpy as np
    from bench_calibration import dbfs,SENSITIVITY_DBFS
    if board == 'echoear':
        # Direct Jabra measurement: no board microphone sensitivity or SPL.
        target=-22 if target_dbfs is None else target_dbfs
        tolerance=ECHOEAR_REPLY_TOLERANCE_DB  # Fixed before acquisition.
        level=dbfs(samples) if len(samples) else None
        valid=level is not None and np.isfinite(level) and rise_db is not None and np.isfinite(rise_db)
        return dict(rms_dbfs=level, rms_rise_db=rise_db,
                    board_mic_tone_transfer_spl=None, spl_window_db=None,
                    target_dbfs=target, tolerance_db=tolerance, volume_relative_db=volume_relative_db,
                    rms_window_db=[target-tolerance,target+tolerance],
                    available=True, passed=bool(valid and volume_relative_db==0 and rise_db>=10 and abs(level-target)<=tolerance),
                    reason='Direct Jabra reply RMS at volume max 0, +/-3 dB and >=10 dB above room; no SPL inference')
    def tone_gain(chain):
        h=np.asarray(model['chains'][chain]['fir'])
        return abs(np.sum(h*np.exp(-2j*np.pi*1000*np.arange(len(h))/16000)))
    a,b=tone_gain('board'),tone_gain('jabra')
    if not len(samples) or a<=0 or b<=0:
        return dict(passed=False,reason='Missing level calibration or reply samples')
    target=-22 if target_dbfs is None else target_dbfs
    center=REPLY_SPL_CENTERS.get(target,73.+(target+25))
    window=(center-7,center+7)
    level=dbfs(samples)
    spl=94-SENSITIVITY_DBFS+level+20*np.log10(a/b)
    return dict(rms_dbfs=level,rms_rise_db=rise_db,board_mic_tone_transfer_spl=float(spl),
        spl_window_db=list(window),target_dbfs=target,uncertainty='Several dB; not Jabra-position or 1 m SPL',
        passed=bool(rise_db is not None and rise_db>=10 and window[0]<=spl<=window[1]))


def decoded_match(decoded,source):
    """Align successive 100 ms content blocks across small playout edits.

    Anchor on the whole reply (or a 500 ms reference prefix if truncated),
    then search blocks within +/-40 ms of the preceding alignment. A lone
    leading syllable/silence block must not choose the global anchor. Keep every score/offset and the old whole-reply score.
    The mean block correlation must pass .95; small edit/filter boundaries are
    included in that mean, so replacement content still lowers the verdict.
    Reference blocks below -30 dB of segment RMS do not score or steer the
    chain; their aligned decoded blocks must be below -20 dB of decoded
    segment RMS or contribute a zero-score failed block to the mean. Quiet
    checks use the running offset without a local search. Borderline natural
    pause tails may cross -30 dB between references; they never steer alignment.
    Short quiet blocks use the decoded samples present; empty blocks are
    unavailable and excluded, within the entry tolerance of 320 samples.
    Only a correlation >=.5 can move the running search centre.
    """
    import numpy as np
    from scipy import signal
    decoded=np.asarray(decoded);source=np.asarray(source)
    if not len(source) or len(decoded)<len(source)-320:
        return dict(passed=False,reason='Missing/truncated decoded PCM')
    sos=signal.butter(4,[500,1500],btype='bandpass',fs=16000,output='sos')
    y=signal.sosfilt(sos,decoded);r=signal.sosfilt(sos,source)
    def match(haystack,needle):
        if len(haystack)<len(needle):return None
        power=signal.convolve(haystack*haystack,np.ones(len(needle)),mode='valid',method='fft')
        corr=signal.correlate(haystack,needle,mode='valid',method='fft')/np.sqrt(np.maximum(power,1)*max(np.dot(needle,needle),1))
        at=int(np.argmax(corr));return at,float(corr[at])
    whole=match(y,r)
    anchor=whole if whole is not None else match(y,r[:8000])
    if anchor is None:return dict(passed=False,reason='Missing alignment anchor')
    blocks=[];offset=anchor[0];quiet_blocks=[]
    segment=y[offset:offset+len(r)]
    quiet_decoded_power=float(np.mean(segment*segment))*.01
    minimum_power=float(np.mean(r*r))*.001
    for start in range(0,len(r),1600):
        needle=r[start:start+1600]
        if np.mean(needle*needle)<minimum_power or np.dot(needle,needle)<1:
            aligned=y[start+offset:start+offset+len(needle)]
            power=float(np.mean(aligned*aligned)) if len(aligned) else None
            passed=power<=quiet_decoded_power if power is not None else None
            quiet_blocks.append(dict(source_start=start,samples=len(needle),offset_samples=offset,
                decoded_samples=len(aligned),decoded_power=power,maximum_power=quiet_decoded_power,
                status='unavailable' if passed is None else 'passed' if passed else 'failed',passed=passed))
            if passed is False:
                blocks.append(dict(source_start=start,samples=len(needle),offset_samples=offset,correlation=0.,quiet_violation=True))
            continue
        low=max(0,start+offset-640)
        high=min(len(y),start+offset+640+len(needle))
        found=match(y[low:high],needle)
        if found is None:return dict(passed=False,reason='Truncated aligned block',blocks=blocks)
        at,score=found;matched_offset=low+at-start
        if score>=.5:offset=matched_offset
        blocks.append(dict(source_start=start,samples=len(needle),offset_samples=matched_offset,correlation=score))
    score=float(np.average([b['correlation'] for b in blocks],weights=[b['samples'] for b in blocks])) if blocks else 0.
    return dict(passed=score>=.95,correlation=score,offset_samples=blocks[0]['offset_samples'] if blocks else None,
        samples=len(source),blocks=blocks,quiet_blocks_excluded=len(quiet_blocks),quiet_blocks=quiet_blocks,
        quiet_block_failures=sum(b['passed'] is False for b in quiet_blocks),whole_reply_correlation=whole[1] if whole else None,
        basis='Whole-reply anchor, piecewise 100 ms alignment, +/-40 ms local search; reference below -30 dB segment RMS excluded; confident >=.5 offsets only; aligned quiet decoded samples present <= -20 dB decoded segment RMS or zero-score failure; empty quiet blocks unavailable/excluded; sample-weighted scored mean')


def acoustic_content_verdict(stt_passed,segments):
    if stt_passed:return True
    complete=[r for r in segments if not r.get('cancelled',False)]
    if complete and all(r.get('decoded_content',{}).get('passed') is True and
                        r.get('board',{}).get('correlation',0)>=.6 for r in complete):
        return None
    return False


def impaired_conformance(before,after,evidence,*,board_stt_correct,decoded_correlation):
    """Score transport damage, concealment and content separately.

    Evidence fields must cover the same reply interval. Missing counters or
    incomplete identity/run coverage are unavailable, never zero damage.
    """
    strict=transport_gate(before,after)
    same=before.get('generation')==after.get('generation') and before.get('pc_id')==after.get('pc_id')
    a,b=before.get('playout',{}),after.get('playout',{})
    names=('missing_slots','plc_calls','post_plc_zeros','starvation','drift_drop','drift_insert','decode_errors','latency_trim','discontinuity')
    delta={k:b[k]-a[k] if k in a and k in b and same else None for k in names}
    facts=('reply_packets','absent_at_ingress','longest_concealment_run','longest_wire_absent_run','starvation_without_wire_gap')
    available=(strict['available'] and same and evidence.get('coverage_complete') is True and
        all(delta[k] is not None and delta[k]>=0 for k in names) and
        all(evidence.get(k) is not None for k in facts) and evidence['reply_packets']>0)
    checks={}
    if available:
        checks=dict(concealment_excess=delta['missing_slots']-evidence['absent_at_ingress']<=max(1,.002*evidence['reply_packets']),
            concealment_run=evidence['longest_concealment_run']<=evidence['longest_wire_absent_run']+1,
            starvation_explained=evidence['starvation_without_wire_gap']==0,
            board_stt_correct=board_stt_correct)
    passed=(all(v is True for v in checks.values()) if available and board_stt_correct is not None else None)
    fraction=delta['plc_calls']/evidence['reply_packets'] if available else None
    acoustic=(fraction<=.10 and delta['post_plc_zeros']==0 and delta['starvation']==0 and
        decoded_correlation>=.90) if available and decoded_correlation is not None else None
    # Duplicate rejection and intentional drift corrections do not constitute damage.
    damage=any(delta[k]>0 for k in ('missing_slots','decode_errors','discontinuity','starvation')) if available else None
    edited=any(delta[k]>0 for k in ('drift_drop','drift_insert','latency_trim')) if available else None
    return dict(passed=passed,available=available,checks=checks,deltas=delta,evidence=evidence,
        strict=strict,board_stt_correct=board_stt_correct,plc_fraction=fraction,
        acoustic_scoring_eligible=acoustic,decoded_correlation=decoded_correlation,
        classification='unavailable' if not available else 'damaged' if damage else 'edited' if edited else 'clean',
        basis='Impaired conformance beside the transport-damage verdict; duplicate rejections are not damage, drift edits are edited. STT is independent.')


def counter_damage(deltas):
    if not deltas or any(not isinstance(v,(int,float)) or v<0 for v in deltas.values()):
        return None
    return any(v!=0 for k,v in deltas.items() if k not in EDIT_COUNTERS)
