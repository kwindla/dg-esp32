#!/usr/bin/env python3
"""Duo bundle adapter. Only approved grep counts inspect resolved configuration."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

import make_bundle as base
from duo_identity import require,write_new

ROOT=Path(__file__).resolve().parents[1]


def image_verify(directory,board):
    from compare_masked_image import Image,ElfIndex
    image=Image.parse((Path(directory)/'dg-esp32.bin').read_bytes())
    require(int.from_bytes(image.data[12:14],'little')=={'echoear':9,'p4xc':18}[board],'image chip differs')
    index=ElfIndex(Path(directory)/'dg-esp32.elf',image)
    names=[row['name'] for row in index.symbols]
    require(any('DuoBoot' in name for name in names) and any('dg_duo' in name for name in names),'duo firmware symbols missing')
    return dict(elf_sha256=index.sha256,checksum_verified=True,duo_symbols=True)


def verify_bundle(directory,board='echoear',*,image_check=image_verify):
    directory=Path(directory).resolve();m=json.loads((directory/'manifest.json').read_text())
    require(board in base.CHIPS and m.get('board')==board and m.get('target')==base.CHIPS[board],'bundle board/target differs')
    require(m.get('profile')=='duo' and m.get('aec_enabled') is True and m.get('autostart') is True,'duo requires AEC and autostart')
    require(m.get('config',{}).get('CONFIG_DG_DUO')==1 and m['config'].get('CONFIG_DG_ECHOEAR_AEC')==1 and
            m['config'].get('CONFIG_DG_DEMO_AUTOSTART')==1,'duo configuration metadata missing')
    hashes=m.get('sha256',{});files=m.get('files',{})
    require({'flasher_args.json','dg-esp32.elf','dg-esp32.bin'}<=hashes.keys(),'missing image hashes')
    require(hashes=={name:row['sha256'] for name,row in files.items()},'bundle files/hashes disagree')
    for name,sha in hashes.items():
        require(base.digest(base.local_file(directory,name))==sha and
                base.local_file(directory,name).stat().st_size==files[name]['bytes'],'bundle hash/size differs: '+name)
    require(m['elf_sha256']==hashes['dg-esp32.elf'],'ELF identity differs')
    import re
    require(re.fullmatch('[0-9a-f]{40}',m.get('firmware_commit','')),'firmware commit missing')
    flash=base.flash_files(directory)
    require(set(flash['flash_files'].values())<=hashes.keys() and 'dg-esp32.bin' in flash['flash_files'].values(),'unhashed/missing flash input')
    require(flash.get('extra_esptool_args',{}).get('chip')==base.CHIPS[board],'flash chip differs')
    require(m.get('flash_args')==flash,'flash metadata differs')
    gain=m.get('boot_ref_gain_db')
    require(type(gain) is int and gain in range(0,34,3) and
            m['config'].get('CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB')==gain,'AEC gain metadata missing')
    require(m.get('expected_boot_line')==f'MIC3_GAIN=0x{0x10+gain//3:02x} analog_db={gain}','AEC boot metadata differs')
    result=image_check(directory,board)
    require(result['elf_sha256']==m['elf_sha256'],'image ELF binding differs')
    # These source/dependency hashes describe the snapshot recorded by the
    # builder. They are not independently reconstructed from the firmware.
    m=dict(m,provenance_status='recorded-only')
    return m,flash


def make_bundle(build,output,board):
    build=Path(build).resolve();output=Path(output).resolve()
    require(os.environ.get('DG_BUILD_LOCK_HELD')=='1','run through scripts/build_locked.sh')
    project=json.loads((build/'project_description.json').read_text())
    require(Path(project['project_path']).resolve()==ROOT and project['target']==base.CHIPS[board],'build worktree/target differs')
    require(project['app_elf']=='dg-esp32.elf' and project['app_bin']=='dg-esp32.bin','unexpected application artifacts')
    version=subprocess.check_output(['git','describe','--always','--tags','--dirty'],cwd=ROOT,text=True).strip()
    require(project['project_version'].removesuffix('-dirty')==version.removesuffix('-dirty'),'stale build version')
    inputs=subprocess.check_output(['git','ls-files','main','components','third_party','boards','cmake','CMakeLists.txt',
                                  'partitions.csv','sdkconfig.defaults*','hil/fixtures/echoear360'],cwd=ROOT,text=True).splitlines()
    require(not any((ROOT/p).is_file() and (ROOT/p).stat().st_mtime_ns>(build/'dg-esp32.elf').stat().st_mtime_ns for p in inputs),
            'firmware input newer than ELF')
    config=Path(project['config_file'])
    for key in ('DG_DUO','DG_ECHOEAR_AEC','DG_DEMO_AUTOSTART','DG_BOARD_'+board.upper()):
        require(base.config_enabled(config,key),'resolved config missing '+key)
    gain=base.config_gain(config);flash=base.flash_files(build)
    names=set(flash['flash_files'].values())|{'dg-esp32.elf','flasher_args.json'}
    require('dg-esp32.bin' in names,'application missing from flash map')
    # Inspect/bind the actual app and ELF before copying any bytes.
    image_verify(build,board)
    output.mkdir(parents=True,exist_ok=False)
    for name in names:
        target=base.local_file(output,name);target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(base.local_file(build,name),target)
    hashes={name:base.digest(output/name) for name in sorted(names)}
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    lock='dependencies.p4xc.lock' if board=='p4xc' else 'dependencies.lock'
    # Hash source inputs for provenance, excluding every sdkconfig-named file.
    source={p:base.digest(ROOT/p) for p in inputs if (ROOT/p).is_file() and not Path(p).name.startswith('sdkconfig')}
    lock_sha=base.digest(ROOT/lock)
    m=dict(schema=1,profile='duo',board=board,target=base.CHIPS[board],firmware_commit=commit,commit=commit,
           aec_enabled=True,autostart=True,elf_sha256=hashes['dg-esp32.elf'],sha256=hashes,
           files={name:dict(sha256=sha,bytes=(output/name).stat().st_size) for name,sha in hashes.items()},flash_args=flash,
           config=dict(CONFIG_DG_DUO=1,CONFIG_DG_ECHOEAR_AEC=1,CONFIG_DG_DEMO_AUTOSTART=1,
                       CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB=gain),boot_ref_gain_db=gain,
           expected_boot_line=f'MIC3_GAIN=0x{0x10+gain//3:02x} analog_db={gain}',
           project_version=version,build_dir=str(build),boot_verified=False,bench_used=False,
           provenance_status='recorded-only',source_files_sha256=source,target_dependencies_lock=lock,target_dependencies_lock_sha256=lock_sha,dependencies_lock_sha256=lock_sha,
           config_basis='Only grep -c of approved booleans and gain grid; no private configs copied')
    write_new(output/'manifest.json',m);verify_bundle(output,board);return m


@contextmanager
def frozen_adapters():
    """Route duo inputs only; preserve frozen legacy/R7 verification unchanged."""
    import board_smoke
    original=base.verify_bundle
    def verify(directory,board='echoear',reference_only=False):
        m=json.loads((Path(directory)/'manifest.json').read_text())
        if m.get('profile')=='duo':
            require(not reference_only,'duo cannot be a legacy reference');return verify_bundle(directory,board)
        return original(directory,board,reference_only)
    with patch.object(base,'verify_bundle',verify),patch.object(board_smoke,'verify_bundle',verify):yield


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('make','verify'))
    p.add_argument('--board',choices=base.CHIPS,required=True);p.add_argument('--bundle',type=Path,required=True)
    p.add_argument('--build',type=Path);a=p.parse_args(argv)
    if a.action=='make':
        require(a.build is not None,'--build required')
        if os.environ.get('DG_BUILD_LOCK_HELD')!='1':
            return subprocess.call([str(ROOT/'scripts/build_locked.sh'),sys.executable,str(Path(__file__).resolve()),*(sys.argv[1:] if argv is None else argv)])
        m=make_bundle(a.build,a.bundle,a.board)
    else:m,_=verify_bundle(a.bundle,a.board)
    print(json.dumps(dict(profile=m['profile'],board=m['board'],elf_sha256=m['elf_sha256'],provenance_status='recorded-only')));return 0


if __name__=='__main__':raise SystemExit(main())
