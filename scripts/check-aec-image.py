#!/usr/bin/env python3
"""Fail the optional AEC build if its audited init table still consumes DRAM."""
import argparse
import hashlib
import io
from pathlib import Path
import subprocess
from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection

P4_ARCHIVE_SHA256 = '16269d8abda631b872ef438f1f0a91105f4f6cd876451d44aaa0e65bd26c6100'
P4_TABLE_SHA256 = '13a1ee2441e4b416fd754c55927c24ef28218e84739dcb297b6d4f7ddb6fcef8'
TABLE_OBJECT = 'dios_ssp_share_subband_dlfft.c.obj'


def require(condition, message):
    if not condition: raise ValueError(message)


def verify_p4_table(section, address, size, contents, original, flags=2):
    # GNU ld preserves SHF_WRITE from the original .data input even when the
    # audited table is mapped into flash rodata, just as for the S3 mapping.
    # SPIRAM_XIP_FROM_PSRAM=y copies image text/rodata to cached PSRAM at
    # boot. This is its mapped window; non-XIP P4 layouts fail closed.
    require(section=='.flash.rodata' and 0x48000000<=address<address+size<=0x4c000000 and flags in (2,3),
            'P4 AEC source coefficient table must reside entirely in PSRAM-XIP flash rodata')
    require(size==3072 and len(contents)==size and contents==original and
            hashlib.sha256(original).hexdigest()==P4_TABLE_SHA256,
            'P4 AEC coefficient table size or bytes changed')


def verify_p4_references(references):
    require(references==[('.text.dios_ssp_share_subband_init',0xc0,26,0),
                         ('.text.dios_ssp_share_subband_init',0xc4,27,0)],'P4 coefficient runtime-reference audit changed')


def verify_no_dram_copy(section, address, contents, original):
    if section.startswith('.dram') or 0x4ff00000<=address<0x50000000:
        require(original not in contents,'P4 AEC source coefficient table has an initialized internal SRAM copy')


def check_p4(elf_path, archive, ar, expected_hash):
    require(expected_hash==P4_ARCHIVE_SHA256 and hashlib.sha256(archive.read_bytes()).hexdigest()==P4_ARCHIVE_SHA256,
            'Unaudited P4 AEC processor archive')
    members=subprocess.run([ar,'t',str(archive)],check=True,capture_output=True,text=True).stdout.splitlines()
    require(members.count(TABLE_OBJECT)==1,'Expected one P4 AEC coefficient object in the pinned archive')
    member=subprocess.run([ar,'p',str(archive),TABLE_OBJECT],check=True,capture_output=True).stdout
    obj=ELFFile(io.BytesIO(member))
    require(obj.elfclass==32 and obj.little_endian and obj['e_machine']=='EM_RISCV','P4 AEC archive requires ELF32 RISC-V')
    initialized=[s for s in obj.iter_sections() if s.name.startswith('.data') and s['sh_size']]
    require([s.name for s in initialized]==['.data.subband_filter_coef'],'P4 AEC object has unexpected initialized data')
    original=initialized[0].data()
    table=obj.get_section_by_name('.symtab')
    symbols=table.get_symbol_by_name('subband_filter_coef') if table else None
    require(symbols is not None and len(symbols)==1,'Expected one P4 archive coefficient symbol')
    symbol=symbols[0]
    require(symbol['st_info']['type']=='STT_OBJECT' and symbol['st_info']['bind']=='STB_LOCAL' and
            type(symbol['st_shndx']) is int and obj.get_section(symbol['st_shndx']).name=='.data.subband_filter_coef' and
            symbol['st_value']==0 and symbol['st_size']==3072,'P4 archive coefficient symbol contract changed')
    # Bind the recorded audit: only HI20/LO12_I in the init function refer to
    # the table at runtime. Debug relocations are non-allocated metadata.
    references=[]
    for section in obj.iter_sections():
        if not isinstance(section,RelocationSection): continue
        target=obj.get_section(section['sh_info'])
        if not target['sh_flags']&2: continue
        for relocation in section.iter_relocations():
            referenced=table.get_symbol(relocation['r_info_sym'])
            if referenced['st_shndx']==symbol['st_shndx']:
                references.append((target.name,relocation['r_offset'],relocation['r_info_type'],relocation['r_addend']))
    verify_p4_references(references)
    with elf_path.open('rb') as stream:
        elf=ELFFile(stream)
        require(elf.elfclass==32 and elf.little_endian and elf['e_machine']=='EM_RISCV','P4 AEC image requires ELF32 RISC-V')
        table=elf.get_section_by_name('.symtab')
        symbols=table.get_symbol_by_name('subband_filter_coef') if table else None
        require(symbols is not None and len(symbols)==1,'Expected exactly one local P4 AEC source coefficient table')
        symbol=symbols[0]
        require(symbol['st_info']['type']=='STT_OBJECT' and symbol['st_info']['bind']=='STB_LOCAL' and
                type(symbol['st_shndx']) is int,'Invalid P4 coefficient symbol type/binding/section')
        section=elf.get_section(symbol['st_shndx']);offset=symbol['st_value']-section['sh_addr']
        require(section['sh_type']=='SHT_PROGBITS' and 0<=offset and offset+symbol['st_size']<=section['sh_size'],
                'P4 AEC coefficient contents outside the file-backed section')
        contents=section.data()[offset:offset+symbol['st_size']]
        verify_p4_table(section.name,symbol['st_value'],symbol['st_size'],contents,original,section['sh_flags'])
        for candidate in elf.iter_sections():
            if candidate['sh_type']=='SHT_PROGBITS' and candidate['sh_flags']&2:
                verify_no_dram_copy(candidate.name,candidate['sh_addr'],candidate.data(),original)
        initializers=table.get_symbol_by_name('dios_ssp_share_subband_init') or []
        require(len(initializers)==1 and type(initializers[0]['st_shndx']) is int and
                elf.get_section(initializers[0]['st_shndx']).name=='.flash.text',
                'P4 coefficient initializer must remain in cache-enabled flash code')
        print(f'P4 AEC coefficient placement verified: {symbol["st_size"]} unchanged bytes in {section.name}; archive/runtime-reference audit verified')


