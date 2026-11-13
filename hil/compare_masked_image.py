#!/usr/bin/env python3
"""Compare ESP-IDF app/bootloader images or flash sets with explicit metadata masks.

ESP32-family 24-byte header, esp_app_desc_t in the first segment, optional
simple SHA-256. Both checksums/digests are verified BEFORE masking. Signed,
padded or unknown trailers fail closed. No byte values from code/data are
printed (firmware may contain credentials). Optional ELF annotation requires
pyelftools and a matching embedded ELF SHA-256, not merely matching filenames.

Exit codes: 0 = equal outside mask, 1 = unmasked differences, 2 = invalid input.
Format references: ESP-IDF components/bootloader_support/include/esp_app_format.h,
components/esp_app_format/include/esp_app_desc.h,
components/esp_bootloader_format/include/esp_bootloader_desc.h; esptool/bin_image.py.
"""
import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct
import sys


# Offsets relative to the 256-byte app descriptor, NOT a magic-word search.
FIELDS = {
    'version': (16, 32), 'project_name': (48, 32), 'time': (80, 16),
    'date': (96, 16), 'idf_version': (112, 32), 'elf_sha256': (144, 32),
}
DEFAULT_FIELDS = ('version', 'time', 'date', 'elf_sha256')
BOOT_FIELDS = {'idf_version': (8, 32), 'date_time': (40, 24)}


def sha(data):
    return hashlib.sha256(data).hexdigest()


