#!/usr/bin/env python3
"""Fixed-reference geometry discrimination. Never modifies qualification gates."""
import json
import math
from pathlib import Path
import numpy as np


def measure(jabra,native,reference,*,jabra_search_s,baseline_end_s,native_search_s=.2):
    from step7_support import acoustic_match,pcm
    from bench_calibration import dbfs
    source=pcm(reference);rows={}
    for name,path,first,baseline in (('jabra',jabra,jabra_search_s,baseline_end_s),('native',native,native_search_s,.3)):
        match=acoustic_match(path,reference,first,0,baseline)
        samples=pcm(path);at=round(match['offset_s']*16000);segment=samples[at:at+len(source)]
        if len(segment)!=len(source):raise ValueError('Truncated matched full-reference window')
        rows[name]=dict(alignment=match,rms_dbfs=dbfs(segment),peak=float(np.max(np.abs(segment))),
            peak_dbfs=20*math.log10(max(1,float(np.max(np.abs(segment))))/32768),samples=len(segment))
    rows['passed']=all(row['alignment']['correlation']>=.6 for row in rows.values())
    from mic_headroom import microphone_headroom
    rows['native_headroom_full_capture']=microphone_headroom(pcm(native),125)
    return rows


def analyze(plan,arms):
    if len(arms)!=3 or [a.get('role') for a in arms]!=['A-old','B-new','A-old-repeat']:
        return dict(classification='inconclusive',reason='Need exactly the original A/B/A arms')
    try:
        if any(a.get('diagnostic_valid') is not True for a in arms):raise ValueError('An original diagnostic arm failed')
        expected=[plan['old_elf_sha256'],plan['new_elf_sha256'],plan['old_elf_sha256']]
        if any(a.get('elf_sha256')!=wanted or a.get('reference_sha256')!=plan['reference']['sha256'] for a,wanted in zip(arms,expected)):
            raise ValueError('Exact image/reference binding missing or changed')
        levels=[a['measurements']['jabra']['rms_dbfs'] for a in arms]
        peaks=[a['measurements']['native']['peak_dbfs'] for a in arms]
        if not all(type(v) in (int,float) and math.isfinite(v) for v in (*levels,*peaks)):raise ValueError('Unavailable finite levels')
        old=(levels[0]+levels[2])/2;native=(peaks[0]+peaks[2])/2
        spec=plan['analysis'];historical=spec['historical_replayed']
        delta=old-historical['jabra']['rms_dbfs'];difference=levels[1]-old
        historical_peak_db=20*math.log10(historical['native']['peak']/32768)
        checks=dict(old_repeatability=abs(levels[0]-levels[2])<=spec['old_repeatability_max_db'],
            old_native_repeatability=abs(peaks[0]-peaks[2])<=spec['native_new_vs_old_mean_abs_max_db'],
            no_old_new_level_difference=abs(difference)<=spec['new_minus_old_mean_abs_max_db'],
            native_comparable=abs(peaks[1]-native)<=spec['native_new_vs_old_mean_abs_max_db'],
            historical_native_stable=abs(native-historical_peak_db)<=spec['native_old_vs_historical_max_db'],
            old_shift=spec['geometry_shift_db_range'][0]<=delta<=spec['geometry_shift_db_range'][1])
        classification='inconclusive';candidate=None
        if all(checks.values()):
            classification='changed-measurement-path-consistent'
            candidate=dict(target_dbfs=-22+delta,tolerance_db=3,minimum_rise_db=10,volume_relative_db=0,
                basis='Original -22 center shifted by OLD-only matched-reference new-minus-historical delta',
                requires_astra_review=True,applied=False)
        elif checks['old_repeatability'] and abs(delta)<=3 and all(levels[1]-levels[i]<=-3 for i in (0,2)):
            classification='new-firmware-level-regression'
        return dict(classification=classification,checks=checks,old_mean_dbfs=old,old_repeatability_db=abs(levels[0]-levels[2]),
            new_minus_old_db=difference,old_historical_shift_db=delta,native_new_minus_old_db=peaks[1]-native,
            native_old_minus_historical_db=native-historical_peak_db,new_band_candidate=candidate,
            qualification=False,unique_geometry_cause_proven=False,original_failures_unchanged=True)
    except (KeyError,TypeError,ValueError) as exc:return dict(classification='inconclusive',reason=str(exc),qualification=False)


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--plan',type=Path,required=True)
    p.add_argument('--arms',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();result=analyze(json.loads(a.plan.read_text()),json.loads(a.arms.read_text()))
    with a.out.open('x') as stream:stream.write(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
