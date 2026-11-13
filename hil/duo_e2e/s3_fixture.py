#!/usr/bin/env python3
"""Play cohort phrases into the EchoEar's microphone path through the console fixture loader.

    DG_BOARD=echoear python3 hil/bench.py uv run --project server --frozen --offline --with pyserial \\
        python hil/duo_e2e/s3_fixture.py ~/src/evidence/duo/e2e/cohorts/functional.wav

Coordinator only, and only under hil/bench.py: main() refuses without the
inherited bench lock and opens nothing but the pinned EchoEar port. The image
needs CONFIG_DG_SPEAKER_DIAGNOSTICS, and the board a duo session with the bot.

The WAV is played in segments: one phrase each, or one run of phrases that
follow each other closely (a barge-in pair). Per segment:

    audio 0, mic-load-*, mic-fixture 2, audio 1, <listen>, audio 0, mic-fixture 0, audio 1, duo status

Everything above main() works on an injected byte transport and is what the
tests run. The run directory gets serial.log (every line redacted), status.json,
played.json and marks.json for journal.py, and the start/end of its window.
"""
import argparse
from collections import deque
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[1:1] = [str(ROOT / "hil"), str(ROOT / "scripts")]  # After this directory: its own modules win.
import boards  # noqa: E402
import mic_fixture_lines  # noqa: E402

from cohort import RATE, evidence  # noqa: E402

# Silence kept around a phrase. `audio 1` sends 100 ms of zeros while the
# capture filter settles, and the fixture has been advancing since `mic-fixture 2`.
LEAD_S = TRAIL_S = 0.5
# Phrases this close are one segment: a barge-in has to land in the reply to the phrase before it.
JOIN_S = 10.0
LISTEN_S = 9.0  # After a segment's audio, for the reply. s3switch.py's value.
# The fixture loops (dg_audio pipeline.cpp). This much more silence than the
# listening time keeps the phrase from coming round a second time.
SPARE_S = 2.0
STATUS_KEYS = ("character", "rev", "toggles_sent", "answered", "displayed", "taps_injected", "generation")
SECRET = re.compile(r"""((?:user|psw|pass|ssid)\w*["']?\s*[:=]\s*)("[^"]*"|'[^']*'|\S+)""", re.I)


def redact(text):
    """Serial text without WiFi names, user names or passwords."""
    return SECRET.sub(r"\1<r>", text)


class ConsoleError(RuntimeError):
    """The device answered ERR."""


class Console:
    """Sequenced console commands over a byte transport; every received line is redacted, then logged.

    `read()` returns the bytes that have arrived, or b"" after a short wait of
    its own; `write(data)` sends. Nothing received is dropped: lines that
    follow an answer wait for the next call.
    """

    def __init__(self, read, write, log=lambda line: None, clock=time.monotonic):
        self.read, self.write, self.log, self.clock = read, write, log, clock
        self.seq, self.lines, self.partial = 0, deque(), b""

    def _line(self, deadline):
        """The next received line, or None once `deadline` has passed with none waiting."""
        while not self.lines:
            if self.clock() >= deadline:
                return None
            *whole, self.partial = (self.partial + self.read()).split(b"\n")
            for raw in whole:
                line = redact(raw.decode("utf-8", "replace").rstrip())
                if line:
                    self.log(line)
                    self.lines.append(line)
        return self.lines.popleft()

    def wait(self, seconds):
        """Keep reading, and so logging, for `seconds`."""
        deadline = self.clock() + seconds
        while self._line(deadline) is not None:
            pass

    def command(self, text, timeout=5.0):
        """Send `#<seq> <text>` and return its `OK #<seq> <word>` line.

        main/console.cpp answers every command once, with OK or ERR and the
        command's sequence number. A refused fixture line states its reason on
        the line before the ERR, `MIC_LOAD_ERROR <reason>`; the error raised
        here carries it.
        """
        self.seq += 1
        self.log(f"> #{self.seq} {redact(text)[:72]}")
        self.write(f"#{self.seq} {text}\n".encode())
        answer, reason, deadline = re.compile(rf"(OK|ERR) #{self.seq}(?:\s|$)"), None, self.clock() + timeout
        while (line := self._line(deadline)) is not None:
            if line.startswith("MIC_LOAD_ERROR "):
                reason = line[len("MIC_LOAD_ERROR "):]
            found = answer.match(line)
            if found and found[1] == "ERR":
                raise ConsoleError(f"{text[:40]}: {reason or line}")
            if found:
                return line
        raise TimeoutError(f"no answer to #{self.seq} {text[:40]}")

    def status(self, timeout=12.0):
        """The device's duo state: the first DUO_STATUS record after the ack of our own `duo status`.

        The console task queues the request for the app-control task, writes
        the ack and only then blocks (main/console.cpp, main/app.cpp); both
        tasks have priority 4 on core 0, so the record the request prints
        (main/duo_ui.cpp) normally follows the ack. The device also prints the
        same record unasked every 10 s. Any record before the ack may predate
        the request and is ignored; any record after it is at least as new as
        the request, whichever of the two it is.

        Rarely a tick hands the core to app-control between the request and
        the ack, and the requested record comes first. The next periodic
        record is then the answer, which is why the wait outlasts 10 s.
        """
        deadline = self.clock() + timeout
        self.command("duo status")
        while (line := self._line(deadline)) is not None:
            if line.startswith("DUO_STATUS "):
                try:
                    return json.loads(line[len("DUO_STATUS "):])
                except ValueError:
                    pass  # The console queue cut this record short; the next one is whole.
        raise TimeoutError("no DUO_STATUS record followed the ack of `duo status`")


