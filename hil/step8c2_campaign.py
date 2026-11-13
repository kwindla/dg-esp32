#!/usr/bin/env python3
"""Immutable CPU images and fresh-boot fixed-PCM crossover for step 8c2.

Build first, measure separately. All variants retain -22 dBFS / 26%, 20 ms
uplink, mailbox32/cache200/direct select/recv20 and the 8e transport fixes.
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
from step8c_campaign import VARIANTS,build_variant
from step8c_analysis import summarize_cpu,uplink_payload_summary
from step8c2_analysis import cache_summary,device_payload_summary

VARIANTS_8C2={
    'control':{},
    'control-v2':{},
    'platform-control':{},
    'control-layout':{'CONFIG_DG_CODEC_LAYOUT_TRACE':'y'},
    'combined-screen':{'CONFIG_DG_CODEC_LAYOUT_TRACE':'y','CONFIG_DG_CODEC_EAGER_INITIALIZATION':'y','CONFIG_DG_OPUS_ENCODER_HOT_IRAM':'y'},
    'dsp-iram-layout':{'CONFIG_DG_CODEC_LAYOUT_TRACE':'y','CONFIG_DG_PLAYBACK_DSP_IRAM':'y'},
    'dsp-iram':{'CONFIG_DG_PLAYBACK_DSP_IRAM':'y'},
    'opus-iram':{'CONFIG_DG_OPUS_ENCODER_HOT_IRAM':'y'},
    'xip':{'CONFIG_SPIRAM_XIP_FROM_PSRAM':'y'},
    'dcache64':{'CONFIG_ESP32S3_DATA_CACHE_32KB':'n','CONFIG_ESP32S3_DATA_CACHE_64KB':'y',
                'CONFIG_ESP32S3_DATA_CACHE_LINE_32B':'n','CONFIG_ESP32S3_DATA_CACHE_LINE_64B':'y'},
    'breakdown':{'CONFIG_DG_PLAYBACK_BREAKDOWN':'y'},
    'qio':{'CONFIG_ESPTOOLPY_FLASHMODE_DIO':'n','CONFIG_ESPTOOLPY_FLASHMODE_QIO':'y'},
    'qio-xip':{'CONFIG_ESPTOOLPY_FLASHMODE_DIO':'n','CONFIG_ESPTOOLPY_FLASHMODE_QIO':'y',
               'CONFIG_SPIRAM_XIP_FROM_PSRAM':'y'},
}

def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n')

def retain_source(directory):
    source=directory/'source';source.mkdir(exist_ok=False)
    paths=subprocess.check_output(['git','ls-files'],cwd=ROOT,text=True).splitlines()
    paths+=['main/cache_metrics.cpp','main/cache_metrics.h','components/dg_audio/playback_hot.lf',
            'components/dg_diagnostics/include/dg_playback_cost.h','hil/step8c2_analysis.py',
            'components/pipecat_esp_peer/src/receive_budget_policy.h',
            'components/pipecat_esp_peer/src/codec_memory_layout.h',
            'hil/step8c2_campaign.py','hil/step8c2_outage.py','hil/step8c2_archive.py','hil/test_step8c2.py']
    hashes={}
    for name in sorted(set(paths)):
        file=ROOT/name
        if not file.is_file():continue
        target=source/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(file,target)
        hashes[name]=hashlib.sha256(file.read_bytes()).hexdigest()
    save(directory/'source-manifest.json',dict(base=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        files=hashes,private_configuration_exported=False))

def retain_placement(directory):
    nm=next((Path.home()/'.espressif/tools/xtensa-esp-elf/esp-15.2.0_20251204').glob('**/xtensa-esp32s3-elf-nm'))
    output=subprocess.check_output([str(nm),'-S','-C','--defined-only',str(directory/'dg-esp32.elf')],text=True)
    (directory/'symbols.txt').write_text(output)
    selected=[]
    exact={'opus_encode','opus_encode_native','opus_decode','opus_decode_native','silk_encode_frame_FIX',
        'silk_NSQ_c','silk_NSQ_del_dec_c','dtls_srtp_encrypt_rtp_packet','dtls_srtp_decrypt_rtp_packet',
        'srtp_protect','srtp_unprotect','lwip_select','udp_input','i2s_channel_write','__wrap_select'}
    for line in output.splitlines():
        fields=line.split(maxsplit=3)
        if len(fields)!=4:continue
        address,size,kind,name=fields
        if name in exact or ('Session::owner_run()' in name and not name.startswith('std::')) or name.startswith('dg_audio::PlaybackDsp::'):
            value=int(address,16)
            selected.append(dict(name=name,address='0x'+address,size=int(size,16),
                placement='IRAM' if 0x40300000<=value<0x40400000 else 'flash/XIP virtual' if 0x42000000<=value<0x44000000 else 'other'))
    save(directory/'hot-placement.json',selected)
    sections=subprocess.check_output(['readelf','--wide','--sections',str(directory/'dg-esp32.elf')],text=True)
    (directory/'sections.txt').write_text(sections)

def build(out,names):
    for name in names:
        key='8c2-'+name;VARIANTS[key]=VARIANTS_8C2[name]
        build_variant(out,key,bench=False)
        sys.path.insert(0,str(ROOT/'scripts'))
        from importlib import import_module
        actual=import_module('check-sdkconfig').settings(ROOT/'build/step8c'/key/'sdkconfig')
        invariant={'CONFIG_DG_PLAYBACK_TARGET_DBFS':'-22','CONFIG_DG_PLAYBACK_CEILING_PERCENT':'26',
            'CONFIG_DG_OPUS_UPLINK_FRAME40':'n','CONFIG_LWIP_UDP_RECVMBOX_SIZE':'32',
            'CONFIG_DG_PEER_JITTER_TIMEOUT_MS':'200','CONFIG_DG_PEER_DIRECT_SOCKET_SELECT':'y',
            'CONFIG_DG_PEER_RECV_TIMEOUT_MS':'20','CONFIG_ESPTOOLPY_FLASHFREQ':'"80m"'}
        mismatches=[k for k,v in invariant.items() if actual.get(k)!=k+'='+v]
        save(out/'images'/key/'production-invariants.json',dict(expected=invariant,differences=mismatches))
        if mismatches:raise ValueError('Production invariant mismatch: '+', '.join(mismatches))
        safe_keys=('CONFIG_DG_CACHE_COUNTERS','CONFIG_SPIRAM_XIP_FROM_PSRAM','CONFIG_SPIRAM_FETCH_INSTRUCTIONS','CONFIG_SPIRAM_RODATA',
            'CONFIG_ESP32S3_DATA_CACHE_SIZE','CONFIG_ESP32S3_DATA_CACHE_LINE_SIZE','CONFIG_DG_OPUS_FEC',
            'CONFIG_DG_CODEC_EAGER_INITIALIZATION','CONFIG_DG_CODEC_LAYOUT_TRACE','CONFIG_DG_PLAYBACK_BREAKDOWN','CONFIG_DG_PLAYBACK_DSP_IRAM')
        save(out/'images'/key/'experiment-settings.json',{k:actual[k].split('=',1)[1] for k in safe_keys if k in actual})
        retain_source(out/'images'/key)
        retain_placement(out/'images'/key)

def cpu(out,label,image,options,wifi_host,ethernet_host,seconds=140):
    target=out/'cpu'/label;target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():raise ValueError('Existing capture; use a new label')
    flash(image,target.with_suffix('.flash.log'))
    command=[sys.executable,str(ROOT/'hil/step8c_capture.py'),'--out',str(target),
        '--elf',str(image/'dg-esp32.elf'),'--wifi-host',wifi_host,'--ethernet-host',ethernet_host,
        '--duration',str(seconds),'--cpu-window','30','--fixed-mic','--cpu-only','--gate','open',
        '--payload-sizes','--label',label]
    if json.loads((image/'experiment-settings.json').read_text()).get('CONFIG_DG_CACHE_COUNTERS')=='y':command+=['--cache-counters']
    for option in options:command+=['--option',option]
    with target.with_suffix('.log').open('w') as log:
        subprocess.run(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=seconds+300)
    summary=summarize_cpu(target)
    directory=target/'webrtc-ethernet'
    snapshots=json.loads((directory/'cpu-snapshots.json').read_text())
    packets=[json.loads(l) for l in (directory/'rtp/rtp.jsonl').read_text().splitlines()]
    # Retain the same CPU brackets for payload rates, using each snapshot's
    # host observation time (the capture's PERF calls have separate clocks).
    summary['boot_timing']=json.loads((target/'boot-timing.json').read_text())
    summary['cache_counters']=[s.get('cache_counters') for s in snapshots]
    summary['cache_summary']=cache_summary(snapshots)
    summary['opus_fec']=snapshots[-1].get('opus_fec')
    summary['eager_codec_initialization']=snapshots[-1].get('eager_codec_initialization')
    serial=(target/'serial.log').read_text(errors='replace')
    import re
    summary['codec_allocations']=[dict(role=int(role),index=int(index),address=address,bytes=int(size))
        for role,index,address,size in re.findall(r'CODEC_LAYOUT role=(\d+) index=(\d+) ptr=(0x[0-9a-fA-F]+) bytes=(\d+)',serial)]
    summary['codec_construction']=[dict(role=int(role),count=int(count),truncated=bool(int(truncated)),begin_us=int(begin),end_us=int(end))
        for role,count,truncated,begin,end in re.findall(r'CODEC_LAYOUT_END role=(\d+) count=(\d+) truncated=(\d+) begin_us=(\d+) end_us=(\d+)',serial)]
    summary['device_uplink_payload']=device_payload_summary(snapshots)
    summary['payload_records']=len(packets)
    summary['uplink_payload']=uplink_payload_summary(packets,snapshots[0]['host_after_ns'],snapshots[-1]['host_after_ns'])
    save(target/'summary.json',summary)
    print(label+': complete',flush=True)
    return summary

def crossover(out,name,left,right,left_options,right_options,pairs,wifi_host,ethernet_host):
    result=dict(completed=False,name=name,runs=[])
    try:
        for pair in range(pairs):
            for arm in ('A','B') if pair%2==0 else ('B','A'):
                image=left if arm=='A' else right;options=left_options if arm=='A' else right_options
                summary=cpu(out,f'{name}-{arm}{pair+1:02}',image,options,wifi_host,ethernet_host)
                result['runs'].append(dict(pair=pair+1,arm=arm,image=str(image),options=options,summary=summary))
                save(out/(name+'.json'),result)
        result['completed']=True
    finally:save(out/(name+'.json'),result)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    sub=p.add_subparsers(dest='mode',required=True)
    b=sub.add_parser('build');b.add_argument('names',nargs='+',choices=VARIANTS_8C2)
    c=sub.add_parser('cpu');c.add_argument('name');c.add_argument('--left',type=Path,required=True);c.add_argument('--right',type=Path,required=True)
    c.add_argument('--left-option',action='append',default=[]);c.add_argument('--right-option',action='append',default=[])
    c.add_argument('--pairs',type=int,default=2)
    c.add_argument('--wifi-host',required=True);c.add_argument('--ethernet-host',required=True)
    a=p.parse_args();a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=True)
    if a.mode=='build':build(a.out,a.names)
    else:crossover(a.out,a.name,a.left.resolve(),a.right.resolve(),a.left_option,a.right_option,a.pairs,a.wifi_host,a.ethernet_host)
