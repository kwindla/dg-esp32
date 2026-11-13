#!/usr/bin/env python3
"""Cohorts: the phrases a run plays into the microphone path, and who each one addresses.

    uv run --project server python hil/duo_e2e/cohort.py functional functional2 stay switch bargein

That command is the coordinator's: it loads the Deepgram key by name, asks
Deepgram TTS for each distinct phrase and writes <cohort>.wav and <cohort>.json
(the marks) under ~/src/evidence/duo/e2e/cohorts. Everything above deepgram()
is pure, and is all the tests run.

cohorts.json gives each cohort its TTS voice, its silences in seconds (lead_s
before the first phrase, gap_s after every phrase that does not name its own,
tail_s at the end), an optional repeat count and its ordered phrases. A phrase
states the character it addresses: "angel", "devil" or "none". A leading
vocative or an explicit request to hear from a character addresses that
character; a mention or an idiom addresses nobody.
"""
import argparse
import io
import json
import os
from pathlib import Path
import re
import sys
import urllib.request
import wave

ROOT = Path(__file__).resolve().parents[2]
DEFINITIONS = Path(__file__).with_name("cohorts.json")
RATE = 16000
CHARACTERS = ("angel", "devil")
ADDRESSEES = (*CHARACTERS, "none")
# suite.sh stopped a run 55 s before the end of a WAV with a 90 s tail: 35 s into the tail.
RUN_TAIL_S = 35
RUN_FLOOR_S = 60


def words(text):
    """A phrase or a transcript as lower-case runs of letters and digits: what two renderings of it share.

    Punctuation and either kind of apostrophe separate words, so "the Angel's view" has the word "angel".
    """
    return re.findall(r"[a-z0-9]+", text.lower())


def names(text):
    """The character names said, in order. Routing can only be judged on a turn whose transcript kept them."""
    return [word for word in words(text) if word in CHARACTERS]


def _seconds(value):
    return type(value) in (int, float) and value >= 0


def check(cohort):
    """Refuse a definition the builder or the scorer would misread."""
    unknown = set(cohort) - {"voice", "lead_s", "gap_s", "tail_s", "repeat", "phrases"}
    if unknown:
        raise ValueError(f"unknown keys {sorted(unknown)}")
    if not isinstance(cohort.get("voice"), str) or not re.fullmatch(r"[a-z0-9-]+", cohort["voice"]):
        raise ValueError("voice must be a Deepgram model name")
    if not all(_seconds(cohort.get(key)) for key in ("lead_s", "gap_s", "tail_s")):
        raise ValueError("lead_s, gap_s and tail_s must be seconds")
    if type(cohort.get("repeat", 1)) is not int or cohort.get("repeat", 1) < 1:
        raise ValueError("repeat must be a positive integer")
    if not isinstance(cohort.get("phrases"), list) or not cohort["phrases"]:
        raise ValueError("phrases must be a non-empty list")
    for number, phrase in enumerate(cohort["phrases"], 1):
        if not isinstance(phrase, dict) or not {"text", "expected"} <= set(phrase) <= {"text", "expected", "gap_s"}:
            raise ValueError(f"phrase {number}: text, expected and at most gap_s")
        text, expected = phrase["text"], phrase["expected"]
        if (not isinstance(text, str) or not words(text) or expected not in ADDRESSEES or
                not _seconds(phrase.get("gap_s", 0))):
            raise ValueError(f"phrase {number}: a spoken text, expected angel|devil|none, gap_s in seconds")
        vocative = re.match(r"(angel|devil),", text, re.I)
        # The two mistakes a typo makes: a vocative given to nobody or to the other
        # character, and a character expected to answer whom the phrase never names.
        if (vocative and vocative[1].lower() != expected) or (expected != "none" and expected not in names(text)):
            raise ValueError(f"phrase {number}: expected {expected!r} contradicts {text!r}")


def load(path=DEFINITIONS):
    cohorts = json.loads(Path(path).read_text())
    for name, cohort in cohorts.items():
        try:
            check(cohort)
        except ValueError as error:
            raise ValueError(f"cohort {name}: {error}") from None
    return cohorts


def silence(seconds):
    return b"\0\0" * int(RATE * seconds)


