# Display 9a interface and ownership

The console and the EchoEar RTVI adapter use `dg_display::Request`, `Engine::post`,
and the same admission/commit path. The board wrapper `post(const Request&)` uses
`xSemaphoreTake(...,0)` and returns accepted/full/invalid without allocation,
waiting, IO, or a callback to an audio task. Console parsing runs on its PSRAM stack. RTVI server messages dispatch on app-control,
after the existing session generation check, using the same bounded PSRAM parser.
The wire envelope is `{"t":"display.<op>","d":{"v":1,"epoch":...,"seq":...,"id":...}}`.
The fixed app event limits the serialized envelope to 1023 bytes; truncated events
are rejected. Wire requests require metadata and cannot set `any_host`; configure
the asset origin locally. Admissions are observable as `DISPLAY_WIRE` and work
completions as `DISPLAY_WORK_RESULT` on the console. Network result replies and
automatic capability/epoch publication are not implemented. No real
audio-presentation ledger is provided; fake-clock semantics are unchanged.

`disp init` initializes the board, 40 MHz QSPI, 20,480 bytes of 64-byte-aligned
PSRAM DMA staging, both PSRAM-stack workers, and the fixed HTTP client. The
original SPI/I2C/GDMA ISR placement is retained. `psram_dma_direct` is enabled;
every transfer rejects a bounce buffer or DMA underflow. The SPI queue has one
transaction. Two 10,240-byte halves overlap CPU composition and DMA while each
half has a single owner. Full-frame throughput improved by more than 10% in the
initial board comparison; this selects two halves. `disp buffers 1|2` is an Idle
measurement control. It cannot change ownership during rendering.

`disp origin http://host:port` sets the permitted asset authority outside a
session. Only plain HTTP with a DNS/IPv4 authority and numeric port is accepted;
credentials, fragments, backslashes, malformed percent escapes, redirects and
chunked bodies are rejected. `any_host:true` is the console override. URLs are
never logged. Fetch reserves the advertised compressed bytes in the arena
before opening HTTP, requires an exact Content-Length, reads at most 4096 bytes,
yields every chunk, and uses 5-second open, 2-second read and 30-second total
limits. SHA-256 and the full DGI stream are validated before publishing a handle.

`disp arena bytes F W largest` allocates exactly one PSRAM arena. The caller
supplies the retained no-arena reconnect measurement; admission checks
`bytes <= F-W-65536-524288`, the measured largest block, and current free/largest
heap bounds. Metadata, queues, HTTP buffers, stacks, decode scratch, and catalogs
are separately reported fixed PSRAM overhead. The arena charges allocation
headers and alignment, including in-flight download reservations. LRU evicts
only unpinned objects. Identical DGI bytes have one cache entry.

## Current board and parity coverage

EchoEar and P4 share a 360×360 logical canvas and the same wire limits.
Waveshare-1.46 keeps its native 412×412 canvas and remains build-only. Hardware
differences belong in `PanelBackend`; the engine and simple UI do not know
panel dimensions. `LogicalAdapter` alone scales/centers logical submissions.
P4 uses a centered 720×720 image on the 800×800 panel with 40 px borders;
native UI can submit through `Presenter(Native)` without changing the wire UI.

The P4 JD9365 path uses two DSI lanes, a single RGB565 PSRAM framebuffer,
explicit cache synchronization before commit and 40 MHz DPI. A committed
framebuffer update is distinct from a full scan starting after it. The
optional scan fence reports Presented or Superseded; wire completion still
uses the existing commit semantics. ISR/sample latency does not prove optical
tearing or border appearance. GT911 is polled under the board bus/reset mutex;
touch coordinates are transformed before the shared gesture code runs.

[Step 16 parity run](../hil/p4_parity.md) collects bench-display captures on
normal, demo and AEC-demo for each board using one unchanged `server/bot.py`.
It validates actual AEC intent even though display-only audio is held off,
and pairs each matching image type with `wire_contract.compare`. Separate
product pairs cover voice/capability/media evidence. The table in
[p4xc-parity.md](p4xc-parity.md) retains missing/report-only coverage. A real
double-tap is requested separately once per board; injection does not prove
F11. Current source preparation has not run these checks on the bench.

## Requests

Console scene/store commands take one JSON object, for example:

```
disp load {"hash":"<64 lowercase hex>","bytes":98787,"url":"http://host:7874/<hash>.dgi"}
disp show {"layer":0,"hash":"<hash>","x":0,"y":0}
disp batch {"ops":[{"op":"show","layer":1,"hash":"<hash>","x":168,"y":192},{"op":"hide","layer":2}]}
disp fill {"color":0}
disp cancel {"utterance_id":"utterance-1"}
disp delete {"hash":"<hash>"}
disp preload {"hash":"<hash>"}
disp persist {"hash":"<hash>"}
```

The console supplies missing `v:1`, the current `epoch`, an auto `id`, and an
increasing `seq`. The future wire adapter must supply all four. IDs/utterance IDs
are 1–32 characters from letters, digits, `-_.:`; an omitted scene utterance is
allowed. `seq` is a positive uint32. `epoch` is a JavaScript-exact integer
(<=2^53-1), seeded from 49 random boot bits and incremented at each session start;
stale epochs are rejected. Requests are <=4096 JSON bytes, depth <=8, and <=128
structural/separator tokens. Positions are int16; there are eight layers and at
most eight scene operations per batch. Load/fetch/preload/persist/delete cannot
occur inside a scene batch. SHA names are exactly 64 lowercase hex characters.

