#!/usr/bin/env python3
"""Offline step-1 conversion estimates and art evidence; no board/service access.

Run with system Python + Pillow: python3 hil/duo_art.py --out ~/src/evidence/duo/step-1
The encoder and endpoint rounding match step9a_echoear_assets.py. This does not
install assets, create a runtime descriptor, or change the frozen cat fixtures.
"""
import argparse
import base64
import hashlib
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
import dgi

CHARACTERS = ('angel', 'devil')


def scaled(value):
    return value*360//412


def scaled_bounds(bounds):
    x, y, w, h = bounds
    return [scaled(x), scaled(y), scaled(x+w)-scaled(x), scaled(y+h)-scaled(y)]


def assets(character):
    source = ROOT/'assets/characters'/character
    manifest = json.loads((source/'animation.json').read_text())
    rows = [(manifest['base'], [0, 0, 360, 360])]
    for layer in manifest['layers'].values():
        rows.extend((file, scaled_bounds(layer['bounds'])) for file in layer['frames'].values())
    return source, manifest, rows


def resized(path, width, height):
    oldw, oldh, pixels = dgi.png(path)
    return [pixels[(y*oldh//height)*oldw+x*oldw//width] for y in range(height) for x in range(width)]


def encode_best(width, height, pixels):
    options = {codec: dgi.encode(width, height, pixels, codec=codec) for codec in (1, 2)}
    codec = min(options, key=lambda key: (len(options[key]), key))
    return options[codec], {str(key): len(value) for key, value in options.items()}


def charge(size):
    # Both targets are 32-bit: Arena::Block is 16 bytes, payload aligned to 16.
    return (size+15)//16*16+16


def estimate(out):
    rows = []
    (out/'runtime-estimate').mkdir(parents=True, exist_ok=True)
    for character in CHARACTERS:
        source, _, files = assets(character)
        for filename, bounds in files:
            w, h = bounds[2:]
            data, options = encode_best(w, h, resized(source/filename, w, h))
            identity = hashlib.sha256(data).hexdigest()
            (out/'runtime-estimate'/(identity+'.dgi')).write_bytes(data)
            rows.append(dict(character=character, source=filename, bounds=bounds, hash=identity,
                             bytes=len(data), charged_bytes=charge(len(data)), codec=data[6],
                             alpha=data[5], codec_bytes=options,
                             source_sha256=hashlib.sha256((source/filename).read_bytes()).hexdigest()))
    unique = {row['hash']: row for row in rows}
    status = json.loads((ROOT/'hil/fixtures/echoear360/status.json').read_text())['assets']
    status_charge = sum(charge(row['bytes']) for row in status)
    packs_charge = sum(row['charged_bytes'] for row in unique.values())
    largest = max(row['bytes'] for row in rows)
    cat_hashes = re.findall(r'"([0-9a-f]{64})"', (ROOT/'main/demo_assets.h').read_text())
    existing = {identity: (ROOT/'hil/fixtures/echoear360'/(identity+'.dgi')).stat().st_size
                for identity in set(cat_hashes)}
    existing.update({row['hash']: row['bytes'] for row in status})
    summary = dict(arena_bytes=491520, arena_limit=368640, status_payload=sum(r['bytes'] for r in status),
                   status_charged=status_charge, packs_charged=packs_charge,
                   per_pack_charged={c: sum(row['charged_bytes'] for row in
                                          {r['hash']: r for r in rows if r['character'] == c}.values())
                                     for c in CHARACTERS},
                   resident_charged=status_charge+packs_charge,
                   extra_install_reservation=charge(largest),
                   peak_charged=status_charge+packs_charge+charge(largest),
                   unique_duo_hashes=len(unique), unique_resident_hashes=len(set(unique)|{r['hash'] for r in status}),
                   unique_flash_hashes=len(set(unique)|set(existing)),
                   existing_payload=sum(existing.values()), duo_payload=sum(r['bytes'] for r in unique.values()),
                   largest_install_temp=largest,
                   flash_payload_plus_temp=sum(existing.values())+sum(r['bytes'] for r in unique.values())+largest,
                   flash_gate='Coordinator must measure each board: payload plus temp <= floor(0.75 * usable).')
    result = dict(basis='Canonical dgi.py encoder, nearest-neighbor 412→360, floor endpoints, smaller codec; target charges.',
                  rows=rows, summary=summary)
    (out/'runtime-estimate.json').write_text(json.dumps(result, indent=2)+'\n')
    return result


def draft_estimate(out):
    from PIL import Image, ImageDraw
    rows = []
    for kind in ('flat', 'gradient'):
        image = Image.new('RGBA', (412, 412), '#F8C24A')
        if kind == 'gradient':
            pixels = image.load()
            for y in range(412):
                for x in range(412):
                    t = min(1, math.hypot((x-206)/260, (y-185)/220))
                    pixels[x, y] = (round(254-6*t), round(239-45*t), round(162-88*t), 255)
        draw = ImageDraw.Draw(image)
        if kind == 'flat':
            draw.ellipse((49, 62, 363, 382), fill='#FDE385')
        draw.ellipse((112, 94, 300, 145), fill='#FFFDF4')
        draw.ellipse((130, 106, 282, 133), fill='#FDE385')
        for x in (119, 292):
            draw.ellipse((x-20, 268, x+20, 292), fill='#FDCB63')
        source = out/f'draft-{kind}-412.png'
        image.save(source)
        data, options = encode_best(360, 360, resized(source, 360, 360))
        (out/f'draft-{kind}-360.dgi').write_bytes(data)
        rows.append(dict(draft=kind, rle_bytes=options['1'], q565_bytes=options['2'], chosen_codec=data[6], bytes=len(data)))
    if rows[0]['bytes'] > 100*1024:
        raise RuntimeError('Flat draft exceeds faceplate cap; stop before authoring packs')
    (out/'draft-estimate.json').write_text(json.dumps(rows, indent=2)+'\n')
    return rows


def rgba565(width, height, pixels):
    from PIL import Image
    # Display the actual quantized colours and alpha used by the DGI encoder.
    converted = dgi.converted(pixels, 0 if all(p[3] == 255 for p in pixels) else 2)
    image = Image.new('RGBA', (width, height))
    image.putdata([(((c >> 11) << 3) | (c >> 13),
                    (((c >> 5) & 63) << 2) | ((c >> 9) & 3),
                    ((c & 31) << 3) | ((c & 31) >> 2), a) for c, a in converted])
    return image


def preview(out):
    from PIL import Image, ImageDraw
    browser = {}
    for character in CHARACTERS:
        source, manifest, files = assets(character)
        images = {file: rgba565(b[2], b[3], resized(source/file, b[2], b[3])) for file, b in files}
        bounds = {group: scaled_bounds(layer['bounds']) for group, layer in manifest['layers'].items()}

        def composite(eyes, mouth):
            image = images[manifest['base']].copy()
            for group, key in (('eyes', eyes), ('mouth', mouth)):
                image.alpha_composite(images[manifest['layers'][group]['frames'][key]], tuple(bounds[group][:2]))
            return image

        mask = Image.new('L', (360, 360))
        ImageDraw.Draw(mask).ellipse((0, 0, 359, 359), fill=255)
        still = composite('open', 'rest-smile')
        round_still = still.copy()
        round_still.putalpha(mask)
        round_still.save(out/f'{character}-s3-360.png')
        panel = Image.new('RGB', (800, 800), '#000000')
        panel.paste(still.resize((720, 720), Image.Resampling.NEAREST), (40, 40))
        panel.save(out/f'{character}-p4-800.png')
        frames = []
        # 4 seconds, 20 Hz: idle, 200ms blink, then deterministic simulated energy.
        for tick in range(80):
            eye = {20: 'half', 21: 'closed', 22: 'closed', 23: 'half'}.get(tick, 'open')
            energy = max(0, math.sin((tick-30)*0.61))*abs(math.sin((tick-30)*0.17)) if 30 <= tick < 70 else 0
            mouth = 'rest-smile' if energy < .08 else 'small' if energy < .32 else 'medium' if energy < .65 else 'wide'
            frame = Image.new('RGB', (360, 360), '#17131D')
            frame.paste(composite(eye, mouth), (0, 0), mask)
            frames.append(frame)
        frames[0].save(out/f'{character}-idle-blink-speaking.gif', save_all=True, append_images=frames[1:],
                       duration=50, loop=0, disposal=2, optimize=False)
        # Self-contained HTML so the evidence can move without losing pack-relative paths.
        encoded = {}
        import io
        for name, image in images.items():
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            encoded[name] = 'data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode()
        browser[character] = dict(manifest=manifest, bounds=bounds, images=encoded)
    html = '''<!doctype html><meta charset="utf-8"><title>Angel & Devil · step 1 art review</title>
<style>body{background:#17131d;color:#fff4df;font:16px system-ui;margin:32px}h1{font-weight:600}
main{display:flex;gap:40px;flex-wrap:wrap}article{max-width:380px}canvas{width:360px;height:360px;border-radius:50%}
select,button{font:inherit;padding:8px;margin:12px 4px 12px 0}a{color:#ffd78a}p{line-height:1.5}</style>
<h1>Angel & Devil</h1><p>Round S3 preview · simulated speaking energy · flat Little Voices palette</p>
<main></main><p>Authored at 412; previews use 360 RGB565 colours and endpoint-rounded patches.
P4 stills show the 720 square image centred in 800 with 40 px borders. Art approval pending.</p>
<script>const packs=PACKS;
(async()=>{for(const [name,p] of Object.entries(packs)){
 const card=document.createElement('article');card.innerHTML=`<h2>${name}</h2><canvas width="360" height="360"></canvas>
 <div><button>Pause</button><select aria-label="Eyes"></select><select aria-label="Mouth"></select></div>
 <p><a href="${name}-s3-360.png">S3 still</a> · <a href="${name}-p4-800.png">P4 still</a> ·
 <a href="${name}-idle-blink-speaking.gif">Animation GIF</a></p>`;document.querySelector('main').append(card);
 const images={};await Promise.all(Object.entries(p.images).map(async([key,src])=>{const image=new Image();
 image.src=src;await image.decode();images[key]=image}));
 const canvas=card.querySelector('canvas'),ctx=canvas.getContext('2d'),selects=card.querySelectorAll('select');
 for(const [i,group] of ['eyes','mouth'].entries())for(const key of Object.keys(p.manifest.layers[group].frames))
 selects[i].add(new Option(key,key));let playing=true,tick=0;
 card.querySelector('button').onclick=e=>{playing=!playing;e.target.textContent=playing?'Pause':'Play'};
 const draw=()=>{ctx.clearRect(0,0,360,360);ctx.drawImage(images[p.manifest.base],0,0);
 for(const [i,group] of ['eyes','mouth'].entries()){const [x,y]=p.bounds[group];
 ctx.drawImage(images[p.manifest.layers[group].frames[selects[i].value]],x,y)}};
 selects.forEach(s=>s.onchange=()=>{playing=false;card.querySelector('button').textContent='Play';draw()});
 setInterval(()=>{if(playing){const t=tick++%80;selects[0].value=({20:'half',21:'closed',22:'closed',23:'half'})[t]||'open';
 const energy=t>=30&&t<70?Math.max(0,Math.sin((t-30)*.61))*Math.abs(Math.sin((t-30)*.17)):0;
 selects[1].value=energy<.08?'rest-smile':energy<.32?'small':energy<.65?'medium':'wide';draw()}},50);draw();
}})();</script>'''
    (out/'preview.html').write_text(html.replace('PACKS', json.dumps(browser)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path.home()/'src/evidence/duo/step-1')
    parser.add_argument('--draft-only', action='store_true')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.draft_only:
        print(json.dumps(draft_estimate(args.out), indent=2))
    else:
        result = estimate(args.out)
        preview(args.out)
        print(json.dumps(result['summary'], indent=2))


if __name__ == '__main__':
    main()