@dataclass
class Image:
    data: bytes
    segments: list
    checksum_offset: int
    digest_offset: int | None
    descriptor_offset: int = 32
    kind: str = 'app'

    @classmethod
    def parse(cls, data, kind='app'):
        if kind not in ('app', 'bootloader'):
            raise ValueError('Unknown image kind')
        if len(data) < 24 or data[0] != 0xE9:
            raise ValueError('Expected ESP32-family image header (24 bytes, magic 0xe9)')
        if not 1 <= data[1] <= 16 or data[23] not in (0, 1):
            raise ValueError('Invalid segment count or hash_appended flag')
        cursor, checksum, segments = 24, 0xEF, []
        for index in range(data[1]):
            if cursor+8 > len(data):
                raise ValueError('Truncated segment header')
            address, size = struct.unpack_from('<II', data, cursor)
            start = cursor+8
            if size % 4 or start+size > len(data) or address+size > 2**32:
                raise ValueError('Unaligned, truncated or overflowing segment')
            segments.append(dict(index=index, header_offset=cursor, offset=start,
                                 size=size, load_address=address))
            for value in data[start:start+size]:
                checksum ^= value
            cursor = start+size
        # Checksum is the last byte of the NEXT 16-byte-aligned image extent.
        checksum_offset = (cursor//16+1)*16-1
        digest_offset = checksum_offset+1 if data[23] else None
        expected_size = checksum_offset+1+(32 if digest_offset is not None else 0)
        if len(data) != expected_size:
            raise ValueError('Truncated image or unsupported trailer (signed/padded images are not masked)')
        if data[checksum_offset] != checksum:
            raise ValueError('Invalid image XOR checksum')
        if digest_offset is not None and hashlib.sha256(data[:digest_offset]).digest() != data[digest_offset:]:
            raise ValueError('Invalid appended image SHA-256')
        if kind == 'app':
            if segments[0]['size'] < 256 or struct.unpack_from('<I', data, 32)[0] != 0xABCD5432:
                raise ValueError('Missing app descriptor at the start of the first segment')
        elif segments[0]['size'] < 80 or data[32] != 80:
            raise ValueError('Missing bootloader descriptor at the start of the first segment')
        return cls(data, segments, checksum_offset, digest_offset, kind=kind)

    def fields(self):
        values = {}
        for name, (offset, size) in (FIELDS if self.kind == 'app' else BOOT_FIELDS).items():
            value = self.data[self.descriptor_offset+offset:self.descriptor_offset+offset+size]
            values[name] = value.hex() if name == 'elf_sha256' else value.split(b'\0',1)[0].decode('utf-8',errors='replace')
        if self.kind == 'bootloader':
            values.update(secure_version=self.data[35], version=struct.unpack_from('<I', self.data, 36)[0])
        return values

    def masks(self, fields):
        layout = FIELDS if self.kind == 'app' else BOOT_FIELDS
        result = {name: (self.descriptor_offset+layout[name][0], layout[name][1]) for name in fields}
        result['image_checksum'] = (self.checksum_offset, 1)
        if self.digest_offset is not None:
            result['image_sha256'] = (self.digest_offset, 32)
        return result


class ElfIndex:
    def __init__(self, path, image):
        from elftools.elf.elffile import ELFFile
        self.path = str(path)
        raw = Path(path).read_bytes()
        self.sha256 = sha(raw)
        if self.sha256 != image.fields()['elf_sha256']:
            raise ValueError('ELF SHA-256 does not match the image app descriptor: '+str(path))
        import io
        elf = ELFFile(io.BytesIO(raw))
        self.sections, self.symbols = [], []
        for index, section in enumerate(elf.iter_sections()):
            if section['sh_flags'] & 2 and section['sh_type'] != 'SHT_NOBITS' and section['sh_size']:
                self.sections.append(dict(index=index, name=section.name, start=section['sh_addr'],
                                          end=section['sh_addr']+section['sh_size']))
        for section in elf.iter_sections():
            if section['sh_type'] not in ('SHT_SYMTAB','SHT_DYNSYM'):
                continue
            for symbol in section.iter_symbols():
                if (not isinstance(symbol['st_shndx'], int) or not symbol.name
                        or symbol['st_info']['type'] in ('STT_FILE','STT_SECTION')):
                    continue
                self.symbols.append(dict(name=symbol.name, section_index=symbol['st_shndx'],
                                         start=symbol['st_value'], size=symbol['st_size']))

    def locate(self, start, end):
        result=[]
        for section in self.sections:
            lo, hi = max(start,section['start']), min(end,section['end'])
            if lo >= hi:
                continue
            symbols=[]
            for symbol in self.symbols:
                if symbol['section_index'] != section['index']:
                    continue
                a, size = symbol['start'], symbol['size']
                if (size and a < hi and a+size > lo) or (not size and lo <= a < hi):
                    symbols.append(dict(name=symbol['name'], address=a, size=size,
                                        offset_in_symbol=max(lo,a)-a,
                                        coverage='sized-symbol' if size else 'zero-sized-label-only'))
            result.append(dict(section=section['name'], address_start=lo, address_end=hi, symbols=symbols))
        return result


def locations(image, start, end, elf=None):
    """Map file intervals through segment load addresses, retaining unmapped bytes."""
    regions=[(0,24,dict(kind='image-header'))]
    for segment in image.segments:
        off, size = segment['offset'], segment['size']
        regions.append((off-8,off,dict(kind='segment-header',segment=segment['index'])))
        regions.append((off,off+size,dict(kind='segment-data',segment=segment['index'],
                                        load_address=segment['load_address'])))
    last=image.segments[-1]['offset']+image.segments[-1]['size']
    regions += [(last,image.checksum_offset,dict(kind='checksum-alignment-padding')),
                (image.checksum_offset,image.checksum_offset+1,dict(kind='image-checksum'))]
    if image.digest_offset is not None:
        regions.append((image.digest_offset,image.digest_offset+32,dict(kind='image-sha256')))
    if end>len(image.data):
        regions.append((len(image.data),end,dict(kind='absent')))
    result=[]
    for a,b,detail in regions:
        lo,hi=max(a,start),min(b,end)
        if lo>=hi:
            continue
        item=dict(detail,offset=lo,end=hi)
        if detail['kind']=='segment-data':
            address=detail['load_address']+lo-a
            item.update(address_start=address,address_end=address+hi-lo,
                        elf=elf.locate(address,address+hi-lo) if elf else [])
        result.append(item)
    return result


def compare(left, right, *, left_elf=None, right_elf=None,
            mask_project_name=None, mask_idf_version=None, kind='app'):
    """Compare bytes at identical file offsets; never relocate or normalize code."""
    a,b=Image.parse(left,kind),Image.parse(right,kind)
    if kind == 'bootloader' and any(x is not None for x in (left_elf,right_elf,mask_project_name,mask_idf_version)):
        raise ValueError('Bootloader supports only date_time/trailer masks and no ELF annotation')
    fields=list(DEFAULT_FIELDS) if kind == 'app' else ['date_time']
    declarations={}
    for name,reason in (('project_name',mask_project_name),('idf_version',mask_idf_version)):
        if reason is not None:
            if not isinstance(reason,str) or not reason.strip():
                raise ValueError('Optional mask requires a nonempty declaration reason: '+name)
            fields.append(name);declarations[name]=reason
    ai=ElfIndex(left_elf,a) if left_elf else None
    bi=ElfIndex(right_elf,b) if right_elf else None
    am,bm=a.masks(fields),b.masks(fields)
    # Only the SAME semantic field at the SAME offsets may be ignored. A shifted
    # checksum must not mask payload bytes belonging to the other image.
    common={name:span for name,span in am.items() if bm.get(name)==span}
    mask=bytearray(max(len(left),len(right)))
    names=['unmasked']+list(common)
    normalized_a,normalized_b=bytearray(left),bytearray(right)
    for number,(name,(offset,size)) in enumerate(common.items(),1):
        mask[offset:offset+size]=bytes([number])*size
        normalized_a[offset:offset+size]=b'\0'*size
        normalized_b[offset:offset+size]=b'\0'*size
    ranges=[];current=None
    for i in range(len(mask)):
        different=i>=len(left) or i>=len(right) or left[i]!=right[i]
        if different:
            label=mask[i]
            if current is None or current[2]!=label:
                if current is not None:ranges.append(current)
                current=[i,i+1,label]
            else:current[1]=i+1
        elif current is not None:
            ranges.append(current);current=None
    if current is not None:ranges.append(current)
    regions=[dict(offset=lo,end=hi,bytes=hi-lo,masked=bool(label),field=names[label],
                  left=locations(a,lo,hi,ai),right=locations(b,lo,hi,bi)) for lo,hi,label in ranges]
    def identity(image,index,normalized):
        return dict(bytes=len(image.data),sha256=sha(image.data),masked_sha256=sha(normalized),
                    descriptor=image.fields(),segments=image.segments,
                    checksum_verified=True,sha256_verified=image.digest_offset is not None,
                    elf=dict(path=index.path,sha256=index.sha256,binding_verified=True) if index else None)
    return dict(format='esp-idf-'+kind+'-masked-comparison-v1',image_kind=kind,raw_equal=left==right,
                masked_equal=normalized_a==normalized_b,
                masked_differing_bytes=sum(r['bytes'] for r in regions if r['masked']),
                unmasked_differing_bytes=sum(r['bytes'] for r in regions if not r['masked']),
                policy=dict(descriptor_fields=fields,optional_declarations=declarations,
                            integrity='Both image checksums and appended hashes verified before masking',
                            trailers='Only parsed checksum and optional simple SHA-256; unknown trailers rejected'),
                masks=[dict(field=name,offset=span[0],bytes=span[1]) for name,span in common.items()],
                left=identity(a,ai,normalized_a),right=identity(b,bi,normalized_b),regions=regions,
                scope=kind+' image only. No qualification, signature validation, other flash-file comparison, or ELF/debug-byte equivalence is implied.')


FLASH_ROLES = ('bootloader', 'partition-table', 'app')
FLASH_OPTIONS = ('write_flash_args', 'flash_settings', 'extra_esptool_args')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key: '+key)
        result[key] = value
    return result


def flash_offset(value):
    if not isinstance(value, str):
        raise ValueError('Flash offset must be a string')
    offset = int(value, 0)
    if not 0 <= offset < 2**32:
        raise ValueError('Flash offset outside 32-bit range')
    return offset


def read_flash_set(path):
    """Fail closed on unsupported/encrypted sets; never infer missing files."""
    path = Path(path)
    raw = path.read_bytes()
    data = json.loads(raw, object_pairs_hook=unique_object)
    expected = set(FLASH_ROLES + FLASH_OPTIONS + ('flash_files',))
    if not isinstance(data, dict) or set(data) != expected:
        raise ValueError('Expected app, bootloader, partition table and standard flasher arguments only')
    if (not isinstance(data['write_flash_args'], list)
            or not all(isinstance(x, str) for x in data['write_flash_args'])
            or not isinstance(data['flash_settings'], dict)
            or not isinstance(data['extra_esptool_args'], dict)
            or not isinstance(data['flash_files'], dict)):
        raise ValueError('Malformed flasher arguments')
    mapping = {}
    for offset, name in data['flash_files'].items():
        offset = flash_offset(offset)
        if offset in mapping or not isinstance(name, str):
            raise ValueError('Duplicate flash offset or invalid file path')
        mapping[offset] = name
    if len(mapping) != 3:
        raise ValueError('Flash set must contain exactly three declared files')
    images = {}
    for role in FLASH_ROLES:
        entry = data[role]
        if not isinstance(entry, dict) or set(entry) != {'file','offset','encrypted'}:
            raise ValueError('Malformed flash role: '+role)
        if entry['encrypted'] is not False and entry['encrypted'] != 'false':
            raise ValueError('Encrypted flash sets are unsupported')
        offset = flash_offset(entry['offset'])
        name = entry['file']
        if not isinstance(name, str) or mapping.get(offset) != name:
            raise ValueError('Role disagrees with flash_files: '+role)
        file = path.parent / name
        if Path(name).is_absolute() or not file.resolve().is_relative_to(path.parent.resolve()):
            raise ValueError('Flash input must be within the manifest directory')
        blob = file.read_bytes()
        if not blob or offset+len(blob) > 2**32:
            raise ValueError('Empty or overflowing flash file: '+role)
        images[role] = dict(path=file, offset=offset, data=blob)
    spans = sorted((v['offset'], v['offset']+len(v['data'])) for v in images.values())
    if any(a[1] > b[0] for a,b in zip(spans,spans[1:])):
        raise ValueError('Flash file ranges overlap')
    return dict(path=path, sha256=sha(raw), options={k:data[k] for k in FLASH_OPTIONS}, images=images)


def compare_partition(left, right):
    # Partition bytes, including their own MD5 record and padding, are all strict.
    regions = []
    for i in range(max(len(left),len(right))):
        if i >= len(left) or i >= len(right) or left[i] != right[i]:
            if regions and regions[-1]['end'] == i:
                regions[-1]['end'] += 1
                regions[-1]['bytes'] += 1
            else:
                regions.append(dict(offset=i,end=i+1,bytes=1,masked=False,field='unmasked'))
    return dict(raw_equal=left==right,masked_equal=left==right,
                masked_differing_bytes=0,unmasked_differing_bytes=sum(r['bytes'] for r in regions),
                left=dict(bytes=len(left),sha256=sha(left)),right=dict(bytes=len(right),sha256=sha(right)),
                masks=[],regions=regions,scope='Partition table: exact byte comparison, no masks or semantic validation.')


def compare_flash_sets(left, right, *, left_elf=None, right_elf=None,
                       mask_project_name=None, mask_idf_version=None):
    """Compare parsed snapshots; file names may differ, roles/offsets/options may not."""
    results = {}
    for role in FLASH_ROLES:
        a,b = left['images'][role],right['images'][role]
        if role == 'partition-table':
            result = compare_partition(a['data'],b['data'])
        elif role == 'bootloader':
            result = compare(a['data'],b['data'],kind='bootloader')
        else:
            result = compare(a['data'],b['data'],left_elf=left_elf,right_elf=right_elf,
                             mask_project_name=mask_project_name,mask_idf_version=mask_idf_version)
        result['offset_equal'] = a['offset'] == b['offset']
        for side,entry in (('left',a),('right',b)):
            result[side].update(path=str(entry['path']),flash_offset=entry['offset'])
        results[role] = result
    # JSON types matter (e.g. true must not compare equal to numeric 1).
    option_equal = {k:json.dumps(left['options'][k],sort_keys=True,allow_nan=False) ==
                      json.dumps(right['options'][k],sort_keys=True,allow_nan=False)
                    for k in FLASH_OPTIONS}
    options_equal = all(option_equal.values())
    return dict(format='esp-idf-flash-set-masked-comparison-v1',
                masked_equal=options_equal and all(r['masked_equal'] and r['offset_equal'] for r in results.values()),
                raw_equal=options_equal and all(r['raw_equal'] and r['offset_equal'] for r in results.values()),
                flash_options_equal=options_equal,
                differing_option_groups=[k for k in FLASH_OPTIONS if not option_equal[k]],
                left=dict(path=str(left['path']),sha256=left['sha256'],options=left['options']),
                right=dict(path=str(right['path']),sha256=right['sha256'],options=right['options']),
                images=results,
                masked_differing_bytes=sum(r['masked_differing_bytes'] for r in results.values()),
                unmasked_differing_bytes=sum(r['unmasked_differing_bytes'] for r in results.values()),
                scope='Three-file flash set and flasher options only; no qualification, signature, partition semantic or ELF/debug-byte equivalence claim.')


def protect_inputs(output, inputs):
    if output is None:
        return
    for path in inputs:
        if path is not None and (output.resolve() == path.resolve()
                                or (output.exists() and output.samefile(path))):
            raise ValueError('Output must not overwrite an image, ELF or flash manifest input')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('left',type=Path);parser.add_argument('right',type=Path)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--image-kind',choices=('app','bootloader'),default='app')
    mode.add_argument('--flash-set',action='store_true',help='Inputs are flasher_args.json files')
    parser.add_argument('--left-elf',type=Path);parser.add_argument('--right-elf',type=Path)
    parser.add_argument('--mask-project-name',metavar='REASON')
    parser.add_argument('--mask-idf-version',metavar='REASON')
    parser.add_argument('--output',type=Path,help='Write JSON here instead of stdout; may not overwrite an input')
    args=parser.parse_args(argv)
    try:
        inputs=[args.left,args.right,args.left_elf,args.right_elf]
        options=dict(left_elf=args.left_elf,right_elf=args.right_elf,
                     mask_project_name=args.mask_project_name,mask_idf_version=args.mask_idf_version)
        if args.flash_set:
            a,b=read_flash_set(args.left),read_flash_set(args.right)
            inputs += [v['path'] for side in (a,b) for v in side['images'].values()]
            protect_inputs(args.output,inputs)
            result=compare_flash_sets(a,b,**options)
        else:
            protect_inputs(args.output,inputs)
            result=compare(args.left.read_bytes(),args.right.read_bytes(),kind=args.image_kind,**options)
            result['left']['path']=str(args.left);result['right']['path']=str(args.right)
        text=json.dumps(result,indent=2)+'\n'
        if args.output:args.output.write_text(text)
        else:print(text,end='')
        return 0 if result['masked_equal'] else 1
    except (ValueError,OSError,ImportError) as exc:
        print(json.dumps(dict(error=str(exc),masked_equal=False)),file=sys.stderr)
        return 2


if __name__=='__main__':
    sys.exit(main())