Identical typed requests with the same id return their recorded state. Reusing
an id for different semantic fields fails. Retries must retain the original
metadata; the auto-metadata wrapper intentionally creates a new request. There
are 31 normal result records plus one dedicated cancel record. Pending/applied
records are protected from eviction. After terminal-result eviction, stale seq
requests cannot repeat side effects. A dedicated synchronous bounded cancel path
stays available under the 16-entry scene-queue limit; store admission has two
slots. The core is serialized by the board mutex.

`DISPLAY_POST` is the diagnostic console admission return, not a wire event or
completion. `disp result id` queries retained state: 1 pending, 2 applied at a
logical boundary, 3 completed, 4 rejected, 5 cancelled, 6 expired, 7 superseded.
Loads, preloads, deletes, persistence and cancel have one terminal state. Scene
successes are aggregate counters; they produce no per-frame wire acknowledgement.
Scene errors and superseded batches are queryable without generating frame events.

Visible layers, accepted pending scenes, an immutable render snapshot, in-flight
IO, and session-only loads have independent reference counts. A failed batch
releases all temporary references and leaves the old scene intact. A successful
immediate replacement supersedes each whole overlapping pending batch. Cancel
releases complete batches for its utterance and retains one of 16 tombstones.
An already submitted DMA completes; subsequent stripes recheck epoch/generation
before submission, including after CPU composition. A cancelled snapshot forces
recomposition for its successor. Decode handles expose optional retain/release
hooks so a later text rasterizer can own its references independently of the DGI
cache. The compositor performs no file IO or allocation.

Dirty half-open rectangles are merged, clipped, and expanded to x multiples of
four. There is no assumed four-row panel rule. A four-pixel-aligned width whose
RGB565 row length is 8 modulo 16 also expands y to even boundaries, solely to
make direct DMA byte lengths multiples of 16. Recomposition restores the entire
expanded area from background and all intersecting layers. It has a bounded
scanline scratch and no framebuffer. Frame starts are capped at 30 fps; eligible
updates coalesce, keeping the dirty union. Software revision atomicity does not
mean optical scanout atomicity.

## Scheduling and lifecycle

`at:{kind:"rtp",timestamp:<uint32>,clock_epoch:<uint32>}` and `utterance_id` reserve
an audio-time reference. `arrival` is reserved but expires in 9a. The deterministic
fake clock maps source timestamps through audio/PLC spans and skips dropped or
trimmed samples; an insertion has no source identity. Unknown/stale clocks and
uncertainty >50 ms expire. Look-ahead is <=2 seconds, late tolerance <=100 ms.
Due time is speaker presentation minus measured render/transfer/scanout latency
and explicit visual lead. I2S residence is already included in the clock and is
not subtracted again. The board's fake clock starts with no spans, so scheduled
console operations cannot silently become immediate. Host tests exercise mapped
schedules, wrap, insertion, PLC, drop/trim, cancellation, expiry and restart.

App control/session hooks change the display epoch and reset the scene on each
session start, cancel work on transport loss, and cancel session-owned fetches on
end. They run outside capture, playback and peer-owner tasks. RAM survives.
Session pins cover the whole connecting/active/reconnecting lifetime and survive
reconnect; they release only after durable persistence or explicit deletion.
The bench remains enabled across reconnect and restores its scene through the
same typed batch path without a console reissue.

## Storage and diagnostics

The physical `storage` partition was explicitly erased and provisioned under the
user's ruling, after two identical full16MiB reads, a documented restore command,
and a complete pinned factory-source archive. Normal mounts use
`format_if_mount_failed:false`; only `disp provision-empty` may format after
checking every byte of the partition-table-selected `storage` region is erased.
The mounted capacity is derived from that partition as well; the current table
still places its 4 MiB region at 0x410000. The worker
mounts LittleFS, removes hash-named temporary files, and validates each final file
against both its SHA and DGI grammar. A temporary524288-byte validation scratch
is released before `lcd-ready` and before the session/arena budget measurement.

Loads outside sessions persist before their loaded terminal result when space is
available. RAM readiness remains successful if persistence fails, with
`persisted:false` and a separately retained `display.persisted` event. Loads in
sessions remain RAM-only and session-pinned; the worker persists them after the
whole connecting/active/reconnecting lifetime ends. Reconnect retains those
pins. The write-policy fence at every physical write/erase cancels an outside-session install if
another session starts. No flash is evicted automatically. Preload reserves its
object inside the arena and verifies SHA/DGI on read. Explicit deletion requires
no active visible/scheduled/render/in-flight refs and removes durable/temp files.
Filesystem-space status is refreshed by the worker; console status performs no IO.

The generated partition guard checks zero `flash_mmap_remain()` immediately
before every LittleFS write/erase and also rechecks the active-session policy at that boundary. XIP is retained; the diagnostic XIP-off image
blocks ALL filesystem IO on the PSRAM worker rather than running cache-disabled
flash paths on that stack. Every write, fsync, close and atomic rename is checked.
Host fault tests and board resets cover interruption during write, before rename,
and after rename. The physical full-store test proves persistence and explicit
space recovery while unpinned RAM objects may be evicted independently.

