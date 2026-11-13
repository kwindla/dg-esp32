#!/usr/bin/env python3
"""Offline P4 THD and linearity over measurable evidence (fixes 7e/7f).

Separate from the frozen acquisition/reanalysis entrypoints. No device IO.
"""
import argparse
import json
import math
from pathlib import Path
import wave

import numpy as np

from make_bundle import digest
from p4_audio import DECISION_ID as CLEAN_BAND_DECISION, clean_run
from wire_contract import require

DECISION_ID = 'decision-2026-11-01-p4-thd-measurable-harmonics'
LINEARITY_DECISION_ID = 'decision-2026-11-01-p4-linearity-measurable'
NOISE_METHOD = 'calibration-local-residual-power-40-to-200-hz-v1'
CHAINS = ('board', 'jabra')
HARMONICS = ('H2', 'H3', 'H4', 'H5')
MIN_SNR_DB = 10


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def calibration_evidence(directory, rows):
    """Integrate local residual power, without per-bin/bandwidth normalization.

    The +/-20 Hz tone measurement and FFT scaling match speaker_sweep_test.
    A +/-40 Hz guard avoids its leakage skirt; the two 160 Hz sidebands
    measure the local chain background in the same capture/scoring window.
    This is integrated noise (nominal 320 Hz), not a noise density or a
    40 Hz equivalent-band estimate. Constants are shared by all frequencies.
    """
    result = {chain:{} for chain in CHAINS}
    for row in rows:
        hz = row['hz']
        require(type(hz) is int and hz>0, 'positive integer calibration frequency required')
        for chain in CHAINS:
            require(str(hz) not in result[chain], 'duplicate calibration frequency')
            path = directory/f'{row["name"]}-{chain}.wav'
            require(path.resolve().is_relative_to(directory.resolve()), 'calibration WAV outside acquisition')
            with wave.open(str(path), 'rb') as wav:
                rate = wav.getframerate()
                require(wav.getnchannels()==1 and wav.getsampwidth()==2 and wav.getcomptype()=='NONE',
                        'native mono PCM16 calibration WAV required')
                samples = np.frombuffer(wav.readframes(wav.getnframes()), '<i2').astype(float)/32768
            measured = row['chains'][chain]
            require(rate==16000 and measured['native_capture_rate_hz']==rate and
                    measured['wav_rate_hz']==rate and measured['recovered_phase'] is None,
                    'native 16 kHz calibration required; resampled evidence unsupported')
            start, end = row['windows'][chain]
            require(finite(start) and finite(end) and 0<=start<end<=len(samples)/rate,
                    'valid saved calibration scoring window required')
            samples = samples[int(start*rate):int(end*rate)]
            require(len(samples)>=rate*.4, 'too short calibration window')
            samples -= samples.mean()
            window = np.hanning(len(samples))
            power = 2*np.abs(np.fft.rfft(samples*window))**2/(len(samples)*np.sum(window**2))
            frequencies = np.fft.rfftfreq(len(samples), 1/rate)
            near = np.flatnonzero(np.abs(frequencies-hz)<=20)
            actual_hz = float(frequencies[near[np.argmax(power[near])]])
            distance = np.abs(frequencies-actual_hz)
            tone = distance<=20
            noise = (distance>40) & (distance<=200)
            require(actual_hz-200>0 and actual_hz+200<rate/2 and np.any(noise),
                    'complete local noise sidebands required')
            db = lambda p: float(10*np.log10(max(float(p), 1e-24)))
            tone_dbfs, noise_dbfs = db(power[tone].sum()), db(power[noise].sum())
            require(finite(measured['fundamental_dbfs']) and
                    abs(tone_dbfs-measured['fundamental_dbfs'])<1e-6 and
                    actual_hz==measured['fundamental_hz'], 'calibration WAV disagrees with saved tone measurement')
            snr = tone_dbfs-noise_dbfs
            result[chain][str(hz)] = dict(hz=hz,actual_hz=actual_hz,tone_dbfs=tone_dbfs,
                noise_dbfs=noise_dbfs,snr_db=snr,classification='measurable' if snr>=MIN_SNR_DB else 'unmeasurable',
                noise_method=NOISE_METHOD,noise_bins=int(np.count_nonzero(noise)),
                noise_bandwidth_hz=float(np.count_nonzero(noise)*rate/len(samples)),
                sample_rate_hz=rate,scoring_window_seconds=[start,end],
                wav=str(path.resolve()),wav_sha256=digest(path))
    return result


