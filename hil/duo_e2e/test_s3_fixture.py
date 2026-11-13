"""Offline: the console driver against a scripted device. No serial port, no pyserial, no bench."""
from collections import deque
import json
import re
import struct
import sys
import types
import wave

import pytest

import s3_fixture as s3


def status_record(character, rev, toggles=0):
    """A DUO_STATUS line as main/duo_ui.cpp prints it."""
    return ('DUO_STATUS {"generation":3,"toggles_sent":%d,"retransmitted":0,"resyncs":0,"answered":%d,"displayed":%d,'
            '"render_busy":0,"taps_ignored_pending":0,"taps_injected":%d,"taps_physical":0,"character":"%s","rev":%d,'
            '"baseline":true,"pending":false,"internal_heap_free":141200,"internal_heap_minimum":98304}'
            % (toggles, rev, rev, toggles, character, rev))


ERRORS = 'DUO_ERRORS {"generation":3,"rev":2,"part":0,"parts":2,"errors":{"malformed":0,"session":0}}'


class Device:
    """The console at the other end of the byte transport. Its clock only moves while nothing arrives."""
    def __init__(self, answer=None):
        self.sent, self.wrote, self.times = [], [], []
        self.arriving, self.later, self.now = deque(), [], 0.0
        self.answer = answer or self.ok

    @staticmethod
    def ok(seq, text):
        return [f"OK #{seq} {text.split()[0]}"]

    def push(self, *lines):
        self.arriving.append("".join(line + "\r\n" for line in lines).encode())

    def write(self, data):
        seq, text = data.decode().rstrip("\n")[1:].split(" ", 1)
        self.wrote.append(data)
        self.sent.append(text)
        self.times.append(self.now)
        self.push(*self.answer(int(seq), text))

    def read(self):
        while self.later and self.later[0][0] <= self.now:
            self.push(*self.later.pop(0)[1])
        if self.arriving:
            return self.arriving.popleft()
        self.now += 0.05
        return b""

    def clock(self):
        return self.now

    def console(self, log=None):
        return s3.Console(self.read, self.write, (lambda line: None) if log is None else log.append, self.clock)


def test_a_command_is_numbered_and_answered_by_the_ok_with_its_own_number():
    device, log = Device(lambda seq, text: [f"OK #{seq}0 audio", f"OK #{seq} {text.split()[0]}"]), []
    device.push("I (4321) dg-esp32: STATE Ready gen=3", "OK #7 stats")
    console = device.console(log)
    assert console.command("audio 0") == "OK #1 audio"
    assert console.command("mic-fixture 2") == "OK #2 mic-fixture"
    assert device.wrote == [b"#1 audio 0\n", b"#2 mic-fixture 2\n"]
    assert log == ["> #1 audio 0", "I (4321) dg-esp32: STATE Ready gen=3", "OK #7 stats", "OK #10 audio", "OK #1 audio",
                   "> #2 mic-fixture 2", "OK #20 audio", "OK #2 mic-fixture"]


def test_err_raises_with_the_reason_the_loader_printed_before_it():
    refusals = iter([["MIC_LOAD_ERROR fixture chunk CRC mismatch: the line arrived damaged", "ERR #1 mic-load-data"],
                     ["ERR #2 duo"]])
    console = Device(lambda seq, text: next(refusals)).console()
    with pytest.raises(s3.ConsoleError) as refused:
        console.command("mic-load-data 0 4 cbca8ec0 AQD+/wMA/P8=")
    assert str(refused.value) == "mic-load-data 0 4 cbca8ec0 AQD+/wMA/P8=: fixture chunk CRC mismatch: the line arrived damaged"
    with pytest.raises(s3.ConsoleError) as busy:  # No reason of its own, and not the earlier command's.
        console.command("duo status")
    assert str(busy.value) == "duo status: ERR #2 duo"