def build(cohort, tts):
    """The cohort as PCM16 mono at RATE, and one mark per phrase played.

    `tts(text, voice)` returns PCM16 mono at RATE. It is asked once per
    distinct phrase, so a repeated phrase is the same audio each time.
    """
    pcm, marks, spoken = bytearray(silence(cohort["lead_s"])), [], {}
    for phrase in cohort["phrases"] * cohort.get("repeat", 1):
        text = phrase["text"]
        if text not in spoken:
            spoken[text] = tts(text, cohort["voice"])
            if not spoken[text] or len(spoken[text]) % 2:
                raise ValueError(f"no PCM16 audio for {text!r}")
        start = len(pcm) // 2
        pcm += spoken[text]
        marks.append(dict(start_s=round(start / RATE, 3), end_s=round(len(pcm) // 2 / RATE, 3),
                          text=text, expected=phrase["expected"]))
        pcm += silence(phrase.get("gap_s", cohort["gap_s"]))
    pcm += silence(cohort["tail_s"])
    return bytes(pcm), marks


def run_seconds(duration_s, tail_s):
    """How long a driver plays the WAV: all of it but the part of the tail nobody needs to wait for."""
    return max(RUN_FLOOR_S, int(duration_s - max(0, tail_s - RUN_TAIL_S)))


def outside(path):
    """`path` resolved, and refused if it lies in the repository: nothing generated is written there."""
    path = Path(path).expanduser().resolve()
    if path == ROOT or ROOT in path.parents:
        raise ValueError(f"{path} is inside the repository; generated audio and logs belong under ~/src/evidence")
    return path


def evidence(path=None, kind="cohorts"):
    """The directory generated files go to, created: `path`, or `kind` under ~/src/evidence/duo/e2e."""
    out = outside(path or Path(os.environ.get("DUO_E2E_EVIDENCE") or Path.home() / "src/evidence/duo/e2e") / kind)
    out.mkdir(parents=True, exist_ok=True)
    return out


def write(out, name, cohort, pcm, marks):
    """<name>.wav for the microphone path and <name>.json for the driver and the scorer."""
    out = evidence(out)
    with wave.open(str(out / f"{name}.wav"), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(RATE)
        audio.writeframes(pcm)
    duration = len(pcm) // 2 / RATE
    (out / f"{name}.json").write_text(json.dumps(dict(
        cohort=name, voice=cohort["voice"], duration_s=round(duration, 3),
        run_s=run_seconds(duration, cohort["tail_s"]), marks=marks), indent=1) + "\n")
    return out / f"{name}.wav", out / f"{name}.json"


def deepgram(key):
    """tts(text, voice) over Deepgram's REST endpoint. The key travels in a header and is never printed."""
    def tts(text, voice):
        request = urllib.request.Request(
            f"https://api.deepgram.com/v1/speak?model={voice}&encoding=linear16&sample_rate={RATE}&container=wav",
            data=json.dumps({"text": text}).encode(), method="POST",
            headers={"Authorization": f"Token {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response, wave.open(io.BytesIO(response.read())) as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (RATE, 1, 2):
                raise ValueError("TTS did not return 16 kHz mono PCM16")
            return audio.readframes(audio.getnframes())
    return tts


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build cohort WAVs and marks with Deepgram TTS (coordinator only).")
    parser.add_argument("cohorts", nargs="+", metavar="cohort")
    parser.add_argument("--out", help="output directory (default: ~/src/evidence/duo/e2e/cohorts)")
    args = parser.parse_args(argv)
    cohorts = load()
    unknown = [name for name in args.cohorts if name not in cohorts]
    if unknown:
        parser.error(f"unknown cohort {unknown[0]!r}; cohorts.json has {', '.join(cohorts)}")
    out = evidence(args.out)
    sys.path.insert(0, str(ROOT / "server"))
    import settings
    settings.load_credentials(approved_only=True)
    tts = deepgram(os.environ["DEEPGRAM_API_KEY"])
    for name in args.cohorts:
        pcm, marks = build(cohorts[name], tts)
        wav, _ = write(out, name, cohorts[name], pcm, marks)
        print(f"{name}: {len(marks)} phrases, {len(pcm) // 2 / RATE:.1f} s, {wav}")


if __name__ == "__main__":
    main()