def verify_table(section, address, size, contents, original):
    if section != '.flash.rodata' or not 0x3c000000 <= address < address+size <= 0x3e000000:
        raise ValueError('AEC source coefficient table must reside entirely in mapped ESP32-S3 rodata')
    if size != 3072 or len(contents) != size or contents != original:
        raise ValueError('AEC coefficient table size or bytes changed')


def check(elf_path, archive, ar, expected_hash, board='echoear'):
    if board == 'p4xc':
        return check_p4(elf_path,archive,ar,expected_hash)
    if board != 'echoear':
        raise ValueError('Unknown AEC image board')
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected_hash:
        raise ValueError('Unaudited AEC processor archive')
    member = subprocess.run([ar, 'p', str(archive), 'dios_ssp_share_subband_dlfft.c.obj'],
                            check=True, capture_output=True).stdout
    obj = ELFFile(io.BytesIO(member))
    initialized = [s for s in obj.iter_sections() if s.name.startswith('.data') and s['sh_size']]
    if [s.name for s in initialized] != ['.data.subband_filter_coef']:
        raise ValueError('Audited object has unexpected initialized data')
    original = initialized[0].data()
    with elf_path.open('rb') as stream:
        elf = ELFFile(stream)
        symbols = elf.get_section_by_name('.symtab').get_symbol_by_name('subband_filter_coef') or []
        if len(symbols) != 1:
            raise ValueError('Expected exactly one local AEC source coefficient table')
        symbol = symbols[0]
        section = elf.get_section(symbol['st_shndx'])
        offset = symbol['st_value']-section['sh_addr']
        contents = section.data()[offset:offset+symbol['st_size']]
        verify_table(section.name, symbol['st_value'], symbol['st_size'], contents, original)
        print(f'AEC coefficient placement verified: {symbol["st_size"]} unchanged bytes in {section.name}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', choices=('echoear','p4xc'), default='echoear')
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--ar', required=True)
    parser.add_argument('--expected-sha256', required=True)
    args = parser.parse_args()
    check(args.elf, args.archive, args.ar, args.expected_sha256, args.board)
