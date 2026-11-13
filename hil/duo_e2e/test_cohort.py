"""Offline: the cohort definitions, and the audio and marks built from them with a stand-in TTS."""
import json
import wave

import pytest

import cohort


class Voice:
    """Every phrase is one second of a constant sample: 1 for the first phrase asked for, 2 for the next."""
    def __init__(self):
        self.asked = []

    def __call__(self, text, voice):
        self.asked.append((text, voice))
        return len(self.asked).to_bytes(2, "little") * cohort.RATE


def sample(pcm, at_s):
    at = int(at_s * cohort.RATE) * 2
    return int.from_bytes(pcm[at:at + 2], "little")


TWO = dict(voice="aura-2-orion-en", lead_s=2, gap_s=3, tail_s=4, phrases=[
    dict(text="Devil, tell me a joke.", expected="devil"),
    dict(text="And another one?", expected="none", gap_s=0.5)])


def test_build_lays_out_lead_phrases_gaps_and_tail():
    tts = Voice()
    pcm, marks = cohort.build(TWO, tts)
    assert marks == [dict(start_s=2.0, end_s=3.0, text="Devil, tell me a joke.", expected="devil"),
                     dict(start_s=6.0, end_s=7.0, text="And another one?", expected="none")]
    assert len(pcm) == 2 * 184000  # 2 s lead, 1 s phrase, 3 s gap, 1 s phrase, its own 0.5 s gap, 4 s tail
    assert tts.asked == [("Devil, tell me a joke.", "aura-2-orion-en"), ("And another one?", "aura-2-orion-en")]
    assert [sample(pcm, at) for at in (1.99, 2.0, 2.99, 3.0, 5.99, 6.0, 6.99, 7.0, 11.49)] == [0, 1, 1, 0, 0, 2, 2, 0, 0]


def test_a_repeated_phrase_is_asked_for_once_and_played_each_time():
    tts = Voice()
    pcm, marks = cohort.build(dict(TWO, repeat=2), tts)
    assert [(mark["start_s"], mark["text"]) for mark in marks] == [
        (2.0, "Devil, tell me a joke."), (6.0, "And another one?"),
        (7.5, "Devil, tell me a joke."), (11.5, "And another one?")]
    assert len(tts.asked) == 2 and [sample(pcm, at) for at in (7.5, 11.5)] == [1, 2]


def test_build_refuses_audio_that_is_not_pcm16():
    for bad in (b"", b"\x01"):
        with pytest.raises(ValueError, match="no PCM16 audio"):
            cohort.build(TWO, lambda text, voice: bad)


def test_write_gives_the_wav_the_marks_and_the_run_length(tmp_path):
    pcm, marks = cohort.build(TWO, Voice())
    wav, marks_file = cohort.write(tmp_path / "cohorts", "two", TWO, pcm, marks)
    with wave.open(str(wav)) as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()) == (16000, 1, 2, 184000)
        assert audio.readframes(184000) == pcm
    assert json.loads(marks_file.read_text()) == dict(
        cohort="two", voice="aura-2-orion-en", duration_s=11.5, run_s=60,
        marks=[dict(start_s=2.0, end_s=3.0, text="Devil, tell me a joke.", expected="devil"),
               dict(start_s=6.0, end_s=7.0, text="And another one?", expected="none")])


def test_a_run_stops_35_seconds_into_a_long_tail_and_lasts_a_minute_at_least():
    assert cohort.run_seconds(200.3, 90) == 145  # suite.sh: int(200.3) - 55
    assert cohort.run_seconds(161.2, 0) == 161   # No tail to skip: the whole WAV.
    assert cohort.run_seconds(161.2, 20) == 161
    assert cohort.run_seconds(70.0, 90) == 60


def test_generated_files_are_refused_inside_the_repository(tmp_path):
    inside = cohort.ROOT / "hil/duo_e2e/generated-by-test"
    for path in (inside, cohort.ROOT):
        with pytest.raises(ValueError, match="inside the repository"):
            cohort.evidence(path)
    assert not inside.exists()
    assert cohort.evidence(tmp_path / "a/b") == tmp_path / "a/b" and (tmp_path / "a/b").is_dir()


def test_the_default_directory_is_under_the_evidence_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DUO_E2E_EVIDENCE", str(tmp_path / "evidence"))
    assert cohort.evidence() == tmp_path / "evidence/cohorts"
    assert cohort.evidence(kind="runs") == tmp_path / "evidence/runs"
    monkeypatch.delenv("DUO_E2E_EVIDENCE")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cohort.evidence() == tmp_path / "home/src/evidence/duo/e2e/cohorts"