def segments(marks, duration_s, only=None):
    """(start_s, length_s) of the WAV per segment: each phrase with its margins, close phrases together.

    `only` keeps the phrases with those numbers, counted from 1 as the report counts them.
    """
    spans, last_end = [], None
    for number, mark in enumerate(marks, 1):
        if only is not None and number not in only:
            continue
        end = min(duration_s, mark["end_s"] + TRAIL_S)
        if last_end is not None and mark["start_s"] - last_end < JOIN_S:
            spans[-1] = (spans[-1][0], round(end - spans[-1][0], 3))
        else:
            start = max(0.0, mark["start_s"] - LEAD_S)
            spans.append((round(start, 3), round(end - start, 3)))
        last_end = mark["end_s"]
    return spans


def segment(pcm, start_s, length_s, padded_s):
    """The fixture for one segment: `length_s` of the WAV from `start_s`, then silence up to `padded_s`."""
    first, count, total = (int(round(seconds * RATE)) for seconds in (start_s, length_s, padded_s))
    cut = pcm[first * 2:(first + count) * 2]
    if count < 1 or len(cut) != count * 2:
        raise ValueError(f"segment {start_s}:{length_s} is not inside the WAV")
    if not count <= total <= mic_fixture_lines.MAX_SAMPLES:
        raise ValueError(f"segment {start_s}:{length_s} padded to {padded_s} s does not fit the 30 s loader")
    return cut + b"\0\0" * (total - count)


def load(console, pcm, attempts=3):
    """Send the fixture one acknowledged line at a time, in mic_fixture_lines' own chunks.

    The device keeps nothing of a data line it refuses (main/fixture_load.h),
    so a refused data line is sent again. Any other refusal is final.
    """
    for line in mic_fixture_lines.lines(pcm):
        for attempt in range(1, attempts + 1):
            try:
                console.command(line)
                break
            except ConsoleError:
                if not line.startswith("mic-load-data ") or attempt == attempts:
                    raise


def restore(console):
    """After a failure: the fixture off and the board's own microphone on again. Returns what did not work.

    A run that stopped half way would otherwise leave the board deaf, or
    leave the fixture looping into the session. Each step is tried whatever
    happened to the one before it, a failing port included. A board that no
    longer answers cannot be restored from here, and the caller is told.
    """
    failed = []
    for text in ("audio 0", "mic-fixture 0", "audio 1"):
        try:
            console.command(text)
        except (ConsoleError, TimeoutError, OSError) as error:
            failed.append(f"{text}: {error}")
    return failed


def play(console, pcm, listen_s, wall=time.time):
    """Play one fixture once and return the wall-clock time it began.

    The loader and the fixture switch refuse while capture is on, hence the
    `audio 0` on either side. `mic-fixture 2` puts the fixture at its first
    sample, and it advances from then on whether or not capture is on.
    """
    try:
        # Inside the handler from the first command on: the device switches capture off
        # before it acknowledges, so a lost ack or an interrupt here already needs the restore.
        console.command("audio 0")
        load(console, pcm)
        console.command("mic-fixture 2")
        began = wall()
        console.command("audio 1")
        console.wait(listen_s)
        console.command("audio 0")
        console.command("mic-fixture 0")
        console.command("audio 1")
    except BaseException as error:  # An interrupt from the keyboard too.
        failed = restore(console)
        if failed:
            error.add_note("THE BOARD WAS NOT RESTORED: its microphone may be off or the fixture still playing ("
                           + "; ".join(failed) + ")")
        raise
    return began


