Phase 1 keeps codec, task, WiFi, cache, clock, and playout settings intact.
Non-AEC builds keep the optimization switches below off. AEC builds now reuse
the three transport workers after a separate step-2 cat/reconnect resource hold
(see `docs/echoear-aec-bench.md`). The original phase-1 campaign rejected
the combined configuration; see
[`step-6a-phase1-report.md`](../../proj-2026-10-08-0949/codex-jobs/step-6a-phase1-report.md)
for measured bytes, failures and final configuration. These are experimental
implementations, not accepted optimizations.
`DG_REUSE_TRANSPORT_WORKERS` creates three persistent pthreads (owner, encoder, JSON dispatcher)
after step 6b removed the decoder worker. They are allocated during transport
construction, before audio initialization. Their stacks, priorities,
and cores match the per-session workers. Disconnect waits for cooperative jobs
before closing the peer and releasing the session; idle workers block on a
task notification; a static semaphore confirms completion. `tasks_exited` means all session jobs completed.

A native peer prewarm was tried and removed: it did not keep the main heap's
largest block from shrinking after the first session. The message-type registry
and certificate pre-generation remain boot-time initialization.

`DG_PEER_PEM_PSRAM` uses a generated DTLS source/header copy under `build/`,
with checked declaration/entry shapes. Only the two 2,048-byte cached PEM array
lvalues become one process-lifetime 4,096-byte PSRAM allocation. It retains
`sizeof`, cache lengths, signing, and import behavior. The pointer costs four
internal static bytes. The original managed sources are read-only. This avoids
`SPIRAM_ALLOW_BSS_SEG_EXTERNAL_MEMORY`, which would also move WiFi/lwIP BSS.
Re-review the adapter when the pinned dependency changes.
