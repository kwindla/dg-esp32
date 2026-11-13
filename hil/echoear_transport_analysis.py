"""Correlate retained diagnostic windows; never changes acceptance thresholds."""
import argparse
import json
import statistics
import subprocess
from pathlib import Path

def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

def gaps(records, key, scale, start, end):
    selected=[]
    for a,b in zip(records,records[1:]):
        lo,hi=a[key]*scale,b[key]*scale
        if lo<start or hi>end:continue
        selected.append(dict(start=lo,end=hi,ms=(b[key]-a[key])*scale*1000,seq=b.get('seq'),
                             seq_step=(b.get('seq',0)-a.get('seq',0))%65536))
    return dict(count=len(selected),over60=sum(x['ms']>60 for x in selected),
                over150=sum(x['ms']>150 for x in selected),
                maximum_ms=max((x['ms'] for x in selected),default=None),
                tails=[x for x in selected if x['ms']>60])

def analyze(root):
    result=json.loads((root/'result.json').read_text())
    anchors=rows(root/'serial.audio.jsonl')
    offsets=[a['host_s']-a['device_us']/1e6 for a in anchors]
    offset=statistics.median(offsets)
    trace=rows(root/'arrivals.jsonl');host=rows(root/'rtp/rtp.jsonl');down=rows(root/'rtp/downlink.jsonl')
    report=dict(offset_s=offset,anchor_spread_ms=1000*(max(offsets)-min(offsets)),phases=[])
    kernel=[]
    if (root/'packets.pcapng').exists():
        remote=rows(root/'rtp/ice-pairs.jsonl')[-1]['pairs'][0]['remote'][0]
        clock=result['packet_clock'];wall_offset=clock['wall_s']-clock['monotonic_s']
        exported=subprocess.check_output(['tshark','-r',str(root/'packets.pcapng'),'-Y','udp',
            '-T','fields','-e','frame.time_epoch','-e','ip.src','-e','udp.payload'],text=True,stderr=subprocess.DEVNULL)
        for line in exported.splitlines():
            parts=line.split('\t')
            if len(parts)!=3 or parts[1]!=remote:continue
            raw=bytes.fromhex(parts[2].replace(':',''))
            if len(raw)<12 or raw[0]>>6!=2 or 192<=raw[1]<=223:continue
            kernel.append(dict(host_s=float(parts[0])-wall_offset,seq=int.from_bytes(raw[2:4],'big'),ssrc=int.from_bytes(raw[8:12],'big')))
        matched={(r['seq'],r['ssrc']):r['host_s'] for r in kernel}
        delays=[r['observed_ms']/1000-matched[(r['seq'],r['ssrc'])] for r in host if (r['seq'],r['ssrc']) in matched]
        report['host_kernel_to_bot_ms']=dict(count=len(delays),maximum=max(delays)*1000 if delays else None,median=statistics.median(delays)*1000 if delays else None)

    for phase in result['phases']:
        if 'end' not in phase:continue
        start,end=phase['start'],phase['end'];lo,hi=start-offset,end-offset
        row=dict(name=phase['name'],start=start,end=end,
                 uplink_host=gaps(host,'observed_ms',.001,start,end),
                 downlink_host=gaps(down,'send_ms',.001,start,end))
        for kind,name in ((1,'downlink_wire'),(12,'uplink_wire'),(13,'ingress'),(14,'render')):
            stream=[x for x in trace if x['kind']==kind]
            row[name]=gaps(stream,'device_us',1e-6,lo,hi)
            row[name]['first_event_delay_ms']=next((x['device_us']/1000-lo*1000 for x in stream if lo<=x['device_us']/1e6<=hi),None)
        if kernel:row['uplink_kernel']=gaps(kernel,'host_s',1,start,end)
        row['events']=[x for x in trace if lo<=x['device_us']/1e6<=hi and x['kind'] in (4,5,6,7,8,10,15,16,17,18)]
        row['counter_deltas_including_boundary_stats']={k:phase['after'].get(k,0)-phase['before'].get(k,0) for k in ('lost','concealed','i2s_underflows','i2s_write_errors','drift_drops','drift_inserts')}
        row['trace_overwritten']=phase['after']['peer_profile']['trace']['overwritten']
        report['phases'].append(row)
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);a=p.parse_args()
    report=analyze(a.root);(a.root/'analysis.json').write_text(json.dumps(report,indent=2)+'\n')
    for r in report['phases']:
        print(r['name'],r['counter_deltas_including_boundary_stats'])
        print({k:dict(count=r[k]['count'],max_ms=r[k]['maximum_ms'],over60=r[k]['over60']) for k in ('uplink_host','uplink_wire','downlink_host','downlink_wire','render')})