def test_status_is_the_first_record_after_the_ack_whatever_was_printed_unasked():
    device = Device(lambda seq, text: [status_record("angel", 1), f"OK #{seq} duo", status_record("devil", 2), ERRORS,
                                       status_record("devil", 2, toggles=9)])
    device.push(status_record("angel", 1), ERRORS)  # The periodic record, printed before anything was asked.
    console = device.console()
    assert console.status() == dict(
        generation=3, toggles_sent=0, retransmitted=0, resyncs=0, answered=2, displayed=2, render_busy=0,
        taps_ignored_pending=0, taps_injected=0, taps_physical=0, character="devil", rev=2, baseline=True,
        pending=False, internal_heap_free=141200, internal_heap_minimum=98304)
    # The unasked record left behind (toggles 9) precedes the next ack, so the next status is not it.
    device.answer = lambda seq, text: [f"OK #{seq} duo", status_record("angel", 3)]
    assert (console.status()["rev"], console.status()["rev"]) == (3, 3)
    assert device.now == 0.0  # Never waited: every answer was already there.


def test_status_takes_the_next_periodic_record_when_the_asked_one_overtook_the_ack():
    device = Device(lambda seq, text: [status_record("devil", 2), f"OK #{seq} duo"])
    device.later = [(7.0, [status_record("devil", 2, toggles=1)])]
    assert device.console().status()["toggles_sent"] == 1
    assert 7.0 <= device.now < 7.2


def test_a_record_the_console_cut_short_is_skipped():
    device = Device(lambda seq, text: [f"OK #{seq} duo", 'DUO_STATUS {"generation":3,"toggles_se', status_record("angel", 4)])
    assert device.console().status()["rev"] == 4


def test_silence_is_a_timeout_not_a_guess():
    device = Device(lambda seq, text: [])
    with pytest.raises(TimeoutError, match="no answer to #1 audio 1"):
        device.console().command("audio 1")
    assert 5.0 <= device.now < 5.2
    device = Device()  # Acks, but never prints a record.
    with pytest.raises(TimeoutError, match="no DUO_STATUS record"):
        device.console().status()
    assert 12.0 <= device.now < 12.2


def test_every_line_is_redacted_before_it_is_logged_or_returned():
    device, log = Device(lambda seq, text: [f"OK #{seq} wifi ssid=daily-633"]), []
    device.arriving.extend([b"I (512) net: ssid=daily-633 pass", b"word=hunter2 ok\r\nI (513) cfg: {\"ssid\":\"My Net\",",
                            b"\"psw\":\"a b\"} user: kwin\r\n\r\nE (514) wifi: PASSPHRASE = 'x y' bssid=aa:bb\n"])
    console = device.console(log)
    console.wait(0.2)
    assert log == ["I (512) net: ssid=<r> password=<r> ok", 'I (513) cfg: {"ssid":<r>,"psw":<r>} user: <r>',
                   "E (514) wifi: PASSPHRASE = <r> bssid=<r>"]
    assert console.command("wifi-status") == "OK #1 wifi ssid=<r>"
    assert not any(secret in line for line in log for secret in ("daily-633", "hunter2", "My Net", "kwin"))
    assert s3.redact("user=ab psw:cd pass=ef password=gh ssid=ij sta_ssid: kl") == \
        "user=<r> psw:<r> pass=<r> password=<r> ssid=<r> sta_ssid: <r>"
    assert s3.redact("MIC_LOAD samples=448000 crc32=0badf00d") == "MIC_LOAD samples=448000 crc32=0badf00d"


MARKS = [dict(start_s=10.0, end_s=12.5, text="Devil, should I eat the whole cake tonight?", expected="devil"),
         dict(start_s=34.5, end_s=36.5, text="And what would you put on top of it?", expected="none"),
         dict(start_s=58.5, end_s=60.5, text="Can I hear from the angel now?", expected="angel")]


def test_each_phrase_is_a_segment_with_margins_and_close_phrases_share_one():
    assert s3.segments(MARKS, 152.0) == [(9.5, 3.5), (34.0, 3.0), (58.0, 3.0)]
    assert s3.segments(MARKS, 152.0, only={1, 3}) == [(9.5, 3.5), (58.0, 3.0)]
    # A barge-in pair: the interruption starts 4.5 s after the request ends. The next request is 16 s away.
    pairs = [dict(start_s=8.0, end_s=11.0), dict(start_s=15.5, end_s=17.5), dict(start_s=33.5, end_s=36.0),
             dict(start_s=40.5, end_s=42.0)]
    assert s3.segments(pairs, 58.0) == [(7.5, 10.5), (33.0, 9.5)]
    assert s3.segments([dict(start_s=0.2, end_s=1.8)], 2.0) == [(0.0, 2.0)]  # Margins stop at the ends of the WAV.


