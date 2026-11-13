import hashlib
import json
import wave
import re
from pathlib import Path
from aiortc_client import FIXTURES, client_ready

def test_committed_fixture_contract():
    manifest = json.loads((FIXTURES/"manifest.json").read_text())
    assert set(manifest) == {"greeting.wav","reply_pineapple.wav","prompt_france.wav"}
    for name, info in manifest.items():
        data = (FIXTURES/name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == info["sha256"]
        with wave.open(str(FIXTURES/name)) as stream:
            assert (stream.getframerate(),stream.getnchannels(),stream.getsampwidth()) == (16000,1,2)
            assert 1 < stream.getnframes()/16000 < 5

def test_cpp_ready_wire_shape():
    upstream = Path(__file__).resolve().parents[2] / "third_party/pipecat-client-cxx"
    source = (upstream / "src/rtvi/messages.cpp").read_text()
    header = (upstream / "include/pipecat/rtvi/messages.h").read_text()
    ready = re.search(r"Message Message::client_ready.*?\n}", source, re.S)[0]
    assert '{"version", PROTOCOL_VERSION}' in ready and '{"about", about}' in ready
    type_enum = re.search(r"MessageType::(\w+)", ready)[1]
    wire_type = re.search(r"case MessageType::" + type_enum + r':\s*return "([^"]+)"', source)[1]
    protocol = re.search(r'PROTOCOL_VERSION = "([^"]+)"', header)[1]
    label = re.search(r'MESSAGE_LABEL = "([^"]+)"', header)[1]
    message = client_ready()
    assert set(message) == {"id", "label", "type", "data"}
    assert message["label"] == label and message["type"] == wire_type
    assert set(message["data"]) == {"version", "about"}
    assert message["data"]["version"] == protocol
    version_header = (upstream / "src/rtvi/about.cpp").read_text()
    library = re.search(r'LIBRARY_NAME = "([^"]+)"', version_header)[1]
    version = re.search(r'project\(pipecat VERSION ([\d.]+)',
                        (upstream / "CMakeLists.txt").read_text())[1]
    assert message["data"]["about"] == {"library": library, "library_version": version}



def test_offline_egress_guard():
    import os
    from pathlib import Path
    import subprocess
    import sys
    guard = Path(__file__).parent / "offline_guard"
    result = subprocess.run([sys.executable, "-c",
        "import socket; socket.getaddrinfo('example.com',443)"],
        env={**os.environ, "DG_OFFLINE_GUARD":"1", "PYTHONPATH":str(guard)},
        capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert "Offline external network access denied: example.com" in result.stderr
