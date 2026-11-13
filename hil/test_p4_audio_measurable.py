"""Offline measurable-harmonic controls, including the retained 1500 Hz failure."""
import copy
import json
import os
from pathlib import Path
import wave

import numpy as np
import pytest

import p4_audio_measurable as audio
from wire_contract import ContractError


def calibration(snr=30):
    return {str(hz):dict(tone_dbfs=-50,noise_dbfs=-50-snr,snr_db=snr,
        classification='measurable' if snr>=10 else 'unmeasurable')
        for hz in range(500,8000,100)}


def measurement(raw=-45, snr=30, clips=0):
    return dict(fundamental_dbfs=-70+snr,noise_dbfs=-70,native_capture_rate_hz=16000,
        clipping_count=clips,harmonics_dbc={h:raw for h in audio.HARMONICS})


@pytest.mark.parametrize('snr,state',[(9.999,'unmeasurable'),(10,'measurable'),(10.001,'measurable')])
def test_calibration_threshold_and_genuinely_high_measurable_h5(snr,state):
    cal=calibration();cal['7500'].update(snr_db=snr,classification=state)
    point=measurement();point['harmonics_dbc']['H5']=-20
    result=audio.classify_chain(point,1500,cal)
    assert result['harmonics']['H5']['classification']==state
    assert result['harmonics']['H5']['included_in_thd']==(snr>=10)
    assert result['classification']==('fail' if snr>=10 else 'pass')
    assert (result['corrected_thd_percent']>5)==(snr>=10)


def test_real_retake_1500_h5_regression():
    # r1-1500-6 Jabra, acoustic-retake. Retained numbers, not synthetic
    # high distortion: old corrected THD was 6.266615842590902%.
    point=measurement();point.update(fundamental_dbfs=-44.81988507828511,noise_dbfs=-64.58170216719628)
    point['harmonics_dbc']={
        'H2':-42.87528285160472,'H3':-42.28976652572916,
        'H4':-49.771710772850454,'H5':-65.89302357163295}
    cal=calibration()
    for hz,level in ((1500,-54.088676450127934),(3000,-52.32333945391858),
                     (4500,-53.862807330402454),(6000,-66.42158139846487)):
        cal[str(hz)]['tone_dbfs']=level
    cal['7500'].update(tone_dbfs=-95.6121338322314,noise_dbfs=-98.53,
                       snr_db=2.91,classification='unmeasurable')
    result=audio.classify_chain(point,1500,cal)
    h5=result['harmonics']['H5']
    assert h5['corrected_dbc']>-26 and h5['raw_dbc']<-43
    assert h5['classification']=='unmeasurable' and not h5['included_in_thd']
    assert result['classification']=='pass' and result['corrected_thd_percent']<5
    assert h5['corrected_dbc']==pytest.approx(-24.369566189529493)
    assert result['corrected_thd_percent']==pytest.approx(1.6454005291897216)


@pytest.mark.parametrize('harmonic', ['H2','H3'])
def test_unmeasurable_bounded_harmonic_never_passes_by_omission(harmonic):
    cal=calibration();cal[str(1000*int(harmonic[1:]))]['classification']='unmeasurable'
    point=measurement()
    result=audio.classify_chain(point,1000,cal)
    assert result['classification']=='unmeasurable'
    assert result['unmeasurable_bounded_harmonics']==[harmonic]
    point['clipping_count']=1
    assert audio.classify_chain(point,1000,cal)['classification']=='fail'
    point['clipping_count']=0
    point['harmonics_dbc']['H3' if harmonic=='H2' else 'H2']=-20
    assert audio.classify_chain(point,1000,cal)['classification']=='fail'


@pytest.mark.parametrize('harmonic',['H2','H3'])
def test_measurable_h2_h3_bounds_unchanged(harmonic):
    point=measurement(raw=-100);point['harmonics_dbc'][harmonic]=-26
    result=audio.classify_chain(point,1000,calibration())
    assert result['failed_harmonic_bounds']==[]
    # -26 dBc alone is 5.0119%: the independent 5% THD limit still applies.
    assert result['classification']=='fail' and result['corrected_thd_percent']>5
    point['harmonics_dbc'][harmonic]=-26.1
    assert audio.classify_chain(point,1000,calibration())['classification']=='pass'
    point['harmonics_dbc'][harmonic]=-25.999
    result=audio.classify_chain(point,1000,calibration())
    assert result['classification']=='fail' and result['failed_harmonic_bounds']==[harmonic]


