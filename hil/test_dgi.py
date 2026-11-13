"""Independent RGB565 expectations against the compiled production decoder."""
import json
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import dgi


class DgiGolden(unittest.TestCase):
 def setUp(self):
  self.cli=ROOT/'build/host-display/dgi_cli'
  self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'image.dgi'
 def tearDown(self):self.temp.cleanup()
 def decode(self,data,args=()):
  self.path.write_bytes(data);return subprocess.run([str(self.cli),str(self.path),*map(str,args)],capture_output=True)
 def expected(self,pixels,alpha):
  output=bytearray()
  for red,green,blue,coverage in pixels:
   a=255 if alpha==0 else (255 if coverage>=128 else 0) if alpha==1 else coverage
   c=(int(red/255*31+.5)<<11)|(int(green/255*63+.5)<<5)|int(blue/255*31+.5)
   output+=struct.pack('>HB',c if a else 0,a)
  return bytes(output)
 def test_all_modes_odd_width_and_middle_crop(self):
  rng=random.Random(98)
  pixels=[(rng.randrange(256),rng.randrange(256),rng.randrange(256),rng.randrange(256)) for _ in range(37*13)]
  for codec in (1,2):
   for alpha in (0,1,2):
    data=dgi.encode(37,13,pixels,alpha,codec);out=self.decode(data)
    self.assertEqual(out.returncode,0);self.assertEqual(out.stdout,self.expected(pixels,alpha))
    selected=[p for y in range(4,9) for p in pixels[y*37+3:y*37+29]]
    crop=self.decode(data,(3,4,26,5));self.assertEqual(crop.returncode,0);self.assertEqual(crop.stdout,self.expected(selected,alpha))
 def test_runs_indexes_deltas_and_transparency(self):
  colors=[(0,0,0,255),(255,255,255,255),(20,40,80,128),(255,0,255,0)]
  pixels=sum(([c]*98 for c in colors),[])
  for codec in (1,2):
   for alpha in (0,1,2):
    result=self.decode(dgi.encode(392,1,pixels,alpha,codec));self.assertEqual(result.returncode,0);self.assertEqual(result.stdout,self.expected(pixels,alpha))
 def test_every_truncation_and_random_inputs(self):
  valid=dgi.encode(7,3,[(255,80,20,128)]*21,2,2)
  for end in range(len(valid)):self.assertNotEqual(self.decode(valid[:end]).returncode,0)
  rng=random.Random(7)
  for _ in range(100):self.assertNotEqual(self.decode(rng.randbytes(rng.randrange(1,500))).returncode,0)
 def test_structural_and_row_errors(self):
  data=dgi.encode(8,2,[(255,0,0,255)]*16,0,1)
  for at,value in [(4,2),(5,3),(6,3),(7,1),(8,0),(24,1),(32,1)]:
   bad=bytearray(data);bad[at]=value;self.assertNotEqual(self.decode(bad).returncode,0)
  bad=bytearray(data);bad[-3]=0;self.assertNotEqual(self.decode(bad).returncode,0)
 def test_one_pixel_edges_and_canaries(self):
  for alpha in (0,1,2):
   for codec in (1,2):
    data=dgi.encode(1,1,[(240,80,30,127)],alpha,codec);out=self.decode(data,(0,0,1,1));self.assertEqual(out.returncode,0);self.assertEqual(out.stdout,self.expected([(240,80,30,127)],alpha))
    self.assertNotEqual(self.decode(data,(1,0,1,1)).returncode,0)
 def test_png_assets_are_read_only_and_decode(self):
  pack=ROOT/'assets/characters/halloween-cat';manifest=json.loads((pack/'animation.json').read_text());names=[manifest['base']]+[name for layer in manifest['layers'].values() for name in layer['frames'].values()]
  for p in (pack/name for name in names):
   w,h,pixels=dgi.png(p);self.assertEqual(len(pixels),w*h)


if __name__=='__main__':unittest.main()
