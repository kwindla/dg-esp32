#!/usr/bin/env python3
"""Retrieve an existing trace tail without resetting or reconnecting the board."""
import argparse
import json
from pathlib import Path
from arrival_trace import drain
from esp_serial import EspSerial
from netem import Netem
from quiet_nic import QuietNic,wait_for_quiet


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--capture',type=Path,required=True)
    args=parser.parse_args();out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    capture=args.capture.resolve();identity=json.loads((capture/'result.json').read_text())
    wait_for_quiet(out/'quiet-preflight.json')
    with Netem('192.168.4.29',['loss','0%'],'both',state=out/'tc-state.json'),QuietNic(out/'quiet-nic.json'),EspSerial('/dev/ttyACM0',log_path=out/'serial.log') as board:
        rows=drain(board,out/'arrivals.jsonl')
    result=dict(capture=str(capture),elf_sha256=identity['elf_sha256'],records=len(rows),
        generations=sorted({r['reserved'] for r in rows}),
        device_us_min=min((r['device_us'] for r in rows),default=None),
        device_us_max=max((r['device_us'] for r in rows),default=None),
        reset=False,reconnect=False,basis='Read-only retrieval of the transport-owned persistent trace ring after prior capture teardown; original record generation/time retained.')
    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result),flush=True)


if __name__=='__main__':main()