def test_point_snr_boundary_clips_and_above_nyquist():
    point=measurement(snr=10);point['harmonics_dbc'].update(H3=None,H4=None,H5=None)
    result=audio.classify_chain(point,3000,calibration())
    assert result['classification']=='pass'
    assert result['harmonics']['H3']['classification']=='unavailable-above-nyquist'
    point['clipping_count']=1
    assert audio.classify_chain(point,3000,calibration())['classification']=='fail'
    point['fundamental_dbfs']-=.001
    assert audio.classify_chain(point,3000,calibration())['classification']=='unmeasurable'


def wav_calibration(directory, hz=1500, noise_amplitude=.001):
    rate=16000;time=np.arange(rate)/rate
    # Local background is a known 1600 Hz line; a stronger out-of-band
    # line must not inflate the local floor, nor may the 1500 Hz tone.
    samples=.05*np.sin(2*np.pi*hz*time)+noise_amplitude*np.sin(2*np.pi*(hz+100)*time)+.1*np.sin(2*np.pi*500*time)
    pcm=np.rint(samples*32768).astype('<i2')
    x=pcm.astype(float)/32768;x-=x.mean();window=np.hanning(rate)
    power=2*np.abs(np.fft.rfft(x*window))**2/(rate*np.sum(window**2))
    f=np.fft.rfftfreq(rate,1/rate)
    level=float(10*np.log10(power[np.abs(f-hz)<=20].sum()))
    row=dict(name=f'cal-{hz}',hz=hz,windows={},chains={})
    for chain in audio.CHAINS:
        with wave.open(str(directory/f'cal-{hz}-{chain}.wav'),'wb') as w:
            w.setparams((1,2,rate,0,'NONE','not compressed'));w.writeframes(pcm.tobytes())
        row['windows'][chain]=[0,1]
        row['chains'][chain]=dict(fundamental_dbfs=level,fundamental_hz=hz,
            native_capture_rate_hz=rate,wav_rate_hz=rate,recovered_phase=None)
    return row


def test_noise_floor_integrates_fixed_local_sidebands_and_binds_wav_and_window(tmp_path):
    row=wav_calibration(tmp_path)
    result=audio.calibration_evidence(tmp_path,[row])['jabra']['1500']
    assert result['noise_method']==audio.NOISE_METHOD
    assert result['noise_dbfs']==pytest.approx(10*np.log10(.001**2/2),abs=.1)
    assert result['noise_bandwidth_hz']==320 and result['noise_bins']==320
    assert result['snr_db']==pytest.approx(20*np.log10(.05/.001),abs=.1)
    assert result['classification']=='measurable'
    assert len(result['wav_sha256'])==64 and result['scoring_window_seconds']==[0,1]
    assert result==audio.calibration_evidence(tmp_path,[row])['jabra']['1500']


@pytest.mark.parametrize('mutation',['tone','window','rate','duplicate','missing'])
def test_bad_or_missing_calibration_evidence_is_refused(tmp_path,mutation):
    row=wav_calibration(tmp_path);rows=[row]
    if mutation=='tone':row['chains']['board']['fundamental_dbfs']+=1
    elif mutation=='window':row['windows']['board']=[0,2]
    elif mutation=='rate':row['chains']['board']['native_capture_rate_hz']=8000
    elif mutation=='duplicate':rows.append(copy.deepcopy(row))
    else:(tmp_path/'cal-1500-board.wav').unlink()
    with pytest.raises((ContractError,FileNotFoundError)):audio.calibration_evidence(tmp_path,rows)


