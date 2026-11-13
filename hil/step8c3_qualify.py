#!/usr/bin/env python3
"""Required serial step 8c3 gates; stop and preserve evidence at a failure."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from esp_serial import ROOT,EspSerial
from phase2_campaign import flash


def metadata_only_binary_change(old,new):
    if len(old)!=len(new) or len(old)<320:return False
    mutable=((48,80),(112,144),(176,208),(len(old)-33,len(old)))
    return all(a==b or any(lo<=i<hi for lo,hi in mutable)
               for i,(a,b) in enumerate(zip(old,new)))


def qualify(out):
    image=out/'images/step8c3-xip';elf=image/'dg-esp32.elf'
    # Revalidate the archived binary after any source cleanup made during CPU
    # analysis. Builds run outside measurement windows and cannot silently
    # substitute a new image for the image used by the CPU crossover.
    expected=hashlib.sha256(elf.read_bytes()).hexdigest()
    build=ROOT/'build/step8c/step8c3-xip'
    with (out/'candidate-revalidation-build.log').open('w') as log:
        subprocess.run([str(ROOT/'scripts/idf.sh'),'-B',str(build),
            '-DSDKCONFIG='+str(build/'sdkconfig'),
            '-DSDKCONFIG_DEFAULTS='+str(image/'sdkconfig.defaults')+';'+str(ROOT/'sdkconfig.local'),
            'build'],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True)
    actual=hashlib.sha256((build/'dg-esp32.elf').read_bytes()).hexdigest()
    # CMake regeneration recompiles esp_app_desc after the orchestrator commits
    # review/routing notes. Permit only version/time/ELF-hash metadata changes;
    # always flash the original immutable CPU image, never the rebuilt image.
    old_binary=(image/'dg-esp32.bin').read_bytes()
    new_binary=(build/'dg-esp32.bin').read_bytes()
    metadata_equal=metadata_only_binary_change(old_binary,new_binary)
    (out/'candidate-revalidation.json').write_text(json.dumps(dict(archived_elf_sha256=expected,
        rebuilt_elf_sha256=actual,identical=actual==expected,
        metadata_only_binary_change=metadata_equal,measured_image_retained=True),indent=2)+'\n')
    if actual!=expected and not metadata_equal:
        raise RuntimeError('Revalidation changed executable image bytes; retain evidence and stop')
    result=dict(completed=False,passed=False,image=str(image),checks={})
    def save():(out/'qualification.json').write_text(json.dumps(result,indent=2)+'\n')
    def command(label,args):
        print('START '+label,flush=True)
        with (out/(label+'-command.log')).open('w') as log:
            return subprocess.run([sys.executable,*map(str,args)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT).returncode
    try:
        for mode in ('offline','live','reconnect'):
            # A completed passing E2E population can retain its original result
            # when only the outer wrapper's evidence location was obsolete.
            audit_path=out/mode/'path-audit.json'
            if audit_path.exists():
                audit=json.loads(audit_path.read_text())
                original=json.loads((out/mode/'test/result.json').read_text())
                if not (audit['passed'] and audit['one_hop_path_verified'] and original['passed']):
                    raise RuntimeError('Existing population lacks a passing path audit')
                result['checks'][mode]=True;result[mode+'_basis']=str(audit_path);save()
                print(mode+': preserved 5/5; corrected-path audit passes',flush=True)
                continue
            code=command(mode,[ROOT/'hil/step8c_qualify.py','--out',out/mode,'--image',image,
                '--ethernet-host','192.168.42.75','--mode',mode])
            details=json.loads((out/mode/'result.json').read_text())
            result['checks'][mode]=code==0 and details['passed'];save()
            print(mode+': '+str(result['checks'][mode]),flush=True)
            if not result['checks'][mode]:return result
        flash(image,out/'owner-burst-flash.log')
        code=command('owner-burst',[ROOT/'hil/step8e_burst.py','--out',out/'owner-burst','--elf',elf])
        details=json.loads((out/'owner-burst/result.json').read_text())
        result['checks']['owner_burst']=details.get('completed',False) and details.get('owner_burst_gate',{}).get('passed',False)
        result['broader_rtp_burst_gate']=details.get('passed',False)
        result['owner_burst_scope']='Required gate is N=50 x3 with no owner interval >40 ms and complete trace. Broader max-60-ms RTP receipt gate is retained separately.'
        save();print('owner-burst: '+str(result['checks']['owner_burst']),flush=True)
        if not result['checks']['owner_burst']:return result
        flash(image,out/'network-flash.log')
        code=command('network',[ROOT/'hil/step8c_capture.py','--out',out/'network','--elf',elf,
            '--wifi-host','192.168.7.222','--ethernet-host','192.168.42.75','--duration','300',
            '--cpu-window','30','--fixed-mic','--gate','auto','--voice-path','ethernet','--label','step8c3-auto'])
        details=json.loads((out/'network/result.json').read_text())
        result['checks']['network_capture']=code==0 and details['passed'];save()
        print('network: '+str(result['checks']['network_capture']),flush=True)
        if not result['checks']['network_capture']:return result
        code=command('soak',[ROOT/'hil/step8c_qualify.py','--out',out/'soak','--image',image,
            '--ethernet-host','192.168.42.75','--mode','soak'])
        details=json.loads((out/'soak/result.json').read_text())
        result['checks']['soak']=code==0 and details['passed'];save()
        print('soak: '+str(result['checks']['soak']),flush=True)
        result['completed']=True;result['passed']=all(result['checks'].values())
    finally:save()
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();r=qualify(a.out.resolve());raise SystemExit(0 if r['passed'] else 1)