`disp persisted start` pages up to six bounded terminal persistence events.
`disp fault 1|2|3` is an Idle-only HIL pause after the first4KiB write, before
rename, or after rename; `0` resumes. `disp defer-persistence 1` and
`disp call-persistence 1` are Idle-only diagnostic controls for preparing and
scoring 5b; production defaults remain outside-session persistence. Prepared
inputs are RAM-only and pinned, then `disp store-bench 1` submits four checked
write/sync/close/rename/delete pairs and a near-full rejection through the typed
worker path without console/network traffic inside measurement windows.
`disp store-results` returns the bounded completion trace after the window.

`disp status` emits <=1024 bytes. Compact arrays are: panel
`[width,height,format,x_alignment]`; revisions `[accepted,rendering,completed]`;
queue `[scenes,store]`; limits `[layers,scene_slots,store_slots,replay_records,
max_image_bytes,max_url_bytes,horizon_ms,late_ms]`; updates
`[requested,accepted,rendered,coalesced]`; errors `[errors,dropped,superseded]`;
bench `[mode(0 off/1 cat/2 full),requested_fps,requested_frames]`; DMA
`[bounce_buffers,underflows]`. RAM accounting is separate from whole-device heap
PERF. Flash capacity/used/free are filesystem block totals. `largest_free` is null;
`max_admissible_bytes` uses cached free space minus a conservative16KiB metadata
reserve, limited to524288. Header capabilities list codecs, alpha modes and
operations. `disp status layers` reports the eight logical layers separately
within the same1KiB call limit.

`disp inventory start revision` returns <=6 entries (under the eight-entry cap)
and a revision-checked next index. Each entry is `[hash,bytes,references,ram_ready,flash_usable,persisted,error]`.
It merges the flash catalog and RAM cache, includes per-image corruption errors,
and reports the current epoch and a combined monotonic inventory revision.
Status and inventory must stay outside measurement windows.

HIL controls `disp asset index hash` configure the fixed pack: base 0, mouths 1–6,
eyes 7–12, whiskers 13–15, noise 16. `disp bench hold 30` reserves the cat pack without rendering during
large-first 5b arena preparation. `disp bench cat 30` requests mouth every frame
and eyes/whiskers every fifth frame at their manifest positions. `full` alternates
base/noise at the characterized maximum rate; `off` stops requests. All modes use
production decoder/cache/batch ownership. `disp measure N` compares 1x20KiB and
2x10KiB composition/transfer for full and `[92,176,224,56]` rectangles on the
current scene. `disp qualify N` holds rotating full-disc quadrant patterns for
200 ms each, records software CRC/transfer intervals, and asserts zero DMA
failures. `disp trace start` pages <=8 records; `disp te` reads the bounded GPIO18
edge history. `disp probe URL` is an Idle-only no-arena HTTP census exercise, not
a production load. It drains a bounded body into fixed worker scratch.

With XIP from PSRAM and zero runtime flash mappings, SPI1 flash operations keep
cache enabled but compete on MSPI with SPI0 PSRAM/code traffic and the LCD DMA
source. The unqualified 9a-r2 candidate takes one PSRAM binary semaphore before
each color stripe, releases it exactly once in the completion ISR (with an
explicit ISR yield), and gates each synchronous LittleFS read/program/erase and
direct provisioning read outside the engine mutex. A consumed DMA-failed LCD
result is counted and normalized for esp_lcd bookkeeping; the failed frame is
invalidated and fully repainted. Any production raw error, recovery or timeout
fails qualification. Writer timeout fails IO without accessing flash; missing
completion/display timeout makes the display terminally dead and retains live
DMA buffers until reboot. Isolated persistence exceeded24fps, but the new
production fetch cleanup still underflowed, so this gate is not a qualified
production remedy. Any runtime flash mmap breaks the cache-enabled contract
required by PSRAM semaphore/storage and PSRAM task stacks.

R3 adds lifetime DMA errors, prequeue send failures and late callbacks to PERF
health; these counters independently fail production monitoring even when ROM
error text is missing. A recovery attempt means a failed current frame requesting
one full repaint; a successful recovery closes one episode, so several failed
repaints may produce N attempts and one recovery. Repainting increases DMA
exposure while contention continues. The lost-callback diagnostic suppresses the
notification/gate-release path although the hardware result still reaches the SPI
queue; it does not simulate absent hardware completion. Whole flash-op hold
maxima include equal-priority display-task preemption during compose (the r2
34/43 ms program/erase tails are not flash wire time). `disp clock` sets only the
requested QSPI pclk before init; actual driver kHz is recorded. R3's SPI pre/post
observers measure an ISR-bounded transaction window, not the precise starvation
instant. Phase bits are coarse overlapping activity markers, not a CPU trace.


