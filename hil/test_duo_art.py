"""Offline acceptance checks for the derived art; never imports hardware helpers.

Run: TMPDIR=$HOME/.cache/gap3-tmp PYTHONDONTWRITEBYTECODE=1 python3 -m unittest hil/test_duo_art.py -v
Requires Node, a C++17 host compiler, and system Python with Pillow.
"""
import hashlib
import itertools
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from PIL import Image, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duo_art as art


class DuoArtTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = Path(os.environ.get('TMPDIR', Path.home()/'.cache/gap3-tmp'))
        scratch.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix='duo-art-', dir=scratch)
        cls.out = Path(cls.temp.name)
        cls.decoder = cls.out/'dgi-360'
        # Compile only the production decoder, not ESP firmware; serial and services are untouched.
        subprocess.run(['c++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-DCONFIG_DG_BOARD_ECHOEAR=1',
                        '-I'+str(art.ROOT/'components/dg_display/include'),
                        '-I'+str(art.ROOT/'components/dg_board/include'),
                        str(art.ROOT/'components/dg_display/src/dgi.cpp'),
                        str(art.ROOT/'components/dg_display/test_host/dgi_cli.cpp'), '-o', str(cls.decoder)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @unittest.skip('superseded by test_duo_art_rev1: authorized gradient field and seven eye frames')
    def test_bounds_alpha_safe_circle_and_frames(self):
        for character in art.CHARACTERS:
            source, manifest, _ = art.assets(character)
            self.assertEqual((manifest['version'], manifest['display']['width'], manifest['display']['height']), (2, 412, 412))
            self.assertEqual(set(manifest['layers']), {'eyes', 'mouth'})
            self.assertEqual(set(manifest['layers']['eyes']['frames']), {'open', 'half', 'closed', 'look-left', 'look-right', 'happy'})
            self.assertEqual(set(manifest['layers']['mouth']['frames']), {'rest-smile', 'small', 'medium', 'wide', 'round'})
            base = Image.open(source/manifest['base']).convert('RGBA')
            self.assertEqual(base.getchannel('A').getextrema(), (255, 255))
            background = base.getpixel((0, 0))
            for y in range(412):
                for x in range(412):
                    if base.getpixel((x, y)) != background:
                        self.assertLessEqual((x+.5-206)**2+(y+.5-206)**2, 204**2, character)
            for layer in manifest['layers'].values():
                x, y, w, h = layer['bounds']
                self.assertTrue(0 <= x < x+w <= 412 and 0 <= y < y+h <= 412)
                sx, sy, sw, sh = art.scaled_bounds(layer['bounds'])
                self.assertTrue(0 <= sx < sx+sw <= 360 and 0 <= sy < sy+sh <= 360)
                union = Image.new('L', (w, h))
                for file in layer['frames'].values():
                    patch = Image.open(source/file).convert('RGBA')
                    self.assertEqual(patch.size, (w, h))
                    self.assertEqual(patch.getchannel('A').getextrema(), (0, 255))
                    union = ImageChops.lighter(union, patch.getchannel('A'))
                    has_fang = False
                    for py in range(h):
                        for px in range(w):
                            r, g, b, a = patch.getpixel((px, py))
                            has_fang |= (r, g, b, a) == (255, 255, 255, 255)
                            if not a:
                                self.assertEqual((r, g, b), (0, 0, 0))
                            else:
                                self.assertLessEqual((x+px+.5-206)**2+(y+py+.5-206)**2, 204**2)
                    if character == 'devil' and file.startswith('sprites/mouth-'):
                        self.assertTrue(has_fang, file)
                self.assertEqual(union.getbbox(), (0, 0, w, h), 'Layer union must be tightly bounded')

    def test_every_combination_and_transition_has_no_stale_pixels(self):
        for character in art.CHARACTERS:
            source, manifest, _ = art.assets(character)
            for canvas in (412, 360):
                base = Image.open(source/manifest['base']).convert('RGBA')
                if canvas == 360:
                    base = Image.new('RGBA', (360, 360))
                    base.putdata(art.resized(source/manifest['base'], 360, 360))
                layers = manifest['layers']
                bounds = {g: art.scaled_bounds(l['bounds']) if canvas == 360 else l['bounds'] for g, l in layers.items()}
                patches = {}
                for g, layer in layers.items():
                    for key, file in layer['frames'].items():
                        if canvas == 360:
                            w, h = bounds[g][2:]
                            patch = Image.new('RGBA', (w, h))
                            patch.putdata(art.resized(source/file, w, h))
                        else:
                            patch = Image.open(source/file).convert('RGBA')
                        patches[g, key] = patch
                poses = list(itertools.product(layers['eyes']['frames'], layers['mouth']['frames']))

                def full(pose):
                    result = base.copy()
                    for group, key in zip(('eyes', 'mouth'), pose):
                        result.alpha_composite(patches[group, key], tuple(bounds[group][:2]))
                    return result

                expected = {pose: full(pose) for pose in poses}
                for old in poses:
                    for new in poses:
                        changed = [bounds[g] for i, g in enumerate(('eyes', 'mouth')) if old[i] != new[i]]
                        if not changed:
                            continue
                        x0, y0 = min(b[0] for b in changed), min(b[1] for b in changed)
                        x1, y1 = max(b[0]+b[2] for b in changed), max(b[1]+b[3] for b in changed)
                        box = (x0, y0, x1, y1)
                        # Restore changed regions, then redraw every intersecting layer, clipped
                        # to the dirty union. This catches overlapping eye/mouth patch bounds.
                        result = expected[old].copy()
                        result.paste(base.crop(box), box)
                        for group, key in zip(('eyes', 'mouth'), new):
                            x, y, w, h = bounds[group]
                            clip = (max(x0, x), max(y0, y), min(x1, x+w), min(y1, y+h))
                            if clip[0] < clip[2] and clip[1] < clip[3]:
                                patch = patches[group, key].crop((clip[0]-x, clip[1]-y, clip[2]-x, clip[3]-y))
                                result.alpha_composite(patch, clip[:2])
                        self.assertEqual(result.tobytes(), expected[new].tobytes(), (character, canvas, old, new))

    def test_rebuild_is_deterministic(self):
        for character in art.CHARACTERS:
            source, _, _ = art.assets(character)
            before = {p.relative_to(source): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in source.rglob('*') if p.suffix == '.png' or p.name == 'animation.json'}
            subprocess.run(['node', str(source/'build-sprites.cjs')], check=True, capture_output=True)
            after = {p.relative_to(source): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in source.rglob('*') if p.suffix == '.png' or p.name == 'animation.json'}
            self.assertEqual(before, after)

    def test_production_decoder_and_budget(self):
        estimate = art.estimate(self.out)
        for row in estimate['rows']:
            source = art.ROOT/'assets/characters'/row['character']/row['source']
            w, h = row['bounds'][2:]
            expected = art.dgi.converted(art.resized(source, w, h), row['alpha'])
            decoded = subprocess.check_output([str(self.decoder), str(self.out/'runtime-estimate'/(row['hash']+'.dgi'))])
            self.assertEqual(decoded, b''.join(c.to_bytes(2, 'big')+bytes([a]) for c, a in expected), row['source'])
            self.assertLessEqual(row['bytes'], 256*1024)
            if row['bounds'] == [0, 0, 360, 360]:
                self.assertLessEqual(row['bytes'], 100*1024)
        # The binary is really limited to 360, not the default 412px host canvas.
        invalid = self.out/'too-wide.dgi'
        invalid.write_bytes(art.dgi.encode(361, 1, [(0, 0, 0, 255)]*361))
        self.assertEqual(subprocess.run([str(self.decoder), str(invalid)], capture_output=True).returncode, 3)
        summary = estimate['summary']
        self.assertLessEqual(summary['unique_resident_hashes'], 96)
        self.assertLessEqual(summary['unique_flash_hashes'], 96)
        self.assertLessEqual(summary['peak_charged'], summary['arena_limit'])
        self.assertTrue(all(n <= (summary['arena_limit']-summary['status_charged']-summary['extra_install_reservation'])//2
                            for n in summary['per_pack_charged'].values()))


if __name__ == '__main__':
    unittest.main()
