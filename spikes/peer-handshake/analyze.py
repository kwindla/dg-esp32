"""Validate causal fixture segments, paced tone, readiness, and five reconnect pairs."""
import argparse
import json
import re
from pathlib import Path
import sys
import wave
import numpy as np
from scipy import signal
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'server/tests'))
from aiortc_client import correlation

def wav(path):
    with wave.open(str(path)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000,1,2)
        return np.frombuffer(w.readframes(w.getnframes()), '<i2').astype(float)

def span(x):
    # 20 ms RMS envelope: ignore low-level codec ringing using relative threshold.
    rms = np.sqrt(np.convolve(x*x, np.ones(320)/320, mode='valid'))
    indices = np.flatnonzero(rms > max(80, rms.max() * .08))
    return (int(indices[0]), int(indices[-1])+320) if len(indices) else (0,0)

def fixture(segment, name):
    ref = wav(ROOT/'server/fixtures'/name)
    score = correlation(segment, ref)
    # Match the beginning; compare actual audible support rather than counting idle silence.
    c = signal.correlate(segment, ref, mode='valid', method='fft')
    at = int(np.argmax(c))
    first,last = span(segment[max(0,at-320):min(len(segment),at+len(ref)+320)])
    rf,rl = span(ref)
    duration = (last-first)/16000
    expected = (rl-rf)/16000
    return dict(correlation=score, duration=duration, expected_duration=expected,
        active_samples=last-first, expected_active_samples=rl-rf,
        peak_hz=float(np.fft.rfftfreq(len(ref),1/16000)[np.argmax(np.abs(np.fft.rfft(segment[at:at+len(ref)])))]),
        rms_dbfs=float(20*np.log10(max(np.sqrt(np.mean(segment[at:at+len(ref)]**2))/32768,1e-12))),
        passed=bool(score>=.9 and abs(duration/expected-1)<=.05))