The accepted R3 v2 stop (superseded by the R3b amendment below) was an
uninformative positive control. The fixed core1/priority1 PSRAM memcpy stressor (two256KiB buffers,
20ms bursts/21ms periods, mandatory idle tick) produced zero DMA errors over
100,007 cat stripes and100,035 bounded full-screen stripes at driver40,000kHz,
with95.9% measured burst wall duty and positive DMA overlap. Separate one-sided
95% Poisson upper bounds are2.9955e-5/2.9947e-5 per stripe; unlike shapes are not
pooled and independence is not proved. These diagnostics do not override r2's
retained production underflow. At that stop D40/acceleration were undefined,26.67/20MHz were
unassessed, no clock was selected, and chosen-clock performance and step3 were
pending. The stressor allocates buffers/task only when explicitly armed on a
diagnostic Idle boot and frees its buffers when stopped. The predeclared rule
and full accounting are in the step-9a-r3 report.


R3b retires core1 copy traffic as the margin instrument and uses natural triggers.
The frozen diagnostic ELF94ecafbb records durable DMA pre/post and frame/phase
context and optionally pins the identical copy task to core0. At40MHz, teardown
A produces49/100,040 stripe errors in45/60 affected cycles; flash C produces
3/4,852 errors in3/20 cycles. C retains a failed adjacent cleanup liveness
snapshot and is not a clean cell. Console B is clean over1,050 command pairs;
core0 copy is clean over100,040 stripes at95.03% achieved burst wall duty.
All53 captured hardware errors across the control and26.67MHz teardown cell
have zero measured SPI1 API overlap. This excludes those recorded explicit
API windows, not XIP/cache misses or PSRAM traffic, and does not identify cause.

Clock selection is blocked:26.67MHz teardown produces one hardware underflow
before an accepted delete lacks a durable result;20MHz teardown has zero DMA
errors but its delete result is truncated/interleaved with a counter response.
Both teardown cells retain invalid, incomplete captures (18/46 complete cycles
plus one partial each), and neither completes its60cycle/100,000stripe dose.
No JSON reconstruction or retake makes either clock eligible. Remaining lower
console/flash cells, exposure bounds and every error context are in the R3
report. No clock is selected; fresh confirmation, chosen-clock performance
and step3 requalification are unrun. The20MHz capture failure does not
demonstrate a20MHz hardware underflow or prove internal staging is necessary.


The user-authorized R3b item5 tests hardware SHA/GDMA using diagnostic ELFe2e81566.
Ungated whole-buffer PSA SHA256 on core0/core1 produces136/1611 and921/1058
stripe errors before rendering stops. Both are invalid overload captures; core1
also loses five DMA contexts. Whole-hash display-gated controls are clean over
10,039/10,016stripes. All three isolated core0 candidates are clean:8192B
gated incremental SHA(28.68fps,6.91MB/s), explicit software SHA(28.17fps,
2.33MB/s), and4096B internal-RAM chunk SHA(28.16fps,18.33MB/s). Internal
chunk SHA still overlaps LCD DMA in8,718stripes but reads zero PSRAM descriptor
bytes. This supports investigating the PSRAM SHA/GDMA path; observer overhead,
overload and unequal traffic density limit attribution. Production asset
validation is unchanged and no candidate or clock is adopted.

The inspected IDFv6.1 S3 SHA256 driver selects DMA when64-byte-aligned bulk
length exceeds256B and splits at3968B. Enabled bulk AES functions select DMA
above128B, with mode/API-dependent exceptions. The linked SRTP AES-ICM here
is software; DTLS uses PSA but packet-level AES DMA is unmeasured. S3 GDMA
priority0 is default round-robin; levels0–5 are available via a private API,
and deletion resets priority (connect does not). The original R3b finding
misidentified the enclosing SDK function; R4 records the correction. No
arbitration setting was changed in R3b. Detailed
source findings and every retained diagnostic/coverage gap are in the R3 report.
The original26.67MHz hardware failure and20MHz capture failure still block
clock selection, confirmation, chosen-clock performance and step3.


R4 changes asset validation to incremental PSA SHA-256 updates from one
preallocated 4,096 B internal DMA buffer. A store-task owner assertion covers
fetch, install, preload and catalog validation. The candidate's fresh internal
cost is 24,440 B against the 27,108 B ceiling; all four memory floors pass.
Its measured PSRAM allowance gives a 421,888 B arena.

Always-active SHA/AES descriptor counters distinguish TX input bytes from RX
capacity and group them by task, peripheral and asset scope. Asset hashing reads
zero PSRAM bytes through DMA. DTLS peer traffic still does: the five-minute
1 KB saturation flood reads 66.84 MB through SHA and 73.63 MB through AES, but
fails watchdog, parser-queue and render-liveness checks. It is not a clean rate
result. Candidate calls negotiate DTLS 1.2 ECDHE-ECDSA-AES256-SHA, meaning AES-CBC
and SHA-1 HMAC. SRTP AES and HMAC/SHA-1 use their linked software implementations.
HTTPS signalling is a potential, unexercised path; asset origins allow plain HTTP.

The ungated SHA positive control produces two LCD errors immediately. LCD SPI2
TX priority 1 passes 0 / 10,046 stripes at 28.48 fps with 6.21 GB of PSRAM SHA
input and 9,712 SHA/LCD overlapping stripes. The brief therefore selects priority
1 as the candidate's default; the gate alternative is unselected. Link wraps
capture the driver-owned channel and reapply priority after each connect.
SDK priority persists across connect and resets on deletion, correcting the
old reconnect-reset claim. The r2 flash gate remains unchanged. Old read-overlap
errors have no timed SHA observation; diagnose-01 #1 (ring index 0), previously
called mid-persist, remains residual. The R4 report gives each R2 error its own
attribution verdict. The 53 natural errors fell inside validation-containing
phases with zero measured SPI1 overlap. Of 45 persistence errors, 44 occurred
on repaint stripes 1–2; install validates before its first write. This supports
the validation explanation, but the old image had no SHA timing observer, so
actual hash coincidence remains inferred.