def classify_chain(measurement, hz, calibration):
    fundamental, noise = measurement['fundamental_dbfs'], measurement['noise_dbfs']
    require(finite(fundamental) and finite(noise), 'finite fundamental/noise required')
    require(measurement['native_capture_rate_hz']==16000, 'native 16 kHz point required')
    clips = measurement['clipping_count']
    require(type(clips) is int and clips>=0, 'nonnegative clip count required')
    harmonics = {}
    require(str(hz) in calibration, f'missing calibration frequency {hz} Hz')
    require(calibration[str(hz)]['classification']=='measurable',
            f'fundamental calibration at {hz} Hz is unmeasurable')
    response = calibration[str(hz)]['tone_dbfs']
    for name in HARMONICS:
        frequency = hz*int(name[1:])
        raw = measurement['harmonics_dbc'][name]
        if frequency+20>=8000:
            require(raw is None, 'above-Nyquist harmonic must be unavailable')
            harmonics[name] = dict(hz=frequency,classification='unavailable-above-nyquist',
                calibration_snr_db=None,raw_dbc=None,corrected_dbc=None,included_in_thd=False,
                floor_response_bound_dbc=None)
            continue
        require(finite(raw), 'in-band harmonic measurement required')
        require(str(frequency) in calibration, f'missing calibration frequency {frequency} Hz')
        evidence = calibration[str(frequency)]
        measurable = evidence['classification']=='measurable'
        harmonics[name] = dict(hz=frequency,classification=evidence['classification'],
            calibration_snr_db=evidence['snr_db'],calibration_tone_dbfs=evidence['tone_dbfs'],
            calibration_noise_dbfs=evidence['noise_dbfs'],raw_dbc=raw,
            corrected_dbc=raw+response-evidence['tone_dbfs'],included_in_thd=measurable,
            floor_response_bound_dbc=raw+response-evidence['noise_dbfs']
                if not measurable and name in ('H4','H5') else None)
    thd = 100*math.sqrt(sum(10**(h['corrected_dbc']/10) for h in harmonics.values() if h['included_in_thd']))
    # An in-band H2/H3 without calibration SNR cannot satisfy its bound by
    # omission. Other measurable violations still fail this chain.
    missing_bounds = [name for name in ('H2','H3') if harmonics[name]['classification']=='unmeasurable']
    failed_bounds = [name for name in ('H2','H3') if harmonics[name]['included_in_thd'] and
                     harmonics[name]['corrected_dbc']>-26]
    snr = fundamental-noise
    state = ('unmeasurable' if snr<MIN_SNR_DB else
             'fail' if clips or thd>5 or failed_bounds else
             'unmeasurable' if missing_bounds else 'pass')
    return dict(classification=state,snr_db=snr,fundamental_dbfs=fundamental,noise_dbfs=noise,
        corrected_thd_percent=thd,harmonics=harmonics,unmeasurable_bounded_harmonics=missing_bounds,
        failed_harmonic_bounds=failed_bounds,clipping_count=clips,
        floor_response_thd_bound_percent=100*math.sqrt((thd/100)**2+sum(
            10**(h['floor_response_bound_dbc']/10) for h in harmonics.values()
            if h['floor_response_bound_dbc'] is not None)))


def linearity_report(saved):
    """Evaluate original dedicated steps only when both endpoints are measurable."""
    linearity = saved['linearity']
    require([p['amplitude_percent'] for p in linearity]==[1,2,4,8] and
            all(p['hz']==1000 for p in linearity), 'original 1000 Hz 1/2/4/8 linearity evidence required')
    require(all(finite(p['chains'][c].get(key)) for p in linearity for c in CHAINS
                for key in ('fundamental_dbfs','noise_dbfs')), 'finite original linearity levels/noise required')
    doubling = {c:[b['chains'][c]['fundamental_dbfs']-a['chains'][c]['fundamental_dbfs']
                  for a,b in zip(linearity,linearity[1:])] for c in CHAINS}
    steps = []; chains = {}; failures = []
    for chain in CHAINS:
        for a,b,value in zip(linearity,linearity[1:],doubling[chain]):
            endpoints = []
            for point in (a,b):
                measurement = point['chains'][chain]
                snr = measurement['fundamental_dbfs']-measurement['noise_dbfs']
                endpoints.append(dict(name=point.get('name'),hz=point['hz'],repeat=point.get('repeat'),
                    amplitude_percent=point['amplitude_percent'],fundamental_dbfs=measurement['fundamental_dbfs'],
                    noise_dbfs=measurement['noise_dbfs'],snr_db=snr,
                    classification='measurable' if snr>=MIN_SNR_DB else 'unmeasurable'))
            evaluated = all(e['snr_db']>=MIN_SNR_DB for e in endpoints)
            step = dict(chain=chain,from_percent=a['amplitude_percent'],to_percent=b['amplitude_percent'],
                doubling_db=value,expected_db=6.0206,tolerance_db=1,endpoints=endpoints,
                classification='evaluated' if evaluated else 'unmeasurable',
                passed=abs(value-6.0206)<=1 if evaluated else None)
            steps.append(step)
            if evaluated and not step['passed']: failures.append(step)
        evaluated = [step for step in steps if step['chain']==chain and step['classification']=='evaluated']
        chains[chain] = dict(evaluated_steps=len(evaluated),available=bool(evaluated),
            classification='unavailable' if not evaluated else 'pass' if all(s['passed'] for s in evaluated) else 'fail')
    available = all(c['available'] for c in chains.values())
    return dict(linearity_doubling_db=doubling,linearity_steps=steps,linearity_chains=chains,
        linearity_available=available,linearity_valid=available and not failures,linearity_failures=failures,
        linearity_unavailable_chains=[c for c in CHAINS if not chains[c]['available']])


