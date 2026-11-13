#!/usr/bin/env python3
"""Decision 2026-11-01 P4 storage setup over frozen product-image helpers.

Usage: p4_product.py hil/p4_display.py live ... (or another listed helper).
No device IO on import/offline paths. The coordinator owns bench/R7/bot lifecycle.
"""
import argparse
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import importlib
import json
from pathlib import Path
import struct
import subprocess
import sys

DECISION = 'decision-2026-11-01-p4-storage-erase'
# Approved current P4 layout: compare with the authenticated *named* entry;
# commands below always use the entry's values, never unchecked addresses.
EXPECTED_STORAGE = (0x410000, 0x400000)
HELPERS = {f'hil/{name}.py': name for name in (
    'p4_display', 'p4_audio', 'p4_audio_fix4', 'p4_headless', 'board_smoke',
    'board_smoke_bounds', 'p4_contention')}
_active = ContextVar('p4_product_storage', default=None)


def require(value, message):
    if not value: raise ValueError(message)


def storage_partition(data):
    """IDF 6.1 gen_esp32part format: 32-byte entries, MD5 record, FF padding."""
    require(64 <= len(data) <= 0x1000 and len(data)%32 == 0, 'invalid partition table size')
    entries=[]; checked=False
    for at in range(0,min(len(data),0xc00),32):
        block=data[at:at+32]
        if block[:16] == b'\xeb\xeb'+b'\xff'*14:
            require(hashlib.md5(data[:at]).digest()==block[16:], 'partition table MD5 mismatch')
            require(data[at+32:]==b'\xff'*(len(data)-at-32), 'invalid partition table padding')
            checked=True;break
        require(block[:2]==b'\xaa\x50', 'missing partition table entry/MD5')
        _,kind,subtype,offset,size,label,flags=struct.unpack('<2sBBLL16sL',block)
        try:name=label.rstrip(b'\0').decode('ascii')
        except UnicodeDecodeError as exc:raise ValueError('invalid partition label') from exc
        require(size>0 and offset%0x1000==0 and size%0x1000==0 and offset+size<=0x2000000,
                'invalid partition bounds')
        entries.append(dict(name=name,type=kind,subtype=subtype,offset=offset,size=size,flags=flags))
    require(checked, 'partition table MD5 record required')
    matches=[row for row in entries if row['name']=='storage']
    require(len(matches)==1, 'bundle must contain exactly one named storage partition')
    storage=matches[0]
    require(storage['type']==1 and storage['subtype'] in (0x82,0x83) and storage['flags']==0,
            'storage partition type/subtype/flags mismatch')
    require((storage['offset'],storage['size'])==EXPECTED_STORAGE, 'storage offset/size mismatch')
    require(all(row is storage or row['offset']+row['size']<=storage['offset'] or
                storage['offset']+storage['size']<=row['offset'] for row in entries), 'storage overlaps another partition')
    return storage


def storage_plan(bundle):
    from make_bundle import verify_bundle, local_file, digest
    bundle=Path(bundle).resolve();manifest,flash=verify_bundle(bundle,'p4xc')
    tables=[(address,name) for address,name in flash['flash_files'].items() if Path(name).name=='partition-table.bin']
    require(len(tables)==1, 'bundle requires one partition-table flash input')
    address,name=tables[0]
    require(int(address,16)==0x8000, 'partition table flash offset mismatch')
    if 'partition-table' in flash:
        table=flash['partition-table']
        require(table.get('file')==name and int(table.get('offset','0'),16)==int(address,16), 'partition table metadata mismatch')
    path=local_file(bundle,name);partition=storage_partition(path.read_bytes())
    return dict(decision=DECISION,bundle=str(bundle),elf_sha256=manifest['elf_sha256'],
                manifest_sha256=digest(bundle/'manifest.json'),partition_table=name,
                partition_table_sha256=digest(path),partition=partition,flash=flash)


