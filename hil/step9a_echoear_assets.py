#!/usr/bin/env python3
"""Deterministic 360px cat fixtures; original assets are read-only inputs."""
import hashlib
import json
from pathlib import Path
import random
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import dgi

def scaled(value):return value*360//412

def main():
    source=ROOT/'assets/characters/halloween-cat';out=ROOT/'hil/fixtures/echoear360';out.mkdir(parents=True,exist_ok=True)
    original=json.loads((source/'animation.json').read_text());manifest=json.loads(json.dumps(original));manifest['display'].update(width=360,height=360);manifest['display'].pop('grid',None);manifest['display'].pop('pixelScale',None)
    sizes={original['base']:(360,360)}
    for name,layer in original['layers'].items():
        x,y,w,h=layer['bounds'];bounds=[scaled(x),scaled(y),scaled(x+w)-scaled(x),scaled(y+h)-scaled(y)];manifest['layers'][name]['bounds']=bounds
        for filename in layer['frames'].values():sizes[filename]=tuple(bounds[2:])
    rows=[]
    for filename,(w,h) in sizes.items():
        oldw,oldh,pixels=dgi.png(source/filename)
        # Nearest-neighbor preserves the source pixel-art palette and alpha.
        resized=[pixels[min(oldh-1,y*oldh//h)*oldw+min(oldw-1,x*oldw//w)] for y in range(h) for x in range(w)]
        for codec in (1,2):
            data=dgi.encode(w,h,resized,codec=codec);identity=hashlib.sha256(data).hexdigest();(out/(identity+'.dgi')).write_bytes(data)
            rows.append(dict(source=filename,source_sha256=hashlib.sha256((source/filename).read_bytes()).hexdigest(),hash=identity,bytes=len(data),width=w,height=h,codec=codec,alpha=data[5]))
    rng=random.Random(360);pixels=[(rng.randrange(256),rng.randrange(256),rng.randrange(256),255) for _ in range(360*360)]
    for codec in (1,2):
        data=dgi.encode(360,360,pixels,codec=codec);identity=hashlib.sha256(data).hexdigest();(out/(identity+'.dgi')).write_bytes(data);rows.append(dict(source='synthetic-noise',hash=identity,bytes=len(data),width=360,height=360,codec=codec,alpha=data[5]))
    (out/'animation.json').write_text(json.dumps(manifest,indent=2)+'\n');(out/'codec-benchmark.json').write_text(json.dumps({'basis':'canonical tools/dgi.py encoder; nearest-neighbor scale; fixture metadata only, no timing claim','rows':rows},indent=2)+'\n')
    print(json.dumps({'fixtures':len(rows),'bytes':sum(r['bytes'] for r in rows),'directory':str(out)}))
if __name__=='__main__':main()
