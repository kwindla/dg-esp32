"""Coordinator-authorized OLD-image-only geometry calibration; no rescoring."""
import hashlib
import json
import math
from pathlib import Path

# Decision 134 D4: the bench has not moved since 10-14/15; the same OLD-image geometry calibration is re-bound
# to the step-10 requal-n1 normal ELF (the 9936da45 policy is historical).
NORMAL='55152f25dcec0e164c8a087d7f5962dd81191e2cdd3f41ac882a58a0a57a626e'
OLD='94a5a13ccd5555a8fd39710a140cf514c012d7d7e248554d632915b61ec7d444'


def derive(historical_dbfs,current_old_dbfs,fixed_rise_db,long_rise_db):
    values=(historical_dbfs,current_old_dbfs,fixed_rise_db,long_rise_db)
    if not all(type(v) in (int,float) and math.isfinite(v) for v in values):
        raise ValueError('Finite historical OLD-image measurements required')
    offset=historical_dbfs-current_old_dbfs
    return dict(geometry_attenuation_db=offset,target_dbfs=-22-offset,tolerance_db=3,
        minimum_rise_db=10,volume_relative_db=0,
        expected_fixed_reply_rise_db=fixed_rise_db-offset,
        expected_long_reply_rise_db=long_rise_db-offset)


def load(path):
    value=json.loads(Path(path).read_text())
    if value.get('authorization')!='explicit-coordinator-geometry-decision' or value.get('normal_elf_sha256')!=NORMAL:
        raise ValueError('Explicit geometry decision and frozen normal ELF required')
    required={str(Path(value[k]).resolve()) for k in ('old_reference_plan','old_acquisition','long_reference_plan')}
    if not required.issubset(value['source_sha256']):raise ValueError('Missing frozen calibration inputs')
    for name,wanted in value['source_sha256'].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=wanted:
            raise ValueError('Rebaseline source changed: '+name)
    plan=json.loads(Path(value['old_reference_plan']).read_text())
    acquired=json.loads(Path(value['old_acquisition']).read_text())['arms'][0]
    if plan['old_elf_sha256']!=OLD or acquired['elf_sha256']!=OLD or acquired['role']!='A-old':
        raise ValueError('Wrong historical reference image/arm')
    if acquired['reference_sha256']!=plan['reference']['sha256']:
        raise ValueError('Historical and new-geometry OLD reply differ')
    long_reference=json.loads(Path(value['long_reference_plan']).read_text())
    result=derive(plan['analysis']['historical_replayed']['jabra']['rms_dbfs'],
        acquired['measurements']['jabra']['rms_dbfs'],
        plan['analysis']['historical_replayed']['jabra']['alignment']['rms_rise_db'],
        long_reference['analysis']['historical_replayed']['jabra']['alignment']['rms_rise_db'])
    if value['calibration']!=result or value.get('prior_failures_unchanged') is not True:
        raise ValueError('Rebaseline arithmetic or preservation policy differs')
    return dict(result,policy_path=str(Path(path).resolve()),
        policy_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),old_reference_elf_sha256=OLD)