R4 is blocked at the acoustic gate. Natural A passes all 60 teardown cycles and
0 / 100,022 stripes; B passes 1,050 stats pairs and 0 / 8,853 stripes. Both pass
cleanup and memory checks. C's Jabra baseline is −45.192669 dBFS, above −50 dBFS,
so it stops before resetting or arming the board. The failed arm is retained
without a retake. C needs the amended 60-cycle dose; zero cycles ran, so a separate 20-cycle
continuation does not apply. Candidate SHA/flood confirmation and performance remain
unrun; step 7 awaits review. Candidate ELFa17be387 remains unqualified. Final
read-only verification on the B boot shows benches/stressors off, zero DMA errors,
the original 16 files, 520,192 B used, 3,674,112 B free and zero temporary bytes.
Owned servers are closed; qdiscs are unchanged. No commit or push. See the R4
report for the progress notes, evidence and authoritative acoustic blocker.

The review amendment adds a production monitor hard-fail key: after LCD init,
TX channel capture must be true, `priority_applies >= 1`, and
`last_priority_written == 1`. These fields and SHA/AES PSRAM/internal input
bytes plus RX capacity are reported in every PERF. Nonzero PSRAM crypto bytes
remain reported metrics because the measured priority guard was selected.
A Ready callback reads all five GDMA pairs' TX/RX `peri_sel` and priority
registers once per initialized boot. The amended ELF f271b756 builds and its
14 monitor tests pass. R4b's specific acoustic waiver allowed its flash and fresh
census: internal cost 24,720 B and all four memory floors pass, and the actual
Ready dump captures all five pairs once (SPI2 TX priority 1). The first R4b
census fails its overall audit because the harness sent the diagnostic-only SHA
stop command on a production boot; firmware correctly rejected it. That failure
is retained. The user authorised exactly one harness-fault census rerun on the
same image: census-production-02 passes at 24,668 B, with all strict checks clean.
The subsequent one fresh flood fails session/boot/largest internal floors
(71,939/53,284/31,744 B), and terminal display dead persists through cleanup.
There are zero DMA errors over 318 stripes and no raw driver errors; observed
priority and coverage remain correct. This incomplete arm has 16 watchdog
episodes and a 7.730301 s scheduling gap. No 300 s/drain completion or new-image
qualification is claimed. Part 2 profiling is gated and no further rerun occurs.
A/B stay associated
with their frozen a17be387 image; review decides whether the change requires
repeating them. C60/SHA/performance/step7 remain held outside the R4b waiver.

The bench fetch is plain HTTP; HTTPS fetch and signalling are outside this
qualification. Disabling hardware AES and, if needed, hardware SHA through
Kconfig remains a fallback, not needed or adopted after the priority test passed.
It would require a new image, memory census and CPU/latency measurements.

For 9b, the inventory flood exposes a CPU/parser/scheduling risk: 69,627
1,024-byte messages over 300 s (232.09 messages/s) reduce cat to 1.6187 fps
and starve IDLE0's watchdog. The maximum scheduling gap is 8.442781 s, with
15,137 native queue drops and 1,307 diagnostic log drops. LCD DMA errors are
zero over 798 completed stripes. This is not an observed display-DMA failure
or a sustainable receiver rate; the selected priority guard does not fix it.

EchoEar B1 review correction: `disp init` now also compares the actual captured
channel's GDMA `tx_pri` register with 1, and PERF / DISPLAY_R4_CRYPTO expose
that readback. A failed guard aborts display initialization before the panel
is created or enabled. The console reports ERR and continues; voice may run
without a display. This does not terminate firmware or the session. HIL treats
the initialization error or a non-1/missing readback as a failed graphics run.

## Presentation seam (P4 port, step 10)

`dg_display/present.h` separates the logical scene from the physical display:

```
engine / ui/simple -> Presenter(Logical) -> LogicalAdapter -> PanelBackend
native UI          -> Presenter(Native) -----------------> PanelBackend
```

`dg_board/canvas.h` supplies the compile-time canvas and dirty/DMA alignment
constants. EchoEar and P4 use 360×360; the retired Waveshare keeps 412×412. The
engine, DGI size cap, scanline scratch and simple UI use those logical dimensions.
Only `LogicalAdapter` scales and offsets logical coordinates or fills borders.
A native backend reports its own width, height, roundness, RGB565 byte order,
native framebuffer stride in pixels and optional scan-fence capability.
Rectangles have exclusive right/bottom edges. `stride_px` describes the source
rows, starting at the submitted rectangle's top-left; it is not a panel pitch.
RGB565 words are opaque big-endian byte pairs, matching the existing compositor.

The three lifetime events are different:

- `Released` permits the source buffer to be reused. Successful submission owns
  it until this callback (or successful `wait_released`); rejected submission
  leaves it with the caller. Callbacks run on the display owner, not an ISR.
