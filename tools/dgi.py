#!/usr/bin/env python3
"""Canonical DGI1 encoder. PNG decoding uses the standard library only."""
import argparse
import hashlib
from pathlib import Path
import struct
import zlib


def png(path):
    data = Path(path).read_bytes()
    if data[:8] != b'\x89PNG\r\n\x1a\n': raise ValueError('PNG signature')
    at = 8; payload = bytearray(); geometry = None; ended = False
    while at < len(data):
        if len(data)-at < 12: raise ValueError('PNG truncated chunk')
        count, = struct.unpack_from('>I', data, at); kind = data[at+4:at+8]
        if count > len(data)-at-12: raise ValueError('PNG truncated payload')
        body = data[at+8:at+8+count]
        crc, = struct.unpack_from('>I', data, at+8+count)
        if zlib.crc32(kind+body) != crc: raise ValueError('PNG CRC')
        if kind == b'IHDR':
            if geometry is not None or count != 13: raise ValueError('PNG IHDR')
            w,h,depth,color,compression,filtering,interlace = struct.unpack('>IIBBBBB',body)
            if not 1<=w<=412 or not 1<=h<=412 or depth!=8 or color not in (2,6) or compression or filtering or interlace: raise ValueError('unsupported PNG')
            geometry=(w,h,3 if color==2 else 4)
        elif kind == b'IDAT': payload += body
        elif kind == b'IEND':
            if count: raise ValueError('PNG IEND')
            ended=True; at+=12; break
        elif kind[0] & 32 == 0: raise ValueError('unsupported critical PNG chunk')
        at += count+12
    if not ended or at != len(data) or geometry is None: raise ValueError('PNG end')
    w,h,channels=geometry; stride=w*channels
    inflater=zlib.decompressobj(); raw=inflater.decompress(payload,(stride+1)*h+1)
    if not inflater.eof or inflater.unused_data or len(raw)!=(stride+1)*h: raise ValueError('PNG inflated length')
    previous=bytearray(stride); pixels=[]; at=0
    for _ in range(h):
        mode=raw[at]; row=bytearray(raw[at+1:at+1+stride]); at+=stride+1
        if mode>4: raise ValueError('PNG filter')
        for i in range(stride):
            left=row[i-channels] if i>=channels else 0; up=previous[i]; diagonal=previous[i-channels] if i>=channels else 0
            if mode==1: predictor=left
            elif mode==2: predictor=up
            elif mode==3: predictor=(left+up)//2
            elif mode==4:
                p=left+up-diagonal; a,b,c=abs(p-left),abs(p-up),abs(p-diagonal)
                predictor=left if a<=b and a<=c else up if b<=c else diagonal
            else: predictor=0
            row[i]=(row[i]+predictor)&255
        pixels.extend(tuple(row[i:i+channels])+(255,) if channels==3 else tuple(row[i:i+channels]) for i in range(0,stride,channels))
        previous=row
    return w,h,pixels


def converted(pixels, alpha):
    out=[]
    for r,g,b,a in pixels:
        if alpha==0: a=255
        elif alpha==1: a=255 if a>=128 else 0
        color=((r*31+127)//255<<11)|((g*63+127)//255<<5)|((b*31+127)//255)
        out.append((color if a else 0,a))
    return out


def index(pixel):
    c,a=pixel
    return ((c>>11)*3+((c>>5)&63)*5+(c&31)*7+a*11)&63


def raw_row(row,alpha):
    out=bytearray(b'\0')
    for c,a in row:out+=c.to_bytes(2,'big')
    if alpha==1:
        bits=bytearray((len(row)+7)//8)
        for i,(_,a) in enumerate(row):bits[i//8]|=(a!=0)<<(7-i%8)
        out+=bits
    elif alpha==2:out+=bytes(a for _,a in row)
    return bytes(out)


def rle(row,alpha):
    out=bytearray(b'\1');at=0
    while at<len(row):
        pixel=row[at];n=1
        while at+n<len(row) and row[at+n]==pixel and n<255:n+=1
        out.append(n);out+=pixel[0].to_bytes(2,'big')
        if alpha:out.append(pixel[1])
        at+=n
    return bytes(out)


def q565(row,alpha):
    out=bytearray(b'\1');previous=(0,255);table=[(0,0)]*64;at=0
    while at<len(row):
        p=row[at]
        if p==previous:
            n=1
            while at+n<len(row) and row[at+n]==p and n<62:n+=1
            out.append(0xc0+n-1);table[index(p)]=p;at+=n;continue
        slot=index(p)
        if table[slot]==p:out.append(slot)
        else:
            c,a=p;old,oa=previous
            diff=((c>>11)-(old>>11),((c>>5)&63)-((old>>5)&63),(c&31)-(old&31))
            if a==oa and all(-2<=v<=1 for v in diff):out.append(0x80|((diff[0]+2)<<4)|((diff[1]+2)<<2)|(diff[2]+2))
            else:
                out.append(0xfe if a==oa else 0xff);out+=c.to_bytes(2,'big')
                if a!=oa:out.append(a)
        table[slot]=p;previous=p;at+=1
    return bytes(out)


def encode(w,h,pixels,alpha=None,codec=2):
    if not 1<=w<=412 or not 1<=h<=412 or len(pixels)!=w*h:raise ValueError('geometry')
    if alpha is None:
        coverage={p[3] for p in pixels};alpha=0 if coverage=={255} else 1 if coverage<={0,255} else 2
    if alpha not in (0,1,2) or codec not in (1,2):raise ValueError('mode')
    px=converted(pixels,alpha);payload=bytearray();offsets=[0]
    for y in range(h):
        row=px[y*w:(y+1)*w];raw=raw_row(row,alpha);compressed=(rle if codec==1 else q565)(row,alpha)
        payload+=compressed if len(compressed)<len(raw) else raw;offsets.append(len(payload))
    start=32+4*(h+1);size=start+len(payload)
    if size>524288:raise ValueError('file cap')
    header=struct.pack('<4sBBBBHHIII8s',b'DGI1',1,alpha,codec,0,w,h,size,32,start,b'\0'*8)
    return header+struct.pack('<'+'I'*(h+1),*offsets)+payload


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('png',type=Path);p.add_argument('out',type=Path)
    p.add_argument('--codec',choices=['rle','q565'],default='q565');p.add_argument('--alpha',choices=['none','a1','a8'])
    a=p.parse_args();w,h,pixels=png(a.png);data=encode(w,h,pixels,{'none':0,'a1':1,'a8':2}.get(a.alpha),1 if a.codec=='rle' else 2)
    a.out.parent.mkdir(parents=True,exist_ok=True);a.out.write_bytes(data);print(hashlib.sha256(data).hexdigest(),len(data),w,h)


if __name__=='__main__':main()
