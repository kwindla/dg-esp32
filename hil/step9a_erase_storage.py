#!/usr/bin/env python3
"""Authorized storage-only erase, gated by two verified complete backups and restore docs."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
from esp_serial import port_lock

ROOT=Path('/home/khkramer/src/dg-esp32')
OUT=Path('/home/khkramer/src/evidence/step9a/full-flash-backup')
PYTHON='/home/khkramer/.espressif/python_env/idf6.1_py3.12_env/bin/python'


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as file:
        for data in iter(lambda:file.read(1024*1024),b''):h.update(data)
    return h.hexdigest()


def main():
    evidence=json.loads((OUT/'verification.json').read_text());assert evidence['identical'] and len(evidence['reads'])==2
    for record in evidence['reads']:assert Path(record['path']).stat().st_size==16777216 and sha(Path(record['path']))==record['sha256']
    baseline=(OUT/'flash-read-1.bin').read_bytes();assert baseline==(OUT/'flash-read-2.bin').read_bytes()
    pin=json.loads((ROOT/'sources/waveshare-demo-pin.json').read_text());assert sha(ROOT/'sources'/pin['archive'])==pin['sha256']
    docs=(ROOT/'docs/board.md').read_text();assert '## Restore' in docs and 'write-flash' in docs and str(OUT/'flash-read-1.bin') in docs and pin['sha256'] in docs
    partitions=[]
    for pos in range(0x8000,0x9000,32):
        magic,kind,subtype,offset,size,label,flags=struct.unpack_from('<HBBII16sI',baseline,pos)
        if magic!=0x50aa:break
        partitions.append({'label':label.split(b'\0')[0].decode(),'type':kind,'subtype':subtype,'offset':offset,'bytes':size})
    storage=next(p for p in partitions if p['label']=='storage');assert storage['offset']==0x410000 and storage['bytes']==0x400000
    result={'authorization':'user storage ruling; full backup and restore/source prerequisites verified','partitions':partitions,'erased_offset':'0x410000','erased_bytes':'0x400000','before_storage_sha256':hashlib.sha256(baseline[0x410000:0x810000]).hexdigest(),'completed':False}
    (OUT/'erase-verification.json').write_text(json.dumps(result,indent=2)+'\n')
    with port_lock():
        argv=[PYTHON,'-m','esptool','--chip','esp32s3','--port','/dev/ttyACM0','--baud','460800','--before','default-reset','--after','hard-reset']
        with (OUT/'erase-storage.log').open('w') as log:subprocess.run(argv+['erase-region','0x410000','0x400000'],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=600,cwd=ROOT)
        after=OUT/'after-storage-erase.bin'
        with (OUT/'read-after-erase.log').open('w') as log:subprocess.run(argv+['read-flash','0x0','0x1000000',str(after)],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=900,cwd=ROOT)
        after.chmod(0o600);data=after.read_bytes();assert len(data)==16777216
        result['storage_all_ff']=data[0x410000:0x810000]==b'\xff'*0x400000;result['before_storage_unchanged']=data[:0x410000]==baseline[:0x410000];result['after_storage_unchanged']=data[0x810000:]==baseline[0x810000:];result['after_flash_sha256']=hashlib.sha256(data).hexdigest();result['completed']=all(result[k] for k in ('storage_all_ff','before_storage_unchanged','after_storage_unchanged'))
        (OUT/'erase-verification.json').write_text(json.dumps(result,indent=2)+'\n');assert result['completed'],'Storage-only erase verification failed'
    print(json.dumps({k:result[k] for k in ('completed','storage_all_ff','before_storage_unchanged','after_storage_unchanged','after_flash_sha256')}),flush=True)

if __name__=='__main__':main()
