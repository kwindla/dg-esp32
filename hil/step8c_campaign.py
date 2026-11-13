#!/usr/bin/env python3
"""Build immutable step-8c images, then run serialized fresh-boot captures.

Build and capture are separate commands: build all images outside measurement
windows. sdkconfig files containing local credentials stay in ignored build/;
only public defaults, hashes and the validated setting counts enter evidence.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from esp_serial import ROOT
from phase2_campaign import flash
from step8c_analysis import summarize, summarize_cpu

VARIANTS = {
    'observer': {},
    'mbox16': {'CONFIG_LWIP_UDP_RECVMBOX_SIZE': '16'},
    'mbox32': {'CONFIG_LWIP_UDP_RECVMBOX_SIZE': '32'},
    'observer-v2': {},
    'mbox32-v2': {'CONFIG_LWIP_UDP_RECVMBOX_SIZE': '32'},
    'tx24': {'CONFIG_ESP_WIFI_STATIC_TX_BUFFER_NUM': '24'},
    # Compare these with mbox32-v2. The mailbox is held at 32 throughout
    # CPU/flash experiments; it is not part of the flash-mode treatment.
    'qio80': {'CONFIG_LWIP_UDP_RECVMBOX_SIZE': '32',
              'CONFIG_ESPTOOLPY_FLASHMODE_DIO': 'n', 'CONFIG_ESPTOOLPY_FLASHMODE_QIO': 'y'},
    'qio40': {'CONFIG_LWIP_UDP_RECVMBOX_SIZE': '32',
              'CONFIG_ESPTOOLPY_FLASHMODE_DIO': 'n', 'CONFIG_ESPTOOLPY_FLASHMODE_QIO': 'y',
              'CONFIG_ESPTOOLPY_FLASHFREQ_80M': 'n', 'CONFIG_ESPTOOLPY_FLASHFREQ_40M': 'y',
              'CONFIG_ESPTOOLPY_FLASHFREQ': '"40m"'},
    'dio40': {'CONFIG_LWIP_UDP_RECVMBOX_SIZE': '32',
              'CONFIG_ESPTOOLPY_FLASHFREQ_80M': 'n', 'CONFIG_ESPTOOLPY_FLASHFREQ_40M': 'y',
              'CONFIG_ESPTOOLPY_FLASHFREQ': '"40m"'},
    'hotiram': {'CONFIG_LWIP_UDP_RECVMBOX_SIZE': '32', 'CONFIG_DG_OPUS_ENCODER_HOT_IRAM': 'y'},
    # Hold stack capacity equal in both 20/40 ms comparison arms. The 40 ms
    # encoder screen exceeded the existing 25% margin at 29,696 bytes.
    'frame40-stack': {'CONFIG_LWIP_UDP_RECVMBOX_SIZE': '32',
                      'CONFIG_DG_AUDIO_TX_STACK_BYTES': '32768'},
    # Production comparison keeps the pre-8c performance settings explicit
    # even after measured candidates are adopted in sdkconfig.defaults.
    'final-control': {'CONFIG_LWIP_UDP_RECVMBOX_SIZE': '6',
                      'CONFIG_ESPTOOLPY_FLASHMODE_DIO': 'y',
                      'CONFIG_ESPTOOLPY_FLASHMODE_QIO': 'n',
                      'CONFIG_ESPTOOLPY_FLASHFREQ_80M': 'y',
                      'CONFIG_ESPTOOLPY_FLASHFREQ_40M': 'n',
                      'CONFIG_ESPTOOLPY_FLASHFREQ': '"80m"',
                      'CONFIG_DG_PEER_JITTER_TIMEOUT_MS': '100',
                      'CONFIG_DG_PEER_OWNER_PRIORITY': '6',
                      'CONFIG_DG_PEER_DIRECT_SOCKET_SELECT': 'n',
                      'CONFIG_DG_PEER_RECV_TIMEOUT_MS': '10',
                      'CONFIG_DG_PEER_LOOP_DELAY_MS': '1',
                      'CONFIG_DG_AUDIO_TX_STACK_BYTES': '29696',
                      'CONFIG_DG_OPUS_UPLINK_FRAME40': 'n',
                      'CONFIG_DG_OPUS_ENCODER_HOT_IRAM': 'n'},
    'final': {},
}

# Immutable archived contexts from before the 18:30 convergence instruction.
# Retained mailbox/cache populations pass its corrected materiality guard.
_six_dio80 = {'CONFIG_LWIP_UDP_RECVMBOX_SIZE': '6',
              'CONFIG_ESPTOOLPY_FLASHMODE_DIO': 'y',
              'CONFIG_ESPTOOLPY_FLASHMODE_QIO': 'n',
              'CONFIG_ESPTOOLPY_FLASHFREQ_80M': 'y',
              'CONFIG_ESPTOOLPY_FLASHFREQ_40M': 'n',
              'CONFIG_ESPTOOLPY_FLASHFREQ': '"80m"'}
_six_qio80 = {**_six_dio80,'CONFIG_ESPTOOLPY_FLASHMODE_DIO':'n',
             'CONFIG_ESPTOOLPY_FLASHMODE_QIO':'y'}
VARIANTS.update({
    'dio80-six-v3': dict(_six_dio80),
    'qio80-six-v3': dict(_six_qio80),
    'qio40-six-v3': {**_six_qio80,'CONFIG_ESPTOOLPY_FLASHFREQ_80M':'n',
                     'CONFIG_ESPTOOLPY_FLASHFREQ_40M':'y',
                     'CONFIG_ESPTOOLPY_FLASHFREQ':'"40m"'},
    'qio80-mbox32-v3': {**_six_qio80,'CONFIG_LWIP_UDP_RECVMBOX_SIZE':'32'},
    'qio80-frame40-six-v3': {**_six_qio80,'CONFIG_DG_AUDIO_TX_STACK_BYTES':'32768'},
    'qio80-frame40-mbox32-v3': {**_six_qio80,'CONFIG_LWIP_UDP_RECVMBOX_SIZE':'32',
                               'CONFIG_DG_AUDIO_TX_STACK_BYTES':'32768'},
    'dio80-frame40-six-v3': {**_six_dio80,'CONFIG_DG_AUDIO_TX_STACK_BYTES':'32768'},
})


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def archive_root(out, *, label='final', flash_qio=None):
    """Archive the actual production root build that the orchestrator checks.

    This does not build or flash anything. An isolated final build can have
    a different ELF identity; retain the root image itself for qualification.
    """
    sys.path.insert(0,str(ROOT/'scripts'))
    from importlib import import_module
    settings=import_module('check-sdkconfig').settings
    image=out/'images'/label
    image.mkdir(parents=True,exist_ok=False)
    requested=settings(ROOT/'sdkconfig.defaults')
    production_frame40=requested.get('CONFIG_DG_OPUS_UPLINK_FRAME40')=='CONFIG_DG_OPUS_UPLINK_FRAME40=y'
    if flash_qio is None:
        flash_qio=requested.get('CONFIG_ESPTOOLPY_FLASHMODE_QIO')=='CONFIG_ESPTOOLPY_FLASHMODE_QIO=y'
    private=set()
    local=ROOT/'sdkconfig.local'
    if local.exists():
        values=settings(local);requested.update(values);private=set(values)
    actual=settings(ROOT/'sdkconfig')
    # A local override must not silently turn the final adopted combination
    # back into a different image, even if the generic defaults check passes.
    candidate={
        'CONFIG_ESPTOOLPY_FLASHMODE_QIO':'y' if flash_qio else 'n',
        'CONFIG_ESPTOOLPY_FLASHMODE_DIO':'n' if flash_qio else 'y',
        'CONFIG_ESPTOOLPY_FLASHFREQ':'"80m"','CONFIG_LWIP_UDP_RECVMBOX_SIZE':'32',
        'CONFIG_DG_PEER_JITTER_TIMEOUT_MS':'200','CONFIG_DG_PEER_DIRECT_SOCKET_SELECT':'y',
        'CONFIG_DG_PEER_RECV_TIMEOUT_MS':'20','CONFIG_DG_OPUS_UPLINK_FRAME40':'y' if production_frame40 else 'n',
        'CONFIG_DG_AUDIO_TX_STACK_BYTES':'32768',
    }
    mismatched=[key for key,value in candidate.items() if actual.get(key)!=key+'='+value]
    write_json(image/'candidate-check.json',dict(expected_public_settings=candidate,differences=mismatched,
                                                private_values_exported=False))
    if mismatched:raise RuntimeError('Root differs from qualified candidate: '+', '.join(mismatched))
    differences=[key for key,line in requested.items() if actual.get(key)!=line]
    if actual.get('CONFIG_LWIP_STATS')!='CONFIG_LWIP_STATS=n' and 'CONFIG_LWIP_STATS' not in differences:
        differences.append('CONFIG_LWIP_STATS')
    write_json(image/'config-check.json',dict(requested=len(requested),differences=differences,
                                             private_values_exported=False))
    if differences:raise RuntimeError('Root settings differ: '+', '.join(differences))
    directory=ROOT/'build'
    for name,relative in [('dg-esp32.bin','dg-esp32.bin'),('dg-esp32.elf','dg-esp32.elf'),
                          ('dg-esp32.map','dg-esp32.map'),('bootloader.bin','bootloader/bootloader.bin'),
                          ('partition-table.bin','partition_table/partition-table.bin')]:
        shutil.copy2(directory/relative,image/name)
    hashes={file.name:hashlib.sha256(file.read_bytes()).hexdigest() for file in image.iterdir()
            if file.suffix in ('.bin','.elf')}
    write_json(image/'identity.json',dict(hashes=hashes,root_build=True,bench_statistics=False,
                                         private_setting_count=len(private)))
    platform_keys=('CONFIG_ESPTOOLPY_FLASHMODE','CONFIG_ESPTOOLPY_FLASHMODE_QIO',
        'CONFIG_ESPTOOLPY_FLASHMODE_DIO','CONFIG_ESPTOOLPY_FLASHFREQ','CONFIG_SPIRAM_MODE_OCT',
        'CONFIG_SPIRAM_SPEED','CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ','CONFIG_LWIP_UDP_RECVMBOX_SIZE',
        'CONFIG_LWIP_STATS','CONFIG_DG_OPUS_ENCODER_HOT_IRAM')
    write_json(image/'platform-settings.json',{key:actual[key].split('=',1)[1] for key in platform_keys})
    print('final: actual production root image archived; settings verified',flush=True)


def build_variant(out, name, *, bench=True):
    sys.path.insert(0, str(ROOT / 'scripts'))
    from importlib import import_module
    settings = import_module('check-sdkconfig').settings
    image = out / 'images' / name
    image.mkdir(parents=True, exist_ok=False)
    requested = settings(ROOT / 'sdkconfig.defaults')
    if bench:
        requested.update(settings(ROOT / 'hil/step8c-sdkconfig.defaults'))
    requested.update({key: key + '=' + value for key, value in VARIANTS[name].items()})
    defaults = image / 'sdkconfig.defaults'
    defaults.write_text('\n'.join(requested.values()) + '\n')
    directory = ROOT / 'build' / 'step8c' / name
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / 'sdkconfig'
    if config.exists():
        raise RuntimeError('Refusing to reuse generated variant configuration: ' + name)
    sources = [str(defaults)]
    local = ROOT / 'sdkconfig.local'
    private = set()
    if local.exists():
        sources.append(str(local))
        local_settings = settings(local)
        requested.update(local_settings)
        private = set(local_settings)
    with (image / 'build.log').open('w') as stream:
        subprocess.run([str(ROOT / 'scripts/idf.sh'), '-B', str(directory),
                        '-DSDKCONFIG=' + str(config), '-DSDKCONFIG_DEFAULTS=' + ';'.join(sources),
                        'build'], cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=600)
    actual = settings(config)
    differences = [key for key, line in requested.items() if actual.get(key) != line]
    differences += [key for key,value in VARIANTS[name].items() if actual.get(key)!=key+'='+value and key not in differences]
    stats='CONFIG_LWIP_STATS='+('y' if bench else 'n')
    if actual.get('CONFIG_LWIP_STATS')!=stats and 'CONFIG_LWIP_STATS' not in differences:
        differences.append('CONFIG_LWIP_STATS')
    write_json(image / 'config-check.json', dict(requested=len(requested), differences=differences,
                                               private_values_exported=False))
    if differences:
        raise RuntimeError('Variant settings differ: ' + ', '.join(differences))
    for file in ('dg-esp32.bin', 'dg-esp32.elf', 'dg-esp32.map'):
        shutil.copy2(directory / file, image / file)
    shutil.copy2(directory / 'bootloader/bootloader.bin', image / 'bootloader.bin')
    shutil.copy2(directory / 'partition_table/partition-table.bin', image / 'partition-table.bin')
    hashes = {file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in image.glob('*.bin')}
    hashes['dg-esp32.elf'] = hashlib.sha256((image / 'dg-esp32.elf').read_bytes()).hexdigest()
    write_json(image / 'identity.json', dict(hashes=hashes, public_changes=VARIANTS[name],
                                            bench_statistics=bench,
                                            private_setting_count=len(private)))
    # Explicit public allowlist: generated configuration also contains local
    # credentials and must never be copied wholesale into evidence.
    platform_keys = (
        'CONFIG_ESPTOOLPY_FLASHMODE', 'CONFIG_ESPTOOLPY_FLASHMODE_QIO',
        'CONFIG_ESPTOOLPY_FLASHMODE_DIO', 'CONFIG_ESPTOOLPY_FLASHFREQ',
        'CONFIG_SPIRAM_MODE_OCT', 'CONFIG_SPIRAM_SPEED',
        'CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ', 'CONFIG_LWIP_UDP_RECVMBOX_SIZE',
        'CONFIG_LWIP_STATS', 'CONFIG_DG_OPUS_ENCODER_HOT_IRAM',
    )
    write_json(image / 'platform-settings.json',
               {key: actual[key].split('=', 1)[1] for key in platform_keys})
    print(name + ': built and settings verified', flush=True)


def block(out, name, label, duration, options, wifi_host, ethernet_host, gate='open', fixed=True, cpu_only=False, voice_path='ethernet'):
    image = out / 'images' / name
    directory = out / 'blocks' / label
    directory.parent.mkdir(parents=True, exist_ok=True)
    if any(path.exists() for path in (directory, directory.with_suffix('.flash.log'), directory.with_suffix('.log'))):
        raise RuntimeError('Capture label already has evidence; use a new rerun label: ' + label)
    flash(image, directory.with_suffix('.flash.log'))
    command = [sys.executable, str(ROOT / 'hil/step8c_capture.py'), '--out', str(directory),
               '--elf', str(image / 'dg-esp32.elf'), '--wifi-host', wifi_host,
               '--ethernet-host', ethernet_host, '--duration', str(duration), '--label', label, '--gate', gate,
               '--voice-path',voice_path]
    if fixed:
        command.append('--fixed-mic')
    if cpu_only:command.append('--cpu-only')
    for option in options:
        command += ['--option', option]
    with directory.with_suffix('.log').open('w') as stream:
        subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True)
    result = summarize_cpu(directory) if cpu_only else summarize(directory)
    result['image'] = name
    result['options'] = options
    write_json(directory / 'summary.json', result)
    print(label + ': complete' + ('' if cpu_only else '; PLC/min=' + str(round(result['plc_per_minute'], 3))), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    sub = p.add_subparsers(dest='mode', required=True)
    build = sub.add_parser('build')
    build.add_argument('variant', nargs='+', choices=list(VARIANTS))
    build.add_argument('--production',action='store_true',help='Omit the bench overlay and require lwIP statistics disabled')
    archive=sub.add_parser('archive-root',help='Archive the verified production root build; no build or flash')
    archive.add_argument('--label',default='final',help='New immutable image label; existing archives are never overwritten')
    run = sub.add_parser('block')
    run.add_argument('image', choices=list(VARIANTS))
    run.add_argument('label')
    run.add_argument('--duration', type=float, default=610)
    run.add_argument('--option', action='append', default=[])
    run.add_argument('--wifi-host', required=True)
    run.add_argument('--ethernet-host', required=True)
    run.add_argument('--gate', choices=['auto', 'open', 'closed'])
    run.add_argument('--cpu-only',action='store_true')
    run.add_argument('--voice-path',choices=['wifi','ethernet'],default='ethernet')
    a = p.parse_args()
    a.out = a.out.resolve()
    a.out.mkdir(parents=True, exist_ok=True)
    if a.mode == 'build':
        for variant in a.variant:
            build_variant(a.out, variant,bench=not a.production)
    elif a.mode == 'archive-root':
        archive_root(a.out,label=a.label)
    else:
        block(a.out, a.image, a.label, a.duration, a.option, a.wifi_host, a.ethernet_host,
              a.gate or ('open' if a.cpu_only else 'auto'),cpu_only=a.cpu_only,voice_path=a.voice_path)
