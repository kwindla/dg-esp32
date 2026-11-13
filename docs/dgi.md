# DGI1 image format

This specification precedes the encoder/decoder implementation. DGI1 is an immutable, row-restarting RGB565 image with straight alpha, identified by the full lowercase SHA-256 of every file byte. The RGB565 words in pixel data are **big endian**. Integer header/table fields are **little endian**. No native-endian representation is permitted on disk.

## Header and row index

The 32-byte header is:

| Offset | Width | Meaning |
| --- | --- | --- |
| 0 | 4 | ASCII `DGI1` |
| 4 | 1 | Version, exactly 1 |
| 5 | 1 | Alpha: 0 none, 1 A1, 2 A8 |
| 6 | 1 | Row codec: 1 RLE565, 2 Q565 (custom QOI-style codec, not QOI) |
| 7 | 1 | Flags, exactly zero |
| 8 | 2 | Width, 1–412 |
| 10 | 2 | Height, 1–412 |
| 12 | 4 | Exact total file bytes, at most 524,288 |
| 16 | 4 | Row-table offset, exactly 32 |
| 20 | 4 | Payload offset, exactly `32 + 4*(height+1)` |
| 24 | 8 | Reserved, all zero |

The table contains **height+1** unsigned 32-bit offsets relative to the payload. First offset is zero; offsets strictly increase; the final offset equals payload length. Each row owns exactly `[offset[y], offset[y+1])`. Checked arithmetic, exact length, complete table, mode values, non-overlap and every row are validated before an image becomes usable. Extra/truncated bytes, cross-row runs, too few/many pixels and unused row bytes are errors. No decoder reads beyond a row slice.

## Row representation

Every row starts with a marker byte: 0 raw escape, 1 compressed with the header codec. Other markers are invalid. A raw row contains `width` big-endian RGB565 colors followed by its alpha plane: none for alpha 0, `ceil(width/8)` A1 bytes or `width` A8 bytes. A1 uses MSB first, coverage 0 or 255; trailing unused bits are zero. Pixels with alpha zero have color zero, including compressed representations. Alpha is straight, not premultiplied.

The canonical encoder selects the compressed representation only when shorter than raw (raw wins ties). It canonicalizes transparent colors, A1 padding and reserved bytes. Both codecs are supported for measurement and decoding; the default encoder codec is selected from recorded cat/synthetic size and decode measurements.

### RLE565 (codec 1)

A compressed row is a sequence of runs: an unsigned count byte 1–255, then color (2 bytes, big endian), then alpha (one byte) for A1/A8. None implies alpha 255. A1 alpha bytes must be 0 or 255. Counts sum exactly to width. Canonical runs are maximal, split at 255.

Worst compressed expansion is `1 + width*(3 + has_alpha)` bytes. Raw escape bounds encoded row size to `1 + 2*width + alpha_plane_bytes`.

### Q565 (codec 2)

Each row starts with previous pixel `(color=0, alpha=255)` and a 64-entry index of `(0,0)`. State never crosses rows. Hash uses quantized color channels: `(r5*3 + g6*5 + b5*7 + alpha*11) & 63`.

- `00iiiiii`: emit index entry i.
- `10rrggbb`: emit previous color with signed channel differences, each 2-bit field minus 2, alpha unchanged. Reject channels outside r/b 0–31 or g 0–63.
- `11llllll`, values C0–FD: repeat previous pixel `l+1` times (1–62).
- FE: literal 2-byte color, retaining alpha.
- FF: literal 2-byte color plus alpha byte; forbidden for alpha none; A1 permits only 0/255.
- 40–7F: reserved/invalid.

After each emitted pixel, update previous pixel and its hash slot. Every run is checked against remaining row pixels. Transparent color canonicalization applies to all emitted pixels. The encoder prefers runs, then an exact index match, then a legal small delta, then FE/FF literals. An initial `(0,255)` run is legal; index entries for alpha-none/A1 may not emit unsupported coverage.

Worst literal expansion is `1 + width*4` bytes; raw escape applies as above. A horizontal crop decodes from column zero to its right edge; it is not random access within a row. Validation still checks the entire row. Narrow dirty rectangles over a wide background must include that prefix cost in benchmarks.

## Composition arithmetic

Expand 5-bit channels with `(v<<3)|(v>>2)` and 6-bit channels with `(v<<2)|(v>>4)`. For each 8-bit channel, straight-alpha blend is `(src*a + dst*(255-a) + 127)/255`, integer division. Requantize with `(channel*31+127)/255` for red/blue and `(channel*63+127)/255` for green. Alpha 0 leaves destination unchanged; alpha 255 copies the exact source RGB565 value. RGB565 output sent to the panel is big endian.

No-alpha images are opaque. A1 images have only binary coverage. A8 tests include fractional coverage, odd widths, mid-image stripes, clipped edges and output canaries. The production decoder is compiled into host tests; independent host expectations check decoding and blend math, not only encoder round trips.

## Host PNG input

`tools/dgi.py` uses the Python standard library (`zlib`, `struct`, CRC32) only. It accepts non-interlaced 8-bit RGB/RGBA PNG, validates chunk CRCs and PNG filters, and rejects unsupported formats. Pillow is not required. Runtime image decoding uses DGI1 only.

## Default codec selection (2026-10-13)

The 16 manifest-referenced cat images total 185,208 B with RLE565 versus 106,895 B with Q565. On the production host decoder (100 repetitions), the faceplate full decode is 955/1,116 µs and the `[92,176,224,56]` row-prefix patch is 93/102 µs (RLE/Q565). Q565 saves 42.3% of cat bytes for a 9 µs host patch penalty, so it is the default encoder. Both remain accepted and are measured again on target. Synthetic noise uses raw escape in both; the A8 worst-case fixture is 511,328 B, below the file cap. Exact files, hashes and timings are in `hil/fixtures/step9a/codec-benchmark.json` and the resume evidence.
