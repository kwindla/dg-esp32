"""Synthetic ESP-IDF images and ELF fixtures; no device/build required."""
import hashlib
import json
from pathlib import Path
import struct

import pytest

from compare_masked_image import FIELDS, Image, compare, main


DROM=0x3C000020
TEXT=0x42000020


def descriptor(**values):
    data=bytearray(256)
    struct.pack_into('<I',data,0,0xABCD5432)
    defaults=dict(version='version-a',project_name='demo',time='00:00:00',date='Oct 18 2026',
                  idf_version='v6.1',elf_sha256=bytes(range(32)))
    defaults.update(values)
    for name,value in defaults.items():
        offset,size=FIELDS[name]
        if isinstance(value,str):value=value.encode()
        assert len(value)<=size
        data[offset:offset+len(value)]=value
    return data


def app(desc=None,code=b'abcdefghijklmnop',extra=b'',digest=True,header_changes=None):
    desc=descriptor() if desc is None else desc
    header=bytearray(24);header[0]=0xE9;header[1]=2;header[23]=int(digest)
    struct.pack_into('<I',header,4,TEXT)
    struct.pack_into('<H',header,12,9) # ESP32-S3
    for offset,value in (header_changes or {}).items():header[offset]=value
    result=header;checksum=0xEF
    for address,payload in [(DROM,bytes(desc)+b'unmasked-rodata!'+extra),(TEXT,code)]:
        assert len(payload)%4==0
        result+=struct.pack('<II',address,len(payload))+payload
        for value in payload:checksum^=value
    result+=b'\0'*(15-len(result)%16)+bytes([checksum])
    if digest:result+=hashlib.sha256(result).digest()
    return bytes(result)


def rehash(image):
    # For intentional header/padding/checksum corruption fixtures, only rehash.
    return image[:-32]+hashlib.sha256(image[:-32]).digest()


@pytest.mark.parametrize('digest',[False,True])
def test_self_comparison_and_optional_digest(digest):
    x=app(digest=digest);r=compare(x,x)
    assert r['raw_equal'] and r['masked_equal'] and r['regions']==[]
    assert r['left']['sha256_verified']==digest
    assert r['left']['checksum_verified']


@pytest.mark.parametrize('field',['version','time','date','elf_sha256'])
def test_only_declared_default_descriptor_fields_are_masked(field):
    a=app();b=app(descriptor(**{field:b'changed'}));r=compare(a,b)
    assert not r['raw_equal'] and r['masked_equal']
    assert r['unmasked_differing_bytes']==0
    assert all(x['field'] in (field,'image_checksum','image_sha256') for x in r['regions'])


@pytest.mark.parametrize('field,option',[('project_name','mask_project_name'),('idf_version','mask_idf_version')])
def test_optional_fields_require_a_declaration(field,option):
    a,b=app(),app(descriptor(**{field:'different'}))
    assert not compare(a,b)['masked_equal']
    r=compare(a,b,**{option:'Explicit fixture declaration'})
    assert r['masked_equal'] and r['policy']['optional_declarations'][field]=='Explicit fixture declaration'
    with pytest.raises(ValueError,match='nonempty declaration'):compare(a,b,**{option:' '})


@pytest.mark.parametrize('offset',[4,8,176,180,181,184,255])
def test_security_revision_mmu_reserved_fields_are_not_masked(offset):
    d=descriptor();d[offset]^=1
    r=compare(app(),app(d))
    assert not r['masked_equal'] and r['unmasked_differing_bytes']==1


@pytest.mark.parametrize('offset',[2,3,4,8,12,14,15,17,19])
def test_image_header_changes_are_not_metadata(offset):
    r=compare(app(),app(header_changes={offset:1}))
    assert not r['masked_equal']
    assert any(x['left'][0]['kind']=='image-header' for x in r['regions'] if not x['masked'])


def test_payload_and_unrelated_version_string_are_never_masked():
    a=app(extra=b'version-a___');b=app(descriptor(version='version-b'),extra=b'version-b___')
    r=compare(a,b)
    assert not r['masked_equal'] and r['unmasked_differing_bytes']==1
    assert all('value' not in x and 'hex' not in x for x in r['regions'])


def test_shifted_trailer_does_not_mask_payload_at_other_images_trailer():
    a,b=app(),app(extra=b'x'*48)
    r=compare(a,b)
    assert not r['masked_equal']
    assert not any(x['field'].startswith('image_') for x in r['masks'])
    old_checksum=Image.parse(a).checksum_offset
    assert any(loc['kind']=='segment-data' and loc['offset']<=old_checksum<loc['end']
               for region in r['regions'] for loc in region['right'])
    assert r['unmasked_differing_bytes']>48


