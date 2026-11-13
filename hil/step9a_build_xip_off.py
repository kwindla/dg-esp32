#!/usr/bin/env python3
"""Build an isolated XIP-off diagnostic without modifying production sdkconfig."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT=Path('/home/khkramer/src/dg-esp32')
OUT=Path('/home/khkramer/src/evidence/step9a/resume/xip-off-image')
KEYS=('CONFIG_SPIRAM_XIP_FROM_PSRAM','CONFIG_SPIRAM_FETCH_INSTRUCTIONS','CONFIG_SPIRAM_RODATA')

def values(text):
    result={}
    for line in text.splitlines():
        if line.startswith('CONFIG_') and '=' in line:
            key,value=line.split('=',1);result[key]=value
        elif line.startswith('# CONFIG_') and line.endswith(' is not set'):
            result[line[2:-11]]='n'
    return result


def main():
    OUT.mkdir(parents=True,exist_ok=False)
    original=(ROOT/'sdkconfig').read_bytes();normal_elf=hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest()
    config=original.decode()
    for key in KEYS:
        config,count=re.subn(r'^'+key+r'=y$', '# '+key+' is not set', config, flags=re.MULTILINE)
        assert count==1,key
    diagnostic=ROOT/'build/step9a-xip-off.sdkconfig'
    descriptor=os.open(diagnostic,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as stream:stream.write(config)
    directory=ROOT/'build/step9a-xip-off'
    with (OUT/'build.log').open('w') as log:
        subprocess.run([str(ROOT/'scripts/idf.sh'),'-B',str(directory),'-DSDKCONFIG='+str(diagnostic),'build'],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=600)
    effective=values(diagnostic.read_text());before=values(original.decode());changed={k for k in before.keys()|effective.keys() if before.get(k)!=effective.get(k)}
    assert all(effective.get(k)=='n' for k in KEYS)
    assert all(effective.get(k)=='y' for k in ('CONFIG_SPI_MASTER_ISR_IN_IRAM','CONFIG_I2C_MASTER_ISR_HANDLER_IN_IRAM','CONFIG_GDMA_CTRL_FUNC_IN_IRAM','CONFIG_GDMA_ISR_HANDLER_IN_IRAM','CONFIG_GDMA_OBJ_DRAM_SAFE'))
    assert (ROOT/'sdkconfig').read_bytes()==original
    assert hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest()==normal_elf
    info={'purpose':'diagnostic attribution only; filesystem IO disabled; original ISR placement and audio policy','build_dir':str(directory),'selected_options':{k:effective[k] for k in KEYS},'changed_option_names':sorted(changed),'production_sdkconfig_unchanged':True,'production_elf_unchanged':True,'images':{}}
    for name in ('dg-esp32.bin','dg-esp32.elf','dg-esp32.map'):
        path=directory/name;info['images'][name]={'path':str(path),'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    assert info['images']['dg-esp32.bin']['bytes']<=3670016
    (OUT/'identity.json').write_text(json.dumps(info,indent=2)+'\n')
    print(json.dumps({'selected_options':info['selected_options'],'changed_option_names':info['changed_option_names'],'production_preserved':True,'bin_bytes':info['images']['dg-esp32.bin']['bytes']}),flush=True)
if __name__=='__main__':main()
