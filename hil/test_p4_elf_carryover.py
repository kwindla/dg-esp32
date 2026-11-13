"""Synthetic ELF/config controls for the strict TX-frame carry-over decision."""
import copy
import json
import os
import struct

import pytest
import p4_elf_carryover as carry


def xtensa(size):return (0x136|(size//8)<<12).to_bytes(3,'little')
def addi(amount):return ((amount&0xfff)<<20|0x10113).to_bytes(4,'little')


@pytest.mark.parametrize('machine,code,size',[
    ('EM_XTENSA',xtensa(880),880),('EM_XTENSA',xtensa(160),160),('EM_XTENSA',xtensa(192),192),
    ('EM_RISCV',addi(-880),880),('EM_RISCV',addi(-160),160),
    ('EM_RISCV',addi(-2032),2032),
    ('EM_RISCV',bytes.fromhex('0171'),512),  # C.ADDI16SP sp,-512.
    ('EM_RISCV',bytes.fromhex('7d71'),16),   # C.ADDI16SP sp,-16.
    ('EM_RISCV',bytes.fromhex('4111'),16),   # C.ADDI sp,-16.
])
def test_actual_instruction_encodings(machine,code,size):
    assert carry.entry_frame(machine,code)==size


@pytest.mark.parametrize('machine,code',[
    ('EM_XTENSA',b''),('EM_XTENSA',b'\x36\x41'),('EM_XTENSA',b'\x00'*4),
    ('EM_XTENSA',xtensa(8)),('EM_RISCV',b'\x13\x01'),('EM_RISCV',addi(16)),
    ('EM_RISCV',addi(0)),('EM_RISCV',addi(-12)),('EM_RISCV',b'\x01\x00'),
    ('EM_RISCV',bytes.fromhex('0161')),('EM_ARM',addi(-160)),
    ('EM_RISCV',addi(-2048)),
])
def test_unknown_truncated_and_nonallocating_entries_fail_closed(machine,code):
    with pytest.raises(ValueError):carry.entry_frame(machine,code)


@pytest.mark.parametrize('first',[addi(-160),bytes.fromhex('3571')])  # C.ADDI16SP sp,-160.
@pytest.mark.parametrize('second',[
    addi(-16),bytes.fromhex('7d71'),bytes.fromhex('4111'),
    (0x00510133).to_bytes(4,'little'),  # ADD sp,sp,t0: register-sized second allocation.
    bytes.fromhex('1681'),bytes.fromhex('1691'),  # C.MV/C.ADD sp,t0.
])
def test_split_stack_adjustment_after_register_saves_is_refused(first,second):
    saves=(0x00112623).to_bytes(4,'little')+bytes.fromhex('0100')*3  # SW ra,12(sp), C.NOPs.
    with pytest.raises(ValueError,match='multiple RISC-V TX stack adjustments'):
        carry.entry_frame('EM_RISCV',first+saves+second)


@pytest.mark.parametrize('boundary',[
    (0x000000ef).to_bytes(4,'little'),  # JAL ra,0.
    (0x00000063).to_bytes(4,'little'),  # BEQ zero,zero,0.
    bytes.fromhex('01a0'),bytes.fromhex('82 80'),  # C.J / RET.
])
def test_body_or_epilogue_after_control_flow_is_not_part_of_the_entry_frame(boundary):
    assert carry.entry_frame('EM_RISCV',addi(-160)+boundary+addi(160))==160


@pytest.mark.parametrize('tail',[b'\x13',bytes.fromhex('1301'),bytes.fromhex('1f00')])
def test_truncated_or_long_following_instruction_is_refused(tail):
    with pytest.raises(ValueError):carry.entry_frame('EM_RISCV',addi(-160)+tail)


def images(machine='EM_XTENSA'):
    old=dict(machine=machine,symbols={carry.TX:[4000],'encode':[64],'local':[16,32]},tx_frame_bytes=880)
    new=copy.deepcopy(old);new['symbols'][carry.TX]=[1000];new['symbols'][carry.HELPER]=[720];new['tx_frame_bytes']=160
    config=dict(equal=['CONFIG_DG_ECHOEAR_AEC'],differ=[],added=[],removed=[],missing_required=[])
    return old,new,config


@pytest.mark.parametrize('machine',['EM_XTENSA','EM_RISCV'])
def test_only_helper_and_tx_may_change(machine):
    result=carry.evaluate(*images(machine))
    assert result['passed'] and result['status']=='PASS' and result['tx_frame_bytes']==dict(old=880,new=160)


@pytest.mark.parametrize('mutation',[
    'extra-function','removed-function','size-change','local-multiplicity','missing-helper','duplicate-helper',
    'old-helper','missing-tx','duplicate-tx','architecture','frame-unreduced','s3-frame-too-large',
    'config-differ','config-added','config-removed','config-missing',
])
def test_each_binding_condition_rejects_a_mutation(mutation):
    old,new,config=images()
    if mutation=='extra-function':new['symbols']['other']=[1]
    elif mutation=='removed-function':new['symbols'].pop('encode')
    elif mutation=='size-change':new['symbols']['encode']=[80]
    elif mutation=='local-multiplicity':new['symbols']['local']=[16]
    elif mutation=='missing-helper':new['symbols'].pop(carry.HELPER)
    elif mutation=='duplicate-helper':new['symbols'][carry.HELPER]=[720,720]
    elif mutation=='old-helper':old['symbols'][carry.HELPER]=[720]
    elif mutation=='missing-tx':new['symbols'].pop(carry.TX)
    elif mutation=='duplicate-tx':new['symbols'][carry.TX]=[1000,1000]
    elif mutation=='architecture':new['machine']='EM_RISCV'
    elif mutation=='frame-unreduced':new['tx_frame_bytes']=880
    elif mutation=='s3-frame-too-large':new['tx_frame_bytes']=208
    else:config[{'config-differ':'differ','config-added':'added','config-removed':'removed','config-missing':'missing_required'}[mutation]]=['CONFIG_DG_OPUS_BITRATE']
    result=carry.evaluate(old,new,config)
    assert result['status']=='FAIL' and not result['passed']


def test_p4_frame_is_reported_without_s3_limit():
    old,new,config=images('EM_RISCV');new['tx_frame_bytes']=224
    assert carry.evaluate(old,new,config)['passed']


@pytest.mark.parametrize('suffix',['','.isra.0','$isra$0','.constprop.2'])
def test_only_named_helper_clones_are_recognized(suffix):
    assert carry.helper(carry.HELPER+suffix)
    assert not carry.helper(carry.HELPER+suffix+'unrelated')


@pytest.mark.parametrize('name',[
    '_ZN7pipecat12_GLOBAL__N_1L21emit_encoder_settingsERKNS_8esp_peer15EncoderSettingsE',
    '_ZN7pipecat12_GLOBAL__N_121emit_encoder_settingsERKNS_8esp_peer15EncoderSettingsE',
])
def test_gcc_internal_linkage_marker_is_part_of_the_same_exact_helper(name):
    assert carry.helper(name)


def write_elf(path,code,machine=94,extra=(),tx_size=8):
    """Tiny ELF32 fixture with .text, .symtab and their string tables; no compiler."""
    names=[carry.TX,'encode',carry.HELPER,*[row[0] for row in extra]]
    strings=b'\0';name_offsets={}
    for name in names:
        if name not in name_offsets:name_offsets[name]=len(strings);strings+=name.encode()+b'\0'
    shstrings=b'\0.text\0.symtab\0.strtab\0.shstrtab\0'
    text=code+b'\0'*(16-len(code))+b'\0'*16
    symbols=b'\0'*16
    rows=[(carry.TX,0,tx_size,1),('encode',16,4,1),(carry.HELPER,20,4,1),*extra]
    for row in rows:
        name,offset,size,section=row if len(row)==4 else (*row,1)
        symbols+=struct.pack('<IIIBBH',name_offsets[name],0x1000+offset,size,0x12,0,section)
    blobs=[text,symbols,strings,shstrings];offsets=[];data=bytearray(52)
    for blob in blobs:
        while len(data)%4:data+=b'\0'
        offsets.append(len(data));data+=blob
    while len(data)%4:data+=b'\0'
    section_offset=len(data);data+=b'\0'*40
    specs=[(1,1,6,0x1000,len(text),0,0,4,0),(7,2,0,0,len(symbols),3,1,4,16),
        (15,3,0,0,len(strings),0,0,1,0),(23,3,0,0,len(shstrings),0,0,1,0)]
    for off,(name,kind,flags,addr,size,link,info,align,entsize) in zip(offsets,specs):
        data+=struct.pack('<10I',name,kind,flags,addr,off,size,link,info,align,entsize)
    ident=b'\x7fELF\x01\x01\x01'+b'\0'*9
    data[:52]=struct.pack('<16sHHIIIIIHHHHHH',ident,2,machine,1,0x1000,0,section_offset,0,52,0,0,40,5,4)
    path.write_bytes(data);return path


@pytest.mark.parametrize('machine,code,expected',[(94,xtensa(160),'EM_XTENSA'),(243,addi(-192),'EM_RISCV')])
def test_real_elf_parser_reads_defined_function_sizes_and_entry(tmp_path,machine,code,expected):
    path=write_elf(tmp_path/'fixture.elf',code,machine,extra=[('undefined',0,4,0),('encode',24,8,1)])
    result=carry.inspect_elf(path)
    assert result['machine']==expected and result['symbols']['encode']==[4,8]
    assert 'undefined' not in result['symbols']
    assert result['tx_frame_bytes']==(160 if machine==94 else 192)


def test_elf_parser_does_not_hide_a_second_sp_adjustment_after_eight_bytes(tmp_path):
    code=addi(-2032)+(0x00112623).to_bytes(4,'little')+addi(-16)
    path=write_elf(tmp_path/'split.elf',code,machine=243,tx_size=len(code))
    with pytest.raises(ValueError,match='multiple RISC-V TX stack adjustments'):
        carry.inspect_elf(path)


@pytest.mark.parametrize('change',['machine','duplicate-tx','entry','outside-section'])
def test_invalid_elf_evidence_cannot_be_accepted(tmp_path,change):
    extra=[(carry.TX,0,8,1)] if change=='duplicate-tx' else []
    path=write_elf(tmp_path/'bad.elf',b'\0'*4 if change=='entry' else xtensa(160),40 if change=='machine' else 94,extra)
    if change=='outside-section':
        raw=bytearray(path.read_bytes());struct.pack_into('<I',raw,52+32+16+4,0x9000);path.write_bytes(raw)
    with pytest.raises(ValueError):carry.inspect_elf(path)


def configs(tmp_path):
    left=tmp_path/'sdkconfig-old';right=tmp_path/'sdkconfig-new'
    keys=['DG_ECHOEAR_AEC','DG_ECHOEAR_AEC_REF_GAIN_DB','DG_ECHOEAR_AEC_GUARD_MS',
        'DG_ECHOEAR_AEC_GUARD_REARM_MS','DG_AUDIO_TX_STACK_BYTES','DG_OPUS_BITRATE','DG_MIC_GAIN_PERCENT',
        'DG_PLAYBACK_SEED_DBFS_X10','DG_DEMO_AUTOSTART']
    text=''.join('CONFIG_'+key+'=y\n' for key in keys)
    left.write_text(text);right.write_text(text);return left,right


def test_configuration_comparison_never_serializes_values(tmp_path):
    left,right=configs(tmp_path);secret='fixture-private-value-do-not-emit'
    with left.open('a') as stream:stream.write('CONFIG_DG_WIFI_PASSWORD="'+secret+'"\n')
    with right.open('a') as stream:stream.write('CONFIG_DG_WIFI_PASSWORD="different-private-value"\n')
    row=carry.config_diff(left,right);output=json.dumps(row)
    assert row['differ']==['CONFIG_DG_WIFI_PASSWORD']
    assert secret not in output and 'different-private-value' not in output
    assert not row['missing_required']


def test_configuration_missing_required_key_cannot_pass(tmp_path):
    left,right=configs(tmp_path)
    for p in (left,right):p.write_text(p.read_text().replace('CONFIG_DG_AUDIO_TX_STACK_BYTES=y\n',''))
    assert carry.config_diff(left,right)['missing_required']==['CONFIG_DG_AUDIO_TX_STACK_BYTES']


def test_cli_retains_failure_without_exposing_parser_exception(tmp_path,monkeypatch,capsys):
    def fail(*args):raise ValueError('private-input-must-not-be-printed')
    monkeypatch.setattr(carry,'audit',fail);out=tmp_path/'carryover.json'
    argv=['--pair','p4-normal','--old-bundle','old','--new-bundle','new',
        '--old-config','sdkconfig-old','--new-config','sdkconfig-new','--out',str(out)]
    assert carry.main(argv)==1
    assert json.loads(out.read_text())['status']=='FAIL'
    assert 'private-input' not in out.read_text()+capsys.readouterr().out
    with pytest.raises(FileExistsError):carry.main(argv)


def test_checked_receipt_is_recomputed_and_cannot_be_promoted(tmp_path,monkeypatch):
    row=dict(kind='tx-frame-carryover-check',pair='p4-normal',old_bundle='old',new_bundle='new',
        old_config='sdkconfig-old',new_config='sdkconfig-new',passed=True)
    path=tmp_path/'receipt.json';path.write_text(json.dumps(row))
    monkeypatch.setattr(carry,'audit',lambda *args:dict(row))
    assert carry.checked(path)==row
    changed=dict(row,passed=False);monkeypatch.setattr(carry,'audit',lambda *args:changed)
    with pytest.raises(ValueError):carry.checked(path)


def test_config_binding_requires_original_digest(tmp_path):
    config=tmp_path/'sdkconfig';config.write_text('CONFIG_FIXTURE=y\n')
    carry.config_binding(tmp_path,dict(resolved_config_sha256=carry.digest(config)),config)
    with pytest.raises(ValueError):carry.config_binding(tmp_path,dict(resolved_config_sha256='0'*64),config)


@pytest.fixture
def retained_s3_build(tmp_path):
    build=tmp_path/'retained-s3-build';build.mkdir()
    config=build/'sdkconfig';config.write_text('CONFIG_FIXTURE=y\n')
    elf=build/'dg-esp32.elf';elf.write_bytes(b'synthetic ELF identity; binding does not parse it')
    project=build/'project_description.json';project.write_text(json.dumps(dict(config_file=str(config))))
    os.utime(config,ns=(1000000000,1000000000));os.utime(elf,ns=(2000000000,2000000000))
    manifest=dict(build_dir=str(build),elf_sha256=carry.digest(elf))
    return build,config,elf,project,manifest


def test_s3_binding_without_config_digest_accepts_matching_path_elf_and_mtime(retained_s3_build):
    build,config,_,_,manifest=retained_s3_build
    carry.config_binding(build,manifest,config)


@pytest.mark.parametrize('mutation',['config-path','elf','newer-config','missing-project'])
def test_s3_binding_without_config_digest_refuses_unbound_inputs(retained_s3_build,mutation):
    build,config,elf,project,manifest=retained_s3_build
    if mutation=='config-path':project.write_text(json.dumps(dict(config_file=str(build/'sdkconfig-other'))))
    elif mutation=='elf':elf.write_bytes(b'overwritten ELF')
    elif mutation=='newer-config':os.utime(config,ns=(3000000000,3000000000))
    else:project.unlink()
    with pytest.raises((ValueError,OSError)):carry.config_binding(build,manifest,config)


@pytest.mark.parametrize('mutation',['old-elf','same-elf','profile'])
def test_pair_audit_rejects_wrong_image_before_inspecting_functions(tmp_path,monkeypatch,mutation):
    import make_bundle
    board,profile,pinned=carry.PAIRS['p4-normal']
    old=dict(profile=profile,elf_sha256=pinned);new=dict(profile=profile,elf_sha256='b'*64)
    if mutation=='old-elf':old['elf_sha256']='c'*64
    elif mutation=='same-elf':new['elf_sha256']=pinned
    else:new['profile']='aec-demo'
    left=tmp_path/'old';right=tmp_path/'new'
    monkeypatch.setattr(make_bundle,'verify_bundle',lambda path,board:(old if path==left else new,{}))
    def forbidden(*args):raise AssertionError('must reject before parsing any ELF or config')
    monkeypatch.setattr(carry,'inspect_elf',forbidden);monkeypatch.setattr(carry,'config_binding',forbidden)
    with pytest.raises(ValueError):carry.audit('p4-normal',left,right,tmp_path/'sdkconfig-old',tmp_path/'sdkconfig-new')


def test_unavailable_config_binding_retains_independent_frame_and_symbol_results(tmp_path,monkeypatch):
    import make_bundle
    old,new,config=images('EM_RISCV');pinned=carry.PAIRS['p4-normal'][2]
    left=tmp_path/'old';right=tmp_path/'new'
    for p in (left,right):p.mkdir();(p/'manifest.json').write_text('{}')
    configs_pair=configs(tmp_path)
    monkeypatch.setattr(make_bundle,'verify_bundle',lambda p,board:(
        dict(profile='aec-normal',elf_sha256=pinned if p==left else 'b'*64),{}))
    monkeypatch.setattr(carry,'inspect_elf',lambda p:old if p.parent==left else new)
    monkeypatch.setattr(carry,'config_diff',lambda *args:config)
    def bind(bundle,*args):
        if bundle==left:raise ValueError('old retained build overwritten')
    monkeypatch.setattr(carry,'config_binding',bind)
    row=carry.audit('p4-normal',left,right,*configs_pair)
    assert row['status']=='FAIL' and row['tx_frame_bytes']==dict(old=880,new=160)
    assert row['checks']['other_function_sizes_equal'] and row['checks']['effective_config_equal']
    assert not row['checks']['old_config_bound_to_elf'] and row['checks']['new_config_bound_to_elf']


def test_real_s3_config_binding_failure_alone_makes_the_audit_fail(tmp_path,monkeypatch,retained_s3_build):
    import make_bundle
    build,old_config,_,_,manifest=retained_s3_build
    old,new,config=images('EM_XTENSA')
    left=tmp_path/'old';right=tmp_path/'new'
    for p in (left,right):p.mkdir();(p/'manifest.json').write_text('{}')
    new_config=right/'sdkconfig';new_config.write_bytes(old_config.read_bytes())
    # Keep the decision-pinned old bundle identity; the retained build ELF was
    # overwritten. Exercise the actual S3 path/hash/mtime branch, not a stub.
    old_manifest=dict(manifest,profile='aec-normal',elf_sha256=carry.PAIRS['s3-normal'][2])
    new_manifest=dict(profile='aec-normal',elf_sha256='b'*64,resolved_config_sha256=carry.digest(new_config))
    monkeypatch.setattr(make_bundle,'verify_bundle',lambda p,board:(old_manifest if p==left else new_manifest,{}))
    monkeypatch.setattr(carry,'inspect_elf',lambda p:old if p.parent==left else new)
    monkeypatch.setattr(carry,'config_diff',lambda *args:config)
    row=carry.audit('s3-normal',left,right,old_config,new_config)
    assert row['status']=='FAIL' and not row['passed']
    assert not row['checks']['old_config_bound_to_elf'] and row['checks']['new_config_bound_to_elf']
    assert all(value for name,value in row['checks'].items() if name!='old_config_bound_to_elf')
