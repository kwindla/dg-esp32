"""Placement calibration using native PCM and a verified board gain path.

EchoEar uses measured ES7210 PCM targets with no SPL inference.

SPL is an estimate from the datasheet (-26 +/-1 dBFS at 94 dB SPL),
not a traceable sound-level meter reading. ALSA readbacks remain Step 7's.
"""
import hashlib
import os
import re
import json
import time
import wave
from pathlib import Path

import numpy as np
from scipy import signal
from alsa import play_wav, resolve_jabra, set_volumes
from esp_serial import ROOT
from speaker_dual_sweep_test import capture, dump_mic, MicDumpUnavailable
from step7_support import pcm

SENSITIVITY_DBFS = -26.0
SCHEMATIC = 'https://files.waveshare.com/wiki/ESP32-S3-Touch-LCD-1.46/ESP32-S3-Touch-LCD-1.46.pdf'
DATASHEET = 'https://files.waveshare.com/upload/0/01/MSM261S4030H0R.pdf'

class BoardRoomUnavailable(ValueError):
    """Only the unscored board-room PCM acquisition is unavailable."""


def record_board_room(board,out):
    # Command/strict-monitor errors remain fatal. Only unavailable PCM from
    # the completed measurement is classified as an eligibility deferral.
    board.command('mic-record 1');time.sleep(1.5);board.command('mic-record 0')
    try:dump_mic(board,out/'room-board.wav')
    except MicDumpUnavailable as exc:raise BoardRoomUnavailable(str(exc)) from exc
    try:
        samples=pcm(out/'room-board.wav')
        if not len(samples) or not np.isfinite(samples).all():raise ValueError('Missing finite room PCM')
        return dbfs(samples)
    except (OSError,ValueError,wave.Error) as exc:
        raise BoardRoomUnavailable(str(exc)) from exc

def dbfs(x):
    return float(20*np.log10(max(np.sqrt(np.mean(np.asarray(x, dtype=float)**2))/32768, 1e-6)))