def test_a_segment_is_cut_from_the_wav_and_padded_with_silence():
    pcm = struct.pack("<48000h", *(index % 30000 for index in range(48000)))  # 3 s
    fixture = s3.segment(pcm, 1.0, 0.5, 2.0)
    assert len(fixture) == 64000 and fixture[:16000] == pcm[32000:48000] and fixture[16000:] == bytes(48000)
    assert s3.segment(pcm, 0.0, 3.0, 30.0)[:96000] == pcm
    for start_s, length_s, padded_s, message in ((2.8, 0.5, 2.0, "not inside the WAV"), (0.0, 0.0, 2.0, "not inside the WAV"),
                                                 (0.0, 1.0, 30.1, "does not fit the 30 s loader"),
                                                 (0.0, 1.0, 0.5, "does not fit the 30 s loader")):
        with pytest.raises(ValueError, match=message):
            s3.segment(pcm, start_s, length_s, padded_s)


FOUR = struct.pack("<4h", 1, -2, 3, -4)  # The fixture main/test_host/test_fixture_load.cpp loads.
FOUR_LINES = ["mic-load-begin 4", "mic-load-data 0 4 cbca8ec0 AQD+/wMA/P8=", "mic-load-end 4 cbca8ec0"]


def test_play_sends_the_segment_sequence_and_listens_between_capture_on_and_off():
    device = Device()
    began = s3.play(device.console(), FOUR, 2.0, wall=lambda: 1234.5)
    assert device.sent == ["audio 0", *FOUR_LINES, "mic-fixture 2", "audio 1", "audio 0", "mic-fixture 0", "audio 1"]
    assert began == 1234.5
    assert device.times[:6] == [0.0] * 6 and 2.0 <= device.times[6] < 2.1 and device.times[6:] == [device.times[6]] * 3


def test_a_refused_data_line_is_sent_again_and_any_other_refusal_is_final():
    refused = []

    def once(seq, text):
        if text.startswith("mic-load-data") and not refused:
            refused.append(seq)
            return ["MIC_LOAD_ERROR fixture chunk CRC mismatch: the line arrived damaged", f"ERR #{seq} mic-load-data"]
        return Device.ok(seq, text)

    device = Device(once)
    s3.load(device.console(), FOUR)
    assert device.sent == [FOUR_LINES[0], FOUR_LINES[1], FOUR_LINES[1], FOUR_LINES[2]] and refused == [2]

    device = Device(lambda seq, text: ["MIC_LOAD_ERROR fixture chunk is not base64", f"ERR #{seq} {text.split()[0]}"]
                    if text.startswith("mic-load-data") else Device.ok(seq, text))
    with pytest.raises(s3.ConsoleError, match="fixture chunk is not base64"):
        s3.load(device.console(), FOUR)
    assert device.sent == [FOUR_LINES[0], FOUR_LINES[1], FOUR_LINES[1], FOUR_LINES[1]]

    device = Device(lambda seq, text: ["MIC_LOAD_ERROR Speaker diagnostics disabled", f"ERR #{seq} mic-load-begin"])
    with pytest.raises(s3.ConsoleError, match="mic-load-begin 4: Speaker diagnostics disabled"):
        s3.load(device.console(), FOUR)
    assert device.sent == [FOUR_LINES[0]]


def duo_device(statuses):
    """Acks everything; each `duo status` is answered with the next of `statuses`."""
    answers = iter(statuses)
    return Device(lambda seq, text: [f"OK #{seq} duo", next(answers)] if text == "duo status" else Device.ok(seq, text))


def commands(device):
    """What was sent, with each fixture load folded into one entry."""
    return [text for text in device.sent if not text.startswith(("mic-load-data", "mic-load-end"))]


