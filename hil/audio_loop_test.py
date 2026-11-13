#!/usr/bin/env python3
"""Causally triggered, bidirectional acoustic acceptance test."""
import argparse
from pathlib import Path
import hashlib
from datetime import datetime, timezone
import json
import re
import time
import wave
import numpy as np
from alsa import resolve_jabra, set_volumes, play_wav, record_wav, analyze_wav, band_rise_db, tone_windows, record_room_baseline
from esp_serial import ROOT, EspSerial
from flash import firmware_info
from speaker_sweep_test import distortion
from speaker_dual_sweep_test import correct_measurement

def make_wav(path, hz=1000, seconds=3, rate=48000):
    t = np.arange(int(rate * seconds)) / rate
    env = np.minimum(1, np.minimum(t / .02, (seconds - t) / .02))
    x = (0.5 * env * np.sin(2*np.pi*hz*t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.column_stack([x, x]).tobytes())

def reports(esp, cursor):
    result = []
    for _, stamp, line in esp.lines_since(cursor):
        m = re.fullmatch(r"MIC (?:rmsL=\S+ )?rmsR=(\S+) peak=(\S+) band_snr=(\S+) band_power=(\S+)", line)
        if m:
            right, peak, snr, power = map(float, m.groups())
            result.append(dict(time=stamp, rms_dbfs=right, peak_hz=peak, band_snr_db=snr,
                               band_power_dbfs=power))
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=None)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--firmware", choices=["loop", "app"], default="loop")
    parser.add_argument('--calibration',type=Path,default=ROOT/'hil/speaker_calibration.json')
    parser.add_argument('--amplitude',type=int,help='Override the calibrated tone amplitude (percent)')
    args = parser.parse_args()
    # Both firmwares drive the same physical speaker. Step A qualifies its
    # clean amplitude; the former fixed 50% stimulus exceeds that range.
    calibration=json.loads(args.calibration.read_text())
    amplitude=calibration['tone_amplitude_percent'] if args.amplitude is None else args.amplitude
    if not 1<=amplitude<=100:parser.error('--amplitude must be 1..100 percent')
    out = args.out or ROOT / "hil/out" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    out.mkdir(parents=True)
    result = {"passed": False, "output": str(out), "firmware": args.firmware}
    try:
        info = firmware_info(ROOT if args.firmware == "app" else ROOT / "hil/audio-loop-fw")
        (out / "firmware.json").write_text(json.dumps(info, indent=2) + "\n")
        card, device = resolve_jabra()
        result["alsa"] = device
        set_volumes(card, out / "mixer.log")
        result["room_baseline"] = record_room_baseline(out / "room-baseline", "audio-loop")
        with EspSerial(args.port, log_path=out / "serial.log") as esp:
            time.sleep(.2)
            cursor = esp.cursor
            boot = esp.reset()
            if args.firmware == "app":
                esp.wait_for('READY transport-console', 40, since=cursor)
                running = re.search(r'elf_sha256=([0-9a-f]{64})', boot)
                assert running and running[1] == hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest(), 'Running firmware differs from local ELF'
                esp.command('quiet 0')
            esp.command("ping")
            cursor = esp.cursor
            time.sleep(1.7)
            baseline = reports(esp, cursor)
            if len(baseline) < 3:
                raise RuntimeError("Missing microphone baseline")
            base = {"band_power_dbfs": float(np.median([r["band_power_dbfs"] for r in baseline])),
                    "rms_dbfs": float(np.median([r["rms_dbfs"] for r in baseline]))}
            wav = out / "tone_a.wav"
            make_wav(wav)
            cursor = esp.cursor
            start = time.monotonic()
            play_wav(wav, device)
            tone = [r for r in reports(esp, cursor) if start + .5 < r["time"] < start + 2.8]
            for r in tone:
                r["band_rise_db"] = band_rise_db(r, base)
            hits = [r for r in tone if abs(r["peak_hz"] - 1000) <= 40
                    and r["band_snr_db"] >= 15 and r["band_rise_db"] >= 20]
            a = len(tone) >= 3 and len(hits) >= max(2, (len(tone) + 1)//2)
            result["host_to_esp"] = dict(passed=a, baseline_summary=base, baseline=baseline,
                                        reports=tone, passing_reports=len(hits))
            esp.command("quiet 1")
            rec = out / "rec_b.wav"
            with record_wav(rec, device, 5, 16000) as recording:
                time.sleep(1.2)  # baseline captured before the command
                cursor = esp.cursor
                command_at = time.monotonic()
                seq = esp.send(f"tone 1500 2000 {amplitude}")
                _, ack_at, ack = esp.wait_for_entry(rf"^(OK|ERR) #{seq}$", 5, since=cursor)
                if ack.startswith("ERR"):
                    raise RuntimeError(ack)
                windows = tone_windows(recording.ready_at, command_at, ack_at, 2000)
                done = esp.wait_for(rf"^(DONE|ERR) #{seq}$", 5, since=cursor)
                if done.startswith("ERR"):
                    raise RuntimeError(done)
                recording.wait()
            esp.command("quiet 0")
            base_b = analyze_wav(rec, 1500, *windows[0])
            tone_b = analyze_wav(rec, 1500, *windows[1])
            rise = band_rise_db(tone_b, base_b)
            harmonics=distortion(rec,1500,*windows[1])
            if calibration and 'chain_response_dbfs' in calibration:
                # Same measured response correction and unchanged gates as the
                # simultaneous step-7 sweep. Raw harmonics remain in the result.
                correct_measurement(harmonics,1500,calibration['chain_response_dbfs']['jabra'])
                checked_harmonics=harmonics['corrected_harmonics_dbc']
                checked_thd=harmonics['corrected_thd_percent']
            else:
                checked_harmonics=harmonics['harmonics_dbc'];checked_thd=harmonics['thd_percent']
            distortion_ok=(not calibration or (checked_harmonics['H2']<=calibration['h2_limit_dbc'] and
                           checked_harmonics['H3']<=calibration['h3_limit_dbc'] and
                           checked_thd<=calibration['thd_limit_percent']))
            b = distortion_ok and harmonics['clipping_count']==0 and rise >= 20 and tone_b["band_snr_db"] >= 15 and abs(tone_b["peak_hz"] - 1500) <= 40
            result["esp_to_host"] = dict(passed=b, ack=ack, completion=done,
                recording_ready_at=recording.ready_at, command_at=command_at, ack_at=ack_at,
                baseline_window=windows[0], tone_window=windows[1], amplitude_percent=amplitude, distortion=harmonics, distortion_passed=distortion_ok, calibration=calibration,
                baseline=base_b, tone=tone_b, band_rise_db=rise)
            # Qualification requires 5 dB margin on both dB gates for every report.
            # Frequency margin is measured in Hz, since dB does not apply to Hz.
            margins = {
                "mic_min_snr_margin_db": min((r["band_snr_db"] - 15 for r in tone), default=-15),
                "mic_min_band_rise_margin_db": min((r["band_rise_db"] - 20 for r in tone), default=-20),
                "mic_min_frequency_margin_hz": min((40 - abs(r["peak_hz"] - 1000) for r in tone), default=-40),
                "speaker_snr_margin_db": tone_b["band_snr_db"] - 15,
                "speaker_band_rise_margin_db": rise - 20,
                "speaker_frequency_margin_hz": 40 - abs(tone_b["peak_hz"] - 1500)}
            result["margins"] = margins
            result["qualification_passed"] = (a and b and tone_b["clipping_count"] == 0
                and all(value >= 5 for value in margins.values()))
            result["passed"] = a and b and not result["room_baseline"]["noise_contaminated"]
            result["qualification_passed"] &= not result["room_baseline"]["noise_contaminated"]
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        (out / "result.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
        print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["passed"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