def run(console, pcm, spans, *, listen_s=LISTEN_S, tap=False, wall=time.time,
        note=lambda text: None, save=lambda statuses, played: None):
    """Play every span, with a status before the first and after each; then optionally one injected double-tap.

    Returns (statuses, played). `save` receives both however the run ends, so
    a run that fails half way still leaves what it measured.
    """
    statuses, played = [], []
    # Cut first: a span that does not fit is refused before the board hears anything.
    fixtures = [segment(pcm, start_s, length_s, length_s + listen_s + SPARE_S) for start_s, length_s in spans]

    def status(tag):
        state = console.status()
        statuses.append(dict(tag=tag, t=wall(), status=state))
        note(f"{tag} {({key: state.get(key) for key in STATUS_KEYS})}")

    try:
        console.wait(1.0)  # Whatever the device was printing when the port opened.
        status("start")
        for index, ((start_s, length_s), fixture) in enumerate(zip(spans, fixtures)):
            note(f"segment {index}: {start_s} s + {length_s} s, loading {len(fixture) // 2} samples")
            began = play(console, fixture, length_s + listen_s, wall)
            played.append(dict(t=began, wav_s=start_s, len_s=length_s))
            status(f"after-seg{index}")
            console.wait(3.0)
        if tap:
            status("before-tap")
            console.command("duo tap")
            console.wait(6.0)  # s3check.py's wait: the switch, its answer and the new face.
            status("after-tap")
    finally:
        save(statuses, played)
    return statuses, played


def main(argv=None):
    parser = argparse.ArgumentParser(description="Play a cohort WAV into the EchoEar microphone path, under hil/bench.py.")
    parser.add_argument("wav", type=Path, help="a cohort WAV; its marks are the .json beside it")
    parser.add_argument("--out", help="run directory (default: ~/src/evidence/duo/e2e/runs/s3-<cohort>-<time>)")
    parser.add_argument("--only", help="phrase numbers to play, such as 1,3 (default: all)")
    parser.add_argument("--segments", help="start:length[,start:length...] in seconds, instead of the marks")
    parser.add_argument("--listen", type=float, default=LISTEN_S, help="seconds to listen after each segment")
    parser.add_argument("--tap", action="store_true", help="finish with one injected double-tap")
    args = parser.parse_args(argv)
    try:
        only = {int(number) for number in args.only.split(",")} if args.only else None
        spans = [tuple(map(float, span.split(":"))) for span in args.segments.split(",")] if args.segments else None
        if spans and any(len(span) != 2 for span in spans):
            raise ValueError
    except ValueError:
        parser.error("--only takes numbers such as 1,3; --segments takes start:length[,start:length...]")
    try:
        boards.require_bench()
        port = boards.resolve_port(board="echoear")
    except (RuntimeError, OSError) as error:
        raise SystemExit(f"s3_fixture: {error}")
    pcm = mic_fixture_lines.read_wav(args.wav)
    marks_file = args.wav.with_suffix(".json")
    marks = json.loads(marks_file.read_text()) if marks_file.exists() else None
    if spans is None and marks:
        spans = segments(marks["marks"], len(pcm) // 2 / RATE, only)
    if not spans:
        parser.error(f"nothing to play: no phrase selected from {marks_file}, and no --segments")
    out = evidence(args.out or evidence(kind="runs") / f"s3-{args.wav.stem}-{time.strftime('%Y%m%d-%H%M%S')}")
    for name in ("marks.json", "played.json", "status.json", "report.json", "end"):
        (out / name).unlink(missing_ok=True)  # Nothing of an earlier run in this directory passes for this one's.
    if marks:
        (out / "marks.json").write_text(json.dumps(marks, indent=1) + "\n")

    def save(statuses, played):
        (out / "status.json").write_text(json.dumps(statuses, indent=1) + "\n")
        (out / "played.json").write_text(json.dumps(played, indent=1) + "\n")
        # A second later: journalctl --until stops at the start of the second it is given.
        (out / "end").write_text(time.strftime("%Y-%m-%d %H:%M:%S\n", time.localtime(time.time() + 1)))

    import serial  # pyserial, here only: everything above runs without the package or a port
    link = serial.Serial(None, 115200, timeout=0.05, write_timeout=5)
    # Both control lines stay asserted across the open, as in hil/esp_serial.py.
    # Dropping them one after the other passes through RTS=1, DTR=0, and that
    # resets a USB-JTAG board: this run attaches to a live session.
    link.dtr = link.rts = True
    link.port = port
    link.open()
    began = time.time()
    (out / "start").write_text(time.strftime("%Y-%m-%d %H:%M:%S\n", time.localtime(began)))
    with link, (out / "serial.log").open("w") as log:
        def record(line):
            if "MIC_GATE" not in line:  # Ten a second while capture is on.
                log.write(f"{time.time() - began:8.2f} {line}\n")

        def send(data):
            link.write(data)
            link.flush()

        console = Console(lambda: link.read(link.in_waiting or 1), send, record)
        run(console, pcm, spans, listen_s=args.listen, tap=args.tap, save=save,
            note=lambda text: print(f"{time.time() - began:6.1f} {text}", flush=True))
    print(f"run directory: {out}")


if __name__ == "__main__":
    main()