def test_a_run_plays_each_span_reads_status_around_it_and_can_finish_with_a_tap():
    device = duo_device([status_record("angel", 1), status_record("devil", 2), status_record("devil", 2),
                         status_record("devil", 2), status_record("angel", 3, toggles=1)])
    pcm = bytes(2 * 16000)  # 1 s
    notes, saved = [], []
    statuses, played = s3.run(device.console(), pcm, [(0.0, 0.25), (0.5, 0.5)], listen_s=0.25, tap=True,
                              wall=lambda: 1000 + device.now, note=notes.append, save=lambda *results: saved.append(results))
    assert commands(device) == [
        "duo status",
        "audio 0", "mic-load-begin 40000", "mic-fixture 2", "audio 1",  # 0.25 s of WAV, 0.25 s more to listen, 2 s spare
        "audio 0", "mic-fixture 0", "audio 1", "duo status",
        "audio 0", "mic-load-begin 44000", "mic-fixture 2", "audio 1",  # 0.5 s + 0.25 s + 2 s
        "audio 0", "mic-fixture 0", "audio 1", "duo status",
        "duo status", "duo tap", "duo status"]
    assert [(entry["tag"], entry["status"]["character"], entry["status"]["rev"]) for entry in statuses] == [
        ("start", "angel", 1), ("after-seg0", "devil", 2), ("after-seg1", "devil", 2),
        ("before-tap", "devil", 2), ("after-tap", "angel", 3)]
    fixture_on = [1000 + at for at, text in zip(device.times, device.sent) if text == "mic-fixture 2"]
    assert played == [dict(t=fixture_on[0], wav_s=0.0, len_s=0.25), dict(t=fixture_on[1], wav_s=0.5, len_s=0.5)]
    assert saved == [(statuses, played)]
    assert notes[0] == ("start {'character': 'angel', 'rev': 1, 'toggles_sent': 0, 'answered': 1, 'displayed': 1, "
                        "'taps_injected': 0, 'generation': 3}")
    assert notes[1] == "segment 0: 0.0 s + 0.25 s, loading 40000 samples"
    # Six seconds between the tap and the status after it.
    tap = device.sent.index("duo tap")
    assert 6.0 <= device.times[tap + 1] - device.times[tap] < 6.1


def test_a_run_that_fails_still_saves_what_it_measured():
    device = duo_device([status_record("angel", 1)])
    answer = device.answer
    device.answer = lambda seq, text: (["MIC_LOAD_ERROR No loaded fixture", f"ERR #{seq} mic-fixture"]
                                       if text == "mic-fixture 2" else answer(seq, text))
    saved = []
    with pytest.raises(s3.ConsoleError, match="mic-fixture 2: No loaded fixture"):
        s3.run(device.console(), bytes(2 * 16000), [(0.0, 0.25)], listen_s=0.25, save=lambda *results: saved.append(results))
    assert [[entry["tag"] for entry in statuses] for statuses, _ in saved] == [["start"]] and saved[0][1] == []
    # The board is not left without its microphone.
    assert commands(device) == ["duo status", "audio 0", "mic-load-begin 40000", "mic-fixture 2",
                                "audio 0", "mic-fixture 0", "audio 1"]


class Interrupted(Device):
    """Ctrl-C one second into the listening."""
    fired = False

    def read(self):
        if self.now >= 1.0 and not self.fired:
            self.fired = True
            raise KeyboardInterrupt
        return super().read()


def test_an_interrupted_or_failed_play_gives_the_board_its_microphone_back():
    device = Interrupted()
    with pytest.raises(KeyboardInterrupt):
        s3.play(device.console(), FOUR, 5.0)
    assert device.sent == ["audio 0", *FOUR_LINES, "mic-fixture 2", "audio 1", "audio 0", "mic-fixture 0", "audio 1"]
    assert 1.0 <= device.times[-1] < 1.1  # At once, not after the rest of the listening.

    # The restoring is best effort: a board that refuses to drop the fixture still gets `audio 1`,
    # and the error raised is the one that stopped the run.
    def no_diagnostics(seq, text):
        if text.startswith("mic-"):
            return ["MIC_LOAD_ERROR Speaker diagnostics disabled", f"ERR #{seq} {text.split()[0]}"]
        return Device.ok(seq, text)

    device = Device(no_diagnostics)
    with pytest.raises(s3.ConsoleError, match="mic-load-begin 4: Speaker diagnostics disabled"):
        s3.play(device.console(), FOUR, 5.0)
    assert device.sent == ["audio 0", "mic-load-begin 4", "audio 0", "mic-fixture 0", "audio 1"]

    device = Device(lambda seq, text: Device.ok(seq, text) if seq == 1 else [])  # Dead after the first answer.
    with pytest.raises(TimeoutError, match="no answer to #2 mic-load-begin 4"):
        s3.play(device.console(), FOUR, 5.0)
    assert device.sent == ["audio 0", "mic-load-begin 4", "audio 0", "mic-fixture 0", "audio 1"]