def test_alignment_padding_remains_strict():
    a=app();parsed=Image.parse(a);last=parsed.segments[-1]['offset']+parsed.segments[-1]['size']
    assert last<parsed.checksum_offset
    b=bytearray(a);b[last]=17;b=rehash(bytes(b))
    r=compare(a,b)
    assert not r['masked_equal']
    assert [x for x in r['regions'] if not x['masked']][0]['left'][0]['kind']=='checksum-alignment-padding'


@pytest.mark.parametrize('cut',[0,23,28,100,-1,-16,-32,-33])
def test_truncations_fail_closed(cut):
    with pytest.raises(ValueError):compare(app()[:cut],app())


@pytest.mark.parametrize('fault',['magic','count','count-large','hash-flag','length','alignment','descriptor','checksum','digest','trailer'])
def test_corruption_and_unsupported_format_fail_even_when_both_inputs_same(fault):
    x=bytearray(app())
    if fault=='magic':x[0]=0
    if fault=='count':x[1]=0
    if fault=='count-large':x[1]=17
    if fault=='hash-flag':x[23]=2
    if fault=='length':struct.pack_into('<I',x,28,0xFFFFFFFC)
    if fault=='alignment':struct.pack_into('<I',x,28,271)
    if fault=='descriptor':
        d=descriptor();d[:4]=b'nope';x=bytearray(app(d))
    if fault=='checksum':x[Image.parse(x).checksum_offset]^=1;x=bytearray(rehash(x))
    if fault=='digest':x[-1]^=1
    if fault=='trailer':x+=b'\0'*4096
    with pytest.raises(ValueError):compare(bytes(x),bytes(x))


def test_descriptor_is_not_found_by_scanning_payload_magic():
    d=descriptor();d[:4]=b'nope'
    with pytest.raises(ValueError,match='descriptor'):compare(app(d,extra=descriptor()),app())


def tiny_elf(code=b'abcdefghijklmnop'):
    """Minimal little-endian ELF32 with allocated sections, sized and zero labels."""
    ident=b'\x7fELF'+bytes([1,1,1])+b'\0'*9
    result=bytearray(52)
    names=b'\0.flash.rodata\0.text\0.symtab\0.strtab\0.shstrtab\0'
    strings=b'\0esp_app_desc\0calc_func\0zero_label\0'
    sym=bytes(16)
    sym+=struct.pack('<IIIBBH',strings.index(b'esp_app_desc'),DROM,256,0x11,0,1)
    sym+=struct.pack('<IIIBBH',strings.index(b'calc_func'),TEXT+4,8,0x12,0,2)
    sym+=struct.pack('<IIIBBH',strings.index(b'zero_label'),TEXT+12,0,0x10,0,2)
    sections=[(0,0,0,0,0,0,0,0,0,0)]
    entries=[('.flash.rodata',1,2,DROM,bytes(descriptor())+b'unmasked-rodata!',0,0,4,0),
             ('.text',1,6,TEXT,code,0,0,4,0),('.symtab',2,0,0,sym,4,1,4,16),
             ('.strtab',3,0,0,strings,0,0,1,0),('.shstrtab',3,0,0,names,0,0,1,0)]
    for name,kind,flags,addr,data,link,info,align,entsize in entries:
        result+=b'\0'*((-len(result))%align);offset=len(result);result+=data
        sections.append((names.index(name.encode()),kind,flags,addr,offset,len(data),link,info,align,entsize))
    result+=b'\0'*((-len(result))%4);shoff=len(result)
    for section in sections:result+=struct.pack('<IIIIIIIIII',*section)
    result[:52]=struct.pack('<16sHHIIIIIHHHHHH',ident,2,94,1,TEXT,0,shoff,0,52,0,0,40,len(sections),5)
    return bytes(result)


