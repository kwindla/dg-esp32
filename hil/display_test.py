#!/usr/bin/env python3
"""Software CRC and independently observed webcam pattern sequence; no panel readback."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import threading
import time
import zlib
from esp_serial import EspSerial
from step9a_support import read

WIDTH,HEIGHT,X,Y=128,160,976,336
PROBES=((1048,420),(1014,445),(1030,378),(1005,391))


def save_png(path,rgb):
    def chunk(kind,body):return struct.pack('>I',len(body))+kind+body+struct.pack('>I',zlib.crc32(kind+body))
    raw=b''.join(b'\0'+row.tobytes() for row in rgb)
    path.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',rgb.shape[1],rgb.shape[0],8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b''))


def classify(rgb):
    colors=[];means=[]
    for x,y in PROBES:
        x-=X;y-=Y;r,g,b=rgb[y-2:y+3,x-2:x+3].mean(axis=(0,1));means.append([round(float(v),2) for v in (r,g,b)])
        if r>80 and r>2*max(g,b):colors.append(0)
        elif g>60 and g>1.4*max(r,b):colors.append(1)
        elif b>80 and b>2*max(r,g):colors.append(2)
        elif r>90 and g>120 and b>170:colors.append(3)
        else:colors.append(-1)
    for phase in range(4):
        if colors==[(i+phase)%4 for i in range(4)]:return phase,means
    return None,means


def reference_crc(phase,width=412,height=412):
    colors=(0xf800,0x07e0,0x001f,0xffff);body=bytearray()
    for y in range(height):
        for x in range(width):
            c=0xffff if x<8 or x>=width-8 or y<8 or y>=height-8 else colors[((0 if y<height//2 else 2)+(0 if x<width//2 else 1)+phase)%4]
            body.extend(struct.pack('>H',c))
    return zlib.crc32(body)


def capture(board,out,frames):
    import numpy as np
    # V4L2 timestamps come from the kernel. showinfo retains each decoded frame's
    # source PTS alongside the host receipt stamp; no physical lip-skew claim.
    log=(out/'camera-kernel-pts.log').open('w')
    argv=['ffmpeg','-hide_banner','-loglevel','info','-copyts','-f','v4l2','-input_format','mjpeg','-video_size','1280x720','-framerate','30','-i','/dev/video0','-vf',f'crop={WIDTH}:{HEIGHT}:{X}:{Y},format=rgb24,showinfo','-f','rawvideo','pipe:1']
    process=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=log)
    stop=threading.Event();rows=[];runs=[];failure=[];last_rgb=[None]
    stream=(out/'camera-frames.jsonl').open('w')
    def reader():
      try:
        n=0
        while not stop.is_set():
            raw=process.stdout.read(WIDTH*HEIGHT*3)
            if len(raw)!=WIDTH*HEIGHT*3:break
            rgb=np.frombuffer(raw,dtype=np.uint8).reshape(HEIGHT,WIDTH,3);phase,means=classify(rgb);stamp=time.monotonic();last_rgb[0]=rgb.copy();row={'frame':n,'host_received_s':stamp,'phase':phase,'means':means};stream.write(json.dumps(row)+'\n');stream.flush();rows.append(row);n+=1
            if phase is not None:
                if not runs or runs[-1]['phase']!=phase:runs.append({'phase':phase,'first_s':stamp,'last_s':stamp,'count':1,'rgb':rgb.copy(),'frame':n-1})
                else:runs[-1].update(last_s=stamp,count=runs[-1]['count']+1,rgb=rgb.copy(),frame=n-1)
      except Exception as error:failure.append(f'{type(error).__name__}: {error}')
    thread=threading.Thread(target=reader,daemon=True);thread.start()
    try:
        deadline=time.monotonic()+10
        while len(rows)<30 and time.monotonic()<deadline:time.sleep(.05)
        if len(rows)<30:raise RuntimeError('Webcam not ready')
        started=time.monotonic();at=board.cursor;board.command('disp qualify '+str(frames),frames*.2+60);ended=time.monotonic();time.sleep(1)
        status=read(board,'status','DISPLAY');te=read(board,'te','DISPLAY_TE');trace=[]
        for start in range(0,frames,8):trace.extend(read(board,'trace '+str(start),'DISPLAY_TRACE')['frames'])
        if last_rgb[0] is not None:save_png(out/'last-frame.png',last_rgb[0])
    finally:
        stop.set();process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        thread.join(timeout=5);stream.close();log.close()
    observed=[r for r in runs if r['last_s']>=started+.12 and r['first_s']<=ended and r['count']>=2]
    # Startup carries no test-pattern identity; locate only the first new phase0
    # whose camera run intersects the command interval. Never skip an interior run.
    while observed and observed[0]['phase']!=0:observed.pop(0)
    crc_expected=[reference_crc(i,*status["panel"][:2]) for i in range(4)]
    software_bad=[t for t in trace if t[0]>=frames or t[3]!=crc_expected[t[0]%4]]
    optical_bad=[{'index':i,'phase':r['phase'],'count':r['count']} for i,r in enumerate(observed[:frames]) if r['phase']!=i%4]
    for i,r in enumerate(observed[:frames]):save_png(out/f'pattern-{i:04}.png',r['rgb'])
    serial_qualification=[line[2] for line in board.lines_since(at) if 'lcd-qualify:' in line[2]]
    result={'requested_frames':frames,'software_crc_basis':'host-independent RGB565BE pattern reference versus composed stripes before DMA; NOT panel readback','software_crc_expected':crc_expected,'software_frames':len(trace),'software_mismatches':software_bad,'optical_basis':'kernel-PTS webcam frames; unchanged pose; four calibrated quadrant probe colors, at least two observations per successive held full-frame pattern','optical_frames':len(observed),'optical_mismatches':optical_bad,'runs':[{k:v for k,v in r.items() if k!='rgb'} for r in observed],'camera_frames':len(rows),'started_host_s':started,'ended_host_s':ended,'camera_errors':failure,'te':te,'status':status,'device_trace':trace,'qualification_log':serial_qualification}
    result['passed']=len(trace)==frames and not software_bad and len(observed)==frames and not optical_bad and not failure and status['dma']==[0,0] and not status['failed']
    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n');return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--frames',type=int,default=1000);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    (a.out/'plan.json').write_text(json.dumps({'frames':a.frames,'clock_mhz':40,'staging':20480,'pattern_hold_us':200000,'required':'zero software CRC and optical phase/color mismatches; >=1000 full frames for clock qualification','no_retakes':True},indent=2)+'\n')
    from step9a_echoear import bench,verify
    with bench(),EspSerial(verify(a.out),log_path=a.out/'serial.log') as board:
        at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at);board.command('quiet 1');board.command('disp init',30)
        result=capture(board,a.out,a.frames)
        board.command('audio 0');board.command('disconnect',45)
    print(json.dumps({k:result[k] for k in ('passed','software_frames','optical_frames','software_mismatches','optical_mismatches','te')}),flush=True)
    return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
