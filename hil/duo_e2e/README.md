# duo_e2e: end-to-end routing checks for the duo bot

Plays prepared phrases into the microphone path of a duo device (the web
simulator under `server/duo_web/`, or the EchoEar through its console fixture
loader), then reads the bot's log and says, phrase by phrase, whether the bot
routed the turn as expected: switch to the character addressed, or stay.

Only the coordinator runs the live tools. The parsers, the scorer, the latency
summary, the cohort builder and the console driver are pure and have offline
tests:

```sh
TMPDIR=$HOME/.cache/gap3-tmp OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server:scripts \
  uv run --project server --frozen --offline --group dev python -m pytest hil/duo_e2e -q -p no:cacheprovider \
  --basetemp "$HOME/.cache/gap3-tmp/pytest-duo-e2e"
```

## The tools

| File | What it does | Live? |
| --- | --- | --- |
| `cohorts.json` | The cohorts: voice, silences, ordered phrases, and whom each phrase addresses. | |
| `cohort.py` | Validates the cohorts; builds a cohort's WAV and marks from a TTS callable. Its command line calls Deepgram TTS. | network |
| `bot.sh` | Stops, or stops and starts, the A/B bot on 7861. Destructive: read its header. | services, processes |
| `botproc.py` | Says which processes `bot.sh` may kill: this user's Python processes started exactly as the launchers start the bot, from a checkout of this repository. | reads `/proc` |
| `web_driver.mjs` | Opens the web simulator in headless Chrome with the WAV as the microphone, connects, optionally double-taps, logs the page, takes a screenshot. | Chrome, the simulator |
| `run_suite.sh` | One run directory per cohort through `web_driver.mjs`, with the journal window around it. | as above |
| `s3_fixture.py` | Loads segments of the WAV into the EchoEar as microphone fixtures over the console and plays them; reads `duo status` around each. | serial, under `hil/bench.py` |
| `journal.py` | Parses the bot's log lines; its command line fetches a run's window from the user journal, scores it and prints the report. | `journalctl` |
| `score.py` | Aligns phrases with the turns the bot heard and classifies each phrase. | |
| `latency.py` | Reply-timing percentiles per route decision, barge-in latency, redacted error lines. | |

### Cohorts and expectations