def analyze(saved, calibration):
    frequencies = sorted({p['hz'] for p in saved['points']}-{500})
    require(frequencies==[800,1000,1500,3000] and any(p['hz']==500 for p in saved['points']),
            'complete P4 frequency sweep required')
    amplitudes = sorted({p['amplitude_percent'] for p in saved['points']})
    require(all(finite(a) and a>0 for a in amplitudes), 'positive finite amplitudes required')
    linearity = linearity_report(saved)
    points = []
    for point in saved['points']:
        chains = {c:classify_chain(point['chains'][c],point['hz'],calibration[c]) for c in CHAINS}
        states = [c['classification'] for c in chains.values()]
        state = 'fail' if 'fail' in states else 'pass' if 'pass' in states else 'unmeasurable'
        points.append(dict(name=point['name'],hz=point['hz'],amplitude_percent=point['amplitude_percent'],
            repeat=point['repeat'],excluded=point['hz']==500,classification=state,chains=chains))
    clean = []; started = False
    for amp in amplitudes:
        rows = [p for p in points if not p['excluded'] and p['amplitude_percent']==amp]
        require(len(rows)==2*len(frequencies) and all(
            sum(p['hz']==hz and p['repeat']==repeat for p in rows)==1 for hz in frequencies for repeat in (1,2)),
            'need both original repeats at every retained frequency/amplitude')
        failed = any(p['classification']=='fail' for p in rows)
        if not started:
            coverage = all(any(p['hz']==hz and any(c['classification']=='pass' for c in p['chains'].values())
                               for p in rows) for hz in frequencies)
            if failed or not coverage: continue
            started = True
        if not linearity['linearity_valid'] or failed: break
        clean.append(amp)
    bands = {}
    if clean:
        for hz in frequencies:
            bands[str(hz)] = {}
            for chain in CHAINS:
                levels = [p['chains'][chain]['fundamental_dbfs'] for p in points if p['hz']==hz and
                          p['amplitude_percent'] in clean and p['chains'][chain]['classification']=='pass']
                bands[str(hz)][chain] = [min(levels),max(levels)] if levels else None
    return dict(points=points,retained_hz=frequencies,excluded_hz=[500],clean_amplitudes_percent=clean,
        level_band_dbfs=bands or None,**linearity)


