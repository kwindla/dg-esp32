#!/usr/bin/env python3
"""Two full-flash reads under serial ownership; never writes or erases flash."""
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from esp_serial import port_lock

OUT=Path('/home/khkramer/src/evidence/step9a/full-flash-backup')
PYTHON='/home/khkramer/.espressif/python_env/idf6.1_py3.12_env/bin/python'


def main():
    OUT.mkdir(parents=True,exist_ok=False,mode=0o700)
    result={'created_utc':datetime.now(timezone.utc).isoformat(),'bytes':16777216,'reads':[],'identical':False,'storage_address':'0x410000','storage_bytes':'0x400000','esptool_version':'5.5.0'}
    with port_lock():
        for index in (1,2):
            path=OUT/f'flash-read-{index}.bin'
            command=[PYTHON,'-m','esptool','--chip','esp32s3','--port','/dev/ttyACM0','--baud','460800','--before','default-reset','--after','hard-reset','read-flash','0x0','0x1000000',str(path)]
            with (OUT/f'read-{index}.log').open('w') as log:subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=900,cwd='/home/khkramer/src/dg-esp32')
            os.chmod(path,0o600)
            size=path.stat().st_size
            if size!=16777216:raise RuntimeError('Full flash size mismatch')
            sha=hashlib.sha256(path.read_bytes()).hexdigest();result['reads'].append({'path':str(path),'bytes':size,'sha256':sha,'command':command});(OUT/'verification.json').write_text(json.dumps(result,indent=2)+'\n');print('READ',index,'bytes',size,'sha256',sha,flush=True)
        result['identical']=result['reads'][0]['sha256']==result['reads'][1]['sha256'] and (OUT/'flash-read-1.bin').read_bytes()==(OUT/'flash-read-2.bin').read_bytes()
        (OUT/'verification.json').write_text(json.dumps(result,indent=2)+'\n')
        (OUT/'SHA256SUMS').write_text(''.join(f"{r['sha256']}  {Path(r['path']).name}\n" for r in result['reads']))
        if not result['identical']:raise RuntimeError('Two full flash reads differ; erasing is forbidden')
    print('FULL FLASH BACKUP VERIFIED IDENTICAL',flush=True)

if __name__=='__main__':main()