def analyze(out):
    events=[json.loads(l) for l in (out/'events.jsonl').read_text().splitlines()]
    rtp = [json.loads(l) for l in (out/'rtp.jsonl').read_text().splitlines()] if (out/'rtp.jsonl').exists() else []
    attempts=[]
    for i in range(1,11):
        result={'attempt':i,'passed':False}
        try:
            meta=json.loads((out/str(i)/'device.json').read_text()); result.update(meta)
            rx=np.fromfile(out/str(i)/'rx.pcm',dtype='<i2').astype(float)
            with wave.open(str(out/str(i)/'rx.wav'),'wb') as w:
                w.setparams((1,2,16000,0,'NONE','not compressed')); w.writeframes(rx.astype('<i2').tobytes())
            assert meta['ready_us'] and meta['play_us'], 'RTVI readiness/play missing'
            result['greeting']=fixture(rx[:4*16000], 'greeting.wav')
            result['reply']=fixture(rx[5*16000:], 'reply_pineapple.wav')
            tx=wav(out/'input'/f"{meta['pc_id']}.wav")
            first,last=span(tx); tone=tx[first+1600:last-1600]
            freq=np.fft.rfftfreq(len(tone),1/16000)[np.argmax(abs(np.fft.rfft(tone)))]; duration=(last-first)/16000
            blocks=tone[:len(tone)//320*320].reshape(-1,320)
            rms=np.sqrt(np.mean(blocks**2,axis=1))
            result['tone']=dict(hz=float(freq),duration=duration,total_duration=len(tx)/16000,
                min_block_rms=float(rms.min()),median_block_rms=float(np.median(rms)),
                passed=bool(abs(freq-1000)<10 and abs(duration-8)<.4 and abs(len(tx)/16000-10)<.5 and rms.min()>.7*np.median(rms)))
            packets=[p for p in rtp if p.get('receiver')==i]
            assert len(packets)>=490, 'Missing RTP evidence'
            arrivals = packets
            packets = sorted(packets, key=lambda p: p['seq'])
            ts=np.array([p['timestamp'] for p in packets],dtype=np.int64)
            seq=np.array([p['seq'] for p in packets],dtype=np.int64)
            times=np.array([p['arrival_ms'] for p in arrivals],dtype=np.int64)
            deltas=np.diff(ts) % 2**32; seq_delta=np.diff(seq) % 65536
            result['rtp']=dict(packets=len(packets),timestamp_step_median=float(np.median(deltas)),
                arrival_duration_ms=int(times[-1]-times[0]),max_gap_ms=int(np.diff(times).max()),
                passed=bool(np.all(deltas==960*seq_delta) and np.all(seq_delta==1) and
                    abs((times[-1]-times[0])-9980)<500 and np.diff(times).max()<200))
            session=[e for e in events if e['pc_id']==meta['pc_id']]
            inbound=[e['message'] for e in session if e['event']=='rtvi-in']
            outbound=[e['message'] for e in session if e['event']=='rtvi-out']
            assert any(m.get('type')=='client-ready' and m.get('data',{}).get('version')=='2.1.0' and m.get('data',{}).get('about',{}).get('library_version')=='1.1.0' for m in inbound)
            assert any(m.get('type')=='bot-ready' for m in outbound)
            assert any(e['event']=='session-end' for e in session), 'No server teardown'
            assert meta['tx']==500 and not meta['drops'] and not meta['errors'], 'Device drops/errors'
            result['passed']=all(result[k]['passed'] for k in ['tone','greeting','reply','rtp'])
        except Exception as exc:
            result['error']=str(exc)
        attempts.append(result)
    pairs=[attempts[i]['passed'] and attempts[i+1]['passed'] for i in range(0,10,2)]
    serial=(out/'serial.log').read_text()
    phases=[]
    for m in re.finditer(r'PHASE (\S+) attempt=(\d+) t=(\d+) int=(\d+) largest=(\d+) min=(\d+) dma=(\d+) psram=(\d+) psram_largest=(\d+) task=(\S+) stack=(\d+)',serial):
        name,attempt,t,internal,largest,minimum,dma,psram,psram_largest,task,stack=m.groups()
        phases.append(dict(name=name,attempt=int(attempt),t=int(t),internal=int(internal),largest=int(largest),minimum=int(minimum),dma=int(dma),psram=int(psram),psram_largest=int(psram_largest),task=task,stack=int(stack)))
    stacks={task:min(p['stack'] for p in phases if p['task']==task) for task in {p['task'] for p in phases}}
    budgets={'main':16384,'peer-loop':10240,'signal':8192,'encode':32768,'decode':16384}
    connected=[p for p in phases if p['name'] in {'connected','bot-ready','play'}]
    resource_ok=bool(connected and all(p['internal']>=120*1024 and p['largest']>=32*1024 for p in connected)
                     and set(stacks)==set(budgets) and all(stacks[k]>=budgets[k]*.25 for k in stacks))
    timings=[]
    for i in range(1,11):
        times={p['name']:p['t'] for p in phases if p['attempt']==i}
        if {'start','open','connected','bot-ready','answer-applied'}<=times.keys():
            timings.append(dict(attempt=i,start_to_ready_ms=(times['bot-ready']-times['start'])/1000,
                open_to_connected_ms=(times['connected']-times['open'])/1000,
                answer_to_connected_ms=(times['connected']-times['answer-applied'])/1000))
    result={'attempts':attempts,'reconnect_pairs':pairs,'stack_free_bytes':stacks,
            'min_connected_internal':min((p['internal'] for p in connected), default=0),
            'min_connected_largest':min((p['largest'] for p in connected), default=0),
            'resources_passed':resource_ok,'timings':timings,
            'same_boot':serial.count('READY peer-handshake')==1 and 'MATRIX DONE' in serial,
            'passed':all(pairs) and resource_ok and serial.count('READY peer-handshake')==1 and 'MATRIX DONE' in serial}
    (out/'analysis.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
    return result['passed']
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('out',type=Path);a=p.parse_args()
    sys.exit(0 if analyze(a.out) else 1)