def analysis(directory, report_only=False):
    """Recompute from retained inputs without creating or changing any file."""
    directory = Path(directory)
    source, run_source = directory/'acoustic.json', directory/'result.json'
    saved, run = json.loads(source.read_text()), json.loads(run_source.read_text())
    clean_run(run)
    require(run.get('board')=='p4xc', 'measurable harmonic decision is P4 only')
    require(run.get('acquisition_passed') is True and saved.get('acquisition_passed') is True and
            not saved.get('error'), 'completed acoustic acquisition required')
    calibration = calibration_evidence(directory, saved['calibration'])
    band = analyze(saved, calibration)
    result = dict(schema=2,kind='offline-p4-measurable-harmonics',post_hoc=True,qualification=False,
        report_only=bool(report_only),decision_applied=True,decision_id=DECISION_ID,
        clean_band_decision_id=CLEAN_BAND_DECISION,linearity_decision_id=LINEARITY_DECISION_ID,board='p4xc',
        scope='offline reanalysis; original acoustic verdict unchanged',
        source_elf_sha256=run.get('elf_sha256'),source_plan_sha256=saved['plan_sha256'],
        source_acoustic=str(source.resolve()),source_acoustic_sha256=digest(source),
        source_result=str(run_source.resolve()),source_result_sha256=digest(run_source),restore=run['restore'],
        source_clean_amplitudes_percent=saved['clean_amplitudes_percent'],calibration=calibration,
        rule=dict(id='p4-measurable-harmonics-contiguous-v2',min_chain_snr_db=10,min_calibration_snr_db=10,
            noise_method=NOISE_METHOD,noise_guard_hz=40,noise_outer_hz=200,
            noise_power='sum of local residual FFT bins; no bandwidth normalization',
            calibration_comparison='tone-noise >= 10 dB is measurable',
            bounded_unmeasurable='in-band H2/H3 cannot pass by omission; chain unmeasurable unless measurable violation fails',
            start='lowest all-pass-or-unmeasurable amplitude with a measurable pass per retained frequency',
            continuation='adjacent amplitudes until first measurable failure; never restart',
            corrected_thd_percent_max=5,h2_h3_dbc_max=-26,clips_max=0,linearity_doubling_tolerance_db=1,
            linearity_source='original dedicated 1000 Hz captures at 1/2/4/8 percent',
            linearity_evaluation='both endpoints >=10 dB SNR; at least one evaluated step per chain'),**band)
    return result


def reanalyze(directory, out, report_only=False):
    directory, out = Path(directory), Path(out)
    require(not out.exists(), 'new reanalysis output required')
    require(not out.resolve().is_relative_to(directory.resolve()), 'output must be outside read-only acquisition')
    result = analysis(directory,report_only)
    with out.open('x') as stream:
        stream.write(json.dumps(result,indent=2,allow_nan=False)+'\n')
    return result


def reanalysis_gate(path, manifest):
    """Authenticate every decision, retained input and computed result before IO."""
    path = Path(path)
    row = json.loads(path.read_text())
    require(row.get('kind')=='offline-p4-measurable-harmonics' and row.get('board')=='p4xc' and
            row.get('decision_applied') is True and row.get('decision_id')==DECISION_ID and
            row.get('clean_band_decision_id')==CLEAN_BAND_DECISION and
            row.get('linearity_decision_id')==LINEARITY_DECISION_ID and row.get('post_hoc') is True and
            row.get('qualification') is False, 'approved P4 measurable-harmonic/linearity decisions required')
    require(row.get('report_only') is False, 'report-only reanalysis cannot establish live eligibility')
    source = Path(row['source_acoustic']); run_source = source.parent/'result.json'
    require(row.get('source_acoustic_sha256')==digest(source) and
            row.get('source_result_sha256')==digest(run_source), 'acoustic reanalysis input hash mismatch')
    require(row.get('source_elf_sha256')==manifest['elf_sha256'], 'reanalysis must use this P4 bundle ELF')
    for chain in row['calibration'].values():
        for calibration in chain.values():
            require(digest(Path(calibration['wav']))==calibration['wav_sha256'],
                    'calibration WAV hash mismatch')
    expected = analysis(source.parent)
    require(row==expected, 'reanalysis result differs from the approved rule or retained calibration inputs')
    require(bool(row['clean_amplitudes_percent']) and bool(row['level_band_dbfs']) and row['linearity_valid'],
            'non-empty reanalyzed P4 clean band with available passing linearity required before voice smoke')
    return dict(reanalysis_file=str(path.resolve()),reanalysis_sha256=digest(path),decision_id=DECISION_ID,
        clean_band_decision_id=CLEAN_BAND_DECISION,linearity_decision_id=LINEARITY_DECISION_ID,
        post_hoc=True,result_sha256=digest(run_source),acoustic_sha256=digest(source),restore=row['restore'],
        clean_amplitudes_percent=row['clean_amplitudes_percent'],level_band_dbfs=row['level_band_dbfs'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    command = sub.add_parser('reanalyze')
    command.add_argument('directory',type=Path)
    command.add_argument('--out',type=Path,required=True)
    command.add_argument('--report-only',action='store_true')
    args = parser.parse_args()
    result = reanalyze(args.directory,args.out,args.report_only)
    print(json.dumps({key:result[key] for key in ('decision_id','report_only','clean_amplitudes_percent','level_band_dbfs')},indent=2))
    return 0


if __name__=='__main__': raise SystemExit(main())
