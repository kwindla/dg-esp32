#!/usr/bin/env python3
"""Bind a built spike to its actual AFE/peer archives; never reads sdkconfig."""
import argparse
import hashlib
import json
import re
from pathlib import Path


def version(lock, name):
    section=re.search(r'^  '+re.escape(name)+r':\n(.*?)(?=^  \S|\Z)',lock,re.M|re.S)
    if not section: raise ValueError(f'Missing dependency {name}')
    found=re.search(r'^    version: [\'"]?([^\'"\n]+)',section[1],re.M)
    if not found: raise ValueError(f'Missing version {name}')
    return found[1]


def collect(build):
    build=build.resolve();project=json.loads((build/'project_description.json').read_text())
    if project['target']!='esp32p4': raise ValueError('P4 build required')
    if Path(project['config_file']).resolve()!=build/'sdkconfig': raise ValueError('Isolated build config required')
    source=Path(project['project_path']);lock=(source/'dependencies.lock').read_text()
    if (version(lock,'espressif/esp-sr')!='2.4.7' or version(lock,'espressif/esp_peer')!='1.5.7' or
        version(lock,'espressif/esp_audio_codec')!='2.5.0'): raise ValueError('Production dependency pin mismatch (P4 codec exception: 2.5.0)')
    managed=source/'managed_components'
    afe=managed/'espressif__esp-sr/lib/esp32p4_less_v3_idf6/libesp_audio_processor.a'
    front=afe.with_name('libesp_audio_front_end.a')
    peer=managed/'espressif__esp_peer/libs/esp32p4/libpeer_default.a'
    codec=managed/'espressif__esp_audio_codec/lib/esp32p4/libesp_audio_codec.a'
    linkmap=(build/f"{project['project_name']}.map").read_text()
    bindings=[(afe,'aec_process'),(front,'afe_aec_create'),(peer,'esp_peer_get_default_impl')]
    bindings += [(codec,symbol) for symbol in ('esp_opus_enc_open','esp_opus_enc_process','esp_opus_enc_close',
                                              'esp_opus_dec_open','esp_opus_dec_decode','esp_opus_dec_reset','esp_opus_dec_close')]
    for archive,symbol in bindings:
        definition=r' \.text\.'+re.escape(symbol)+r'\s*\n[^\n]*'+re.escape(str(archive))+r'\('
        inclusion=re.escape(str(archive))+r'\([^\n]+\)\n[^\n]*\('+re.escape(symbol)+r'\)'
        if not re.search(definition,linkmap) and not re.search(inclusion,linkmap):
            raise ValueError(f'Expected linked archive/symbol binding absent: {archive.name} {symbol}')
    def identity(path): return dict(path=str(path),bytes=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    result=dict(target='esp32p4',esp_sr='2.4.7',esp_peer='1.5.7',afe_archive=identity(afe),afe_front_end_archive=identity(front),peer_archive=identity(peer),
                app=identity(build/f"{project['project_name']}.bin"),elf=identity(build/f"{project['project_name']}.elf"),map=identity(build/f"{project['project_name']}.map"))
    result['esp_audio_codec']='2.5.0'
    result['codec_archive']=identity(codec)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--build',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();result=collect(args.build);args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))


if __name__=='__main__': main()