def sweep():
    points=[]
    for amp in (2,4,6,8,10,15):
        for hz in (500,800,1000,1500,3000):
            for repeat in (1,2):
                chains={c:measurement(snr=9 if amp==2 or amp==4 and hz==1000 else 20) for c in audio.CHAINS}
                if hz==3000:
                    for c in chains.values():c['harmonics_dbc'].update(H3=None,H4=None,H5=None)
                if hz==500:
                    for c in chains.values():c['harmonics_dbc']['H2']=-20
                points.append(dict(name=f'r{repeat}-{hz}-{amp}',hz=hz,amplitude_percent=amp,repeat=repeat,chains=chains))
    linearity=[dict(hz=1000,amplitude_percent=amp,chains={c:dict(fundamental_dbfs=level,noise_dbfs=-100) for c in audio.CHAINS})
               for amp,level in zip((1,2,4,8),(-70,-63.9794,-57.9588,-51.9382))]
    return dict(points=points,linearity=linearity,plan_sha256='c'*64,
                acquisition_passed=True,clean_amplitudes_percent=[])


def test_band_coverage_repeats_500_exclusion_and_first_failure():
    saved=sweep()
    point=next(p for p in saved['points'] if p['hz']==800 and p['amplitude_percent']==10)
    point['chains']['board']['harmonics_dbc']['H2']=-20
    result=audio.analyze(saved,{c:calibration() for c in audio.CHAINS})
    assert result['clean_amplitudes_percent']==[6,8]
    assert all(p['excluded'] for p in result['points'] if p['hz']==500)
    assert result['level_band_dbfs']['1000']['board']==[-50,-50]
    saved['points'].remove(point)
    with pytest.raises(ContractError,match='both original repeats'):audio.analyze(saved,{c:calibration() for c in audio.CHAINS})


def test_unchanged_linearity_blocks_an_otherwise_clean_band():
    saved=sweep();saved['linearity'][0]['chains']['board']['fundamental_dbfs']-=1.1
    result=audio.analyze(saved,{c:calibration() for c in audio.CHAINS})
    assert not result['linearity_valid'] and result['clean_amplitudes_percent']==[]
    assert result['level_band_dbfs'] is None and result['linearity_failures'][0]['chain']=='board'


@pytest.mark.parametrize('snr,state',[(9.999,'unmeasurable'),(10,'evaluated'),(10.001,'evaluated')])
def test_linearity_endpoint_snr_boundary(snr,state):
    saved=sweep();point=saved['linearity'][1]['chains']['board']
    point['noise_dbfs']=point['fundamental_dbfs']-snr
    result=audio.linearity_report(saved)
    step=next(s for s in result['linearity_steps'] if s['chain']=='board' and s['from_percent']==2)
    assert step['classification']==state
    assert step['endpoints'][0]['snr_db']==pytest.approx(snr)
    assert step['passed'] is (True if state=='evaluated' else None)


def test_chain_with_no_evaluated_linearity_step_is_unavailable():
    saved=sweep()
    for point in saved['linearity']:
        measurement=point['chains']['board'];measurement['noise_dbfs']=measurement['fundamental_dbfs']-9
    result=audio.analyze(saved,{c:calibration() for c in audio.CHAINS})
    assert not result['linearity_available'] and not result['linearity_valid']
    assert result['linearity_unavailable_chains']==['board']
    assert result['linearity_chains']['jabra']['classification']=='pass'
    assert not result['linearity_failures'] and result['clean_amplitudes_percent']==[]


@pytest.mark.parametrize('mutation',['missing','nan','missing-noise'])
def test_missing_or_nonfinite_linearity_never_defaults_to_true(mutation):
    saved=sweep()
    if mutation=='missing':saved['linearity']=[]
    elif mutation=='nan':saved['linearity'][0]['chains']['board']['fundamental_dbfs']=float('nan')
    else:saved['linearity'][0]['chains']['board'].pop('noise_dbfs')
    with pytest.raises(ContractError,match='linearity'):audio.linearity_report(saved)


def test_bad_fundamental_calibration_and_missing_calibration_fail_clearly():
    cal=calibration();cal['1500']['classification']='unmeasurable'
    with pytest.raises(ContractError,match='fundamental calibration'):audio.classify_chain(measurement(),1500,cal)
    for frequency in ('1500','7500'):
        cal=calibration();del cal[frequency]
        with pytest.raises(ContractError,match=f'missing calibration frequency {frequency}'):
            audio.classify_chain(measurement(),1500,cal)