- `committed(id)` completes transfer into the panel or native framebuffer. S3
  keeps its existing completion notification, flush-DMA drain and DMA generation
  check, including the frame-wide generation check. The wire acknowledgement
  still follows that fence. It does not promise optical atomicity.
- `scan_fence(id, cb)` is optional and never used for wire acknowledgements here.
  A supporting backend reports `Presented` once a complete scan starts after
  commit, or `Superseded` when a later overlapping commit wins first. S3 and the
  null backend return `Unsupported` and advertise `scan_fence=false`.

Errors are returned explicitly through the presentation contract. EchoEar
retains the base `ESP_ERROR_CHECK` abort if its TE-enable command fails; other
panel-init errors propagate before backlight activation. The queue-depth-one S3 backend retains the latest
submission's fence; asking about an expired ID returns `Unavailable`. Resolve
staging ownership before submitting again and resolve the final commit before
leaving a frame. DMA failures still trigger the existing full repaint or terminal
watchdog path. The qualified r3/r4/r5 SPI/GDMA/crypto guards remain in the
shared `backends/qspi/` directory and are also selected for the retired S3 QSPI board,
which used them before the split. The priority-1 readback precondition remains
before panel creation. The staging allocation stays 20,480 bytes, split into the
same 10,240-byte halves with 64-byte ownership alignment; hashing retains its
4,096-byte internal DMA scratch. Those S3 allocations are unchanged.

`Presenter` is the single display-task owner and arbitrates `Logical` and `Native`
modes. `runtime_presenter()` returns a pointer only on the initialized display
owner task. Other tasks use the single-slot `runtime_dispatch_owner({fn,context})`
mailbox: false means unavailable/busy with ownership retained by the sender;
true admits a noexcept callback between frames, outside the engine mutex.
Keep context and any source buffers alive until that callback returns; it must
not block. Mode changes, submissions and fence registration all run there.
Callbacks must not re-enter the backend. While Native owns the display, wire operations continue to update/tick
the logical scene. The runtime suspends logical snapshots; their physical
completion results wait for re-presentation. Wire admission acknowledgements
remain immediate; they never certify rendering. RTP deadlines still expire
unapplied scheduled operations through `Engine::tick`; already applied dirty
scene revisions wait until the logical owner resumes. Switching back drains the
native submission, restores logical borders and requests a full logical redraw.
No Native mode is enabled by the current application. App touch/lifecycle control
stays in `main/demo_ui.cpp`; simple scene production and character animation live
under `ui/simple/`.

The P4 800×800 backend uses layout `{360,360,2,40,40,0}` from
`dg_board/canvas.h`: replication
produces a centered 720×720 logical image and four 40-pixel borders. Supply bounded
adapter scratch sized for full 20,480-byte staging (81,920 bytes at scale 2);
the adapter waits for its previous
staging release before reusing it. Scale 1 passes the existing S3 buffer through
without an allocation. The P4 adapter fills black borders once on display-owner
startup and on return from Native mode. `Presenter::last_` tracks border
submissions before requesting a full logical redraw. Scale 2 retains the
explicit scratch copy followed by the backend framebuffer copy.

A future native renderer can own an 800×800 scene, select `Native`, and call
`submit_native()` with native rectangles and source stride, bypassing the logical
adapter. An LVGL flush callback uses that same entry point and marks its draw
buffer reusable on `Released`, rather than waiting for a physical scan. It must
serialize through the display owner, handle submission errors, and resolve
`committed` where actual transfer completion matters. A renderer that needs
visible-frame timing checks `info().scan_fence` and requests the separate scan
callback. Native UI libraries and their buffer policy belong above this seam;
there is no LVGL dependency in the engine or S3 backends.

Host presentation contracts are in `test_host/present_test.cpp` and
`test_host/dsi_framebuffer_test.cpp`. The latter exercises the production copy,
msync-range and frame-counter logic with fake msync/frame-complete hooks, including
P4 layout borders and native-owner return. The real pinned
ST77916/SPD2010 drivers run against recording IO in the panel trace tests;
reviewed fixtures come from the pre-step-10 source, never from candidate output.
A clean host build needs the managed panel sources fetched by a Waveshare
reconfigure, then caches them across board switches. The optional historical
witness and explicit mutation campaign are:

```bash
python3 components/dg_display/test_host/compare_pre_refactor.py \
  --build build/host-display --out "$EVIDENCE/panel-before"
python3 components/dg_display/test_host/test_present_mutations.py \
  --build build/host-display --out "$EVIDENCE/mutations"
```

## P4 / native UI notes

`Presenter::info()` exposes native geometry, source format, framebuffer stride
and scan-fence support to the current owner. Submissions currently carry opaque
RGB565 big-endian bytes, including Native submissions. A P4 DPI framebuffer and
LVGL's usual RGB565 output are little-endian. Until a future explicit
`PixelFormat::Rgb565LE` capability is added, the JD9365 backend must convert BE
submissions into its LE framebuffer; an LVGL flush must supply BE bytes (using
its byte-swap setting or flush conversion). Do not silently reinterpret LE words
through the existing BE contract. The backend owns conversion and cache sync.

