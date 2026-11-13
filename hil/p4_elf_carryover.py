#!/usr/bin/env python3
"""Decision-bound TX-frame carry-over audit. Offline only; no config values emitted.

Read generated configs only through sdkconfig_keydiff. Compare every effective
key (stronger than an audio allow-list), preserving its last-assignment rules.
An unreadable ELF/config or unsupported entry instruction cannot yield PASS.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
DECISION='decision-2026-11-02-tx-frame-trim-carryover'
TX='_ZN7pipecat16EspPeerTransport4Impl7Session6tx_runEv'
HELPER='_ZN7pipecat12_GLOBAL__N_1L21emit_encoder_settingsERKNS_8esp_peer15EncoderSettingsE'
PAIRS={
    'p4-normal':('p4xc','aec-normal','ae954c7bc18176d46ca35926da1193cba33115d5268280cd8a020bc98c6776f0'),
    'p4-demo':('p4xc','aec-demo','36455c41d8408c75ec5ee3aa0f996b07fc7bfbc7aabd37fa689f04bc0fa65c58'),
    's3-normal':('echoear','aec-normal','d09b3560cdca3bdd8133ea207061f19a24852758560701b28868ed3a143225d7'),
}


def require(value,message):
    if not value:raise ValueError(message)


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def helper(name):
    # GCC may retain the internal-linkage L marker on the anonymous-namespace
    # function. Both spellings identify this exact signature, never a prefix.
    signature=re.escape(HELPER).replace('N_1L21','N_1L?21')
    return re.fullmatch(signature+r'(?:(?:\.|\$)(?:isra|constprop)(?:\.|\$)\d+)?',name) is not None


def sign(value,bits):
    return value-(1<<bits) if value&(1<<(bits-1)) else value


def entry_frame(machine,code):
    """Decode the actual initial stack allocation; fail closed on other prologues.

    Xtensa ENTRY encodes an 8-byte-scaled immediate. RV32 supports ADDI,
    C.ADDI16SP and C.ADDI. RV32 accepts at most 2032 bytes and exactly one
    SP write before the first control-flow instruction. Inspect the complete
    first basic block so split allocations cannot hide after register saves.
    """
    if machine=='EM_XTENSA':
        require(len(code)>=3,'truncated Xtensa TX entry')
        word=int.from_bytes(code[:3],'little')
        require(word&0xfff==0x136,'unsupported Xtensa TX entry')
        size=(word>>12)*8
        require(size>=16 and size%16==0,'invalid Xtensa TX entry frame')
        return size
    require(machine=='EM_RISCV','unsupported ELF architecture')
    require(len(code)>=2,'truncated RISC-V TX entry')
    half=int.from_bytes(code[:2],'little')
    if half&3==3:
        width=4
        require(len(code)>=4,'truncated RISC-V TX instruction')
        word=int.from_bytes(code[:4],'little')
        require(word&0xfffff==0x10113,'unsupported RISC-V TX entry')
        amount=sign(word>>20,12)
    elif half&0xef83==0x6101:  # C.ADDI16SP: nzimm[9|4|6|8:7|5].
        width=2
        value=((half>>12&1)<<9)|((half>>6&1)<<4)|((half>>5&1)<<6)|((half>>3&3)<<7)|((half>>2&1)<<5)
        amount=sign(value,10)
    elif half&0xef83==0x0101:  # C.ADDI sp,nzimm[5|4:0].
        width=2
        amount=sign((half>>12&1)<<5|(half>>2&31),6)
    else:raise ValueError('unsupported RISC-V TX entry')
    require(amount<0 and amount%16==0,'invalid RISC-V TX entry frame')
    require(-amount<=2032,'RISC-V TX entry frame above 2032 bytes unsupported')
    at=width
    while at<len(code):
        require(at+2<=len(code),'truncated RISC-V TX prologue')
        half=int.from_bytes(code[at:at+2],'little')
        if half&3==3:
            require(half&0x1f!=0x1f,'unsupported long RISC-V TX prologue instruction')
            require(at+4<=len(code),'truncated RISC-V TX prologue instruction')
            word=int.from_bytes(code[at:at+4],'little');opcode=word&0x7f
            if opcode in (0x63,0x67,0x6f,0x73):break  # Branch, jump/call, or system boundary.
            writes_sp=(word>>7&31)==2 and opcode in (0x03,0x0b,0x13,0x17,0x1b,0x2b,0x2f,0x33,0x37,0x3b,0x5b,0x7b)
            width=4
        else:
            quadrant=half&3;kind=half>>13;rd=half>>7&31;rs2=half>>2&31
            if quadrant==1 and kind in (1,5,6,7):break  # C.JAL/J/BEQZ/BNEZ.
            if quadrant==2 and kind==4 and rs2==0:break  # C.JR/JALR/EBREAK.
            writes_sp=rd==2 and ((quadrant==1 and kind in (0,2,3)) or
                (quadrant==2 and (kind in (0,2) or (kind==4 and rs2!=0))))
            width=2
        require(not writes_sp,'multiple RISC-V TX stack adjustments unsupported')
        at+=width
    return -amount


def inspect_elf(path):
    from elftools.elf.elffile import ELFFile
    with Path(path).open('rb') as stream:
        elf=ELFFile(stream)
        require(elf.elfclass==32 and elf.little_endian,'32-bit little-endian ELF required')
        machine=elf['e_machine'];require(machine in ('EM_XTENSA','EM_RISCV'),'unsupported ELF architecture')
        table=elf.get_section_by_name('.symtab');require(table is not None,'ELF lacks defined function symbols')
        symbols={};entries=[]
        for symbol in table.iter_symbols():
            if symbol['st_info']['type']!='STT_FUNC' or symbol['st_shndx']=='SHN_UNDEF':continue
            name=symbol.name;require(bool(name),'unnamed defined function')
            symbols.setdefault(name,[]).append(int(symbol['st_size']))
            if name==TX:entries.append(symbol)
        require(len(entries)==1,'exactly one TX function required')
        tx=entries[0];require(isinstance(tx['st_shndx'],int),'TX must reside in an executable section')
        section=elf.get_section(tx['st_shndx']);offset=int(tx['st_value'])-int(section['sh_addr'])
        require(section['sh_flags']&4 and tx['st_size']>=3 and 0<=offset and
            offset+tx['st_size']<=section['sh_size'],'TX function outside executable section')
        code=section.data()[offset:offset+int(tx['st_size'])]
        frame=entry_frame(machine,code)
    return dict(machine=machine,symbols={k:sorted(v) for k,v in symbols.items()},tx_frame_bytes=frame)


def config_diff(old,new):
    spec=importlib.util.spec_from_file_location('carryover_keydiff',ROOT/'scripts/sdkconfig_keydiff.py')
    keydiff=importlib.util.module_from_spec(spec);spec.loader.exec_module(keydiff)
    # Config values never leave this approved parser/comparator or enter JSON.
    result=keydiff.compare(keydiff.settings(old),keydiff.settings(new))
    required=['CONFIG_DG_ECHOEAR_AEC','CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB',
        'CONFIG_DG_ECHOEAR_AEC_GUARD_MS','CONFIG_DG_ECHOEAR_AEC_GUARD_REARM_MS',
        'CONFIG_DG_AUDIO_TX_STACK_BYTES','CONFIG_DG_OPUS_BITRATE','CONFIG_DG_MIC_GAIN_PERCENT',
        'CONFIG_DG_PLAYBACK_SEED_DBFS_X10','CONFIG_DG_DEMO_AUTOSTART']
    result['missing_required']=sorted(set(required)-set(result['equal']))
    return result


def evaluate(old,new,config):
    a,b=old['symbols'],new['symbols'];added=sorted(b.keys()-a.keys());removed=sorted(a.keys()-b.keys())
    helpers=[name for name in added if helper(name)]
    sizes=[dict(symbol=name,old_bytes=a[name],new_bytes=b[name]) for name in sorted(a.keys()&b.keys())
        if name!=TX and a[name]!=b[name]]
    checks=dict(architecture_equal=old['machine']==new['machine'],
        symbol_set_equal_except_helper=len(helpers)==1 and added==helpers and not removed and
            not any(helper(name) for name in a) and len(b[helpers[0]])==1,
        other_function_sizes_equal=not sizes,
        tx_present=TX in a and TX in b and len(a[TX])==len(b[TX])==1,
        tx_frame_reduced=0<new['tx_frame_bytes']<old['tx_frame_bytes'],
        s3_tx_frame_at_most_192=new['machine']!='EM_XTENSA' or new['tx_frame_bytes']<=192,
        effective_config_equal=not any(config[k] for k in ('differ','added','removed','missing_required')))
    return dict(checks=checks,status='PASS' if all(checks.values()) else 'FAIL',passed=all(checks.values()),
        added_functions=added,removed_functions=removed,changed_function_sizes=sizes,
        defined_function_counts=dict(old=sum(map(len,a.values())),new=sum(map(len,b.values()))),
        tx_frame_bytes=dict(old=old['tx_frame_bytes'],new=new['tx_frame_bytes']),config_keydiff=config)


def config_binding(bundle,manifest,config):
    """Bind generated configuration to the exact ELF before comparing it.

    P4 manifests already bind its digest. S3's writer has no config digest;
    require the retained build ELF and project config path to match instead.
    """
    expected=manifest.get('resolved_config_sha256')
    if expected:
        require(digest(config)==expected,'generated config digest differs from bundle')
        return
    build=Path(manifest['build_dir']);project=json.loads((build/'project_description.json').read_text())
    require(Path(project['config_file']).resolve()==Path(config).resolve(),'generated config path differs from build')
    require(digest(build/'dg-esp32.elf')==manifest['elf_sha256'],'retained build ELF differs from bundle')
    require(Path(config).stat().st_mtime_ns<=(build/'dg-esp32.elf').stat().st_mtime_ns,
        'generated config newer than retained build ELF')


def audit(pair,old_bundle,new_bundle,old_config,new_config):
    from make_bundle import verify_bundle
    board,profile,old_sha=PAIRS[pair]
    old_bundle,new_bundle=Path(old_bundle).resolve(),Path(new_bundle).resolve()
    old,_=verify_bundle(old_bundle,board);new,_=verify_bundle(new_bundle,board)
    require(old['profile']==new['profile']==profile,'pair profile differs')
    require(old['elf_sha256']==old_sha,'pair does not use decision-pinned old ELF')
    require(new['elf_sha256']!=old_sha,'fresh trimmed ELF required')
    bindings={}
    for label,bundle,manifest,config in (('old',old_bundle,old,old_config),('new',new_bundle,new,new_config)):
        try:
            config_binding(bundle,manifest,config);bindings[label]=True
        except (ValueError,OSError,KeyError,TypeError):bindings[label]=False
    a,b=inspect_elf(old_bundle/'dg-esp32.elf'),inspect_elf(new_bundle/'dg-esp32.elf')
    expected='EM_RISCV' if board=='p4xc' else 'EM_XTENSA'
    require(a['machine']==b['machine']==expected,'pair ELF architecture differs from board')
    result=evaluate(a,b,config_diff(old_config,new_config))
    # Keep the independent frame/symbol/config findings visible if a retained
    # build was overwritten. Missing binding still prevents any carry-over.
    result['checks'].update(old_config_bound_to_elf=bindings['old'],new_config_bound_to_elf=bindings['new'])
    result['passed']=all(result['checks'].values());result['status']='PASS' if result['passed'] else 'FAIL'
    return dict(result,schema=1,kind='tx-frame-carryover-check',decision=DECISION,pair=pair,board=board,profile=profile,
        old_bundle=str(old_bundle),new_bundle=str(new_bundle),old_elf_sha256=old_sha,new_elf_sha256=new['elf_sha256'],
        old_manifest_sha256=digest(old_bundle/'manifest.json'),new_manifest_sha256=digest(new_bundle/'manifest.json'),
        old_config_sha256=digest(old_config),new_config_sha256=digest(new_config),
        old_config=str(Path(old_config).resolve()),new_config=str(Path(new_config).resolve()),
        helper_sha256=digest(Path(__file__)),measurement_carried=False,
        label=(f'carried from ELF {old_sha}, TX-frame-only change, per this decision' if result['passed'] and board=='p4xc'
            else 'no carry-over; full 15c demo phase required' if not result['passed'] else 'S3 frame check only; smokes reacquired'))


def checked(path):
    row=json.loads(Path(path).read_text())
    require(row.get('kind')=='tx-frame-carryover-check' and row.get('pair') in PAIRS,'carry-over receipt required')
    expected=audit(row['pair'],row['old_bundle'],row['new_bundle'],row['old_config'],row['new_config'])
    require(row==expected and row['passed'],'carry-over receipt changed or failed')
    return row


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pair',choices=PAIRS,required=True)
    for name in ('old-bundle','new-bundle','old-config','new-config','out'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        result=audit(args.pair,args.old_bundle,args.new_bundle,args.old_config,args.new_config)
    except Exception:
        # Config/ELF parsers may include raw input in exceptions. Never echo it.
        result=dict(schema=1,kind='tx-frame-carryover-check',decision=DECISION,pair=args.pair,
            status='FAIL',passed=False,error='carry-over input invalid or unavailable; no carry-over authorized')
    with args.out.open('x') as stream:stream.write(json.dumps(result,indent=2)+'\n')
    print(args.pair+': '+result['status']);return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
