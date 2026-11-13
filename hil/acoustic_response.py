"""Independent known-DSP calibration of this placement's linear response.

Held-out temporal blocks qualify the model; scored utterances never train it.
The modeled gate scores waveform agreement; transport counters gate dropouts.
Nonlinear faults and wrong content can reduce modeled correlation, while
linear response changes can be absorbed by calibration. Raw correlation is diagnostic.
"""
import json
import re
import time
from pathlib import Path
import numpy as np
from scipy import signal,linalg,optimize,fft
from numpy.lib.stride_tricks import sliding_window_view
from alsa import record_wav,resolve_jabra,set_volumes
from esp_serial import ROOT
from speaker_dual_sweep_test import dump_mic
from step7_support import pcm,acoustic_match

TAPS=129

def fractional_align(clean,recorded):
    """One shared sub-sample delay for SDR, bounded to +/- one 16 kHz sample.

    Integer acoustic matching leaves up to a sample of phase error. Fit only
    this remaining time offset over 500-3500 Hz; no per-band delay, frequency
    response, gain envelope or clock correction is fitted to scored audio.
    """
    x=np.asarray(clean,dtype=float);y=np.asarray(recorded,dtype=float)
    if x.shape!=y.shape or x.ndim!=1 or not len(x):
        raise ValueError('SDR alignment requires equal nonempty mono arrays')
    size=fft.next_fast_len(len(x)+256)
    spectrum=fft.rfft(np.pad(x,(128,size-len(x)-128)));frequencies=fft.rfftfreq(size)
    def shift(delay):
        return fft.irfft(spectrum*np.exp(-2j*np.pi*frequencies*delay),size)[128:128+len(x)]
    sos=signal.butter(3,[500,3500],btype='bandpass',fs=16000,output='sos')
    observed=signal.sosfilt(sos,y);observed_power=float(np.dot(observed,observed))
    if observed_power<1e-12 or np.dot(x,x)<1e-12:return x,0.
    def loss(delay):
        reference=signal.sosfilt(sos,shift(delay))
        return -float(np.dot(reference,observed)/np.sqrt(max(1e-12,np.dot(reference,reference)*observed_power)))
    fitted=optimize.minimize_scalar(loss,bounds=(-1.,1.),method='bounded',options={'xatol':1e-4})
    delay=min((float(fitted.x),-1.,0.,1.),key=loss)
    return shift(delay),delay

def filtered_source(source):
    return .33*signal.sosfilt(signal.butter(2,500,btype='highpass',fs=16000,output='sos'),np.asarray(source)/32768)

