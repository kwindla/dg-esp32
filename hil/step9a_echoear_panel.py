#!/usr/bin/env python3
"""EchoEar panel bring-up: software CRC (not LCD RAM readback), fills, and one camera frame."""
import argparse
import json
from pathlib import Path
import subprocess
import time
from esp_serial import EspSerial
from step9a_echoear import bench,verify,flash,check_firmware
from step9a_support import read,request,terminal
from display_test import reference_crc

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--flash',action='store_true');a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    result={'passed':False,'software_crc_basis':'RGB565BE composed staging bytes before DMA; NOT panel readback'}
    try:
        with bench():
            if a.flash:flash(a.out)
            port=verify(a.out)
            with EspSerial(port,log_path=a.out/'serial.log') as board:
                at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at);result['elf_sha256']=check_firmware(board,at);board.command('quiet 1');board.command('disp init',45)
                for i,color in enumerate((0xf800,0x07e0,0x001f)):
                    request(board,'fill',{'color':color},'solid-'+str(i));assert terminal(board,'solid-'+str(i))['error']=='none';time.sleep(.3)
                board.command('disp qualify 8',45)
                result['status']=read(board,'status','DISPLAY');result['te']=read(board,'te','DISPLAY_TE');result['trace']=read(board,'trace 0','DISPLAY_TRACE')['frames'];result['crypto']=read(board,'r4-crypto','DISPLAY_R4_CRYPTO')
                expected=[reference_crc(i,360,360) for i in range(4)];result['expected_crc']=expected
                result['software_mismatches']=[t for t in result['trace'] if t[3]!=expected[t[0]%4]]
                camera=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','v4l2','-input_format','mjpeg','-video_size','1280x720','-i','/dev/video0','-frames:v','1','-y',str(a.out/'camera.png')],capture_output=True,text=True,timeout=15)
                result['camera_capture_ok']=camera.returncode==0;result['camera_error']=camera.stderr
                log=(a.out/'serial.log').read_text(errors='replace');result['spi_dma_errors']=[line for line in log.splitlines() if 'spi_master' in line and ('E (' in line or 'E spi_master' in line) or 'DMA TX underflow' in line]
                s=result['status'];g=result['crypto'];result['passed']=s['panel'][:2]==[360,360] and len(result['trace'])==8 and not result['software_mismatches'] and not result['spi_dma_errors'] and s['dma']==[0,0] and not s['failed'] and not s['dead'] and g['lcd_tx_captured'] and g['last_priority_written']==1 and g['priority_applies']>0 and g['tx_pri']==1 and g['coverage_lost']==0 and s['dma_y_even_if_row8'] is False
    finally:(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result),flush=True)
    return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