def prepare_storage(bundle, out, *, plan=None, runner=None):
    """Verify candidate -> erase named storage -> full FF readback, still in ROM.

    Caller must own an armed R7 hold. Errors propagate before opening/booting the
    board, leaving that hold armed. The writer only calls this with a fake tool.
    """
    import boards
    from board_smoke import verify_flash_mac
    from make_bundle import local_file
    plan=storage_plan(bundle) if plan is None else plan
    # Re-authenticate at use; a stale cached plan may not authorize a changed table.
    require(plan==storage_plan(bundle), 'storage plan changed before erase')
    out=Path(out);require(out.is_dir(), 'storage evidence directory must exist')
    fd=boards.require_bench();port=verify_flash_mac('p4xc')
    require(port==boards.BOARDS['p4xc']['by_id'], 'storage erase requires the pinned P4 by-id path')
    python=boards.checked_esptool_python();run=runner or subprocess.run
    base=[python,'-m','esptool','--chip','esp32p4','--port',port,'--baud','921600','--after','no-reset']
    files=[]
    for offset,name in plan['flash']['flash_files'].items():files += [offset,str(local_file(bundle,name))]
    partition=plan['partition'];offset=hex(partition['offset']);size=hex(partition['size'])
    readback=out/'candidate-storage-readback.bin'
    record=dict(decision=DECISION,elf_sha256=plan['elf_sha256'],manifest_sha256=plan['manifest_sha256'],
        partition_table=plan['partition_table'],partition_table_sha256=plan['partition_table_sha256'],
        partition=partition,port=port,flash_verified=False,erased=False,readback_verified=False,commands=[])
    destination=out/'candidate-storage-erase.json'
    with destination.open('x') as stream:stream.write(json.dumps(record,indent=2)+'\n')
    try:
        for phase,command in (
            ('verify',base+['verify-flash',*files]),
            ('erase',base+['erase-region',offset,size]),
            ('readback',base+['read-flash',offset,size,str(readback)])):
            record['commands'].append(command)
            with (out/('candidate-storage-'+phase+'.log')).open('w') as log:
                run(command,check=True,timeout=180,pass_fds=(fd,),stdout=log,stderr=subprocess.STDOUT)
            if phase=='verify':record['flash_verified']=True
            if phase=='erase':record['erased']=True
        data=readback.read_bytes()
        require(len(data)==partition['size'], 'storage readback size mismatch')
        require(data.count(0xff)==len(data), 'storage readback is not all 0xFF')
        record.update(readback_verified=True,readback_file=readback.name,verified_bytes=len(data),
                      readback_sha256=hashlib.sha256(data).hexdigest())
        return record
    except BaseException as exc:
        record['error']=type(exc).__name__+': '+str(exc);raise
    finally:
        destination.write_text(json.dumps(record,indent=2)+'\n')


@contextmanager
def candidate_storage(board, bundle, out):
    """Gate the first candidate serial open; covers even headless's direct flash.

    These frozen helpers import open_board inside their live/open methods. Only
    the candidate serial.log is intercepted; restore logs/other boards pass
    through. Nested use is idempotent, and restoration always restores the hook.
    """
    if board!='p4xc':yield;return
    key=(Path(bundle).resolve(),Path(out).resolve())
    if _active.get()==key:yield;return
    require(_active.get() is None, 'nested P4 storage policy has a different candidate')
    plan=storage_plan(key[0])  # Refuse bad/missing partition before any live IO.
    import board_serial
    original=board_serial.open_board;prepared=False
    def open_candidate(selected,*args,**kwargs):
        nonlocal prepared
        log=kwargs.get('log_path')
        if selected=='p4xc' and log is not None and Path(log).resolve()==key[1]/'serial.log' and not prepared:
            prepare_storage(key[0],key[1],plan=plan);prepared=True
        return original(selected,*args,**kwargs)
    token=_active.set(key);board_serial.open_board=open_candidate
    try:yield
    finally:board_serial.open_board=original;_active.reset(token)


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('helper',choices=HELPERS);parser.add_argument('arguments',nargs=argparse.REMAINDER)
    args=parser.parse_args(argv)
    options=argparse.ArgumentParser(add_help=False)
    options.add_argument('--board');options.add_argument('--bundle',type=Path);options.add_argument('--out',type=Path)
    selected,_=options.parse_known_args(args.arguments)
    helper=importlib.import_module(HELPERS[args.helper]);previous=sys.argv
    sys.argv=[args.helper,*args.arguments]
    try:
        if selected.board=='p4xc' and selected.bundle is not None and selected.out is not None:
            with candidate_storage(selected.board,selected.bundle,selected.out):return helper.main()
        return helper.main()
    finally:sys.argv=previous


if __name__=='__main__':raise SystemExit(main())
