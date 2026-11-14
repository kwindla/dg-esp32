import subprocess

import pytest

import ice_path


def test_darwin_interface_address_comes_from_ipconfig(monkeypatch):
    monkeypatch.setattr(ice_path.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(ice_path.subprocess, "run", lambda argv, **kw: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0, stdout="10.0.0.5\n"))
    assert ice_path.interface_ipv4("en0") == "10.0.0.5"
    assert calls == [["ipconfig", "getifaddr", "en0"]]


def test_darwin_interface_without_address_is_an_error(monkeypatch):
    monkeypatch.setattr(ice_path.sys, "platform", "darwin")
    monkeypatch.setattr(ice_path.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=""))
    with pytest.raises(OSError):
        ice_path.interface_ipv4("en9")


@pytest.mark.parametrize("name", ["BOT_ICE_REMOTE_NETWORK_ONLY", "BOT_IMPAIRED_INTERFACE"])
def test_darwin_refuses_linux_only_modes(monkeypatch, name):
    monkeypatch.setattr(ice_path.sys, "platform", "darwin")
    monkeypatch.setenv(name, "1")
    with pytest.raises(RuntimeError, match=name):
        ice_path.install()
