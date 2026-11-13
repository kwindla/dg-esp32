"""Offline duo runtime acceptance: regeneration, rejection gates and C++/JSON parity.

Run scripts/host-tests.sh --duo (no firmware, serial, server or bench access).
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duo_assets as duo


class DuoAssetsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = Path(os.environ.get('TMPDIR', Path.home()/'.cache/gap3-tmp'))
        scratch.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix='test-duo-assets-', dir=scratch)
        cls.out = Path(cls.temp.name)
        cls.first = cls.out/'first'
        cls.header = cls.out/'first.h'
        cls.descriptor = duo.generate(cls.first, cls.header)
        duo.generate(cls.out/'second', cls.out/'second.h')
        cls.status, cls.existing = duo.existing_assets()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_two_runs_and_committed_outputs_are_byte_identical(self):
        snapshot = lambda path: {file.name: file.read_bytes() for file in path.iterdir()}
        self.assertEqual(snapshot(self.first), snapshot(self.out/'second'))
        self.assertEqual(snapshot(self.first), {file.name: (duo.ROOT/duo.FIXTURES/file.name).read_bytes()
                                               for file in self.first.iterdir()})
        self.assertEqual(self.header.read_bytes(), (self.out/'second.h').read_bytes())
        self.assertEqual(self.header.read_bytes(), (duo.ROOT/duo.HEADER).read_bytes())

    def test_approved_inputs_and_runtime_hashes(self):
        expected_sizes = {'angel': 19991, 'devil': 23494}
        for pack in self.descriptor['characters']:
            source = duo.ROOT/'assets/characters'/pack['name']
            manifest = source/'animation.json'
            approved = subprocess.check_output(['git', 'show', duo.SOURCE_COMMIT+':'+manifest.relative_to(duo.ROOT).as_posix()], cwd=duo.ROOT)
            self.assertEqual(manifest.read_bytes(), approved)
            self.assertEqual(duo.digest(approved), pack['manifest_sha256'])
            self.assertEqual(pack['faceplate']['bytes'], expected_sizes[pack['name']])
            self.assertEqual(pack['hash'], duo.digest(duo.canonical({key: value for key, value in pack.items() if key != 'hash'})))
            for row in duo.pack_assets(pack):
                data = (duo.ROOT/row['path']).read_bytes()
                self.assertEqual(len(data), row['bytes'])
                self.assertEqual(duo.digest(data), row['hash'])
                self.assertEqual(data[6], min((1, 2), key=lambda codec: (row['codec_bytes'][str(codec)], codec)))
                approved = subprocess.check_output(['git', 'show', duo.SOURCE_COMMIT+':'+(source/row['source']).relative_to(duo.ROOT).as_posix()], cwd=duo.ROOT)
                self.assertEqual((source/row['source']).read_bytes(), approved)
                self.assertEqual(duo.digest(approved), row['source_sha256'])
                x, y, w, h = (row[key] for key in ('x', 'y', 'w', 'h'))
                self.assertTrue(0 <= x < x+w <= 360 and 0 <= y < y+h <= 360)

    def test_budget_matches_revision_1(self):
        budget = self.descriptor['budget']
        expected = dict(status_charged=135952, packs_charged=174992, resident_charged=310944,
                        extra_install_reservation=23520, peak_charged=334464, headroom=34176,
                        unique_duo_hashes=26, unique_resident_hashes=31, unique_flash_hashes=46,
                        duo_payload=174400, flash_payload_plus_temp=418664, minimum_flash_usable=558219)
        for key, value in expected.items():
            self.assertEqual(budget[key], value, key)
        self.assertEqual(budget['per_pack_charged'], dict(angel=86416, devil=88576))

    def test_each_budget_violation_rejected(self):
        cases = ('faceplate', 'asset', 'resident hashes', 'flash hashes', 'charged peak')
        for case in cases:
            with self.subTest(case=case):
                packs = copy.deepcopy(self.descriptor['characters'])
                status, existing = copy.deepcopy(self.status), dict(self.existing)
                if case == 'faceplate':
                    packs[0]['faceplate']['bytes'] = 100*1024+1
                    reason = 'faceplate exceeds 100 KiB'
                elif case == 'asset':
                    packs[0]['layers'][0]['frames'][0]['bytes'] = 256*1024+1
                    reason = 'asset exceeds 256 KiB'
                elif case == 'resident hashes':
                    status.extend(dict(hash=f'{n:064x}', bytes=1) for n in range(66))
                    reason = 'unique hashes exceed 96'
                elif case == 'flash hashes':
                    existing.update({f'{n:064x}': 1 for n in range(51)})
                    reason = 'unique hashes exceed 96'
                else:
                    for pack in packs:
                        pack['faceplate']['bytes'] = 100*1024
                    reason = 'charged peak exceeds 75% of arena'
                with self.assertRaisesRegex(ValueError, reason):
                    duo.enforce_budget(packs, status, existing)

    def test_budget_caps_are_inclusive_and_charge_unique_hashes(self):
        packs = copy.deepcopy(self.descriptor['characters'])
        for pack in packs:
            for row in duo.pack_assets(pack):
                row['bytes'] = 1
        packs[0]['faceplate']['bytes'] = 100*1024
        status = [dict(hash=f'{n:064x}', bytes=1) for n in range(70)]
        budget = duo.enforce_budget(packs, status, {})
        self.assertEqual(budget['unique_resident_hashes'], 96)
        # A status already present in the final set is charged once, plus the
        # explicitly conservative incoming reservation counted by the policy.
        status.append(dict(packs[0]['faceplate']))
        self.assertEqual(duo.enforce_budget(packs, status, {})['resident_charged'], budget['resident_charged'])
        packs[0]['layers'][0]['frames'][0]['bytes'] = 256*1024
        with self.assertRaisesRegex(ValueError, 'charged peak'):
            duo.enforce_budget(packs, [], {})

    def manifest_source(self, name):
        source = self.out/name/'angel'
        shutil.copytree(duo.ROOT/'assets/characters/angel', source)
        return source

    def test_missing_and_duplicate_frames_rejected(self):
        for case in ('missing key', 'duplicate key', 'duplicate file', 'missing file'):
            with self.subTest(case=case):
                source = self.manifest_source(case)
                path = source/'animation.json'
                text = path.read_text()
                manifest = json.loads(text)
                if case == 'missing key':
                    del manifest['layers']['eyes']['frames']['neutral']
                    path.write_text(json.dumps(manifest))
                    reason = 'missing or unexpected eyes frames'
                elif case == 'duplicate key':
                    path.write_text(text.replace('"open":', '"open": "sprites/eyes-neutral.png", "open":', 1))
                    reason = 'duplicate JSON key/frame'
                elif case == 'duplicate file':
                    manifest['layers']['eyes']['frames']['half'] = manifest['layers']['eyes']['frames']['open']
                    path.write_text(json.dumps(manifest))
                    reason = 'duplicate frame file'
                else:
                    (source/manifest['layers']['eyes']['frames']['half']).unlink()
                    reason = 'missing or invalid frame file'
                with self.assertRaisesRegex(ValueError, reason):
                    duo.load_manifest(source)

    def test_out_of_canvas_and_subpixel_bounds_rejected(self):
        for number, bounds in enumerate(([400, 164, 202, 119], [-1, 164, 202, 119], [1, 1, 0, 1], [0, 0, 1, 1])):
            source = self.manifest_source('bounds-'+str(number))
            path = source/'animation.json'
            manifest = json.loads(path.read_text())
            manifest['layers']['eyes']['bounds'] = bounds
            path.write_text(json.dumps(manifest))
            with self.subTest(bounds=bounds), self.assertRaisesRegex(ValueError, 'patch outside'):
                duo.load_manifest(source)

    def test_wrong_png_size_rejected(self):
        source = self.manifest_source('wrong-size')
        manifest = json.loads((source/'animation.json').read_text())
        # Valid source and scaled rectangles, but inconsistent PNG geometry.
        manifest['layers']['eyes']['bounds'][2] -= 1
        (source/'animation.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'PNG dimensions'):
            duo.generate(self.out/'wrong-size-output', self.out/'wrong-size.h', source.parent)
        self.assertFalse((self.out/'wrong-size-output').exists())
        self.assertFalse((self.out/'wrong-size.h').exists())

    def test_production_decoder_and_pixel_mismatch_rejected(self):
        decoder = self.out/'dgi-360'
        duo.compile_decoder(decoder)
        for pack in self.descriptor['characters']:
            for row in duo.pack_assets(pack):
                pixels = duo.art.resized(duo.ROOT/'assets/characters'/pack['name']/row['source'], row['w'], row['h'])
                duo.verify_decode(decoder, self.first/(row['hash']+'.dgi'), pixels, row['alpha'])
        row = self.descriptor['characters'][0]['faceplate']
        with self.assertRaisesRegex(ValueError, 'pixel mismatch'):
            duo.verify_decode(decoder, self.first/(row['hash']+'.dgi'), [(0, 0, 0, 255)], 0)
        bad = self.out/'corrupt.dgi'
        bad.write_bytes(b'not a DGI')
        with self.assertRaises(subprocess.CalledProcessError):
            duo.verify_decode(decoder, bad, [], 0)

    def test_failed_budget_does_not_publish_outputs(self):
        with mock.patch.object(duo, 'enforce_budget', side_effect=ValueError('charged peak exceeds 75% of arena')):
            with self.assertRaisesRegex(ValueError, 'charged peak'):
                duo.generate(self.out/'rejected', self.out/'rejected.h')
        self.assertFalse((self.out/'rejected').exists())
        self.assertFalse((self.out/'rejected.h').exists())

    def test_compiled_header_matches_every_json_field_and_role(self):
        # Compile the committed header and read all fields through its public
        # tables. This catches valid C++ with shifted initializer positions.
        code = self.out/'descriptor.cpp'
        code.write_text('''#include "duo_pack.gen.h"
#include <iostream>
using namespace dg_duo;
void frame(const char* layer,const Frame& f){
    std::cout<<layer<<" "<<f.name<<" "<<f.hash<<" "<<f.bytes<<" "<<f.x<<" "<<f.y<<" "<<f.w<<" "<<f.h<<"\\n";
}
int main(){
    std::cout<<CanvasWidth<<" "<<CanvasHeight<<" "<<PackCount<<"\\n";
    for(const Pack* p:Packs){
        std::cout<<p->name<<" "<<p->hash<<"\\n";frame("faceplate",p->faceplate);
        for(unsigned i=0;i<p->eye_count;++i)frame("eyes",p->eyes[i]);
        for(unsigned i=0;i<p->mouth_count;++i)frame("mouth",p->mouths[i]);
        for(unsigned i=0;i<p->layer_count;++i){
            switch(p->order[i]){
            case Layer::Faceplate:std::cout<<"faceplate ";break;
            case Layer::Eyes:std::cout<<"eyes ";break;
            case Layer::Mouth:std::cout<<"mouth ";break;
            }
        }
        std::cout<<"\\n";
        const auto& a=p->animator;
        for(unsigned index:{a.neutral,a.open,a.half,a.blink,a.look_left,a.look_right,a.happy})
            std::cout<<p->eyes[index].name<<" ";
        std::cout<<"\\n";
        for(unsigned index:a.mouth)std::cout<<p->mouths[index].name<<" ";
        std::cout<<"\\n";
    }
}
''')
        binary = self.out/'descriptor'
        subprocess.run([str(duo.ROOT/'scripts/build_locked.sh'), 'c++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-I'+str((duo.ROOT/duo.HEADER).parent), str(code), '-o', str(binary)], check=True)
        expected = ['360 360 2']
        for pack in self.descriptor['characters']:
            expected.append(pack['name']+' '+pack['hash'])
            groups = [('faceplate', [pack['faceplate']])]+[(layer['name'], layer['frames']) for layer in pack['layers']]
            for group, rows in groups:
                expected.extend(group+' '+' '.join(str(row[key]) for key in ('name', 'hash', 'bytes', 'x', 'y', 'w', 'h')) for row in rows)
            expected.append(' '.join(pack['layer_order']))
            expected.append(' '.join(pack['animator']['eyes'][role] for role in
                                     ('neutral', 'open', 'half', 'blink', 'look_left', 'look_right', 'happy')))
            expected.append(' '.join(pack['animator']['mouth'][role] for role in
                                     ('rest', 'energy_1', 'energy_2', 'energy_3', 'energy_4')))
        actual = subprocess.check_output([str(binary)], text=True).splitlines()
        self.assertEqual([line.strip() for line in actual], expected)


if __name__ == '__main__':
    unittest.main()