Scale 2 currently replicates into adapter scratch, then a framebuffer backend
copies that scratch into the framebuffer. This is two copies. There is no
zero-copy native framebuffer mapping in this contract. Future work may add an
optional native-window/map capability so the adapter can replicate directly into
the framebuffer and a native UI can draw there, with explicit ownership and
cache/commit fences. Keep the current S3 submit path and buffer sizes intact.

The return-to-Logical represent callback may acquire the non-recursive engine
`Guard`. Call `set_mode()` from the display owner without holding that guard.
`runtime_dispatch_owner()` provides that hand-off; the current application has
no Native renderer. Wire admission stays immediate, scene completion waits for
the return-to-Logical full redraw, and scheduled pending deadlines continue to
expire normally. Accepted callbacks require a live display worker; terminal
worker failure stops servicing the mailbox, so contexts must have persistent
lifetime rather than a timed stack wait.

### JD9365 DSI backend (step 13a)

P4 now selects `backends/jd9365_dsi`, with JD9365 2.0.2 enabled by the
target-only manifest rule and optional private requirements after registration.
Bring-up follows the spike/BSP 800×800 command table, using only the GPIO27
hardware pulse before panel init (no extra DCS SWRESET): LDO 3 at 2,500 mV,
two lanes at 1,500 Mbps, 40 MHz DPI (880×840 total timing, 54.112554 Hz), one
1,280,000-byte driver-owned PSRAM framebuffer. `dg_board::lcd::reset()` owns
shared GPIO27 before the first touch probe. The board mutex protects only
cooperating RMW/reset users; codec/touch transfers do not acquire it. It is
not a bus-wide reset barrier. Step 13b must coordinate any later reset and
re-probe Goodix. Touch remains deferred to 13b. The retained
500 ms DCS-read wrapper blocks one tick per busy poll and fails closed on a
timeout/short response. This void-HAL path aborts/reboots; it does not return
PresentError::Io. A display-unavailable recovery path is a later 13b/14 liveness
design option. Link flags root the wrapper explicitly.

The backend swaps each BE source pixel into LE framebuffer bytes, using source
stride independently of framebuffer pitch. Rectangles ending at 800 are valid;
edge overruns are rejected before copy rather than silently clipped, matching
the seam's bounds contract. Cache write-back covers complete destination rows,
from `y0 * stride_px * 2` for `(y1-y0) * stride_px * 2` bytes, with
`DIR_C2M | UNALIGNED` for IDF's bounded cache-line expansion. Success and source
release are published only after successful msync. A sync error is terminal
until reboot and propagates `Io`; no successful ID/release is fabricated.

`info().scan_fence` is true. Only the frame-complete ISR updates the frame
counter and the 16-frame timestamp ring. A commit sampled at counter N becomes
Presented at counter N+2 (the remainder of N, followed by all of N+1).
Owner `poll()` dispatches outcomes; no user callback or logging runs in the
ISR. Presented is a single-buffer accounting fence: a later overlapping CPU
copy can modify pixels during that scan even if the later msync/commit finishes
afterward. It does not preserve an immutable copy of the earlier image or
establish optical atomicity. An overlapping later commit before that boundary
yields Superseded;
disjoint and merely adjacent rectangles do not. Scans already complete at the
later commit win even if owner service was delayed. Callbacks run once, after
commit, and may be registered after settlement while the ID is retained.

The bounded history retains 256 submissions. An unresolved oldest slot makes
the next submit return Busy without taking the source; completed slots may be
reused and then their old IDs return Unavailable. If completions stop and
256 unresolved submissions fill the history, Busy indicates scan completion
is stalled; inspect the bridge/GDMA and interrupt-service progress rather than
treating it as routine history eviction. Source ownership stays with the caller.
Unknown/future IDs return
Invalid. This is distinct from S3's latest-only history. Deferred callbacks
are dispatched in ring-slot order; after history wrap, different IDs can settle
out of submission order (for example 257 before 200). No cross-ID order is
guaranteed; callers must correlate each returned ID. If timestamp history
is overwritten, the scan outcome still completes, with `timed=false` for
diagnostics. `P4_SCAN` logs deterministic 1-in-16 sampled submissions, separating
Presented from Superseded and using ISR timestamps rather than owner wake-up
time. The coordinator helper derives the histogram without treating the latter
as presentation samples; timestamp wrap is unsigned 32-bit microseconds.
`superseded_fraction` divides all sampled Superseded outcomes (including
untimed ones) by total sampled records and accompanies the survivor-only
latency quantiles. `underrun_lines` counts literal `underrun happens` log lines,
report-only, to establish the 13b contention baseline. ISR timestamps reflect
service time: flash-operation masking can overestimate latency and undercount
frames. Those conservative accounting limits must accompany the histogram.

The product preserves the spike/IDF handler placement with
`LCD_DSI_ISR_HANDLER_IN_IRAM=y`, while `LCD_DSI_ISR_CACHE_SAFE=n` still masks
the ISR during cache-disabled flash operations and permits cached user
callbacks. The static panel/history object plus JD9365 init-command array
consume approximately 17.6 KB of internal RAM (reviewed P4 link sizes:
14,480 + 3,152 B). They remain internal in this step; 13b must include this
cost, IRAM-handler driver allocations and live display load in its measured
≥81,920 B minimum / ≥32,768 B largest-block internal-heap gates.

