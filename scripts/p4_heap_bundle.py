#!/usr/bin/env python3
"""Freeze only a verified opt-in P4 diagnostic demo; never pin or use hardware."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import p4_bundle
import sdkconfig_keydiff as keydiff
from p4_heap_tcp_values import tcp_values

def diagnostics(values):
    required={'CONFIG_DG_HEAP_ATTRIBUTION':'y','CONFIG_HEAP_TRACING_STANDALONE':'y',
        'CONFIG_ESP_SYSTEM_USE_FRAME_POINTER':'y','CONFIG_HEAP_TRACING_STACK_DEPTH':'8',
        'CONFIG_DG_DEMO_AUTOSTART':'y','CONFIG_DG_BOARD_P4XC':'y'}
    if any(values.get(key)!=value for key,value in required.items()) or values.get('CONFIG_DG_ECHOEAR_AEC','n')!='n':
        raise ValueError('P4 AEC-off diagnostic demo configuration required')
    return dict(capacity=4096,depth=8,mode='LEAKS',isr='excluded',round=2,new_caller_capacity=8192)

def main():
    if os.environ.get('DG_BUILD_LOCK_HELD')!='1':
        return subprocess.call([str(p4_bundle.ROOT/'scripts/build_locked.sh'),sys.executable,str(Path(__file__).resolve()),*sys.argv[1:]])
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--build',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();project=json.loads((args.build/'project_description.json').read_text())
    marker=diagnostics(keydiff.settings(Path(project['config_file'])))
    tcp=tcp_values(args.build.resolve())
    manifest=p4_bundle.bundle(args.build.resolve(),args.out.resolve(),p4_bundle.ROOT/'build/host-board/p4_audio_profile')
    manifest['heap_attribution']=marker
    manifest['heap_tcp_values']=tcp
    (args.out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    path=args.out/'board-provenance.json';provenance=json.loads(path.read_text())
    provenance['manifest_sha256']=p4_bundle.digest(args.out/'manifest.json');path.write_text(json.dumps(provenance,indent=2)+'\n')
    p4_bundle.verify_bundle(args.out,'p4xc')
    print(json.dumps(dict(bundle=str(args.out),elf_sha256=manifest['elf_sha256'],heap_attribution=marker)))
    return 0

if __name__=='__main__':raise SystemExit(main())