def active_mask(x, rate=16000):
    """20 ms activity windows, fixed -45 dBFS threshold on the clean source."""
    power = signal.convolve(np.asarray(x, dtype=float)**2,
                            np.ones(rate//50)/(rate//50), mode='same')
    return power > (32768*10**(-45/20))**2


def playback(path, x):
    y = signal.resample_poly(x, 3, 1)
    if np.max(np.abs(y)) > 32767:
        raise ValueError('Calibration requires clipped host playback; move-independent target unavailable')
    with wave.open(str(path), 'wb') as w:
        w.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        w.writeframes(np.rint(y).astype('<i2').tobytes())
    return path


def record_prompt(board, device, out, source, scale, name, *, pcm_percent=75):
    card, actual = resolve_jabra()
    if actual != device: raise RuntimeError('Jabra identity changed')
    set_volumes(card, out/f'{name}-mixer.log',pcm_percent=pcm_percent)
    x = pcm(source)
    path = playback(out/f'{name}-playback.wav', x*scale)
    board.command('mic-record 1')
    time.sleep(.4)
    played = time.monotonic()
    play_wav(path, device)
    ended = time.monotonic()
    time.sleep(.4)
    board.command('mic-record 0')
    raw_path = out/f'{name}-board.wav'
    dump_mic(board, raw_path)
    raw = pcm(raw_path)
    # Find the physical speech in native PCM, independent of serial/ALSA delay.
    sos = signal.butter(3, [300, 3500], btype='bandpass', fs=16000, output='sos')
    r, y = signal.sosfilt(sos, x), signal.sosfilt(sos, raw)
    corr = signal.correlate(y, r, mode='valid', method='fft')
    offset = int(np.argmax(corr))
    speech = raw[offset:offset+len(x)]
    mask = active_mask(x)
    level = dbfs(speech[mask])
    return dict(path=str(path), raw_path=str(raw_path), scale=scale,
                active_rms_dbfs=level, estimated_spl=(None if os.getenv("DG_BOARD", "echoear") == "echoear" else 94+level-SENSITIVITY_DBFS),
                source_active_rms_dbfs=dbfs(x[mask]), offset_samples=offset,
                prompt_started=played, prompt_ended=ended)


def echoear_gain_path(board):
    cursor=board.cursor;board.command('mic-gain-status')
    line=board.wait_for(r'MIC_GAIN_PATH analog_db=\d+ reg=0x[0-9a-f]+ digital_percent=\d+',3,since=cursor)
    match=re.search(r'analog_db=(\d+) reg=0x([0-9a-f]+) digital_percent=(\d+)',line)
    analog,reg,digital=int(match[1]),int(match[2],16),int(match[3])
    assert analog in (24,30,33) and reg==(0x10|analog//3),'MIC1 gain register mismatch'
    text='\n'.join(line for _,_,line in board.lines_since(cursor))
    protected=re.search(r'MIC_PROTECTION schema=(\d+) knee=(\d+) ceiling=(\d+) limited_samples=(\d+) adc_clips=(\d+)',text)
    assert protected,'Missing microphone protection readback'
    schema,knee,ceiling,limited,adc=map(int,protected.groups())
    assert (schema,knee,ceiling)==(1,8192,16384),'Unknown microphone protection profile'
    return dict(analog_db=analog,register=reg,digital_percent=digital,
                protection=dict(schema=schema,knee=knee,ceiling=ceiling),limited_samples=limited,adc_clips=adc)


def calibrate(board, device, out, target_spl=65, source=None, *, pcm_percent=75, output_reference=True):
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    card, actual = resolve_jabra()
    if actual != device:
        raise RuntimeError('Jabra identity changed')
    set_volumes(card, out/'mixer.log',pcm_percent=pcm_percent)
    board.command('audio 0'); board.command('quiet 1')
    floor = record_board_room(board,out)
    rate = 16000; t = np.arange(rate*2)/rate
    tone_path = playback(out/'input-tone-48k.wav', .5*32767*np.sin(2*np.pi*1000*t))
    board.command('mic-record 1'); time.sleep(.3)
    play_wav(tone_path, device); time.sleep(.3); board.command('mic-record 0')
    dump_mic(board, out/'input-tone-board.wav')
    raw = pcm(out/'input-tone-board.wav')
    # Stable central window around the strongest one-second segment.
    energies = signal.convolve(raw**2, np.ones(rate)/rate, mode='valid', method='fft')
    at = int(np.argmax(energies)); tone_level = dbfs(raw[at:at+rate])
    output = (capture(board, device, out, 'output-reference', 1500, 20) if output_reference else
              dict(available=False, reason='Direct diagnostic tone omitted during warm media session; reply gates use measured speech/FIR'))
    source = Path(source) if source is not None else ROOT/'hil/fixtures/prompt_speech.wav'
    probe = record_prompt(board, device, out, source, .1, 'prompt-probe',pcm_percent=pcm_percent)
    echoear = os.getenv('DG_BOARD', 'echoear') == 'echoear'
    # EchoEar analog microphone sensitivity has not been calibrated. Set a
    # reproducible ADC speech level, retaining all physical recordings.
    analog=24;digital=400
    if echoear:
        path=echoear_gain_path(board);analog=path['analog_db']
        digital=path['digital_percent']
    # Native PCM precedes digital gain. Preserve the physical prompt target
    # independently of the configured gain and its speaker-echo headroom.
    # Calibration must not silently restore the former 200% clipping setting.
    target = ((-45.0 if target_spl == 65 else -52.0)+(analog-24)) if echoear else target_spl
    level_key = 'active_rms_dbfs' if echoear else 'estimated_spl'
    scale = .1*10**((target-probe[level_key])/20)
    attempts=[]
    for iteration in range(5):
        final = record_prompt(board, device, out, source, scale, f'prompt-adjustment-{iteration}',pcm_percent=pcm_percent)
        attempts.append(final)
        if abs(final[level_key]-target)<=1:
            break
        scale *= 10**((target-final[level_key])/20)
    result = dict(microphone='MSM261S4030H0R (schematic U6)', schematic=SCHEMATIC,
                  datasheet=DATASHEET, sensitivity_dbfs=SENSITIVITY_DBFS,
                  sensitivity_tolerance_db=1, spl_basis='94 + native RMS dBFS - sensitivity; nominal broadband estimate',
                  target_spl=target_spl, pcm_requested_percent=pcm_percent, room_floor_dbfs=floor,
                  room_floor_estimated_spl=94+floor-SENSITIVITY_DBFS,
                  input_tone_rms_dbfs=tone_level, input_tone_estimated_spl=94+tone_level-SENSITIVITY_DBFS,
                  output_reference=output, probe=probe, prompt=final, adjustments=attempts,
                  source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  passed=abs(final[level_key]-target)<=1)
    if echoear:
        result.update(microphone=f'EchoEar ES7210 MIC1; analog gain {analog} dB', analog_gain_db=analog, gain_register=0x10|analog//3,
                      schematic='https://dl.espressif.com/AE/esp-dev-kits/EchoEar_SCH_V1_2.pdf',
                      datasheet=None, sensitivity_dbfs=None, sensitivity_tolerance_db=None,
                      spl_basis='Uncalibrated SPL; measured ADC active speech RMS only',
                      target_spl=None, target_active_rms_dbfs=target, digital_gain_percent=digital,
                      nominal_post_gain_target_dbfs=target+20*np.log10(digital/100),
                      protection=path.get('protection'),
                      room_floor_estimated_spl=None, input_tone_estimated_spl=None)
    (out/'calibration.json').write_text(json.dumps(result, indent=2)+'\n')
    if not result['passed']:
        raise RuntimeError('Prompt calibration missed the declared level target by more than 1 dB')
    return result


if __name__ == '__main__':
    import argparse
    from esp_serial import EspSerial
    p=argparse.ArgumentParser(); p.add_argument('--out',type=Path,required=True)
    p.add_argument('--spl',type=float,default=65)
    p.add_argument('--source',type=Path,help='Immutable alternative spoken stimulus')
    p.add_argument('--pcm-percent',type=int,choices=(75,100),default=75)
    a=p.parse_args()
    with EspSerial(None,log_path=a.out/'serial.log') as board:
        cur=board.cursor; board.reset(); board.wait_for('READY transport-console',60,since=cur)
        _,device=resolve_jabra(); r=calibrate(board,device,a.out,a.spl,source=a.source,pcm_percent=a.pcm_percent)
        print(json.dumps({k:r[k] for k in ('passed','target_spl','room_floor_dbfs','input_tone_rms_dbfs')}))
        print(json.dumps(r['prompt']))
