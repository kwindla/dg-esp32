"""Revision-1 checks; retains the three unchanged step-1 checks by inheritance.

Run TMPDIR=$HOME/.cache/gap3-tmp PYTHONDONTWRITEBYTECODE=1 python3 -m unittest hil/test_duo_art_rev1.py -v
The old solid-background safety assertion is replaced: the authorized full-field
gradient may reach corners, while actual features/accessories must remain safe.
The original flat-field safety test is explicitly superseded by this check.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from PIL import Image, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_duo_art as original
import duo_art as art


class DuoArtRevision1Test(original.DuoArtTest):
    def test_bounds_alpha_safe_circle_and_frames(self):
        for character in art.CHARACTERS:
            source, manifest, _ = art.assets(character)
            self.assertEqual((manifest['version'], manifest['display']['width'], manifest['display']['height']), (2, 412, 412))
            self.assertEqual(set(manifest['layers']), {'eyes', 'mouth'})
            self.assertEqual(set(manifest['layers']['eyes']['frames']), {'open', 'half', 'closed', 'look-left', 'look-right', 'happy', 'neutral'})
            self.assertEqual(set(manifest['layers']['mouth']['frames']), {'rest-smile', 'small', 'medium', 'wide', 'round'})
            base = Image.open(source/manifest['base']).convert('RGBA')
            self.assertEqual(base.getchannel('A').getextrema(), (255, 255))
            self.assertNotEqual(base.getpixel((2, 2)), base.getpixel((2, 409)), 'Field must be a full gradient')
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
                self.assertEqual(union.getbbox(), (0, 0, w, h))
            # Evaluate the renderer's actual face geometry (eyes includes brows),
            # independently of cropped patches and the authorized full-field gradient.
            request = {part: dict(character=character, part=part, renderOptions=dict(glow=False))
                       for part in ('accessories', 'eyes', 'mouth')}
            proof = json.loads(subprocess.check_output(['node', str(art.ROOT/'assets/characters/duo-proof.cjs')], input=json.dumps(request).encode()))
            spec = importlib.util.spec_from_file_location('duo_raster', art.ROOT/'assets/characters/duo-raster.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for part in request:
                image = module.raster(proof[part]['svg'])
                radius = 206 if part == 'accessories' else 204
                self.assertIsNotNone(image.getchannel('A').getbbox(), part)
                for y in range(412):
                    for x in range(412):
                        if image.getpixel((x, y))[3] >= 16:
                            self.assertLessEqual((x+.5-206)**2+(y+.5-206)**2, radius**2, (character, part))

    def test_source_equations_and_required_visual_features(self):
        code = '''const assert=require('node:assert/strict'),r=require('./assets/characters/little-voices.cjs');
        const d=r.expression('devil').mouth,p=r.mouthPoints(d,'devil');
        assert.deepEqual(p[0],[-34,7.5]);assert.deepEqual(p[3],[40,-7.5]);assert.deepEqual(p[6],[3,30]);
        assert.equal(r.mouthOutline(p).commands.filter(c=>c[0]==='C').length,3);
        assert.equal(r.expression('angel').eyes[0].sclera,0);
        const a=r.expression('angel','squished');assert.equal(a.eyes[0].sclera,1);
        assert.equal(a.mouth.width,16);assert.equal(a.mouth.open,20);
        assert.equal(r.expression('angel','surprised').mouth.open,40);
        for(const c of ['angel','devil']) {const m=r.lipMap(c,r.expression(c,'talking').mouth,1);
          assert.equal(m.open,34);assert.equal(m.width,c==='angel'?56:69);}
        assert.equal(r.field.angel[0][1],'#FEEFA2');assert.equal(r.field.devil[0][1],'#FC8C1E');
        '''
        subprocess.run(['node', '-e', code], cwd=art.ROOT, check=True)
        angel = art.ROOT/'assets/characters/angel'
        source, manifest, _ = art.assets('angel')
        patch = Image.open(angel/manifest['layers']['eyes']['frames']['open']).convert('RGBA')
        white = sum(1 for r, g, b, a in zip(*[iter(patch.tobytes())]*4) if min(r, g, b) > 240 and a > 240)
        self.assertGreater(white, 1600, 'Angel must have large white eyes, not two small bead pupils')

    def test_fidelity_metric_identity_and_registration(self):
        import numpy as np
        import duo_art_rev1 as proof
        matrix = proof.similarity([[10, 10], [30, 10]], [[5, 7], [5, 47]])
        np.testing.assert_allclose(matrix, [[0, -2, 25], [2, 0, -13]], atol=1e-12)
        image = Image.new('RGB', (100, 100), '#ffee88')
        from PIL import ImageDraw
        draw = ImageDraw.Draw(image)
        draw.ellipse((20, 25, 40, 55), fill='#220500')
        draw.ellipse((60, 25, 80, 55), fill='#220500')
        score = proof.metrics(image, image, np.ones((100, 100), dtype=bool), [[30, 40], [70, 40]], self.out, 'identity')
        self.assertEqual(score['mean_delta_e_face'], 0)
        self.assertEqual(score['mean_delta_e_field'], 0)
        self.assertEqual(score['edge_overlap_f1'], 1)


if __name__ == '__main__':
    import unittest
    unittest.main()
