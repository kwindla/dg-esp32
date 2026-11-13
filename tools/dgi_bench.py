#!/usr/bin/env python3
"""Reproducible codec size/decode measurements; all generated files are fixtures."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import subprocess
from dgi import png, encode


def main():
 p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--cli',type=Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
 root=Path(__file__).resolve().parents[1];pack=root/'assets/characters/halloween-cat';samples=[]
 manifest=json.loads((pack/'animation.json').read_text());names=[manifest['base']]+[name for layer in manifest['layers'].values() for name in layer['frames'].values()]
 for path in sorted(pack/name for name in names):
  w,h,pixels=png(path);samples.append((str(path.relative_to(pack)),w,h,pixels))
 rng=random.Random(20261013)
 w=h=412
 samples.append(('synthetic-photo',w,h,[(max(0,min(255,x*255//411+rng.randrange(-12,13))),max(0,min(255,y*255//411+rng.randrange(-12,13))),max(0,min(255,(x+y)*255//822+rng.randrange(-12,13))),255) for y in range(h) for x in range(w)]))
 samples.append(('synthetic-noise',w,h,[(rng.randrange(256),rng.randrange(256),rng.randrange(256),255) for _ in range(w*h)]))
 samples.append(('synthetic-a8',w,h,[(x*255//411,y*255//411,140,(x+y)%256) for y in range(h) for x in range(w)]))
 rows=[]
 for name,w,h,pixels in samples:
  for codec in (1,2):
   data=encode(w,h,pixels,codec=codec);digest=hashlib.sha256(data).hexdigest();path=a.out/(digest+'.dgi');path.write_bytes(data)
   timing={}
   for line in subprocess.check_output([str(a.cli),str(path),'bench'],text=True).splitlines():timing.update(json.loads(line))
   rows.append({'source':name,'codec':codec,'hash':digest,'path':str(path),'bytes':len(data),'width':w,'height':h,**timing})
 result={'rows':rows,'cat_totals':{str(c):sum(r['bytes'] for r in rows if r['codec']==c and not r['source'].startswith('synthetic-')) for c in (1,2)},'basis':'100 production decoder repetitions; host steady clock; raw-row escape enabled; dirty patch decodes row prefixes for compressed backgrounds'}
 (a.out/'codec-benchmark.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result['cat_totals']))
 for r in rows:
  if r['width']==412:print(r['source'],r['codec'],r['bytes'],r['full_us'],r['dirty_us'])


if __name__=='__main__':main()
