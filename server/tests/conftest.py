import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone
import httpx
import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from settings import load_credentials, server_port

@pytest.fixture
def running_server():
    port = server_port()
    mode = os.getenv("BOT_MODE", "offline-test")
    if mode not in {"offline-test", "live-test"}:
        pytest.fail("Run acceptance with BOT_MODE=offline-test or live-test")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    evidence_root = Path(os.getenv("TEST_EVIDENCE_ROOT", str(
        SERVER.parent / "proj-2026-10-08-0949/evidence/step2")))
    out = evidence_root / f"{mode}-{stamp}"
    out.mkdir(parents=True)
    if mode == "live-test":
        load_credentials()  # Required here, not skipped when credentials are missing.
    env = {**os.environ, "BOT_MODE": mode, "EVENTS_PATH": str(out/"events.jsonl"),
           "TTS_REFERENCE_DIR": str(out/"tts"), "PIPECAT_SCTP_MAX_CHUNK_SIZE": "1100"}
    if mode == "offline-test":
        env = {k:v for k,v in env.items() if not k.endswith("_API_KEY")}
        env.update(DG_OFFLINE_GUARD="1", HF_HUB_OFFLINE="1",
                   PYTHONPATH=str(SERVER/"tests/offline_guard"))
    # Refuse to test an unrelated process already using this port.
    import socket
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1",port)) == 0:
            pytest.fail(f"Port {port} is already occupied")
    with (out/"server.log").open("w") as log:
        proc = subprocess.Popen([sys.executable,"bot.py","--host","0.0.0.0"],
                                cwd=SERVER, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic()+30
            while time.monotonic()<deadline:
                if proc.poll() is not None:
                    pytest.fail(f"Server exited: {out/'server.log'}")
                try:
                    response = httpx.get(f"http://127.0.0.1:{port}/",timeout=.5)
                    if response.status_code < 500:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.2)
            else:
                pytest.fail(f"Server startup timeout: {out/'server.log'}")
            yield mode, out, port
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