def test_the_first_capture_off_is_already_covered_by_the_restore():
    # The device switches capture off before it acknowledges `audio 0` (main/console.cpp), so a
    # lost ack, or an interrupt while waiting for it, must already put the microphone back.
    device = Device(lambda seq, text: [] if seq == 1 else Device.ok(seq, text))  # The first ack is lost.
    with pytest.raises(TimeoutError, match="no answer to #1 audio 0") as stopped:
        s3.play(device.console(), FOUR, 5.0)
    assert device.sent == ["audio 0", "audio 0", "mic-fixture 0", "audio 1"]
    assert not getattr(stopped.value, "__notes__", [])  # Restored: nothing to warn about.

    class InterruptedAtOnce(Device):
        """Ctrl-C while the first command waits for its ack."""
        fired = False

        def read(self):
            if self.sent == ["audio 0"] and not self.fired:
                self.fired = True
                raise KeyboardInterrupt
            return super().read()

    device = InterruptedAtOnce()
    with pytest.raises(KeyboardInterrupt):
        s3.play(device.console(), FOUR, 5.0)
    assert device.sent == ["audio 0", "audio 0", "mic-fixture 0", "audio 1"]


def test_every_restore_step_is_tried_and_a_board_left_unrestored_is_said_so():
    class PortFailsOnce(Device):
        """The third write, the first step of the restore, fails in the port."""
        def write(self, data):
            if len(self.wrote) == 2 and not getattr(self, "failed", False):
                self.failed = True
                raise OSError("port gone")
            super().write(data)

    def refuses_the_load(seq, text):
        if text.startswith("mic-load-begin"):
            return ["MIC_LOAD_ERROR No memory for the fixture", f"ERR #{seq} mic-load-begin"]
        return Device.ok(seq, text)

    device = PortFailsOnce(refuses_the_load)
    with pytest.raises(s3.ConsoleError, match="mic-load-begin 4: No memory for the fixture") as stopped:
        s3.play(device.console(), FOUR, 5.0)
    # The error is the one that ended the run, the two later steps were still sent,
    # and the step that failed is named.
    assert device.sent == ["audio 0", "mic-load-begin 4", "mic-fixture 0", "audio 1"]
    assert stopped.value.__notes__ == [
        "THE BOARD WAS NOT RESTORED: its microphone may be off or the fixture still playing (audio 0: port gone)"]
    assert s3.restore(Device().console()) == []

    # A board that no longer answers: every step is tried and every one is reported.
    dead = Device(lambda seq, text: [])
    assert s3.restore(dead.console()) == ["audio 0: no answer to #1 audio 0", "mic-fixture 0: no answer to #2 mic-fixture 0",
                                          "audio 1: no answer to #3 audio 1"]


def test_a_segment_too_long_for_the_loader_is_refused_before_the_board_hears_anything():
    device = duo_device([status_record("angel", 1)])
    with pytest.raises(ValueError, match="does not fit the 30 s loader"):  # 20 s + 9 s + 2 s spare
        s3.run(device.console(), bytes(2 * 16000 * 25), [(0.0, 1.0), (0.0, 20.0)], listen_s=9.0)
    assert device.sent == []


def test_main_refuses_to_run_outside_the_bench(tmp_path, monkeypatch):
    for name in ("DG_BENCH_LOCK_FD", "DG_BUILD_LOCK_HELD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DUO_E2E_EVIDENCE", str(tmp_path / "evidence"))
    monkeypatch.setitem(sys.modules, "serial", None)  # An import of pyserial would raise ImportError instead.
    with pytest.raises(SystemExit, match="Run under hil/bench.py with the inherited bench lock"):
        s3.main([str(tmp_path / "functional.wav")])
    for bad in (["--segments", "9.5"], ["--segments", "9.5:3:1"], ["--segments", "a:b"], ["--only", "one"]):
        with pytest.raises(SystemExit) as usage:
            s3.main([str(tmp_path / "functional.wav"), *bad])
        assert usage.value.code == 2
    assert not (tmp_path / "evidence").exists()


