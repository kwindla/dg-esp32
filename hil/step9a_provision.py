#!/usr/bin/env python3
"""Explicitly provision only the backed-up, independently verified erased storage region."""
import json
from pathlib import Path
from esp_serial import EspSerial
from step9a_support import read


def main():
    backup=Path('/home/khkramer/src/evidence/step9a/full-flash-backup');assert json.loads((backup/'verification.json').read_text())['identical'];assert json.loads((backup/'erase-verification.json').read_text())['completed']
    out=Path('/home/khkramer/src/evidence/step9a/resume/provision');out.mkdir(exist_ok=False)
    with EspSerial('/dev/ttyACM0',log_path=out/'serial.log') as board:
        at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at);board.command('quiet 1');board.command('disp init',45);before=read(board,'status','DISPLAY');assert before['flash']['health']=='unavailable'
        board.command('disp provision-empty',90);after=read(board,'status','DISPLAY');assert after['flash']['health']=='ready' and after['flash']['capacity']==4194304 and after['flash']['count']==0
        result={'passed':True,'before':before,'after':after,'guard':'partition label storage, exact0x410000/0x400000, all4MiBFF checked by worker before explicit format, zero mmap guard at every physical write/erase'};(out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result['after']['flash']),flush=True)
        board.command('audio 0');board.command('disconnect',45)

if __name__=='__main__':main()
