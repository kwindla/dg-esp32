#!/usr/bin/env python3
"""Nominal SPL at the board microphone only; uncertainty is several dB.

A ruler and SPL meter are required for any figure at 1 m.
"""
import argparse
import json
import hashlib
import re
import time
from pathlib import Path
from alsa import resolve_jabra,set_volumes
from bench_calibration import SENSITIVITY_DBFS
from esp_serial import EspSerial,ROOT
from speaker_dual_sweep_test import capture


def transferred_spl(board_tone_dbfs,jabra_tone_dbfs,jabra_speech_dbfs):
    return 94+board_tone_dbfs-SENSITIVITY_DBFS+jabra_speech_dbfs-jabra_tone_dbfs

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--campaign',type=Path,required=True);p.add_argument('--target',type=int,required=True);a=p.parse_args()
    a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=False)
    card,device=resolve_jabra();set_volumes(card,a.out/'mixer.log')
    with EspSerial('/dev/ttyACM0',log_path=a.out/'serial.log') as board:
        cur=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cur)
        identity=board.wait_for('elf_sha256=',3,since=cur)
        firmware_hash=hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest()
        assert re.search(r'elf_sha256=([a-f0-9]{64})',identity)[1]==firmware_hash
        board.command('audio 0');board.command('quiet 1')
        reference=capture(board,device,a.out,'tone-transfer-1000',1000,20)
    x=reference['chains']['board']['fundamental_dbfs'];y=reference['chains']['jabra']['fundamental_dbfs']
    result=dict(elf_sha256=firmware_hash,reference=reference,board_reference_spl=94+x-SENSITIVITY_DBFS,jabra_minus_board_tone_db=y-x,
        distance_measured=False,
        method='Nominal board microphone SPL = 94 + board active RMS dBFS - nominal sensitivity. Tone transfer is a spectrally weighted estimate at the board mic, not a Jabra-position measurement.',
        estimated_uncertainty_db='Several dB; ruler and SPL meter required at 1 m.',programs=[])
    data=json.loads(a.campaign.read_text())
    for row in data['utterances']:
        if row['target_dbfs']!=a.target:continue
        nominal=transferred_spl(x,y,row['chains']['jabra']['active_rms_dbfs'])
        result['programs'].append(dict(name=row['name'],board_mic_tone_transfer_spl=nominal,native_board_mic_spl=120+row['chains']['board']['active_rms_dbfs']))
    (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result['programs'],indent=2))
if __name__=='__main__':main()