class Port:
    """What main() uses of serial.Serial, with a scripted device behind it."""
    def __init__(self, device, calls):
        self.device, self.calls, self.dtr, self.rts, self.port, self.in_waiting = device, calls, None, None, None, 0

    def __call__(self, port, baud, **options):
        self.calls.append(("Serial", port, baud, options))
        return self

    def open(self):
        self.calls.append(("open", self.port, self.dtr, self.rts))

    def read(self, size):
        return self.device.read()

    def write(self, data):
        self.device.write(data)

    def flush(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.calls.append(("close",))


def test_main_checks_the_bench_takes_the_pinned_port_and_leaves_a_run_directory(tmp_path, monkeypatch, capsys):
    wav = tmp_path / "cohorts/functional.wav"
    wav.parent.mkdir()
    with wave.open(str(wav), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(bytes(2 * 16000 * 3))
    cohort_marks = dict(cohort="functional", run_s=60, marks=[
        dict(start_s=0.5, end_s=1.0, text="Devil, should I eat the whole cake tonight?", expected="devil"),
        dict(start_s=2.0, end_s=2.5, text="And what would you put on top of it?", expected="none")])
    wav.with_suffix(".json").write_text(json.dumps(cohort_marks))

    device = duo_device([status_record("angel", 1), status_record("devil", 2)])
    answer = device.answer
    device.answer = lambda seq, text: (answer(seq, text) + ["I (9) dg_audio: MIC_GATE t=1 closed=0 pre=-60.0 post=-60.0",
                                                           "I (9) wifi: ssid=daily-633"] if text == "audio 1" else answer(seq, text))
    calls = []
    monkeypatch.setattr(s3.boards, "require_bench", lambda: calls.append(("require_bench",)))
    monkeypatch.setattr(s3.boards, "resolve_port", lambda requested=None, *, board: calls.append(("resolve_port", board)) or "/dev/pinned")
    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=Port(device, calls)))
    console = s3.Console
    monkeypatch.setattr(s3, "Console", lambda read, write, log: console(read, write, log, device.clock))
    out = tmp_path / "runs/s3-functional"

    with pytest.raises(SystemExit) as nothing:  # No such phrase: refused before a directory or the port exists.
        s3.main([str(wav), "--out", str(out), "--only", "9"])
    assert nothing.value.code == 2 and not out.exists() and calls == [("require_bench",), ("resolve_port", "echoear")]
    calls.clear()
    out.mkdir(parents=True)
    (out / "report.json").write_text("{}")  # An earlier run's report must not survive beside this run's data.

    s3.main([str(wav), "--out", str(out), "--only", "1", "--listen", "0.5"])

    assert calls == [("require_bench",), ("resolve_port", "echoear"),
                     ("Serial", None, 115200, dict(timeout=0.05, write_timeout=5)), ("open", "/dev/pinned", True, True), ("close",)]
    assert sorted(path.name for path in out.iterdir()) == ["end", "marks.json", "played.json", "serial.log", "start", "status.json"]
    assert json.loads((out / "marks.json").read_text()) == cohort_marks
    played = json.loads((out / "played.json").read_text())
    assert [(span["wav_s"], span["len_s"]) for span in played] == [(0.0, 1.5)]  # Phrase 1 with its margins, phrase 2 left out.
    assert [(entry["tag"], entry["status"]["character"]) for entry in json.loads((out / "status.json").read_text())] == [
        ("start", "angel"), ("after-seg0", "devil")]
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\n", (out / name).read_text()) for name in ("start", "end"))
    log = (out / "serial.log").read_text()
    assert "MIC_GATE" not in log and "daily-633" not in log and "I (9) wifi: ssid=<r>" in log
    assert "> #2 audio 0" in log and "OK #2 audio" in log and "DUO_STATUS" in log
    assert commands(device) == ["duo status", "audio 0", "mic-load-begin 64000", "mic-fixture 2", "audio 1",  # 1.5 + 0.5 + 2 s
                                "audio 0", "mic-fixture 0", "audio 1", "duo status"]
    shown = capsys.readouterr().out
    assert "after-seg0 {'character': 'devil', 'rev': 2," in shown and f"run directory: {out}" in shown
