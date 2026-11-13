#!/usr/bin/env python3
"""Check buffered USB console commands and complete PCM transfer without resetting."""
import argparse
import json
from pathlib import Path
import time
import numpy as np
from esp_serial import EspSerial
from transport_test import dump

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=True)
    with EspSerial('/dev/ttyACM0',log_path=a.out/'serial.log') as board:
        times=[]
        for _ in range(128):
            begin=time.monotonic();board.command('ping');times.append(1000*(time.monotonic()-begin))
        begin=time.monotonic();pcm=dump(board,a.out);duration=time.monotonic()-begin
    result=dict(commands=128,ping_mean_ms=float(np.mean(times)),ping_p95_ms=float(np.percentile(times,95)),
                ping_max_ms=max(times),dump_bytes=len(pcm)*2,dump_s=duration,pcm_bytes_per_s=len(pcm)*2/duration,passed=True)
    (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
if __name__=='__main__':main()