def test_dropped_h5_reports_floor_response_bound_without_changing_thd():
    cal=calibration();cal['7500'].update(classification='unmeasurable',tone_dbfs=-95,noise_dbfs=-98,snr_db=3)
    point=measurement();result=audio.classify_chain(point,1500,cal)
    bound=point['harmonics_dbc']['H5']+cal['1500']['tone_dbfs']-cal['7500']['noise_dbfs']
    assert result['harmonics']['H5']['floor_response_bound_dbc']==bound
    assert not result['harmonics']['H5']['included_in_thd']
    assert result['corrected_thd_percent']<5<result['floor_response_thd_bound_percent']


def test_exclusive_output_p4_only_and_source_preservation(tmp_path,monkeypatch):
    directory=tmp_path/'source';directory.mkdir();saved=sweep()
    run=dict(board='p4xc',acquisition_passed=True,elf_sha256='a'*64,restore=dict(clean=True))
    saved['calibration']=[]
    source=directory/'acoustic.json';source.write_text(json.dumps(saved))
    run_source=directory/'result.json';run_source.write_text(json.dumps(run))
    before=[p.read_bytes() for p in (source,run_source)]
    monkeypatch.setattr(audio,'calibration_evidence',lambda *args:{c:calibration() for c in audio.CHAINS})
    out=tmp_path/'new.json';result=audio.reanalyze(directory,out,True)
    assert result['decision_id']==audio.DECISION_ID and result['clean_band_decision_id']==audio.CLEAN_BAND_DECISION
    assert result['report_only'] and result['post_hoc'] and not result['qualification']
    assert result['source_acoustic_sha256']==audio.digest(source)
    assert before==[p.read_bytes() for p in (source,run_source)]
    with pytest.raises(ContractError,match='new reanalysis'):audio.reanalyze(directory,out)
    with pytest.raises(ContractError,match='outside'):audio.reanalyze(directory,directory/'bad.json')
    run['board']='echoear';run_source.write_text(json.dumps(run))
    with pytest.raises(ContractError,match='P4 only'):audio.reanalyze(directory,tmp_path/'s3.json')


@pytest.mark.skipif(not os.environ.get('P4_MEASURABLE_EVIDENCE'),reason='external retained acoustic evidence not configured')
@pytest.mark.parametrize('name',['acoustic-retake','acoustic'])
def test_retained_real_acquisitions(name,tmp_path):
    directory=Path(os.environ['P4_MEASURABLE_EVIDENCE'])/name
    original=json.loads((directory/'acoustic.json').read_text())
    result=audio.reanalyze(directory,tmp_path/f'{name}.json',name=='acoustic')
    assert result['calibration']['jabra']['7500']['snr_db']<10
    assert result['calibration']['board']['7500']['snr_db']>=10
    rows=[p for p in result['points'] if p['hz']==1500]
    assert all(p['chains']['jabra']['harmonics']['H5']['classification']=='unmeasurable' for p in rows)
    fixed=[]
    for point in original['points']:
        if point['hz']!=1500:continue
        updated=next(p for p in rows if p['name']==point['name'])['chains']['jabra']
        if point['chains']['jabra']['corrected_thd_percent']>5 and updated['classification']=='pass':
            fixed.append(point['name'])
    assert fixed  # The actual H5 artifact is removed in both retained runs.
    # The new SNR rule deliberately changes failure into unavailable linearity:
    # the dedicated board endpoints still cannot establish any evaluated step.
    assert result['clean_amplitudes_percent']==[] and not result['linearity_valid']
    assert result['linearity_unavailable_chains']==['board']
    assert not result['linearity_failures']
    if name=='acoustic-retake':
        step=next(s for s in result['linearity_steps'] if s['chain']=='board' and s['from_percent']==2)
        assert step['doubling_db']==pytest.approx(7.054954295019229)
        assert step['classification']=='unmeasurable' and step['passed'] is None
        assert [e['snr_db'] for e in step['endpoints']]==pytest.approx([.81245260278456,7.50458018259385])