def test_section_symbol_annotation_and_elf_binding(tmp_path):
    acode=b'abcdefghijklmnop';bcode=b'abcdeZghijklmnop'
    ea,eb=tiny_elf(acode),tiny_elf(bcode)
    pa,pb=tmp_path/'a.elf',tmp_path/'b.elf';pa.write_bytes(ea);pb.write_bytes(eb)
    a=app(descriptor(elf_sha256=hashlib.sha256(ea).digest()),code=acode)
    b=app(descriptor(elf_sha256=hashlib.sha256(eb).digest()),code=bcode)
    r=compare(a,b,left_elf=pa,right_elf=pb)
    changed=[x for x in r['regions'] if not x['masked']]
    assert len(changed)==1 and changed[0]['bytes']==1
    for side in ('left','right'):
        loc=changed[0][side][0]
        assert loc['address_start']==TEXT+5 and loc['elf'][0]['section']=='.text'
        assert loc['elf'][0]['symbols'][0]['name']=='calc_func'
        assert loc['elf'][0]['symbols'][0]['offset_in_symbol']==1
    with pytest.raises(ValueError,match='ELF SHA-256'):compare(a,b,left_elf=pb)


def test_zero_size_symbol_is_a_label_not_inferred_function_extent(tmp_path):
    ea=tiny_elf();path=tmp_path/'a.elf';path.write_bytes(ea)
    a=app(descriptor(elf_sha256=hashlib.sha256(ea).digest()))
    b=app(descriptor(elf_sha256=hashlib.sha256(ea).digest()),code=b'abcdefghijklZnOp')
    r=compare(a,b,left_elf=path)
    symbols=[s for r in r['regions'] if not r['masked'] for loc in r['left'] for section in loc.get('elf',[]) for s in section['symbols']]
    assert any(s['name']=='zero_label' and s['coverage']=='zero-sized-label-only' for s in symbols)
    assert not any(s['name']=='calc_func' for s in symbols)


def test_cli_exit_codes_and_read_only_inputs(tmp_path,capsys):
    a,b,out=tmp_path/'a.bin',tmp_path/'b.bin',tmp_path/'comparison.json'
    a.write_bytes(app());b.write_bytes(app(descriptor(version='different')))
    assert main([str(a),str(b),'--output',str(out)])==0
    assert json.loads(out.read_text())['masked_equal']
    b.write_bytes(app(code=b'1234567890123456'))
    assert main([str(a),str(b)])==1
    assert json.loads(capsys.readouterr().out)['unmasked_differing_bytes']>0
    before=a.read_bytes();assert main([str(a),str(b),'--output',str(a)])==2
    assert a.read_bytes()==before
    alias=tmp_path/'alias';alias.symlink_to(a)
    assert main([str(a),str(b),'--output',str(alias)])==2
    b.write_bytes(b'bad')
    assert main([str(a),str(b)])==2


def boot_descriptor():
    data=bytearray(80);data[0]=80
    struct.pack_into('<I',data,4,7)
    data[8:12]=b'v6.1'
    data[40:64]=b'Oct 16 2026 07:11:29'.ljust(24,b'\0')
    return data


def boot(desc=None,**kwargs):
    return app(boot_descriptor() if desc is None else desc,**kwargs)


@pytest.mark.parametrize('digest',[False,True])
def test_bootloader_datetime_only_mask(digest):
    d=boot_descriptor();d[40:64]=b'Oct 18 2026 00:18:00'.ljust(24,b'\0')
    r=compare(boot(digest=digest),boot(d,digest=digest),kind='bootloader')
    assert r['masked_equal'] and not r['raw_equal']
    assert r['policy']['descriptor_fields']==['date_time']
    assert r['left']['descriptor']['version']==7
    assert r['left']['descriptor']['idf_version']=='v6.1'
    assert {x['field'] for x in r['regions']} <= {'date_time','image_checksum','image_sha256'}


@pytest.mark.parametrize('offset',[1,2,3,4,7,8,39,64,79])
def test_bootloader_version_security_idf_reserved_are_strict(offset):
    d=boot_descriptor();d[offset]^=1
    r=compare(boot(),boot(d),kind='bootloader')
    assert not r['masked_equal'] and r['unmasked_differing_bytes']==1


@pytest.mark.parametrize('option',['left_elf','right_elf','mask_project_name','mask_idf_version'])
def test_bootloader_cannot_use_app_masks_or_unbound_elf(option):
    with pytest.raises(ValueError,match='Bootloader supports only'):
        compare(boot(),boot(),kind='bootloader',**{option:'anything'})