A phrase addresses `angel`, `devil` or `none`. A leading vocative ("Devil, ...")
or an explicit request to hear from one ("Can I hear from the angel now?")
addresses that character. A mention or an idiom does not ("Speak of the devil,
...", "you're an angel for saying that"). `cohort.py` refuses the two slips a
typo makes: a leading vocative expected to address anyone else, and an
expected character the phrase never names.

What a phrase should do depends on who is active when the turn ends: addressing
the character who is already speaking means stay. The active character comes
from the bot's own `DUO outcome` lines, so it follows auto switches, taps and
rollbacks.

### Classes

| Class | Route | Transcript |
| --- | --- | --- |
| `ok` | right | kept the character names |
| `stt-noise` | right | lost or mangled a name |
| `route-miss` | wrong | kept the character names |
| `stt-miss` | wrong | lost or mangled a name ("Angel" heard as "Bangel") |
| `missing` | | the bot logged no turn for the phrase |

"Kept the names" compares the sequence of the words angel and devil in the
phrase and in the transcript. Other word errors do not change the class; such a
row prints the phrase under the transcript (`said:`), so a miss that STT caused
without touching a name can still be seen.

A phrase is scored on what the bot did, not on what its route proposed. The
bot logs a route before it claims the switch, and a claim can lose to the
route deadline. Only an `outcome` line with reason `addressed` counts as a
switch; a row whose proposal came to nothing is printed as
`route=devil BUT claim-lost`, and one that was taken back as `BUT reverted`.

A turn decided by `DUO_VOCATIVE_RULE` is printed as
`route=devil (by rule; Jev alone: below-floor)`. The report counts the turns
the rule was applied to, how many of them switched, and how many of those Jev
alone would not have made.

With `played.json` (both drivers write it) a turn belongs to the phrase that
began last before the turn ended, so one missing or extra turn does not shift
the rest. The first turn in a phrase's window is scored; further ones are
counted as `extra`, and turns before the first phrase as `stray`. Without
`played.json` phrases and turns pair off in order, and the report says so.

The clock of a web run starts at the connect click; the WAV starts when the
page opens the microphone, after the bot answered `/start`. A turn has to be
logged before the next phrase begins to count for its own phrase, so that
delay plus the end-of-turn delay must stay under the gap. The barge-in pairs,
4.5 s apart, are the tightest.

## Prerequisites

- Chrome at `/usr/bin/google-chrome` (or `DUO_E2E_CHROME=<path>`), and Node.
- `puppeteer-core` 22 or newer, installed anywhere outside the repository
  (`npm install puppeteer-core` in a directory of your own). Name that
  directory, or its `node_modules`, in `DUO_E2E_NODE_MODULES`. Nothing is
  vendored. The scratch runs used 25.14.0, which needs Node 22.12.
- The simulator URL, on one line, in `~/.config/duo-web.url`.
- For `cohort.py`: the server environment and network. The Deepgram key is
  loaded by name with `settings.load_credentials(approved_only=True)` and read
  from `DEEPGRAM_API_KEY`; it is never printed.
- For `bot.sh`: the launchers `run-<variant>-bot.sh` in `~/src/evidence/duo/demo`
  (or `DUO_E2E_LAUNCHERS`). The journal identifier of the bot it starts is the
  launcher's file name, such as `run-unify-bot.sh`. Each launcher must export
  `DUO_LLM_URL` itself: the repository names no LLM endpoint, the bot exits
  without one, and the transient unit does not inherit your shell's
  environment. Do not pass it as `VAR=value` to `bot.sh`; that lands on a
  command line.
- For `s3_fixture.py`: the pinned EchoEar with a duo image that has
  `CONFIG_DG_SPEAKER_DIAGNOSTICS`, in a session with the bot, and `pyserial`.

## Commands (coordinator)

All from the repository root.

Build the cohort audio once (again only when `cohorts.json` changes; a run
keeps a copy of the marks it was played from):

```sh
uv run --project server python hil/duo_e2e/cohort.py functional functional2 stay switch bargein
```

A web cohort run:

```sh
hil/duo_e2e/bot.sh start unify on     # stops the demo bot and kills duo bots: see its header
export DUO_E2E_NODE_MODULES=<directory puppeteer-core is installed in>
hil/duo_e2e/run_suite.sh a1 functional functional2 stay switch bargein
python3 hil/duo_e2e/journal.py run-unify-bot.sh ~/src/evidence/duo/e2e/runs/a1-*
hil/duo_e2e/bot.sh stop               # the demo bot stays stopped until you start it
```

`DUO_E2E_TAPS=30,75 hil/duo_e2e/run_suite.sh t1 stay` also double-taps the face
30 s and 75 s after connect. The driver gives up, with a non-zero exit, when
the page is not `ready` 30 s after connect. The driver alone, for 120 s (a
fourth argument such as `30,75` adds the taps); it writes no journal window and
no marks, so there is nothing for `journal.py` to score:

```sh
node hil/duo_e2e/web_driver.mjs ~/src/evidence/duo/e2e/cohorts/stay.wav 120 ~/src/evidence/duo/e2e/runs/x-stay
```

An S3 fixture run, under the bench lock. It attaches to the console without
resetting the board; it does not build, flash or restore anything:

```sh
DG_BOARD=echoear python3 hil/bench.py uv run --project server --frozen --offline --with pyserial \
  python hil/duo_e2e/s3_fixture.py --out ~/src/evidence/duo/e2e/runs/s3-functional \
  ~/src/evidence/duo/e2e/cohorts/functional.wav
python3 hil/duo_e2e/journal.py run-unify-bot.sh ~/src/evidence/duo/e2e/runs/s3-functional
```

`--only 1,3` plays those phrases, `--segments 9.5:3.5,34:3` plays spans of the
WAV given in seconds, `--listen 9` is the time left for the reply after each
segment, `--tap` ends with one injected double-tap and a status on either
side. Phrases less than 10 s apart are loaded as one segment, so a barge-in
pair is played as a pair. A segment and its listening time must fit 28 s: the
loader takes 30 s. A run that fails or is interrupted while a fixture is being
loaded or played switches the fixture off and the board's own microphone back
on before it ends, and still writes what it measured.

`journal.py` on a window of your own: put `start` and `end` (as
`date '+%Y-%m-%d %H:%M:%S'` prints them) in a directory under `~/src/evidence`
and name that directory. Without `marks.json` it prints the latency summary,
the barge-in latency and the error lines only. `--lines FILE` reads saved bot
lines instead of the journal.

## Where things go

Nothing is written into the repository; the tools refuse a directory inside it.
The default root is `~/src/evidence/duo/e2e` (`DUO_E2E_EVIDENCE` moves it):

- `cohorts/<cohort>.wav` and `<cohort>.json`: the audio, and the marks (start,
  end, text and expectation per phrase, and `run_s`, how long a driver plays it).
- `runs/<name>/`: one run.
  - `start`, `end`: the journal window.
  - `marks.json`: the marks the run was played from.
  - `played.json`: when which part of the WAV was played (one span for a web
    run, one per segment for an S3 run).
  - `web.log`, `web.png`: the simulator's log lines and its final screen.
  - `serial.log`, `status.json`: the console, redacted, and each `duo status`.
  - `report.json`: what `journal.py` printed, as data.

The bot's journal itself is never copied: `report.json` holds the turns, the
scores, the latency numbers and the error lines, redacted.

## Privacy rules the tools keep

- The simulator URL is read by `web_driver.mjs` itself from `~/.config/duo-web.url`.
  It is never an argument, never printed, never logged. Every line the driver
  emits, error text included, has the URL, its origin, its host and its secret
  path replaced by `<url>`. The page runs in a browser context of its own, so
  Chrome's profile on disk does not hold it. If a logged line did contain the
  URL, the screenshot is skipped.
- Every serial line is redacted before it is logged, printed or raised: the
  value of any key that contains `user`, `psw`, `pass` or `ssid` becomes `<r>`.
- Error lines from the bot's log lose Deepgram project ids, are cut at the first
  word that announces a credential, and at 220 characters.
- The Deepgram key travels in a request header only.
- `bot.sh` puts its extra `VAR=value` arguments on a command line: no secrets there.
  When a start fails it names the journal command and prints none of the
  journal: those lines are not redacted.

## What `bot.sh` kills

The two units it owns (`duo-demo-bot`, `duo-ab-bot`), and processes that
`botproc.py` verifies one by one. A process passes when it is this user's, its
executable is a Python interpreter, `server/duo_bot.py` from its working
directory is a file in a checkout of this repository, and its arguments are
exactly the launchers':

```
<python> server/duo_bot.py -t webrtc --host 127.0.0.1 --port 7861
```

Nothing may stand before the script and nothing after the port. An interpreter
option is not parsed, it is refused: after `-cpass`, `-ucpass` or `-mmodule`
the interpreter runs something else, and the script's name is only an argument.
A listener on 7861 that does not pass is left alone and the command fails.

So a launcher has to end by starting the bot with that command line from the
checkout's root. Through `uv run` is fine: the Python it starts has it, and
`uv` ends with its child. A duo bot started any other way is stopped with its
unit, but is never killed as a process: stop it yourself.

## Not covered

- Nothing here has been run live by its author: no bot, Chrome, journal or
  board. The first runs are the test of the drivers, of `bot.sh` and of the
  log formats as they appear in the journal (they were read from the source).
- The acoustic path. `s3check.py` played a WAV through the Jabra and took
  webcam snapshots of the face; that needs the audio device and the camera
  and was not ported. The S3 driver replaces the microphone signal inside the
  device.
- What the device displays. `web.png` is kept, not checked; `DUO_PROOF` records
  are logged, not verified (`hil/duo_smoke.py` does that).
- The P4 board: `s3_fixture.py` takes the pinned EchoEar port only.
- Two devices on the bot in one window. The transcript lines carry no session,
  so the turns of two sessions cannot be told apart: one device per run.
- Whether the bot's reply was any good, and which voice spoke it.
- STT errors that leave the names intact but change the sentence: they count as
  `route-miss` or `ok`, with the phrase printed under the transcript.
