#!/usr/bin/env python3
"""Capture all four 500-frame codec CPU cases, without a network session."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time
import numpy as np
from esp_serial import EspSerial,ROOT

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf');a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False);result=dict(passed=False,cases=[])
    try:
        with EspSerial('/dev/ttyACM0',log_path=out/'serial.log') as board:
            time.sleep(.2);cursor=board.cursor;board.reset();board.wait_for('READY transport-console',40,since=cursor)
            identity=board.wait_for('elf_sha256=',3,since=cursor)
            result['elf_sha256']=re.search(r'elf_sha256=([0-9a-f]{64})',identity)[1]
            assert result['elf_sha256']==hashlib.sha256(a.elf.read_bytes()).hexdigest()
            cursor=board.cursor;board.command('codec-bench',timeout=180)
            summary=board.wait_for(r'^CODEC_BENCH \{',10,since=cursor)
            result['metadata']=json.loads(summary.split(' ',1)[1])
            result['cases']=[json.loads(row[2].split(' ',1)[1]) for row in board.lines_since(cursor) if row[2].startswith('CODEC_CASE ')]
        assert len(result['cases'])==4 and result['metadata']['cases']==4
        for case in result['cases']:
            assert case['passed'],case.get('error','Codec case failed')
            assert case['stack_free']>=result['metadata']['stack_bytes']*.25,'Benchmark stack below 25% margin'
            alloc=case['native_allocations'];assert alloc['count']>0
            assert alloc['external' if case['state']=='internal' else 'internal']==0,'Unexpected codec allocation placement'
            for name in ['speech_encode','speech_decode','zero_encode','server_silence_decode']:
                measured=case[name];assert measured['frames']==len(measured['raw_cycles'])==500
                hz=result['metadata']['cpu_hz'];overhead=result['metadata']['clock_overhead_cycles']
                expected=np.maximum(0,np.array(measured['raw_cycles'])-overhead)*1e6/hz
                assert abs(float(expected.mean())-measured['mean_cpu_us'])<.001,'Timing conversion mismatch'
        result['passed']=True
    except Exception as error:result['error']=f'{type(error).__name__}: {error}'
    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='cases'},indent=2))
    return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
