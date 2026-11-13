"""Build and freeze the step-9 candidate on the host. Never flash a device.

Run from an exported ESP-IDF 6.1 environment. The isolated config is generated
from defaults each time so an old cached reference gain cannot override15dB.
Full configs contain credentials and stay in ignored build paths.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
from echoear_aec_bench import ROOT,sha,write,verify_bundle


def build(output):
    output=Path(output).resolve();output.mkdir(exist_ok=False,parents=True)
    build=ROOT/'build/accept';config=ROOT/'build/config-accept';defaults=ROOT/'build/accept.defaults'
    defaults.write_text('CONFIG_DG_ECHOEAR_AEC=y\nCONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB=15\nCONFIG_DG_DEMO_AUTOSTART=y\nCONFIG_DG_WIFI_OBSERVE=y\n')
    if config.exists():config.unlink() # task-owned config, never sdkconfig.local
    with (output/'build.log').open('w') as log:
        subprocess.run(['idf.py','-B',str(build),'-D',f'SDKCONFIG={config}','-D',
            'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.local;sdkconfig.defaults.echoear;sdkconfig.defaults.demo;build/accept.defaults','build'],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True)
    # Only approved non-secret fields are emitted to the manifest.
    fields={}
    for line in (build/'config/sdkconfig.h').read_text().splitlines():
        for key in ('CONFIG_DG_ECHOEAR_AEC','CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB','CONFIG_DG_DEMO_AUTOSTART','CONFIG_DG_WIFI_OBSERVE'):
            if line.startswith('#define '+key+' '):fields[key]=int(line.split()[-1])
    if fields!={'CONFIG_DG_ECHOEAR_AEC':1,'CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB':15,'CONFIG_DG_DEMO_AUTOSTART':1,'CONFIG_DG_WIFI_OBSERVE':1}:raise ValueError('Resolved candidate config mismatch')
    args=json.loads((build/'flasher_args.json').read_text())
    names=set(args['flash_files'].values())|{'dg-esp32.elf','flasher_args.json'}
    for name in names:
        target=output/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(build/name,target)
    files={name:dict(sha256=sha(output/name),bytes=(output/name).stat().st_size) for name in sorted(names)}
    manifest=dict(elf_sha256=sha(output/'dg-esp32.elf'),boot_ref_gain_db=15,config=fields,
        files=files,firmware_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        source_diff_sha256=__import__('hashlib').sha256(subprocess.check_output(['git','diff','HEAD'],cwd=ROOT)).hexdigest(),
        boot_verified=False,bench_used=False,expected_boot_line='MIC3_GAIN=0x15 analog_db=15')
    write(output/'manifest.json',manifest);verify_bundle(output,manifest['elf_sha256'],15)
    print(json.dumps(dict(image=str(output),elf_sha256=manifest['elf_sha256'],boot_ref_gain_db=15,bench_used=False)))
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    build(parser.parse_args().output)
