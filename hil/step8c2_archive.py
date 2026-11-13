#!/usr/bin/env python3
"""Rebuild the archived 8c QIO/DIO contexts with WiFi observability fixed.

The original immutable archives remain untouched. The only source changes
are the 8e previous-log-level fix and INFO logging in these probe-only images.
This is necessary because original archived binaries permanently mute WiFi.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from esp_serial import ROOT
from step8c2_campaign import save

def prepare(out):
    work=ROOT/'build/step8c2-archive-source'
    work.mkdir(parents=True,exist_ok=False)
    archive=subprocess.check_output(['git','archive','0ec2708'],cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:tar.extractall(work,filter='data')
    source=work/'main/console.cpp';text=source.read_text()
    old='esp_log_level_set("wifi", ESP_LOG_ERROR);'
    assert text.count(old)==1
    text=text.replace(old,'esp_log_level_set("wifi", ESP_LOG_INFO);')
    old='esp_log_level_set("wifi",ESP_LOG_INFO);\n                        auto error='
    assert text.count(old)==1
    text=text.replace(old,'auto previous_level=esp_log_level_get("wifi");\n                        esp_log_level_set("wifi",ESP_LOG_INFO);\n                        auto error=')
    text=text.replace('esp_log_level_set("wifi",ESP_LOG_ERROR);','esp_log_level_set("wifi",previous_level);')
    source.write_text(text)
    sys.path.insert(0,str(ROOT/'scripts'))
    from importlib import import_module
    settings=import_module('check-sdkconfig').settings
    base=settings(work/'sdkconfig.defaults')
    for arm,original in [('qio','final'),('dio','final-dio-bisect')]:
        image=out/'images'/('archive-'+arm);image.mkdir(parents=True,exist_ok=False)
        changes=json.loads((ROOT.parent/'evidence/step8c/images'/original/'candidate-check.json').read_text())['expected_public_settings']
        requested={**base,**{k:k+'='+v for k,v in changes.items()}}
        defaults=image/'sdkconfig.defaults';defaults.write_text('\n'.join(requested.values())+'\n')
        directory=ROOT/'build'/('step8c2-archive-'+arm);config=directory/'sdkconfig'
        with (image/'build.log').open('w') as log:
            subprocess.run([str(ROOT/'scripts/idf.sh'),'-B',str(directory),'-DSDKCONFIG='+str(config),
                '-DSDKCONFIG_DEFAULTS='+str(defaults)+';'+str(ROOT/'sdkconfig.local'),'build'],cwd=work,
                stdout=log,stderr=subprocess.STDOUT,check=True,timeout=900)
        actual=settings(config)
        diff=[k for k,line in requested.items() if actual.get(k)!=line]
        save(image/'config-check.json',dict(differences=diff,requested=len(requested),private_values_exported=False))
        if diff:raise RuntimeError('Archive configuration mismatch: '+', '.join(diff))
        for name,relative in [('dg-esp32.bin','dg-esp32.bin'),('dg-esp32.elf','dg-esp32.elf'),('dg-esp32.map','dg-esp32.map'),
            ('bootloader.bin','bootloader/bootloader.bin'),('partition-table.bin','partition_table/partition-table.bin')]:
            shutil.copy2(directory/relative,image/name)
        # Public source archive excludes generated build files and credentials.
        (image/'base-source.tar').write_bytes(archive)
        (image/'console-with-wifi-fix.cpp').write_text(text)
        save(image/'identity.json',dict(base='0ec2708',archived_context=original,public_settings=changes,
            source_changes=['WiFi INFO at boot for probe-only test','Restore previous WiFi log level after statistics'],
            hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in image.glob('*.bin')},
            elf_sha256=hashlib.sha256((image/'dg-esp32.elf').read_bytes()).hexdigest()))
        print(arm+': patched archive built',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    prepare(a.out.resolve())
