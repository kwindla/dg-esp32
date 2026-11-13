"""Read-only host Wi-Fi evidence. Missing diagnostics never gate the soak."""
import json
import re
import subprocess
import threading
import time
from pathlib import Path

POLICY=dict(interface='wlp8s0',boundary_commands=['iw dev wlp8s0 link',
    'journalctl -u NetworkManager --since <hold start>'],scan_command='iw dev wlp8s0 scan dump',
    scan_hz=2,diagnostic_only=True,
    coverage='Poll cached BSS count, never trigger a scan. Record actual times/errors/gaps; cache changes are a scan witness, not a count of scans. Boundary journal counts are cumulative since hold start; failures remain unavailable, not zero.')


def query(argv, timeout=2):
    start=time.monotonic()
    try:
        p=subprocess.run(argv,capture_output=True,text=True,timeout=timeout)
        return dict(start_s=start,end_s=time.monotonic(),returncode=p.returncode,
                    stdout=p.stdout,stderr=p.stderr,available=p.returncode==0)
    except Exception as e:
        return dict(start_s=start,end_s=time.monotonic(),available=False,error=str(e),stdout='',stderr='')


def parse_link(raw):
    text=raw['stdout'];bssid=re.search(r'^Connected to ([0-9a-fA-F:]{17})\b',text,re.M)
    freq=re.search(r'^\s*freq:\s*(\d+)',text,re.M);signal=re.search(r'^\s*signal:\s*(-?[\d.]+)\s*dBm',text,re.M)
    connected=bool(bssid)
    available=raw['available'] and (connected or 'Not connected.' in text)
    return dict(available=available,connected=connected if available else None,
                bssid=bssid[1].lower() if bssid else None,
                frequency_mhz=int(freq[1]) if freq else None,signal_dbm=float(signal[1]) if signal else None)


def scan_summary(samples,lo,hi):
    own=[s for s in samples if lo<=s['start_s']<=hi]
    valid=[s for s in own if s['available']];counts=[s['bss_count'] for s in valid]
    points=[lo]+[s['start_s'] for s in own]+[hi]
    return dict(available=bool(valid),samples=len(own),failed_samples=len(own)-len(valid),
                bss_count_range=[min(counts),max(counts)] if counts else None,
                count_changes=[dict(at_s=b['start_s'],before=a['bss_count'],after=b['bss_count'])
                    for a,b in zip(own,own[1:]) if a['available'] and b['available'] and a['bss_count']!=b['bss_count']],
                max_sample_gap_s=max((b-a for a,b in zip(points,points[1:])),default=hi-lo),
                target_hz=2,diagnostic_only=True)


def block_report(root,folder,lo,hi,samples):
    def load(label):
        path=folder/f'{label}-host-wifi.json'
        try:
            value=json.loads(path.read_text())
            return value if isinstance(value,dict) else {'available':False,'error':'invalid diagnostic object'}
        except (OSError,ValueError):return {'available':False}
    before,after=load('before'),load('after')
    a=before.get('supplicant_state_changes');b=after.get('supplicant_state_changes')
    return dict(before=before,after=after,
                supplicant_state_changes_delta=b-a if isinstance(a,int) and isinstance(b,int) and b>=a else None,
                scan=scan_summary(samples,lo,hi),diagnostic_only=True)


def load_samples(root):
    samples=[];errors=[]
    try:
        for n,line in enumerate((root/'host-wifi-scan.jsonl').read_text().splitlines(),1):
            try:
                row=json.loads(line)
                if not isinstance(row.get('start_s'),(int,float)) or type(row.get('available')) is not bool:
                    raise ValueError('bad timestamp/availability')
                if row['available'] and type(row.get('bss_count')) is not int:raise ValueError('bad count')
                samples.append(row)
            except (ValueError,AttributeError) as e:errors.append(f'row {n}: {e}')
    except OSError as e:errors.append(str(e))
    return samples,errors


class HostWifiDiagnostics:
    def __init__(self,output):
        self.output=Path(output);self.start_s=time.monotonic();self.since=f'@{time.time():.6f}'
        self.cancel=threading.Event();self.thread=None;self.errors=[];self.pending=[]

    def save(self,path,value):
        try:path.write_text(json.dumps(value,indent=2)+'\n')
        except Exception as e:self.errors.append(str(e))

    def sample(self):
        raw=query(['iw','dev','wlp8s0','scan','dump'],timeout=.4)
        return dict(start_s=raw['start_s'],end_s=raw['end_s'],available=raw['available'],
                    bss_count=len(re.findall(r'^BSS [0-9a-fA-F:]{17}\b',raw['stdout'],re.M)) if raw['available'] else None,
                    returncode=raw.get('returncode'),error=raw.get('error'),stderr=raw['stderr'])

    def flush(self):
        # BenchHold alone creates output. Buffer samples during its entry/flash.
        if not self.output.is_dir():return
        try:
            with (self.output/'host-wifi-scan.jsonl').open('a') as f:
                for value in self.pending:f.write(json.dumps(value)+'\n')
            self.pending.clear()
        except Exception as e:self.errors.append(str(e))

    def collect(self):
        deadline=self.start_s
        try:
            while not self.cancel.is_set():
                self.pending.append(self.sample());self.flush()
                deadline+=.5
                now=time.monotonic()
                if deadline<now:deadline+=((now-deadline)//.5+1)*.5
                self.cancel.wait(max(0,deadline-now))
        except Exception as e:self.errors.append(str(e))

    def start(self):
        try:
            self.thread=threading.Thread(target=self.collect,name='host-wifi-diagnostics',daemon=True);self.thread.start()
        except Exception as e:self.errors.append(str(e));self.thread=None

    def boundary(self,folder,label):
        try:
            link=query(['iw','dev','wlp8s0','link'])
            journal=query(['journalctl','-u','NetworkManager','--since',self.since,'--no-pager','-o','short-unix'])
            # Permission warnings make a successful but partial journal unavailable.
            available=journal['available'] and not journal['stderr'].strip()
            count=sum(bool(re.search(r'\bwlp8s0\b.*supplicant.*state',line)) for line in journal['stdout'].splitlines()) if available else None
            value=dict(link=parse_link(link),supplicant_state_changes=count,journal_available=available,
                       since=self.since,link_command=link,journal_command=journal,diagnostic_only=True)
            self.save(folder/f'{label}-host-wifi.json',value)
        except Exception as e:self.save(folder/f'{label}-host-wifi.json',dict(available=False,error=str(e),diagnostic_only=True))

    def stop(self):
        self.cancel.set()
        if self.thread:self.thread.join()  # query timeout is 0.4s, no board commands
        self.flush()
        if self.output.is_dir():self.save(self.output/'host-wifi-diagnostics.json',
            dict(start_s=self.start_s,end_s=time.monotonic(),journal_since=self.since,errors=self.errors,policy=POLICY))
