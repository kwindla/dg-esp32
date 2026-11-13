#!/usr/bin/env python3
"""Flash declared boot/table/app images only, preserving physical storage."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from esp_serial import port_lock

PYTHON='/home/khkramer/.espressif/python_env/idf6.1_py3.12_env/bin/python'


def main():
 p=argparse.ArgumentParser();p.add_argument('--build-dir',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--retained-app',type=Path);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
 arguments=json.loads((a.build_dir/'flasher_args.json').read_text());images={};args=[]
 assert set(arguments['flash_files'])=={'0x0','0x8000','0x10000'}
 for offset,name in arguments['flash_files'].items():
  path=a.retained_app if offset=='0x10000' and a.retained_app else (a.build_dir/name).resolve();assert int(offset,16)+path.stat().st_size<=0x410000
  images[offset]={'path':str(path),'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()};args.extend([offset,str(path)])
 assert images['0x8000']['sha256']=='995004353d7fe64fecb95dc381c56e444a0bebfa365aae8f9a16d50e59a791df'
 with port_lock(),(a.out/'flash.log').open('w') as log:
  subprocess.run([PYTHON,'-m','esptool','--chip','esp32s3','--port','/dev/ttyACM0','--baud','460800','--before','default-reset','--after','hard-reset','write-flash','--flash-mode','keep','--flash-freq','keep','--flash-size','keep']+args,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=180)
 (a.out/'firmware.json').write_text(json.dumps({'images':images,'storage':'not flashed; unchanged table'},indent=2)+'\n');print(json.dumps(images),flush=True)
if __name__=='__main__':main()
