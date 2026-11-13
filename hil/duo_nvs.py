"""Candidate-only NVS reset. No device IO until the coordinator calls erase."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess

from duo_identity import digest,require,write_new


def nvs_partition(data):
    # IDF gen_esp32part format, as in the frozen p4_product storage parser.
    require(64<=len(data)<=0x1000 and len(data)%32==0,'invalid partition table size')
    entries=[];checked=False
    for at in range(0,min(len(data),0xc00),32):
        block=data[at:at+32]
        if block[:16]==b'\xeb\xeb'+b'\xff'*14:
            require(hashlib.md5(data[:at]).digest()==block[16:],'partition table MD5 mismatch')
            require(data[at+32:]==b'\xff'*(len(data)-at-32),'invalid partition table padding')
            checked=True;break
        require(block[:2]==b'\xaa\x50','missing partition table entry/MD5')
        _,kind,subtype,offset,size,label,flags=struct.unpack('<2sBBLL16sL',block)
        try:name=label.rstrip(b'\0').decode('ascii')
        except UnicodeDecodeError as exc:raise ValueError('invalid partition label') from exc
        require(size>0 and offset%0x1000==0 and size%0x1000==0 and offset+size<=0x2000000,
                'invalid partition bounds')
        entries.append(dict(name=name,type=kind,subtype=subtype,offset=offset,size=size,flags=flags))
    require(checked,'partition table MD5 record required')
    matches=[row for row in entries if row['name']=='nvs']
    require(len(matches)==1,'bundle must contain exactly one named nvs partition')
    nvs=matches[0]
    require(nvs['type']==1 and nvs['subtype']==2 and nvs['flags']==0,'nvs partition type/subtype/flags mismatch')
    require(all(row is nvs or row['offset']+row['size']<=nvs['offset'] or
                nvs['offset']+nvs['size']<=row['offset'] for row in entries),'nvs overlaps another partition')
    return nvs


def nvs_plan(bundle,board):
    from duo_bundle import verify_bundle
    from make_bundle import local_file
    bundle=Path(bundle).resolve();manifest,flash=verify_bundle(bundle,board)
    tables=[(address,name) for address,name in flash['flash_files'].items() if Path(name).name=='partition-table.bin']
    require(len(tables)==1,'bundle requires one partition-table flash input')
    address,name=tables[0]
    if 'partition-table' in flash:
        meta=flash['partition-table']
        require(meta.get('file')==name and int(meta.get('offset','0'),16)==int(address,16),'partition table metadata mismatch')
    table=local_file(bundle,name);partition=nvs_partition(table.read_bytes())
    for offset,file in flash['flash_files'].items():
        start=int(offset,16);end=start+local_file(bundle,file).stat().st_size
        require(end<=partition['offset'] or partition['offset']+partition['size']<=start,
                'flash input overlaps nvs; candidate would repopulate erased settings')
    return dict(board=board,bundle=str(bundle),elf_sha256=manifest['elf_sha256'],
                manifest_sha256=digest(bundle/'manifest.json'),partition_table=name,
                partition_table_sha256=digest(table),partition=partition)


def erase_candidate_nvs(bundle,out,board,*,runner=None):
    """Erase only the verified named NVS, then prove full FF readback before flash.

    The existing restore hold must already be armed. Failed erase/readback is a
    normal exception, never a missed injection window eligible for retry.
    """
    import boards
    from board_smoke import verify_flash_mac
    plan=nvs_plan(bundle,board);out=Path(out)
    require(out.is_dir(),'NVS evidence directory must exist')
    fd=boards.require_bench();port=verify_flash_mac(board)
    require(port==boards.BOARDS[board]['by_id'],'NVS erase requires pinned board by-id path')
    python=boards.checked_esptool_python()
    require(plan==nvs_plan(bundle,board),'NVS plan changed before erase')
    base=[python,'-m','esptool','--chip',boards.BOARDS[board]['chip'],'--port',port,
          '--baud','460800' if board=='echoear' else '921600','--after','no-reset']
    partition=plan['partition'];offset=hex(partition['offset']);size=hex(partition['size'])
    readback=out/'candidate-nvs-readback.bin';destination=out/'candidate-nvs-erase.json'
    record=dict(**plan,port=port,erased=False,readback_verified=False,passed=False,commands=[])
    write_new(destination,record);run=runner or subprocess.run
    try:
        for phase,command in (('erase',base+['erase-region',offset,size]),
                              ('readback',base+['read-flash',offset,size,str(readback)])):
            row=dict(phase=phase,command=command,completed=False);record['commands'].append(row)
            with (out/('candidate-nvs-'+phase+'.log')).open('x') as log:
                run(command,check=True,timeout=180,pass_fds=(fd,),stdout=log,stderr=subprocess.STDOUT)
            row['completed']=True
            if phase=='erase':record['erased']=True
        data=readback.read_bytes()
        record.update(readback_file=readback.name,readback_sha256=hashlib.sha256(data).hexdigest(),
                      readback_bytes=len(data),non_ff_bytes=len(data)-data.count(0xff))
        require(len(data)==partition['size'],'NVS readback size mismatch')
        require(record['non_ff_bytes']==0,'NVS readback is not all 0xFF')
        record.update(readback_verified=True,passed=True)
        return record
    except BaseException as exc:
        record['error']=type(exc).__name__+': '+str(exc);raise
    finally:destination.write_text(json.dumps(record,indent=2)+'\n')