def predict(source,model,chain):
    x=filtered_source(source)
    X=sliding_window_view(np.pad(x,(TAPS//2,TAPS//2)),TAPS)[:,::-1]
    return (X@np.asarray(model['chains'][chain]['fir']))*32768

def predict_rendered(rendered,model,chain):
    """Physical FIR only: rendered PCM already contains DSP gain/filtering."""
    x=np.asarray(rendered,dtype=float)/32768
    X=sliding_window_view(np.pad(x,(TAPS//2,TAPS//2)),TAPS)[:,::-1]
    return (X@np.asarray(model['chains'][chain]['fir']))*32768


def calibration_source(source):
    y=filtered_source(source)*(.5/.33)
    magnitude=np.abs(y);knee=.26*.8;width=.26-knee;excess=np.maximum(0,magnitude-knee)
    return np.where(magnitude>knee,np.sign(y)*(knee+width*excess/(width+excess)),y)

def fit(source,observed):
    x=calibration_source(source);y=np.asarray(observed)/32768
    X=sliding_window_view(np.pad(x,(TAPS//2,TAPS//2)),TAPS)[:,::-1]
    # Alternate 160 ms blocks across the entire phonetic excitation. A
    # first/last-half split can lack the consonant spectrum needed to identify
    # the response. Exclude 10 ms boundary guards; no scored audio trains this.
    at=np.arange(len(x));block=2560;guard=160
    interior=(at%block>=guard)&(at%block<block-guard)
    training=interior & ((at//block)%2==0);heldout=interior & ~training
    gram=X[training].T@X[training];ridge=max(1e-8,float(np.trace(gram)/TAPS)*1e-4)
    h=linalg.solve(gram+np.eye(TAPS)*ridge,X[training].T@y[training],assume_a='pos')
    validation=float(np.corrcoef(y[heldout],X[heldout]@h)[0,1])
    if not np.isfinite(validation) or validation<.6:
        raise RuntimeError(f'Independent acoustic response holdout failed: {validation:.3f}')
    # Full independent calibration can now refine the qualified model.
    gram=X.T@X;h=linalg.solve(gram+np.eye(TAPS)*ridge,X.T@y,assume_a='pos')
    return dict(fir=h.tolist(),holdout_correlation=validation,ridge=ridge)

def _capture_response(board,device,out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    card,current=resolve_jabra();assert current==device;set_volumes(card,out/'mixer.log')
    cur=board.cursor;board.command('stats',10)
    text='\n'.join(l for _,_,l in board.lines_since(cur))
    target=int(re.search(r'LOUDNESS target_dbfs=(-?\d+)',text)[1])
    ceiling=int(re.search(r'PLAYBACK .*ceiling_percent=(\d+)',text)[1])
    gain=int(re.search(r'PLAYBACK gain_percent=(\d+)',text)[1]) if not target else None
    board.command('audio 0');board.command('playback-settings 50 26')
    # A greeting can reopen the gate during an inter-phrase pause and close it
    # again before our calibration trigger. Let that whole program drain so
    # the new closed event belongs to the unscored calibration alone.
    settle_seconds=len(pcm(ROOT/'server/fixtures/greeting.wav'))/16000+.5
    time.sleep(settle_seconds)
    source=ROOT/'server/fixtures/prompt_france.wav';duration=len(pcm(source))/16000
    try:
        with record_wav(out/'calibration-jabra.wav',device,int(np.ceil(duration))+4,16000) as rec:
            time.sleep(.8);board.command('mic-record 1');mic_started=time.monotonic();time.sleep(.2)
            cur=board.cursor;started=time.monotonic();board.command('play-fixture prompt_france.wav')
            _,closed,_=board.wait_for_entry('GATE closed',10,since=cur)
            time.sleep(max(0,closed+duration+.7-time.monotonic()))
            board.command('mic-record 0');rec.wait()
        dump_mic(board,out/'calibration-board.wav')
        model=dict(taps=TAPS,settle_previous_playback_seconds=settle_seconds,calibration_program='prompt_france.wav',training='Independent unscored France prompt at legacy gain 50%, ceiling 26% (exact known soft knee included in calibration source), high-pass 500 Hz; alternating 160 ms training/held-out blocks with 10 ms guards, then full-calibration refit',chains={})
        for chain in ('board','jabra'):
            match=acoustic_match(out/f'calibration-{chain}.wav',source,.1 if chain=='board' else started-rec.ready_at,
                                 mic_started if chain=='board' else rec.ready_at,.3 if chain=='board' else started-rec.ready_at-.1)
            observed=pcm(out/f'calibration-{chain}.wav');offset=int(round(match['offset_s']*16000))
            model['chains'][chain]=fit(pcm(source),observed[offset:offset+len(pcm(source))])
            model['chains'][chain]['raw_correlation']=match['correlation']
        (out/'response.json').write_text(json.dumps(model,indent=2)+'\n')
        return model
    finally:
        board.command('mic-record 0')
        board.command(f'playback-loudness {target} {ceiling}' if target else f'playback-settings {gain} {ceiling}')


def calibrate_response(board,device,out):
    """Allow one fresh calibration repeat before any scored speech is played."""
    out=Path(out);out.mkdir(parents=True,exist_ok=True);failed=[]
    for attempt in (1,2):
        directory=out/f'attempt{attempt}'
        try:
            model=_capture_response(board,device,directory)
            model.update(qualified_attempt=attempt,failed_attempts=failed,directory=str(directory))
            (out/'response.json').write_text(json.dumps(model,indent=2)+'\n')
            return model
        except (RuntimeError,TimeoutError) as exc:
            error=dict(attempt=attempt,error=str(exc));failed.append(error)
            (directory/'calibration-error.json').write_text(json.dumps(error,indent=2)+'\n')
            if attempt==2:raise
