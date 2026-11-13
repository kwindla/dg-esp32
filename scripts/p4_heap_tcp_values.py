#!/usr/bin/env python3
"""Compile an allow-listed constant probe using the diagnostic build's flags.

Only numeric TCP values are exported. The compiler includes sdkconfig.h;
neither this script nor its output reads/displays that file's contents.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile

def probe_arguments(command):
    tokens=shlex.split(command);result=[];skip=False
    for token in tokens:
        if skip:skip=False;continue
        if token in ('-o','-MF','-MT','-MQ'):skip=True;continue
        if token in ('-c','-MD','-MMD') or token.endswith('/heap_tcp_state.cpp'):continue
        result.append(token)
    return result

def tcp_values(build):
    from elftools.elf.elffile import ELFFile
    rows=json.loads((build/'compile_commands.json').read_text())
    row=next(r for r in rows if r['file'].endswith('/main/heap_tcp_state.cpp'))
    temporary=Path(os.environ.get('TMPDIR') or Path.home()/'.cache/gap3-tmp')
    temporary.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='heap-tcp-values-',dir=temporary) as work:
        work=Path(work);source=work/'probe.cpp';obj=work/'probe.o'
        source.write_text('#include "sdkconfig.h"\n#include "lwip/priv/tcp_priv.h"\n'
            'extern "C" const unsigned dg_heap_tcp_values[]={CONFIG_LWIP_TCP_MSL,TCP_MSL,2*TCP_MSL,MEMP_NUM_TCP_PCB,CONFIG_LWIP_MAX_ACTIVE_TCP};\n')
        result=subprocess.run([*probe_arguments(row['command']),'-c',str(source),'-o',str(obj)],
            cwd=row['directory'],capture_output=True,text=True,timeout=30)
        if result.returncode:raise ValueError('TCP constant probe failed; compiler diagnostics suppressed to protect config inputs')
        with obj.open('rb') as stream:
            elf=ELFFile(stream);symbols=elf.get_section_by_name('.symtab').get_symbol_by_name('dg_heap_tcp_values')
            if not symbols or len(symbols)!=1:raise ValueError('TCP constant symbol missing')
            symbol=symbols[0];section=elf.get_section(symbol['st_shndx']);offset=symbol['st_value']-section['sh_addr']
            values=struct.unpack(('<' if elf.little_endian else '>')+'5I',section.data()[offset:offset+20])
    if values[0]!=values[1] or values[2]!=2*values[1] or values[3]!=values[4]:raise ValueError('lwIP/config constants differ')
    return dict(TCP_MSL_ms=values[1],TIME_WAIT_ms=values[2],MEMP_NUM_TCP_PCB=values[3])

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--build',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    with args.out.open('x') as stream:stream.write(json.dumps(tcp_values(args.build.resolve()),indent=2)+'\n')
