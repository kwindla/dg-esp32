"""Procedural SVG backend: system librsvg-2/Cairo + Pillow, no reference bitmaps."""
import ctypes as C
import json
from pathlib import Path
import sys
from PIL import Image


def raster(svg, size=412):
    rsvg, cairo, gobject = (C.CDLL(n) for n in ('librsvg-2.so.2', 'libcairo.so.2', 'libgobject-2.0.so.0'))
    rsvg.rsvg_handle_new_from_data.argtypes = [C.c_char_p, C.c_size_t, C.c_void_p]
    rsvg.rsvg_handle_new_from_data.restype = C.c_void_p
    rsvg.rsvg_handle_render_cairo.argtypes = [C.c_void_p, C.c_void_p]
    rsvg.rsvg_handle_render_cairo.restype = C.c_int
    cairo.cairo_image_surface_create.argtypes = [C.c_int, C.c_int, C.c_int]
    cairo.cairo_image_surface_create.restype = C.c_void_p
    cairo.cairo_create.argtypes = [C.c_void_p]
    cairo.cairo_create.restype = C.c_void_p
    cairo.cairo_scale.argtypes = [C.c_void_p, C.c_double, C.c_double]
    cairo.cairo_surface_flush.argtypes = [C.c_void_p]
    cairo.cairo_image_surface_get_data.argtypes = [C.c_void_p]
    cairo.cairo_image_surface_get_data.restype = C.POINTER(C.c_ubyte)
    cairo.cairo_image_surface_get_stride.argtypes = [C.c_void_p]
    cairo.cairo_image_surface_get_stride.restype = C.c_int
    cairo.cairo_destroy.argtypes = [C.c_void_p]
    cairo.cairo_surface_destroy.argtypes = [C.c_void_p]
    gobject.g_object_unref.argtypes = [C.c_void_p]
    data = svg.encode()
    handle = rsvg.rsvg_handle_new_from_data(data, len(data), None)
    if not handle:
        raise ValueError('Invalid procedural SVG')
    surface = cairo.cairo_image_surface_create(0, size*2, size*2)
    context = cairo.cairo_create(surface)
    try:
        cairo.cairo_scale(context, size*2/412, size*2/412)
        if not rsvg.rsvg_handle_render_cairo(handle, context):
            raise ValueError('SVG render failed')
        cairo.cairo_surface_flush(surface)
        stride = cairo.cairo_image_surface_get_stride(surface)
        raw = C.string_at(cairo.cairo_image_surface_get_data(surface), stride*size*2)
        image = Image.frombytes('RGBa', (size*2, size*2), raw, 'raw', 'BGRa', stride).convert('RGBA')
        image = image.resize((size, size), Image.Resampling.LANCZOS)
        pixels = bytearray(image.tobytes())
        for at in range(0, len(pixels), 4):
            if not pixels[at+3]:
                pixels[at:at+3] = b'\0\0\0'
        return Image.frombytes('RGBA', image.size, bytes(pixels))
    finally:
        cairo.cairo_destroy(context)
        cairo.cairo_surface_destroy(surface)
        gobject.g_object_unref(handle)


def build(directory, data):
    directory.mkdir(parents=True, exist_ok=True)
    (directory/'sprites').mkdir(exist_ok=True)
    manifest = dict(version=2, display=dict(width=412, height=412, fps=20), base=data['base'],
                    palette=data['palette'], layers={}, integration=data['integration'], source=data['source'])
    raster(data['faceplate']).save(directory/data['base'], compress_level=9)
    for name, layer in data['layers'].items():
        frames = {key: raster(svg) for key, svg in layer['frames'].items()}
        boxes = [frame.getchannel('A').getbbox() for frame in frames.values()]
        if any(box is None for box in boxes):
            raise ValueError('Empty frame')
        x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
        manifest['layers'][name] = dict(bounds=[x0, y0, x1-x0, y1-y0], frames={})
        for key, image in frames.items():
            filename = f'sprites/{name}-{key}.png'
            image.crop((x0, y0, x1, y1)).save(directory/filename, compress_level=9)
            manifest['layers'][name]['frames'][key] = filename
    (directory/'animation.json').write_text(json.dumps(manifest, indent=2)+'\n')
    count = sum(len(layer['frames']) for layer in data['layers'].values())
    print(f'Built {directory.name}: Little Voices stage faceplate + {count} patches.')


if __name__ == '__main__':
    data = json.load(sys.stdin)
    if len(sys.argv) > 1:
        build(Path(sys.argv[1]), data)
    else:
        for filename, svg in data.items():
            raster(svg).save(filename, compress_level=9)
