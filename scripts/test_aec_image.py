"""AEC table contracts and real P4 ELF/archive mutation controls; no devices."""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import struct
import subprocess

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile
import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('aec_image',ROOT/'scripts/check-aec-image.py')
image=importlib.util.module_from_spec(spec);spec.loader.exec_module(image)


@pytest.fixture
def original(monkeypatch):
    data=bytes(range(256))*12
    monkeypatch.setattr(image,'P4_TABLE_SHA256',hashlib.sha256(data).hexdigest())
    return data


def test_s3_and_p4_are_separate_placement_contracts(original):
    image.verify_table('.flash.rodata',0x3c010000,3072,original,original)
    image.verify_p4_table('.flash.rodata',0x48010000,3072,original,original,3)
    with pytest.raises(ValueError):image.verify_table('.flash.rodata',0x48010000,3072,original,original)
    with pytest.raises(ValueError):image.verify_p4_table('.flash.rodata',0x3c010000,3072,original,original)


def test_relocation_audit_rejects_an_additional_allocated_reference():
    refs=[('.text.dios_ssp_share_subband_init',0xc0,26,0),('.text.dios_ssp_share_subband_init',0xc4,27,0)]
    image.verify_p4_references(refs)
    with pytest.raises(ValueError,match='runtime-reference audit changed'):
        image.verify_p4_references(refs+[('.data.escaped_pointer',0,1,0)])


def test_no_initialized_dram_duplicate(original):
    image.verify_no_dram_copy('.flash.rodata',0x48010000,original,original)
    image.verify_no_dram_copy('.dram0.data',0x4ff20000,b'other bytes',original)
    with pytest.raises(ValueError,match='internal SRAM copy'):
        image.verify_no_dram_copy('.dram0.data',0x4ff20000,b'prefix'+original,original)


@pytest.mark.parametrize('change',['section','internal','overflow','size','truncated','changed','executable','not-allocated'])
def test_p4_table_negative_contracts(original,change):
    section,address,size,contents,flags='.flash.rodata',0x48010000,3072,original,3
    if change=='section':section='.dram0.data'
    elif change=='internal':address=0x4ff20000
    elif change=='overflow':address=0x4bfffffc
    elif change=='size':size=3071
    elif change=='truncated':contents=contents[:-1]
    elif change=='changed':contents=b'X'+contents[1:]
    elif change=='executable':flags=6
    else:flags=0
    with pytest.raises(ValueError):image.verify_p4_table(section,address,size,contents,original,flags)


@pytest.fixture
def real():
    elf=Path(os.environ.get('P4_AEC_TEST_ELF',ROOT/'build-p4xc-aec/dg-esp32.elf'))
    archive=Path(os.environ.get('P4_AEC_TEST_ARCHIVE',ROOT/'spikes/p4_probe/managed_components/espressif__esp-sr/lib/esp32p4_less_v3_idf6/libesp_audio_processor.a'))
    ar=shutil.which('riscv32-esp-elf-ar')
    if not elf.is_file() or not archive.is_file() or not ar:
        pytest.skip('real-ELF controls unavailable: set P4_AEC_TEST_ELF/P4_AEC_TEST_ARCHIVE and export the IDF toolchain; pytest -ra lists these skips')
    return elf,archive,ar


def test_actual_pinned_p4_image_and_archive(real):
    elf,archive,ar=real
    image.check(elf,archive,ar,image.P4_ARCHIVE_SHA256,'p4xc')


@pytest.mark.parametrize('change',['machine','missing','duplicate','size','internal','contents','binding','initializer-iram'])
def test_actual_elf_mutations_are_rejected(real,tmp_path,change):
    path,archive,ar=real;data=bytearray(path.read_bytes());elf=ELFFile(io.BytesIO(data))
    table=elf.get_section_by_name('.symtab')
    index,symbol=next((i,s) for i,s in enumerate(table.iter_symbols()) if s.name=='subband_filter_coef')
    at=table['sh_offset']+index*table['sh_entsize'];section=elf.get_section(symbol['st_shndx'])
    if change=='machine':struct.pack_into('<H',data,18,94)  # EM_XTENSA
    elif change=='missing':struct.pack_into('<I',data,at,0)
    elif change=='duplicate':
        data[table['sh_offset']:table['sh_offset']+table['sh_entsize']]=data[at:at+table['sh_entsize']]
    elif change=='size':struct.pack_into('<I',data,at+8,3071)
    elif change=='internal':struct.pack_into('<I',data,at+4,0x4ff20000)
    elif change=='contents':data[section['sh_offset']+symbol['st_value']-section['sh_addr']]^=1
    elif change=='binding':data[at+12]=0x11  # STB_GLOBAL, STT_OBJECT
    else:
        i,s=next((i,s) for i,s in enumerate(table.iter_symbols()) if s.name=='dios_ssp_share_subband_init')
        iram=next(i for i,s in enumerate(elf.iter_sections()) if s.name=='.iram0.text')
        struct.pack_into('<H',data,table['sh_offset']+i*table['sh_entsize']+14,iram)
    bad=tmp_path/'bad.elf';bad.write_bytes(data)
    with pytest.raises((ValueError,ELFError)):image.check(bad,archive,ar,image.P4_ARCHIVE_SHA256,'p4xc')


def test_archive_hash_is_pinned_even_if_caller_supplies_a_replacement_hash(real,tmp_path):
    elf,archive,ar=real;data=bytearray(archive.read_bytes());data[-1]^=1
    bad=tmp_path/'bad.a';bad.write_bytes(data)
    with pytest.raises(ValueError,match='Unaudited P4'):
        image.check(elf,bad,ar,hashlib.sha256(data).hexdigest(),'p4xc')


def test_duplicate_archive_object_is_rejected(real,monkeypatch):
    elf,archive,ar=real;run=image.subprocess.run
    def duplicate(command,**kwargs):
        if command[1]=='t':return subprocess.CompletedProcess(command,0,stdout=(image.TABLE_OBJECT+'\n')*2)
        return run(command,**kwargs)
    monkeypatch.setattr(image.subprocess,'run',duplicate)
    with pytest.raises(ValueError,match='one P4 AEC coefficient object'):
        image.check(elf,archive,ar,image.P4_ARCHIVE_SHA256,'p4xc')


def test_unknown_board_refused(real):
    elf,archive,ar=real
    with pytest.raises(ValueError,match='Unknown AEC image board'):image.check(elf,archive,ar,image.P4_ARCHIVE_SHA256,'unknown')


def test_p4_image_cannot_pass_the_qualified_s3_contract(real):
    elf,archive,ar=real
    with pytest.raises(ValueError):image.check(elf,archive,ar,image.P4_ARCHIVE_SHA256,'echoear')
