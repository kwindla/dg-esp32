#!/usr/bin/env python3
"""Offline revision-1 previews and eye-registered fidelity evidence.

All reference crops/eye annotations and metric definitions are explicit below.
No image/colour fitting or reference pixels enter procedural sprite generation.
Requires system Pillow, NumPy, OpenCV, librsvg/Cairo and Node.
"""
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import cv2
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duo_art as art

SOURCE = Path('/tmp/claude-1000/-home-khkramer-src-dg-esp32/96431ade-9f70-4dfe-8e05-dc1a59acd461/scratchpad/ilv/repo')
PIN = '7f5613cd9071d4668d3aab9cfe8691ff8c59b4b2'
SWIFT = ['FaceRig', 'FaceGeometry', 'FaceExpressions', 'FaceParameters', 'RigMath', 'DuoCharacter',
         'DuoStageView', 'StageRenderer', 'StageLayout', 'StageColors', 'StagePalette', 'GlowSprites', 'SeamRig', 'LipsyncVisemes']
# Coordinates in the original 2400x1350 screenshot, not the chat-rendered image.
# Eye annotations are anatomical ellipse centers, independent of pupil/gaze.
REFERENCES = {
    'stage-angel': dict(character='angel', crop=[956, 315, 1444, 565],
                        eyes=[[1118, 483], [1282, 483]],
                        request=dict(character='angel', options=dict(expression='squished', sy=.92, press=1, gaze=[.1, .8]))),
    'stage-devil': dict(character='devil', crop=[956, 614, 1444, 892],
                        eyes=[[1107.1636810302734, 777.6240539550781], [1290.4393310546875, 782.1442413330078]],
                        eye_fit=dict(regions=[[108, 130, 195, 210], [294, 130, 379, 218]], lower_arc_min_y=175),
                        request=dict(character='devil', options=dict(expression='talking', scale=412/400*1.10, sy=1.03, gaze=[-.2, -.8]),
                                     talkOpen=.8, width=.65, rounding=0)),
    'avatar-angel': dict(character='angel', eyes=[[27, 51], [74, 50]],
                         request=dict(character='angel', sclera=1)),
    'avatar-devil': dict(character='devil', eyes=[[26, 49], [74, 49]],
                         request=dict(character='devil', sclera=1, eyes=dict(inward=.6))),
}


def similarity(source, target):
    a, b = np.asarray(source, dtype=np.float64)
    u, v = np.asarray(target, dtype=np.float64)
    d, e = b-a, v-u
    norm = float(d@d)
    cos = float(d@e)/norm
    sin = float(d[0]*e[1]-d[1]*e[0])/norm
    rotation = np.array([[cos, -sin], [sin, cos]])
    return np.column_stack((rotation, u-rotation@a))