def test_names_are_the_character_words_in_order():
    assert cohort.names("Devil, do you agree with the angel?") == ["devil", "angel"]
    assert cohort.names("Can I get the angel's view on this?") == ["angel"]
    assert cohort.names("Can I get the Angel\N{RIGHT SINGLE QUOTATION MARK}s view on this?") == ["angel"]
    assert cohort.names("Bangel, is it okay to skip the gym? Dangel! Angela? Devils. The angels view.") == []
    assert cohort.words("Thanks, you're an angel for saying that.") == ["thanks", "you", "re", "an", "angel", "for", "saying", "that"]
    assert cohort.words("You\N{RIGHT SINGLE QUOTATION MARK}re 2nd-best!") == ["you", "re", "2nd", "best"]


def test_the_checked_in_cohorts_are_the_scratch_generators_phrases():
    cohorts = cohort.load()
    played = {name: definition["phrases"] * definition.get("repeat", 1) for name, definition in cohorts.items()}
    assert {name: len(phrases) for name, phrases in played.items()} == dict(
        functional=4, functional2=4, stay=20, switch=20, bargein=12)
    assert {name: (c["voice"], c["lead_s"], c["gap_s"], c["tail_s"]) for name, c in cohorts.items()} == dict(
        functional=("aura-2-orion-en", 10, 22, 90), functional2=("aura-2-orion-en", 10, 22, 90),
        stay=("aura-2-orion-en", 10, 18, 90), switch=("aura-2-orion-en", 10, 22, 90),
        bargein=("aura-2-orion-en", 8, 16, 0))
    assert {phrase["expected"] for phrase in played["stay"] + played["bargein"]} == {"none"}
    # gen.py: every stay question again, behind a vocative that alternates from "Devil".
    assert [(phrase["text"], phrase["expected"]) for phrase in cohorts["switch"]["phrases"]] == [
        (f"{name.title()}, {stay['text'][0].lower()}{stay['text'][1:]}", name)
        for stay, name in zip(cohorts["stay"]["phrases"], ["devil", "angel"] * 5)]
    # genbarge.py: 4.5 s after a request, 16 s after the interruption that follows it.
    assert [phrase.get("gap_s", cohorts["bargein"]["gap_s"]) for phrase in cohorts["bargein"]["phrases"]] == [4.5, 16] * 6


def test_who_each_functional_phrase_addresses():
    expected = {phrase["text"]: phrase["expected"]
                for name in ("functional", "functional2") for phrase in cohort.load()[name]["phrases"]}
    assert expected == {
        "Devil, should I eat the whole cake tonight?": "devil",
        "And what would you put on top of it?": "none",
        "Can I hear from the angel now?": "angel",
        "Speak of the devil, I just found another cake in the fridge.": "none",
        "Devil, do you agree with the angel?": "devil",
        "Thanks, you're an angel for saying that.": "none",
        "What did you think of what the angel said?": "none",
        "Can I get the angel's view on this?": "angel",
    }


@pytest.mark.parametrize("change,message", [
    (dict(voice="aura 2"), "voice"),
    (dict(lead_s=-1), "seconds"),
    (dict(gap_s="3"), "seconds"),
    (dict(repeat=0), "repeat"),
    (dict(phrases=[]), "non-empty"),
    (dict(expect="none"), "unknown keys"),
    (dict(phrases=[dict(text="Hello there.")]), "phrase 1"),
    (dict(phrases=[dict(text="Hello there.", expected="both")]), "phrase 1"),
    (dict(phrases=[dict(text="?!", expected="none")]), "phrase 1"),
    (dict(phrases=[dict(text="Hello there.", expected="none", gap=2)]), "phrase 1"),
    # A vocative given to nobody, a vocative given to the other one, a character the phrase never names.
    (dict(phrases=[dict(text="Angel, tell me a joke.", expected="none")]), "contradicts"),
    (dict(phrases=[dict(text="Angel, what did the devil say?", expected="devil")]), "contradicts"),
    (dict(phrases=[dict(text="Tell me a joke.", expected="devil")]), "contradicts"),
])
def test_a_definition_the_scorer_would_misread_is_refused(tmp_path, change, message):
    path = tmp_path / "cohorts.json"
    path.write_text(json.dumps(dict(broken=dict(TWO, **change))))
    with pytest.raises(ValueError, match=f"cohort broken: .*{message}"):
        cohort.load(path)