@pytest.mark.parametrize('fault',['short-desc','magic','checksum','digest','truncation','trailer','app'])
def test_bootloader_invalid_inputs_fail_closed(fault):
    x=boot()
    if fault=='short-desc':x=boot(boot_descriptor()[:60])
    if fault=='magic':
        d=boot_descriptor();d[0]=0;x=boot(d)
    if fault=='checksum':
        data=bytearray(x);data[Image.parse(x,'bootloader').checksum_offset]^=1;x=rehash(data)
    if fault=='digest':x=x[:-1]+bytes([x[-1]^1])
    if fault=='truncation':x=x[:-32]
    if fault=='trailer':x+=b'\0'*32
    if fault=='app':x=app()
    with pytest.raises(ValueError):compare(x,x,kind='bootloader')


def test_bootloader_descriptor_not_scanned_and_app_mode_not_inferred():
    d=boot_descriptor();d[0]=0
    with pytest.raises(ValueError,match='bootloader descriptor'):
        compare(boot(d,extra=boot_descriptor()),boot(),kind='bootloader')
    with pytest.raises(ValueError,match='app descriptor'):compare(boot(),boot())


def test_bootloader_payload_padding_header_shifted_trailer_strict():
    a=boot()
    for b in (boot(code=b'ABCDEFGHIJKLMNOP'),boot(extra=b'x'*48),boot(header_changes={2:1})):
        assert not compare(a,b,kind='bootloader')['masked_equal']
    parsed=Image.parse(a,'bootloader');end=parsed.segments[-1]['offset']+parsed.segments[-1]['size']
    b=bytearray(a);b[end]^=1
    assert not compare(a,rehash(b),kind='bootloader')['masked_equal']
    r=compare(a,boot(extra=b'x'*48),kind='bootloader')
    assert not any(x['field'].startswith('image_') for x in r['masks'])


def flash_set(root, changed=False):
    root.mkdir()
    d=boot_descriptor()
    if changed:d[45]=ord('8')
    files={'bootloader':('boot.bin','0x0',boot(d)),
           'partition-table':('part.bin','0x8000',b'partition-table-test'+bytes(1024)),
           'app':('app.bin','0x10000',app(descriptor(version='new' if changed else 'old')))}
    data=dict(write_flash_args=['--flash-mode','dio','--flash-size','16MB','--flash-freq','80m'],
              flash_settings=dict(flash_mode='dio',flash_size='16MB',flash_freq='80m'),
              extra_esptool_args=dict(chip='esp32s3',before='default-reset',after='hard-reset',stub=True),
              flash_files={})
    for role,(name,offset,blob) in files.items():
        (root/name).write_bytes(blob)
        data[role]=dict(file=name,offset=offset,encrypted='false')
        data['flash_files'][offset]=name
    path=root/'flasher_args.json';path.write_text(json.dumps(data));return path


def edit_manifest(path,mutate):
    data=json.loads(path.read_text());mutate(data);path.write_text(json.dumps(data))


def test_flash_set_cli_passes_metadata_only_changes(tmp_path):
    a,b=flash_set(tmp_path/'a'),flash_set(tmp_path/'b',True)
    out=tmp_path/'out.json'
    assert main([str(a),str(b),'--flash-set','--output',str(out)])==0
    r=json.loads(out.read_text());assert r['masked_equal'] and not r['raw_equal']
    assert r['unmasked_differing_bytes']==0 and r['flash_options_equal']
    assert r['images']['partition-table']['raw_equal']
    assert r['images']['bootloader']['policy']['descriptor_fields']==['date_time']


@pytest.mark.parametrize('role',['bootloader','partition-table','app'])
def test_flash_set_every_payload_is_compared(tmp_path,role):
    a,b=flash_set(tmp_path/'a'),flash_set(tmp_path/'b')
    entry=json.loads(b.read_text())[role];p=b.parent/entry['file']
    p.write_bytes(boot(code=b'ABCDEFGHIJKLMNOP') if role=='bootloader' else
                  app(code=b'ABCDEFGHIJKLMNOP') if role=='app' else b'changed-partition')
    out=tmp_path/'out.json'
    assert main([str(a),str(b),'--flash-set','--output',str(out)])==1
    assert not json.loads(out.read_text())['images'][role]['masked_equal']


@pytest.mark.parametrize('group',['write_flash_args','flash_settings','extra_esptool_args','offset'])
def test_flash_options_and_offsets_are_strict(tmp_path,group):
    a,b=flash_set(tmp_path/'a'),flash_set(tmp_path/'b')
    def mutate(d):
        if group=='write_flash_args':d[group][-1]='40m'
        elif group=='flash_settings':d[group]['flash_size']='8MB'
        elif group=='extra_esptool_args':d[group]['chip']='esp32'
        else:
            d['app']['offset']='0x20000'
            d['flash_files']['0x20000']=d['flash_files'].pop('0x10000')
    edit_manifest(b,mutate);out=tmp_path/'out.json'
    assert main([str(a),str(b),'--flash-set','--output',str(out)])==1
    r=json.loads(out.read_text());assert r['unmasked_differing_bytes']==0
    assert not r['masked_equal']
    assert (not r['images']['app']['offset_equal']) if group=='offset' else (group in r['differing_option_groups'])


