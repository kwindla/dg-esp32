#!/usr/bin/env python3
"""Generate committed 360px duo fixtures and the unused firmware descriptor.

Regenerate from the repository root (no board or services):
  TMPDIR=$HOME/.cache/gap3-tmp PYTHONDONTWRITEBYTECODE=1 python3 hil/duo_assets.py
The cat/status HIL asset origin uses STEP9A_FIXTURES=hil/fixtures/echoear360.
No origin is started by this tool. Paths in JSON are repository-relative, even
when --out/--header place regeneration evidence outside the repository.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import duo_art as art

ROOT = art.ROOT
SOURCE_COMMIT = '69d391d3858dc554f904cddf3bd67df1f86a2eb2'
FIXTURES = Path('hil/fixtures/echoear360')
HEADER = Path('components/dg_duo/duo_pack.gen.h')
FRAMES = dict(eyes=('open', 'half', 'closed', 'look-left', 'look-right', 'happy', 'neutral'),
              mouth=('rest-smile', 'small', 'medium', 'wide', 'round'))
ANIMATOR = dict(eyes=dict(neutral='neutral', open='open', half='half', blink='closed',
                          look_left='look-left', look_right='look-right', happy='happy'),
                mouth=dict(rest='rest-smile', energy_1='small', energy_2='medium',
                           energy_3='wide', energy_4='round'))
ARENA_BYTES = 491520
ARENA_LIMIT = ARENA_BYTES*3//4


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def unique_object(pairs):
    # json.loads normally silently discards a duplicate frame key.
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key/frame: '+key)
        result[key] = value
    return result


def bounds_inside(bounds, canvas):
    if (len(bounds) != 4 or any(type(v) is not int for v in bounds)):
        raise ValueError('invalid patch bounds')
    x, y, w, h = bounds
    if not (0 <= x < x+w <= canvas and 0 <= y < y+h <= canvas):
        raise ValueError(f'patch outside {canvas} canvas: {bounds}')


def load_manifest(source):
    manifest = json.loads((source/'animation.json').read_text(), object_pairs_hook=unique_object)
    if (manifest.get('version') != 2 or manifest.get('display', {}).get('width') != 412
            or manifest['display'].get('height') != 412):
        raise ValueError('expected animation.json v2 at 412x412')
    if set(manifest.get('layers', {})) != set(FRAMES):
        raise ValueError('missing or unexpected layers')
    seen = {manifest['base']}
    for group, names in FRAMES.items():
        layer = manifest['layers'][group]
        if set(layer['frames']) != set(names):
            raise ValueError('missing or unexpected '+group+' frames')
        bounds_inside(layer['bounds'], 412)
        bounds_inside(art.scaled_bounds(layer['bounds']), 360)
        for name in names:
            filename = layer['frames'][name]
            if filename in seen:
                raise ValueError('duplicate frame file: '+filename)
            seen.add(filename)
    for filename in seen:
        path = (source/filename).resolve()
        if not path.is_relative_to(source.resolve()) or not path.is_file():
            raise ValueError('missing or invalid frame file: '+filename)
    return manifest


def compile_decoder(out):
    # Only a host executable; the shared lock also serializes this small build.
    subprocess.run([str(ROOT/'scripts/build_locked.sh'), 'c++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                    '-DCONFIG_DG_BOARD_ECHOEAR=1',
                    '-I'+str(ROOT/'components/dg_display/include'),
                    '-I'+str(ROOT/'components/dg_board/include'),
                    str(ROOT/'components/dg_display/src/dgi.cpp'),
                    str(ROOT/'components/dg_display/test_host/dgi_cli.cpp'), '-o', str(out)], check=True)
    # Both production boards share this 360 canvas. Prove the limit is active.
    invalid = out.parent/'too-wide.dgi'
    invalid.write_bytes(art.dgi.encode(361, 1, [(0, 0, 0, 255)]*361))
    if subprocess.run([str(out), str(invalid)], capture_output=True).returncode != 3:
        raise ValueError('production decoder did not enforce the 360 canvas')


def verify_decode(decoder, path, pixels, alpha):
    expected = b''.join(c.to_bytes(2, 'big')+bytes([a]) for c, a in art.dgi.converted(pixels, alpha))
    result = subprocess.run([str(decoder), str(path)], capture_output=True, check=True)
    if result.stdout != expected:
        raise ValueError('production decoder pixel mismatch: '+path.name)


def encode_asset(source, filename, name, bounds, source_size, scratch, decoder, blobs):
    bounds_inside(bounds, 360)
    width, height, pixels = art.dgi.png(source/filename)
    if (width, height) != tuple(source_size):
        raise ValueError('frame PNG dimensions differ from manifest: '+filename)
    w, h = bounds[2:]
    resized = [pixels[(y*height//h)*width+x*width//w] for y in range(h) for x in range(w)]
    data, codec_bytes = art.encode_best(w, h, resized)
    identity = digest(data)
    path = scratch/(identity+'.dgi')
    path.write_bytes(data)
    verify_decode(decoder, path, resized, data[5])
    blobs[identity] = data
    x, y = bounds[:2]
    return dict(name=name, hash=identity, x=x, y=y, w=w, h=h, bytes=len(data),
                path=(FIXTURES/path.name).as_posix(), codec=data[6], alpha=data[5], codec_bytes=codec_bytes,
                source=filename, source_sha256=digest((source/filename).read_bytes()))


def pack_assets(pack):
    return [pack['faceplate']]+[frame for layer in pack['layers'] for frame in layer['frames']]


def existing_assets():
    root = ROOT/'hil/fixtures/echoear360'
    status = json.loads((root/'status.json').read_text())['assets']
    cat = set(re.findall(r'"([0-9a-f]{64})"', (ROOT/'main/demo_assets.h').read_text()))
    existing = {identity: (root/(identity+'.dgi')).stat().st_size for identity in cat}
    existing.update({row['hash']: row['bytes'] for row in status})
    return status, existing


def enforce_budget(packs, status, existing):
    rows = [row for pack in packs for row in pack_assets(pack)]
    for pack in packs:
        if pack['faceplate']['bytes'] > 100*1024:
            raise ValueError('faceplate exceeds 100 KiB')
    if any(row['bytes'] > 256*1024 for row in rows):
        raise ValueError('asset exceeds 256 KiB')
    unique = {row['hash']: row['bytes'] for row in rows}
    statuses = {row['hash']: row['bytes'] for row in status}
    resident = {**statuses, **unique}
    flash = {**existing, **unique}
    if len(resident) > 96 or len(flash) > 96:
        raise ValueError('unique hashes exceed 96')
    largest = max(unique.values())
    resident_charge = sum(art.charge(size) for size in resident.values())
    reservation = art.charge(largest)
    peak = resident_charge+reservation
    if peak > ARENA_LIMIT:
        raise ValueError('charged peak exceeds 75% of arena')
    flash_peak = sum(flash.values())+largest
    return dict(arena_bytes=ARENA_BYTES, arena_limit=ARENA_LIMIT,
                status_charged=sum(art.charge(size) for size in statuses.values()),
                packs_charged=sum(art.charge(size) for size in unique.values()),
                per_pack_charged={pack['name']: sum(art.charge(size) for size in
                                 {row['hash']: row['bytes'] for row in pack_assets(pack)}.values()) for pack in packs},
                resident_charged=resident_charge, extra_install_reservation=reservation, peak_charged=peak,
                headroom=ARENA_LIMIT-peak, unique_duo_hashes=len(unique), unique_resident_hashes=len(resident),
                unique_flash_hashes=len(flash), existing_payload=sum(existing.values()),
                duo_payload=sum(unique.values()), largest_install_temp=largest,
                flash_payload_plus_temp=flash_peak, minimum_flash_usable=(flash_peak*4+2)//3,
                hardware_gate='Step 4a: measure used and largest free block in the arena, and per-board '
                              'LittleFS inventory/usable/free; payload plus temp <= floor(0.75 * usable) '
                              'and runtime physical-free reserves must pass.')


def render_header(descriptor):
    lines = ['// Generated by hil/duo_assets.py; do not edit. Linked only in CONFIG_DG_DUO images.',
             '#pragma once', 'namespace dg_duo {',
             'struct Frame {const char* name; const char* hash; unsigned bytes; int x,y; unsigned w,h;};',
             'enum class Layer {Faceplate,Eyes,Mouth};',
             '// Eye slots: 0=open, 1=half, 2=closed (blink), 3=look-left, 4=look-right, 5=happy, 6=neutral (settled rig).',
             '// mouth[0] = rest; mouth[1..4] = energy levels in rising openness (talkOpen .25/.5/.75/1).',
             'struct Animator {unsigned neutral,open,half,blink,look_left,look_right,happy; unsigned mouth[5];};',
             'struct Pack {const char* name; const char* hash; Frame faceplate; const Frame* eyes; unsigned eye_count;',
             '    const Frame* mouths; unsigned mouth_count; const Layer* order; unsigned layer_count; Animator animator;};',
             'inline constexpr unsigned CanvasWidth=360,CanvasHeight=360;']

    def frame(row):
        return '{'+json.dumps(row['name'])+','+json.dumps(row['hash'])+','+','.join(
            str(row[key]) for key in ('bytes', 'x', 'y', 'w', 'h'))+'}'

    for pack in descriptor['characters']:
        prefix = pack['name']
        for layer in pack['layers']:
            lines.append(f'inline constexpr Frame {prefix}_{layer["name"]}[]={{')
            lines.extend('    '+frame(row)+',' for row in layer['frames'])
            lines.append('};')
        order = ','.join('Layer::'+name.title() for name in pack['layer_order'])
        lines.append(f'inline constexpr Layer {prefix}_order[]={{'+order+'};')
        indexes = {layer['name']: {row['name']: i for i, row in enumerate(layer['frames'])} for layer in pack['layers']}
        eye_roles = ','.join(str(indexes['eyes'][pack['animator']['eyes'][role]]) for role in ANIMATOR['eyes'])
        mouth_roles = ','.join(str(indexes['mouth'][pack['animator']['mouth'][role]]) for role in ANIMATOR['mouth'])
        lines.extend([f'inline constexpr Pack {prefix}={{',
                      '    '+json.dumps(prefix)+','+json.dumps(pack['hash'])+','+frame(pack['faceplate'])+',',
                      f'    {prefix}_eyes,{len(indexes["eyes"])},{prefix}_mouth,{len(indexes["mouth"])},'
                      f'{prefix}_order,{len(pack["layer_order"])},',
                      '    {'+eye_roles+',{'+mouth_roles+'}}', '};'])
    lines.extend(['inline constexpr const Pack* Packs[]={&angel,&devil};',
                  'inline constexpr unsigned PackCount=sizeof(Packs)/sizeof(Packs[0]);', '}'])
    return '\n'.join(lines)+'\n'


def generate(out=ROOT/FIXTURES, header=ROOT/HEADER, source_root=ROOT/'assets/characters'):
    scratch_root = Path(os.environ.get('TMPDIR', Path.home()/'.cache/gap3-tmp'))
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='duo-assets-', dir=scratch_root) as temporary:
        scratch = Path(temporary)
        decoder = scratch/'dgi-360'
        compile_decoder(decoder)
        packs, blobs = [], {}
        for character in art.CHARACTERS:
            source = source_root/character
            manifest = load_manifest(source)
            faceplate = encode_asset(source, manifest['base'], 'faceplate', [0, 0, 360, 360],
                                     [412, 412], scratch, decoder, blobs)
            layers = []
            for group, names in FRAMES.items():
                layer = manifest['layers'][group]
                frames = [encode_asset(source, layer['frames'][name], name, art.scaled_bounds(layer['bounds']),
                                       layer['bounds'][2:], scratch, decoder, blobs) for name in names]
                layers.append(dict(name=group, frames=frames))
            pack = dict(name=character, faceplate=faceplate, layers=layers,
                        layer_order=['faceplate', 'eyes', 'mouth'], animator=ANIMATOR,
                        manifest_sha256=digest((source/'animation.json').read_bytes()))
            pack['hash'] = digest(canonical(pack))
            packs.append(pack)
        status, existing = existing_assets()
        budget = enforce_budget(packs, status, existing)
        descriptor = dict(version=1, canvas=[360, 360], source_commit=SOURCE_COMMIT,
                          conversion='floor endpoints 412 to 360; nearest neighbor; smaller codec, ties use RLE',
                          pack_identity='SHA-256 of canonical sorted-key compact JSON character object excluding hash',
                          characters=packs, budget=budget)
        # Nothing is published until every manifest, decoder and budget gate passes.
        out.mkdir(parents=True, exist_ok=True)
        for identity, data in sorted(blobs.items()):
            (out/(identity+'.dgi')).write_bytes(data)
        (out/'duo-pack.json').write_text(json.dumps(descriptor, indent=2)+'\n')
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_text(render_header(descriptor))
        return descriptor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT/FIXTURES)
    parser.add_argument('--header', type=Path, default=ROOT/HEADER)
    args = parser.parse_args()
    try:
        descriptor = generate(args.out, args.header)
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'duo assets rejected: {error}\n')
    print(json.dumps(descriptor['budget'], indent=2))


if __name__ == '__main__':
    main()
