#!/usr/bin/env python3
"""Step 8c3 immutable production image and six-pair CPU crossover."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from esp_serial import ROOT
from step8c_campaign import VARIANTS,build_variant
from step8c2_campaign import retain_source,retain_placement
from step8e2a_closing import cpu


def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n')


def build(out):
    baseline=out/'images/8e2a-b652004'
    source=Path.home()/'src/evidence/step8e2a/images/combined06'
    shutil.copytree(source,baseline)
    identity=json.loads((baseline/'identity.json').read_text())
    actual=hashlib.sha256((baseline/'dg-esp32.elf').read_bytes()).hexdigest()
    assert actual==identity['elf_sha256']=='00da3b2c6838c27bd7f8f21aaae888a195dc969af381a23b9362bae64fcdfff1'
    manifest=json.loads((baseline/'source-manifest.json').read_text())
    differences=[];checked=0
    for name,sha in manifest['files'].items():
        if not (name.startswith(('main/','components/','cmake/')) or name in ('CMakeLists.txt','sdkconfig.defaults','dependencies.lock')):continue
        committed=subprocess.check_output(['git','show','b652004:'+name],cwd=ROOT)
        checked+=1
        if hashlib.sha256(committed).hexdigest()!=sha:differences.append(name)
    save(baseline/'committed-base-check.json',dict(commit='b652004',checked=checked,differences=differences,elf_sha256=actual))
    if differences:raise RuntimeError('Archived image source differs from committed baseline')
    name='step8c3-xip';VARIANTS[name]={};build_variant(out,name,bench=False)
    image=out/'images'/name;retain_source(image);retain_placement(image)
    import importlib.util
    spec=importlib.util.spec_from_file_location('sdkchecker',ROOT/'scripts/check-sdkconfig.py');checker=importlib.util.module_from_spec(spec);spec.loader.exec_module(checker)
    actual=checker.settings(ROOT/'build/step8c'/name/'sdkconfig')
    expected=dict(CONFIG_SPIRAM_XIP_FROM_PSRAM='y',CONFIG_SPIRAM_FETCH_INSTRUCTIONS='y',CONFIG_SPIRAM_RODATA='y',
        CONFIG_DG_OPUS_ENCODER_HOT_IRAM='n',CONFIG_DG_CODEC_EAGER_INITIALIZATION='y',CONFIG_DG_CACHE_COUNTERS='n',
        CONFIG_DG_CODEC_LAYOUT_TRACE='n',CONFIG_DG_PLAYBACK_BREAKDOWN='n',CONFIG_DG_PLAYBACK_DSP_IRAM='n',
        CONFIG_ESP32S3_DATA_CACHE_SIZE='0x8000',CONFIG_ESP32S3_DATA_CACHE_LINE_SIZE='32',
        CONFIG_ESPTOOLPY_FLASHMODE_DIO='y',CONFIG_ESPTOOLPY_FLASHMODE_QIO='n',CONFIG_DG_OPUS_FEC='n')
    differences=[k for k,v in expected.items() if actual.get(k)!=k+'='+v]
    save(image/'production-invariants.json',dict(expected=expected,differences=differences))
    if differences:raise RuntimeError('Candidate settings mismatch: '+','.join(differences))

def crossover(out,pairs=6):
    target=out/'cpu-ab.json'
    record=json.loads(target.read_text()) if target.exists() else dict(completed=False,name='cpu-ab',runs=[],protocol={})
    seen={(r['pair'],r['arm']) for r in record['runs']}
    record['protocol'].update(fresh_boots_per_arm=pairs,duration_s=140,window_s=30,
        arrival_trace=False,call_timing=False,cache_counters=False,
        production_dsp_target_dbfs=-22,production_ceiling_percent=26,
        expanded_by='Orchestrator follow-up: >=6 boots/arm and mode-stratified CPU comparison')
    record['completed']=False;save(target,record)
    try:
        for pair in range(1,pairs+1):
            for arm in ('A','B') if pair%2 else ('B','A'):
                if (pair,arm) in seen:continue
                image=out/'images'/('8e2a-b652004' if arm=='A' else 'step8c3-xip')
                label=f'cpu-ab-{arm}{pair:02}'
                print('CPU '+label,flush=True)
                summary=cpu(out,label,image)
                record['runs'].append(dict(pair=pair,arm=arm,image=str(image),
                    elf_sha256=hashlib.sha256((image/'dg-esp32.elf').read_bytes()).hexdigest(),summary=summary))
                save(target,record)
        record['completed']=True
    finally:save(target,record)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('mode',choices=('build','cpu'))
    p.add_argument('--pairs',type=int,default=6)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if a.mode=='build':build(a.out)
    else:crossover(a.out,pairs=a.pairs)
