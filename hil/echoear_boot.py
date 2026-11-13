#!/usr/bin/env python3
"""EchoEar identity, boot and idle memory evidence; run under hil/bench.py."""
import argparse
import json
import re
from pathlib import Path
from esp_serial import EspSerial
from phase2_endurance import snapshot
from echoear_checks import codec_boot_evidence

p=argparse.ArgumentParser();p.add_argument('--out',required=True,type=Path);a=p.parse_args()
a.out.mkdir(parents=True,exist_ok=True)
result={'passed':False}
try:
    with EspSerial(log_path=a.out/'serial.log') as board:
        start=board.cursor
        board.reset(timeout=20)
        board.wait_for('READY transport-console',60,since=start)
        result['ping']=board.command('ping')
        result['census']=snapshot(board,allow_incomplete=True)
        result['external_heap_region_bytes']=sum(r['length'] for r in result['census']['regions'] if r['kind']=='external')
        result['psram_heap_not_truncated']=14000000<=result['external_heap_region_bytes']<=16777216
        text='\n'.join(x[2] for x in board.lines_since(start))
        result['psram_16mb']=bool(re.search(r'Found 16MB PSRAM|size\s*:\s*128 Mbit',text))
        result['wifi_ready']='WIFI READY' in text or 'got ip:' in text
        result['codec_initialized']='ES7210 MIC1 mono' in text
        result['codec_readback']=codec_boot_evidence(text)
        result['codec_verified']=result['codec_readback']['passed']
        result['i2c_error']='I2C_If: Fail' in text
        result['psram_truncated']='Virtual address not enough' in text
        result['fatal']=bool(re.search(r'Guru Meditation|assert failed|abort\(\)|Task watchdog',text))
        result['passed']=all(result[k] for k in ['psram_16mb','wifi_ready','codec_initialized','codec_verified','psram_heap_not_truncated']) and not any(result[k] for k in ('fatal','i2c_error','psram_truncated'))
except Exception as e:
    result['error']=f'{type(e).__name__}: {e}'
(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k!='census'},indent=2))
if 'census' in result: print('heap:',result['census']['heap'])
raise SystemExit(0 if result['passed'] else 1)