@pytest.mark.parametrize('fault',['missing','extra','duplicate-offset','duplicate-key','mismatch','encrypted',
                                  'overlap','outside','missing-file','invalid-image','empty','bad-offset','bad-schema'])
def test_flash_set_invalid_inputs_fail_closed(tmp_path,fault):
    a,b=flash_set(tmp_path/'a'),flash_set(tmp_path/'b');out=tmp_path/'out.json'
    def mutate(d):
        if fault=='missing':del d['app']
        if fault=='extra':d['flash_files']['0x40000']='extra.bin'
        if fault=='duplicate-offset':d['flash_files']['0x0000']='boot.bin'
        if fault=='mismatch':d['app']['file']='boot.bin'
        if fault=='encrypted':d['app']['encrypted']='true'
        if fault=='overlap':
            d['app']['offset']='0x40';d['flash_files']['0x40']=d['flash_files'].pop('0x10000')
        if fault=='outside':d['app']['file']=d['flash_files']['0x10000']='../outside.bin'
        if fault=='bad-offset':d['app']['offset']=None
        if fault=='bad-schema':d['flash_files']=[]
    edit_manifest(b,mutate)
    if fault=='duplicate-key':b.write_text(b.read_text().replace('"app":','"app": {}, "app":'))
    if fault=='missing-file':(b.parent/'boot.bin').unlink()
    if fault=='invalid-image':(b.parent/'boot.bin').write_bytes(b'bad')
    if fault=='empty':(b.parent/'part.bin').write_bytes(b'')
    assert main([str(a),str(b),'--flash-set','--output',str(out)])==2
    assert not out.exists()


@pytest.mark.parametrize('target',['manifest','bootloader','partition-table','app','symlink','hardlink'])
def test_flash_set_output_cannot_overwrite_any_input(tmp_path,target):
    a,b=flash_set(tmp_path/'a'),flash_set(tmp_path/'b')
    data=json.loads(a.read_text())
    output=a if target=='manifest' else a.parent/data[target]['file'] if target in data else tmp_path/'alias'
    if target=='symlink':output.symlink_to(a.parent/'boot.bin')
    if target=='hardlink':output.hardlink_to(a.parent/'part.bin')
    before={p:p.read_bytes() for p in a.parent.iterdir()}
    assert main([str(a),str(b),'--flash-set','--output',str(output)])==2
    assert all(p.read_bytes()==raw for p,raw in before.items())


def test_flash_set_allows_different_names_not_different_contents(tmp_path):
    a,b=flash_set(tmp_path/'a'),flash_set(tmp_path/'b')
    (b.parent/'app.bin').rename(b.parent/'renamed.bin')
    def mutate(d):d['app']['file']=d['flash_files']['0x10000']='renamed.bin'
    edit_manifest(b,mutate)
    assert main([str(a),str(b),'--flash-set','--output',str(tmp_path/'out.json')])==0


def test_bootloader_cli_and_flash_set_app_mask_is_scoped(tmp_path):
    a,b=flash_set(tmp_path/'a'),flash_set(tmp_path/'b',True);out=tmp_path/'out.json'
    assert main([str(a.parent/'boot.bin'),str(b.parent/'boot.bin'),'--image-kind','bootloader','--output',str(out)])==0
    (b.parent/'app.bin').write_bytes(app(descriptor(idf_version='v-other')))
    assert main([str(a),str(b),'--flash-set','--mask-idf-version','reviewed fixture','--output',str(out)])==0
    d=boot_descriptor();d[8:12]=b'v9.9';(b.parent/'boot.bin').write_bytes(boot(d))
    assert main([str(a),str(b),'--flash-set','--mask-idf-version','reviewed fixture','--output',str(out)])==1


def test_flash_options_preserve_json_types(tmp_path):
    a,b=flash_set(tmp_path/'a'),flash_set(tmp_path/'b')
    edit_manifest(b,lambda d:d['extra_esptool_args'].update(stub=1))
    assert main([str(a),str(b),'--flash-set','--output',str(tmp_path/'out.json')])==1
