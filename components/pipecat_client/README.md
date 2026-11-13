# ESP-IDF Pipecat core

Core sources are the v1.1.0 subtree. `src/rtvi/about.cpp` reports the ESP platform. An ESP-only patch in
`src/rtvi/messages.cpp` keeps the RNG state on the heap: IDF puts GCC TLS on
every task stack, and the upstream 2500-byte generator prevents IPC startup. nlohmann/json 3.12.0 is vendored as a single MIT-licensed header.
The generated version header comes from the upstream template.

Call `pipecat::esp::configure_threads()` **in the task constructing the client
and transport**. The owning task must also be a pthread (for example, a
`std::thread`): upstream teardown calls `std::this_thread::get_id()`, which
asserts on a plain FreeRTOS task on IDF. Defaults are core 1, priority 5, 12288-byte stacks, inherited
by child pthreads. The application check runs on core 0, priority 4, 12288 bytes.

`pipecat/esp/http.h` exposes a value response without exposing private upstream
headers. The ESP backend posts directly through esp_http_client's manual API.
Null JSON has no body; caller headers override the default JSON content type;
non-2xx responses are returned for the core to interpret. Responses are capped
at 65536 bytes and incomplete bodies fail. Cancellation is checked between I/O
calls. Each call gets a timeout of at most 500 ms or the remaining deadline.
Timeout zero deliberately means 30 seconds on ESP, unlike curl's unlimited zero.
No retry resends a POST body. This milestone uses LAN HTTP; HTTPS trust setup
is a later milestone.

Run `scripts/host-tests.sh`: CMake, a C++17 compiler, libcurl development headers
and network access for upstream nlohmann/json and GoogleTest downloads are
required. It runs the upstream curl/fake-transport suite and separately compiles
the production ESP HTTP source against our deterministic shim. Shim time advances
without wall-clock sleeps. It tests body/header semantics, partial writes,
chunked/length bounds, status codes, cancellation, deadlines and failures/cleanup.

Device check: start `server/` with `SERVER_PORT=7861 BOT_MODE=offline-test`,
configure gitignored root sdkconfig.local, build with `scripts/idf.sh build`,
flash using `hil/flash.py`, then run `hil/start_bot_test.py` in the HIL Python
environment. Serial capture and flash share the existing lock. The copied
upstream FakeTransport is only a device test fixture; step 4 proves `/start`,
not WebRTC. Empty local configuration builds and logs a skipped check.
