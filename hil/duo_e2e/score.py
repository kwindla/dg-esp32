"""Score a run: each phrase a cohort played against the turn the bot heard for it.

A turn is what journal.turns() returns: when the user turn ended (`t`), its
transcript, the character active at that moment, the route the bot proposed
(`target`) and what it then did (`switched` is the character it switched to,
or None: a proposal whose claim was lost changes nothing). A phrase's expectation
depends on that active character: addressing the one already speaking means
"stay".

Classes, one per phrase played:

    ok          the route is right and the transcript kept the character names
    stt-noise   the route is right although the transcript did not keep them
    route-miss  the transcript kept them and the route is wrong
    stt-miss    the transcript lost or mangled a name ("Bangel") and the route is wrong
    missing     the bot heard no turn for the phrase

"Kept the names" compares the sequence of "angel"/"devil" words in the phrase
and in the transcript. Other word errors do not change the class; `verbatim`
on the row says whether the whole transcript matches.
"""
from cohort import CHARACTERS, names, words

CLASSES = ("ok", "stt-noise", "route-miss", "stt-miss", "missing")


def wanted(expected, active):
    """The character a phrase should switch to, or None: addressing the active one changes nothing."""
    return expected if expected in CHARACTERS and expected != active else None


def classify(mark, turn):
    if turn is None:
        return "missing"
    routed = turn["switched"] == wanted(mark["expected"], turn["active"])  # What it did, not what it proposed.
    kept = names(turn["transcript"]) == names(mark["text"])
    return ("ok" if kept else "stt-noise") if routed else ("route-miss" if kept else "stt-miss")


def windows(marks, played):
    """(mark index, wall-clock start) of every phrase some span played whole, in time order.

    A span says the WAV was at `wav_s` at wall-clock time `t` and ran for
    `len_s`: one for a web run, one per fixture segment for an S3 run.
    """
    found = []
    for index, mark in enumerate(marks):
        span = next((s for s in played
                     if s["wav_s"] <= mark["start_s"] and mark["end_s"] <= s["wav_s"] + s["len_s"]), None)
        if span is not None:
            found.append((index, span["t"] + mark["start_s"] - span["wav_s"]))
    return sorted(found, key=lambda window: window[1])


def score(marks, turns, played=None):
    """(rows, stray): one row per phrase played, and the turns that belong to no phrase.

    With `played`, a turn belongs to the phrase that began last before the turn
    ended, so a missing or an extra turn does not shift every later pairing. A
    phrase is scored on the first turn in its window; later ones are counted
    in `extra`. Without `played` there is only order: turn n is phrase n.
    """
    turns = sorted(turns, key=lambda turn: turn["t"])
    if played is None:
        heard = {index: turns[index:index + 1] for index in range(len(marks))}
        stray = turns[len(marks):]
    else:
        starts = windows(marks, played)
        heard, stray = {index: [] for index, _ in starts}, []
        for turn in turns:
            begun = [index for index, start in starts if start <= turn["t"]]
            (heard[begun[-1]] if begun else stray).append(turn)
    rows = []
    for index, found in heard.items():
        mark, turn = marks[index], found[0] if found else None
        row = dict(phrase=index + 1, text=mark["text"], expected=mark["expected"], result=classify(mark, turn))
        if turn is not None:
            row.update(transcript=turn["transcript"], verbatim=words(turn["transcript"]) == words(mark["text"]),
                       active=turn["active"], wanted=wanted(mark["expected"], turn["active"]),
                       target=turn["target"], reason=turn["reason"], p=turn["p"], did=turn["did"],
                       switched=turn["switched"], extra=len(found) - 1)
            if "jev" in turn:
                row["jev"] = turn["jev"]
        rows.append(row)
    return rows, stray


def summary(rows):
    """How many phrases fell in each class, every class named."""
    return {name: sum(row["result"] == name for row in rows) for name in CLASSES}