def metrics(reference, candidate, valid, eye_centers, out, key):
    """Mean CIE76 ΔE in sRGB/D65 Lab; bidirectional edge F1 with fixed tolerance."""
    ref = np.asarray(reference.convert('RGB'))
    ours = np.asarray(candidate.convert('RGB'))
    height, width = valid.shape
    yy, xx = np.mgrid[:height, :width]
    left, right = np.asarray(eye_centers)
    distance = float(np.linalg.norm(right-left))
    mid = (left+right)/2
    # Full central face includes eyes, brows, cheeks and mouth. Field includes
    # accessories and the gradient surrounding it; no hand-picked low-error pixels.
    face = (((xx-mid[0])/(.73*distance))**2+((yy-mid[1]-.15*distance)/(.52*distance))**2 <= 1) & valid
    field = valid & ~face
    lab_ref = cv2.cvtColor(ref.astype(np.float32)/255, cv2.COLOR_RGB2LAB)
    lab_ours = cv2.cvtColor(ours.astype(np.float32)/255, cv2.COLOR_RGB2LAB)
    delta = np.linalg.norm(lab_ref-lab_ours, axis=2)
    gray_ref = cv2.cvtColor(ref, cv2.COLOR_RGB2GRAY)
    gray_ours = cv2.cvtColor(ours, cv2.COLOR_RGB2GRAY)
    edge_ref = cv2.Canny(cv2.GaussianBlur(gray_ref, (5, 5), .7), 60, 120) != 0
    edge_ours = cv2.Canny(cv2.GaussianBlur(gray_ours, (5, 5), .7), 60, 120) != 0
    interior = cv2.erode(valid.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    edge_ref &= interior
    edge_ours &= interior
    tolerance = max(1.5, distance*.01)
    dist_ref = cv2.distanceTransform((~edge_ref).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    dist_ours = cv2.distanceTransform((~edge_ours).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    precision = float((dist_ref[edge_ours] <= tolerance).mean()) if edge_ours.any() else 0
    recall = float((dist_ours[edge_ref] <= tolerance).mean()) if edge_ref.any() else 0
    overlap = 2*precision*recall/(precision+recall) if precision+recall else 0
    for suffix, mask in [('face-mask', face), ('field-mask', field), ('reference-edges', edge_ref), ('port-edges', edge_ours)]:
        Image.fromarray(mask.astype(np.uint8)*255).save(out/f'{key}-{suffix}.png')
    return dict(delta_e_variant='CIE76, Euclidean Lab D65 (OpenCV float sRGB conversion)',
                mean_delta_e_face=float(delta[face].mean()), mean_delta_e_field=float(delta[field].mean()),
                mean_delta_e_all=float(delta[valid].mean()), face_pixels=int(face.sum()), field_pixels=int(field.sum()),
                edge_overlap_f1=overlap, edge_precision=precision, edge_recall=recall, edge_tolerance_px=tolerance,
                reference_edge_pixels=int(edge_ref.sum()), port_edge_pixels=int(edge_ours.sum()))


def reference_image(key, spec, source):
    if key.startswith('stage'):
        image = Image.open(source/'docs/screenshots.png').convert('RGBA').crop(spec['crop'])
        x0, y0 = spec['crop'][:2]
        eyes = [[x-x0, y-y0] for x, y in spec['eyes']]
        if 'eye_fit' in spec:
            # Lids obscure the true ellipse centers. Fit only source lower arcs,
            # not the candidate image or pupils; this corrects manual centroids.
            image_array = np.asarray(image.convert('RGB'))
            eyes = []
            for ax, ay, bx, by in spec['eye_fit']['regions']:
                mask = (image_array[ay:by, ax:bx].max(axis=2) < 95).astype(np.uint8)*255
                contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
                points = max(contours, key=cv2.contourArea).reshape(-1, 2)+[ax, ay]
                lower = points[points[:, 1] >= spec['eye_fit']['lower_arc_min_y']].astype(np.float32)
                center, _, _ = cv2.fitEllipse(lower)
                eyes.append(list(center))
    else:
        c = spec['character'].title()
        image = Image.open(source/f'src/ios/PipecatVoice/Assets.xcassets/{c}Avatar.imageset/{c}Avatar@3x.png').convert('RGBA')
        eyes = spec['eyes']
    return image, eyes


def comparisons(out, source):
    cv2.setNumThreads(1)
    requests = {key: spec['request'] for key, spec in REFERENCES.items()}
    # A literal surprised pose is also exported so the brief's wording can be
    # inspected alongside the morphology-matched squished small-O screenshot.
    requests['literal-surprised-angel'] = dict(character='angel', options=dict(expression='surprised'))
    for c in art.CHARACTERS:
        requests[c+'-accessories'] = dict(character=c, part='accessories', renderOptions=dict(glow=False))
    result = json.loads(subprocess.check_output(['node', str(art.ROOT/'assets/characters/duo-proof.cjs')], input=json.dumps(requests).encode()))
    module_spec = importlib.util.spec_from_file_location('duo_raster', art.ROOT/'assets/characters/duo-raster.py')
    raster = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(raster)
    scores = {}
    for key, data in result.items():
        (out/f'{key}-procedural.svg').write_text(data['svg'])
        image = raster.raster(data['svg'])
        image.save(out/f'{key}-render-412.png')
        (out/f'{key}-parameters.json').write_text(json.dumps(data['parameters'], indent=2)+'\n')
        if key not in REFERENCES:
            continue
        spec = REFERENCES[key]
        reference, eyes = reference_image(key, spec, source)
        reference.save(out/f'{key}-reference-crop.png')
        source_eyes = data['eyeCenters']
        native = image
        pixels = art.resized(out/f'{key}-render-412.png', 360, 360)
        runtime = art.rgba565(360, 360, pixels)
        runtime.save(out/f'{key}-runtime-360.png')
        scores[key] = dict(request=spec['request'], reference_eyes=eyes, source_eyes=source_eyes,
                           reference_crop=spec.get('crop'), eye_fit=spec.get('eye_fit'),
                           annotations='Anatomical ellipse centers: devil fitted to source lower arcs (specified ROIs/cutoff), others manually annotated. No candidate-image fitting. Approx ±2px stage / ±1px avatars.')
        for variant, candidate, ratio in [('author', native, 1), ('runtime', runtime, 360/412)]:
            transform = similarity(np.asarray(source_eyes)*ratio, eyes)
            width, height = reference.size
            aligned = cv2.warpAffine(np.asarray(candidate), transform, (width, height), flags=cv2.INTER_LINEAR)
            valid = (np.asarray(reference)[:, :, 3] >= 254) & (aligned[:, :, 3] >= 254)
            aligned_image = Image.fromarray(aligned, 'RGBA')
            aligned_image.save(out/f'{key}-{variant}-aligned.png')
            blend = Image.blend(reference, aligned_image, .5)
            blend.save(out/f'{key}-{variant}-blend50.png')
            # Labelled A/B plus blend, enlarged uniformly for tiny avatar crops.
            scale = 3 if key.startswith('avatar') else 1
            pair = Image.new('RGB', (width*3, height+24), '#17131d')
            draw = ImageDraw.Draw(pair)
            for i, (label, content) in enumerate([('Reference', reference), ('Port: '+variant, aligned_image), ('50% blend', blend)]):
                pair.paste(content.convert('RGB'), (i*width, 24))
                draw.text((i*width+8, 5), label, fill='white')
            if scale != 1:
                pair = pair.resize((pair.width*scale, pair.height*scale), Image.Resampling.NEAREST)
            pair.save(out/f'{key}-{variant}-ab.png')
            score = metrics(reference, aligned_image, valid, eyes, out, key+'-'+variant)
            score['similarity_transform'] = transform.tolist()
            scores[key][variant] = score
    report = dict(method='Eye-center similarity only, no colour matching/warping; fixed geometric face mask and its field complement. All masks and edges retained.',
                  source_revision=PIN, comparisons=scores,
                  differences=[
                      'Stage crop is a live squished/listening angel and speaking devil. No original frame-state trace exists; exact live springs, gaze drift and audio viseme are unavailable. Named source poses and every numeric parameter are recorded.',
                      'Angel small-O uses ExpressionLibrary.squished (16x20), sy=.92 and gaze(.1,.8). Literal .surprised is25x40 and separately exported; it is visibly not the screenshot small-O.',
                      'Devil comparison uses talking eyes and LipSyncMapping(.8,width=.65,rounding=0). Mouth width/depth and eye lid/gaze may differ from the unknown captured audio instant.',
                      'Angel stage residuals: brow placement/weight is slightly different, wing edges differ by a few pixels, and the halo bloom has a different soft falloff. Settled source parameters omit the captured live spring offsets; raster/blur kernels also differ.',
                      'Devil stage residuals: the source talking brows sit closer to the lids than the captured brows, pupils/lid edges are displaced, and the mouth skew, tongue dome and fang root differ slightly. Horn/spark alignment differs by a few pixels. These remain visible in the retained blends; source-preset reconstruction is not a recovered frame-state trace.',
                      'Branding avatars are independently drawn bitmaps exported from avatars.png, not StageRenderer output. Their broader eyes/pupils, mouth silhouettes, palettes/blush and absent halo/wings/horns differ from the requested in-app stage target.',
                      'Single-character framing omits the two-face seam, caption bubbles, phone UI and the other character. The app field gradient and complete accessories remain.',
                      'Cairo versus CoreGraphics differs in subpixel antialiasing and halo shadow kernel. SVG uses two Gaussian passes for the source12fu/24fu halo shadows.',
                      '360px nearest-neighbor conversion and RGB565 introduce edge stepping and field colour banding; runtime A/B scores measure that additional cost.',
                  ])
    (out/'fidelity.json').write_text(json.dumps(report, indent=2)+'\n')
    return report


def contact_sheets(out):
    for character in art.CHARACTERS:
        source, manifest, _ = art.assets(character)
        base = Image.open(source/manifest['base']).convert('RGBA')
        eyes = list(manifest['layers']['eyes']['frames'])
        mouths = list(manifest['layers']['mouth']['frames'])
        sheet = Image.new('RGB', (206*len(mouths), 228*len(eyes)), '#17131d')
        draw = ImageDraw.Draw(sheet)
        for row, eye in enumerate(eyes):
            for col, mouth in enumerate(mouths):
                image = base.copy()
                for group, key in [('eyes', eye), ('mouth', mouth)]:
                    layer = manifest['layers'][group]
                    image.alpha_composite(Image.open(source/layer['frames'][key]).convert('RGBA'), tuple(layer['bounds'][:2]))
                sheet.paste(image.resize((206, 206), Image.Resampling.LANCZOS).convert('RGB'), (col*206, row*228))
                draw.text((col*206+5, row*228+208), f'{eye} / {mouth}', fill='white')
        sheet.save(out/f'{character}-contact-sheet.png')


def revision_previews(out):
    """Preserve original exporter; update the revision's energy range to all4 levels."""
    art.preview(out)
    preview = out/'preview.html'
    text = preview.read_text().replace('flat Little Voices palette', 'Little Voices stage renderer · gradients and source geometry')
    text = text.replace("energy<.65?'medium':'wide'", "energy<.65?'medium':energy<.75?'wide':'round'")
    text = text.replace('Art approval pending.', 'Legacy round key = maximum talking opening. <a href="fidelity.html">Source A/B comparisons and scores</a>. Art approval pending.')
    preview.write_text(text)
    for character in art.CHARACTERS:
        source, manifest, files = art.assets(character)
        images = {file: art.rgba565(b[2], b[3], art.resized(source/file, b[2], b[3])) for file, b in files}
        mask = Image.new('L', (360, 360))
        ImageDraw.Draw(mask).ellipse((0, 0, 359, 359), fill=255)
        frames = []
        timeline = []
        for tick in range(80):
            eye = {20: 'half', 21: 'closed', 22: 'closed', 23: 'half'}.get(tick, 'open')
            energy = max(0, math.sin((tick-30)*.61))*abs(math.sin((tick-30)*.17)) if 30 <= tick < 70 else 0
            mouth = 'rest-smile' if energy < .08 else 'small' if energy < .32 else 'medium' if energy < .65 else 'wide' if energy < .75 else 'round'
            image = images[manifest['base']].copy()
            for group, key in [('eyes', eye), ('mouth', mouth)]:
                layer = manifest['layers'][group]
                image.alpha_composite(images[layer['frames'][key]], tuple(art.scaled_bounds(layer['bounds'])[:2]))
            frame = Image.new('RGB', (360, 360), '#17131D')
            frame.paste(image, (0, 0), mask)
            frames.append(frame)
            timeline.append(dict(tick=tick, eye=eye, mouth=mouth, energy=energy))
        frames[0].save(out/f'{character}-idle-blink-speaking.gif', save_all=True, append_images=frames[1:], duration=50, loop=0, disposal=2, optimize=False)
        (out/f'{character}-preview-timeline.json').write_text(json.dumps(timeline, indent=2)+'\n')
        assert {row['mouth'] for row in timeline} == set(manifest['layers']['mouth']['frames'])
        contact = Image.new('RGB', (1080, 720), '#17131D')
        for i, tick in enumerate([0, 21, 32, 37, 41, 57]):
            contact.paste(frames[tick], (i % 3*360, i//3*360))
        contact.save(out/f'{character}-animation-contact.png')


def fidelity_page(out, report):
    rows = ['<!doctype html><meta charset="utf-8"><title>Little Voices fidelity</title><style>body{background:#17131d;color:#fff4df;font:16px system-ui;margin:32px}img{max-width:100%;height:auto}p{max-width:1000px;line-height:1.5}a{color:#ffd78a}</style>',
            '<h1>Little Voices source comparisons</h1><p>Eye-centre similarity registration only. Each row: source, port, 50% blend. Main stage target and independently drawn avatar branding are reported separately. Scores use CIE76 ΔE and bidirectional edge F1; all masks and parameters accompany these images.</p>']
    for key, value in report['comparisons'].items():
        rows.append(f'<h2>{key}</h2>')
        for variant in ('author', 'runtime'):
            score = value[variant]
            rows.append(f'<p>{variant}: ΔE face {score["mean_delta_e_face"]:.3f}, field {score["mean_delta_e_field"]:.3f}; edge F1 {score["edge_overlap_f1"]:.4f}</p><img src="{key}-{variant}-ab.png" alt="{key} {variant} reference, port, blend">')
    rows.append('<h2>Remaining differences</h2><ul>'+''.join('<li>'+item+'</li>' for item in report['differences'])+'</ul><p><a href="fidelity.json">Exact metrics, annotations and transforms</a></p>')
    (out/'fidelity.html').write_text('\n'.join(rows))


def provenance(out, source):
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    if actual != PIN:
        raise ValueError('Reference revision differs')
    files = {name+'.swift': hashlib.sha256((source/f'src/ios/PipecatVoice/{name}.swift').read_bytes()).hexdigest() for name in SWIFT}
    for name in ['docs/screenshots.png', 'src/branding/avatars.png', 'src/branding/export.py']:
        files[name] = hashlib.sha256((source/name).read_bytes()).hexdigest()
    (out/'source-provenance.json').write_text(json.dumps(dict(commit=actual, files=files), indent=2)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path.home()/'src/evidence/duo/step-1-rev-1')
    parser.add_argument('--source', type=Path, default=SOURCE)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    provenance(args.out, args.source)
    estimate = art.estimate(args.out)
    revision_previews(args.out)
    contact_sheets(args.out)
    report = comparisons(args.out, args.source)
    fidelity_page(args.out, report)
    print(json.dumps(dict(budget=estimate['summary'], fidelity={key: {v: data[v] for v in ('author', 'runtime')} for key, data in report['comparisons'].items()}), indent=2))


if __name__ == '__main__':
    main()
