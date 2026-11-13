# Comparing ESP-IDF images and flash sets

`hil/compare_masked_image.py` compares app or bootloader binaries at identical
file offsets, or the complete three-file flash set in two `flasher_args.json`
manifests. It establishes equality outside a declared metadata mask, including
flash offsets and programmer options in flash-set mode. It does not establish
qualification, signature validity, partition semantics or ELF/debug equality.

```sh
python hil/compare_masked_image.py retained/dg-esp32.bin scratch/dg-esp32.bin \
  --left-elf retained/dg-esp32.elf --right-elf scratch/dg-esp32.elf \
  --output comparison.json
```

The binary comparison uses the Python standard library. Supplying ELF files
requires `pyelftools` (available in the ESP-IDF Python environment). Exit status
is 0 for equality outside the mask, 1 for unmasked differences, and 2 for invalid
or unsupported input. Inputs are read only; `--output` cannot overwrite an input, including flash-set
manifests and any of their three referenced files (symlink/hardlink aliases too).

Both images must have a valid ESP32-family 24-byte image header, complete
segments, and the selected descriptor at the beginning of the first segment.
The default app mode requires the 256-byte `esp_app_desc_t` with magic
`0xABCD5432`; bootloader mode requires the 80-byte `esp_bootloader_desc_t`
with magic byte 80. There is no magic scanning or inferred image kind. The XOR
checksum and, when present, the appended simple SHA-256 are checked **before**
masking. Truncation, unsupported signed/padded trailers and invalid integrity
fields fail closed, even when comparing an invalid file with itself.

The default mask covers only these app descriptor fields: version (32 bytes),
time (16), date (16), embedded ELF SHA-256 (32), followed by the parsed one-byte
image checksum and optional 32-byte image digest. Project name stays strict unless
`--mask-project-name REASON` is supplied. IDF version stays strict unless
`--mask-idf-version REASON` is supplied. Both declarations appear in JSON. Neither optional mask is expected for the
9(d) proof; any use is a visible deviation requiring its own recorded rationale. No
security version, chip revision, flash mode, MMU setting, reserved field, segment
header, alignment padding or arbitrary trailing data is masked.

Each contiguous differing region has an exclusive end offset, byte count,
mask field (or `unmasked`) and locations in both images. Segment load addresses
map changed data to ELF sections and overlapping symbols, when available. A
zero-sized ELF symbol is reported only as a label at its address, not as a guessed
function extent. ELF annotation requires the whole ELF's SHA-256 to equal the
image descriptor's embedded ELF SHA. No raw payload bytes or decoded code/data
strings are emitted; firmware can contain private configuration.

Different image sizes, segment sizes or addresses remain differences. A trailer
is masked only when the same semantic field occupies the same offsets in both
images; a shifted checksum cannot hide payload bytes in the other image.

Build directory layout can itself affect firmware. In particular this project's
PSRAM HTTP adapter compiles a generated `display-http-psram/esp_http_client.c`;
its assertion `__FILE__` path may enter rodata. Use a fresh isolated worktree with
the same relative build directory as the retained candidate when testing
reproducibility. A path difference outside the app descriptor must be reported,
not added to the metadata mask. Always compare the actual files before claiming
that a rebuild is metadata-only.

Synthetic checks (no board or build required):

```sh
uv run --no-project --offline --with pytest --with pyelftools \
  python -m pytest hil/test_compare_masked_image.py -q
```

## Bootloader and full flash-set modes

```sh
python hil/compare_masked_image.py retained/bootloader/bootloader.bin \
  scratch/bootloader/bootloader.bin --image-kind bootloader --output bootloader.json

python hil/compare_masked_image.py retained/flasher_args.json scratch/flasher_args.json \
  --flash-set --left-elf retained/dg-esp32.elf --right-elf scratch/dg-esp32.elf \
  --output flash-set.json
```

Bootloader mode masks only `date_time[24]` at descriptor offset 40 (file offset
72), plus the same parsed/verified checksum and optional digest. IDF version,
bootloader version, secure version, all reserved bytes and payload remain strict.
Optional app masks and ELF annotation are rejected in standalone bootloader mode:
its descriptor contains no ELF hash with which to bind that annotation. In
flash-set mode the ELF arguments and optional app masks apply only to the app.

Flash-set mode supports the standard unencrypted three-file IDF manifest with
`bootloader`, `partition-table`, `app`, `flash_files`, `write_flash_args`,
`flash_settings`, and `extra_esptool_args`. It rejects missing/extra files or keys,
duplicate JSON keys/offsets, role/map disagreements, overlapping ranges, missing
or empty files, encryption and paths outside the manifest directory. The app and
bootloader must each pass format and integrity validation before comparison.
Partition-table bytes (including their MD5 record and padding) must match exactly;
no partition metadata is masked, and partition semantic validity is not claimed.

Relative filenames may differ between builds: roles and numeric flash offsets
bind the comparison. Both manifests' write-flash arguments, flash settings and
extra esptool arguments must be equal; option or offset differences fail even
when image bytes match. JSON formatting/key order are not compared. The result
reports per-file hashes, image differences, offsets and option-group differences;
only a passing overall `masked_equal` covers the full set. A zero unmasked byte
count alone does not establish equality when offsets or arguments differ.

For 9(d), retain the per-file hashes and manifest, the same candidate-relative
`build/accept` layout, private-config hash, dependency lock hash, IDF/toolchain
identity and compared flasher arguments. The tested bundle can contain an earlier
bootloader build than its app; record both descriptor dates and hashes explicitly.
This tool authorizes no replacement of tested artifacts, flash or campaign run.