Single-buffer tearing remains **unverified**. The spike observed an underrun
at 40 MHz; this is not an underrun-free claim. See
[`hil/p4_display.md`](../hil/p4_display.md) for the build matrix, coordinator
acquisition, optical border check and wire/latency analysis. Source and test
cases are implemented. Host tests, P4 normal/demo builds and the S3/Waveshare build matrix pass;
hardware checks and S3 behavioural smokes remain coordinator work.

### Step 12a null-backend link work

The null panel is host-tested, but the P4 firmware selection is configuration-only
at step 10. It cannot yet link the shared runtime. The exact S3-only external
functions referenced by `runtime.cpp` are:

- `census_init(bool, size_t)`, `census_set_clock(unsigned)`;
- `detail::init_te()`, `detail::drain_dma()`, `detail::te_gpio()`;
- `dma_generation()`, `dma_bounces()`, `dma_errors()`, `lcd_requested_hz()`,
  `lcd_actual_khz()`;
- `r3_stress_dma_start()`, `r3_stress_dma_done(int64_t, int64_t)`,
  `r3_stress_status(void (*)(const char*))`, `r3_stress_stop()`,
  `r3_stress_start(unsigned, unsigned, unsigned, unsigned)`;
- `r5_hash_dma_start()`, `r5_hash_dma_done(int64_t, int64_t)`,
  `r5_hash_error(unsigned)`, `r5_hash_status(void (*)(const char*))`,
  `r5_hash_stop()`, `r5_hash_start(unsigned, unsigned)`;
- `r4_gdma_ready_once()` (called from `main/console.cpp`);
- `r4_crypto_health()`, `r4_crypto_guard(unsigned)`,
  `r4_crypto_status(unsigned, void (*)(const char*))`.

Step 12a must move this diagnostic/init coupling behind the backend or provide
appropriate null behavior before its AEC-off headless link/boot gate. The
`detail::send()` → `detail::await_dma()` transfer chain and the r3/r4/r5 includes
in `runtime_state.h` belong to the same sweep, although the runtime now submits
through Presenter and does not directly call `send` or `await_dma`. The initial
`dg_board::backlight::init()` brightness and ordering remain unchanged; subsequent backend
brightness requests use `dg_board::backlight::set(pct)`.


## Board interfaces after step 11

The legacy `dg_board.h`, `dg_power.h` and `dg_touch.h` umbrellas are removed.
Include the split headers and use `dg_board::i2c::{init,bus,mutex,init_expander}`,
`backlight::{init,set}`, `lcd::reset`, `power::{init,available,read}` and
`touch::{init,read_raw,poll,inject_double_tap,status}`. `BoardInfo` owns boot-log
spelling, cache/heap capability choices and simple-UI availability; `AudioHw`
owns the compatibility diagnostics for each codec/AEC profile. The S3 log records
and hardware initialization order stay unchanged.


### P4 pre-main headroom and PSRAM display state (13a fix 2)

On rev v1.3 the 18 KiB/384 KiB startup-stack heap regions are registered only
inside main_task, after its stack has already been allocated. The working 12b
ELF leaves 119,024 B in RETENT_RAM; the failed 13a image leaves 98,816 B. Its
saved stack identifies the failed 4,096-byte internal/8-bit main-stack request.
RTCRAM/SPM are smaller active fallback heaps, not substitutes for preserving
the working pre-scheduler budget. esp_hosted and IPC constructors allocate tasks
before the scheduler; there is no display constructor in the pre-main array.

The 17,632-byte panel/history and command table now share zero-initialized
external BSS. Placement construction happens on first panel_backend() use,
without malloc or an internal-RAM fallback. The framebuffer remains separate
and driver-owned in PSRAM. DSI handler/re-arm code stays in IRAM. CACHE_SAFE and
DW_GDMA_ISR_IRAM_SAFE must stay disabled, so cache-off operations mask interrupts
before a callback can access its PSRAM context; enabling either is a compile
error requiring a different context-placement design.

Every P4 link runs scripts/check_p4_headroom.py. It derives retention space from
ELF heap symbols, requires at least 114,928 B (working 12b minus a declared 4 KiB
allowance for added display IRAM/state), external NOBITS panel storage, no backend
pre-main constructor, and the DMA callback in IRAM. The guard now resolves the
actual preinit/init pointer ranges through DWARF addr2line, including inline
frames, and rejects any source under backends/jd9365_dsi/. It does not depend
on GCC's initializer symbol name. IDF also puts reservation/system-init records
after __init_array_end in .flash.init_array; those are not constructor pointers.
Missing bounds or unresolved source locations fail closed. Generated RISC-V
fixture ELFs exercise extraction, differently named backend constructors and a
floor-specific failure with otherwise valid storage and ISR placement.

The coordinator's fix-2 normal run confirmed DG_BOOT with 113 KiB RETENT_RAM;
factory LittleFS data then blocked provisioning (13a fix-3 adds a named storage
erase to the bench adapter). The 114,928 B floor remains the declared regression
margin, not a measured minimum. At the next coordinator boot retain the initial
heap-region lines and earliest PERF internal-heap snapshot with its time relative
to DG_BOOT, before provisioning/assets. A later PERF snapshot cannot establish
free retention memory at main-task creation. Recalibration needs that pre-main
measurement; live heap/contention gates remain separate.
