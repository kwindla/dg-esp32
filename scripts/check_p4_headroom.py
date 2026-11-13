#!/usr/bin/env python3
"""Fail the P4 display link before a rev<3 pre-scheduler heap regression.

No config files are read. The limits derive from the working step-12b ELF's
119024-byte retention region, with a declared 4096-byte display/IRAM margin.
The 18 KiB and 384 KiB startup-stack regions do not count toward this budget.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

BASELINE_RETENTION_BYTES = 119024
DISPLAY_MARGIN_BYTES = 4096
MIN_RETENTION_BYTES = BASELINE_RETENTION_BYTES-DISPLAY_MARGIN_BYTES
# IDF 6.1 heap/port/esp32p4/memory_layout.c, CONFIG_ESP32P4_SELECTS_REV_LESS_V3:
# SOC_ROM_STACK_START - SOC_ROM_STACK_SIZE, as confirmed by the captured boot.
RETENTION_END = 0x4ff3afc0


def require(value, message):
    if not value: raise ValueError(message)


def evaluate(layout):
    require(layout['machine']=='EM_RISCV', 'P4 headroom check requires RISC-V ELF')
    heap=layout['heap_start_low']
    require(0x4ff00000 <= heap < RETENTION_END and layout['heap_start_high']==0x4ff40000,
            'unsupported P4 rev<3 memory layout; re-audit heap regions')
    available=RETENTION_END-heap
    require(available>=MIN_RETENTION_BYTES,
            f'P4 pre-main retention headroom {available} B below {MIN_RETENTION_BYTES} B; move static state out of internal RAM')
    require(layout['panel_storage_bytes']>0 and layout['panel_storage_section'].startswith('.ext_ram.') and
            layout['panel_storage_type']=='SHT_NOBITS', 'P4 panel/history/init storage must be external BSS')
    require(layout['dsi_isr_section']=='.iram0.text', 'P4 DSI DMA ISR lost IRAM placement')
    require(not layout['backend_global_constructor'], 'P4 display must not run a pre-main constructor')
    return dict(passed=True,retention_bytes=available,expected_boot_retention_kib=available//1024,
        baseline_12b_bytes=BASELINE_RETENTION_BYTES,declared_margin_bytes=DISPLAY_MARGIN_BYTES,
        minimum_bytes=MIN_RETENTION_BYTES,headroom_above_floor_bytes=available-MIN_RETENTION_BYTES,
        psram_panel_storage_bytes=layout['panel_storage_bytes'],dsi_isr_section=layout['dsi_isr_section'],
        basis='ELF heap-start symbols before startup-stack heaps are registered',
        runtime_boot_verified=False)


def constructor_sources(path, elf, symbols):
    """Resolve only the pointer ranges executed by IDF's __libc_init_array.

    .flash.init_array also holds memory reservations and ESP_SYSTEM_INIT_FN
    structs after __init_array_end; those words are not C++ constructors.
    Resolve inlined frames too, without depending on GCC initializer names.
    """
    require(elf.elfclass==32, 'P4 constructor check requires 32-bit ELF')
    addresses=[]
    for prefix in ('__preinit_array','__init_array'):
        require(prefix+'_start' in symbols and prefix+'_end' in symbols, 'missing P4 constructor bounds')
        start=int(symbols[prefix+'_start']['st_value']);end=int(symbols[prefix+'_end']['st_value'])
        require(start<=end and start%4==0 and (end-start)%4==0, 'invalid P4 constructor bounds')
        if start==end:continue
        sections=[s for s in elf.iter_sections() if s.name in ('.init_array','.flash.init_array','.preinit_array') and
                  s['sh_addr']<=start and end<=s['sh_addr']+s['sh_size']]
        require(len(sections)==1, 'P4 constructor bounds outside init array')
        section=sections[0];data=section.data()[start-section['sh_addr']:end-section['sh_addr']]
        addresses.extend(int.from_bytes(data[at:at+4],'little' if elf.little_endian else 'big') for at in range(0,len(data),4))
    require(addresses and all(address not in (0,0xffffffff) for address in addresses), 'invalid/empty P4 constructor table')
    tool=shutil.which('riscv32-esp-elf-addr2line') or shutil.which('addr2line')
    require(tool is not None, 'addr2line required for P4 constructor source audit')
    result=subprocess.run([tool,'-a','-i','-e',str(path),*[hex(address) for address in addresses]],
                          check=True,capture_output=True,text=True,timeout=60)
    rows=[]
    for line in result.stdout.splitlines():
        if re.fullmatch(r'0x[0-9a-fA-F]+',line):rows.append(dict(address=int(line,16),sources=[]))
        else:
            require(rows and line and not line.startswith('??'), 'unresolved P4 constructor source')
            rows[-1]['sources'].append(line.replace('\\','/'))
    require([row['address'] for row in rows]==addresses and all(row['sources'] for row in rows),
            'incomplete P4 constructor source audit')
    return rows


def inspect_elf(path):
    from elftools.elf.elffile import ELFFile
    with Path(path).open('rb') as stream:
        elf=ELFFile(stream);table=elf.get_section_by_name('.symtab')
        require(table is not None,'P4 ELF lacks symbol table')
        symbols={s.name:s for s in table.iter_symbols()}
        require('_heap_start_low' in symbols and '_heap_start_high' in symbols,'missing P4 rev<3 heap symbols')
        stores=[s for name,s in symbols.items() if name.endswith('panel_storageE') and 'dg_display' in name]
        require(len(stores)<=1,'ambiguous P4 panel storage symbol')
        store=stores[0] if stores else None
        section=elf.get_section(store['st_shndx']) if store is not None else None
        require('mipi_dsi_dma_trans_done_cb' in symbols,'missing P4 DSI DMA ISR')
        isr=elf.get_section(symbols['mipi_dsi_dma_trans_done_cb']['st_shndx'])
        constructors=constructor_sources(path,elf,symbols)
        layout=dict(machine=elf['e_machine'],heap_start_low=int(symbols['_heap_start_low']['st_value']),
            heap_start_high=int(symbols['_heap_start_high']['st_value']),panel_storage_bytes=int(store['st_size']) if store is not None else 0,
            panel_storage_section=section.name if section is not None else '',panel_storage_type=section['sh_type'] if section is not None else '',dsi_isr_section=isr.name,
            backend_global_constructor=any('/backends/jd9365_dsi/' in source for row in constructors for source in row['sources']))
    return dict(evaluate(layout),pre_main_constructors=constructors)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--elf',type=Path,required=True)
    parser.add_argument('--out',type=Path);args=parser.parse_args()
    try:
        result=inspect_elf(args.elf)
        result['elf_sha256']=hashlib.sha256(args.elf.read_bytes()).hexdigest()
    except (ValueError,KeyError,OSError,subprocess.SubprocessError) as exc: parser.exit(1,'P4 headroom guard: '+str(exc)+'\n')
    text=json.dumps(result,sort_keys=True)
    if args.out:
        with args.out.open('x') as stream:stream.write(text+'\n')
    print('P4_HEADROOM '+text)


if __name__=='__main__':main()
