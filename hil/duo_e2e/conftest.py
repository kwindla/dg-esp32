"""Shared by the tests of what bot.sh may kill."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import types

import pytest

HERE = Path(__file__).parent
# Stands in for the bot, and for code that is not the bot: says it runs, then waits to be ended. It binds nothing.
WAITS = "import time; print('up', flush=True); time.sleep(300)"


@pytest.fixture
def sandbox(tmp_path):
    """A git repository of its own, with copies of bot.sh and botproc.py and real processes started in it.

    To the copies "this repository" is the sandbox. So the only processes they can verify, and that bot.sh
    can ask to have killed, are this fixture's children: a duo bot of the real repository is another
    project's to them.
    """
    root = (tmp_path / "sandbox").resolve()
    (root / "hil/duo_e2e").mkdir(parents=True)
    (root / "server").mkdir()
    for name in ("bot.sh", "botproc.py"):
        shutil.copy(HERE / name, root / "hil/duo_e2e" / name)
    (root / "server/duo_bot.py").write_text(WAITS + "\n")
    (root / "unrelated_module.py").write_text(WAITS + "\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    spec = importlib.util.spec_from_file_location("sandbox_botproc", root / "hil/duo_e2e/botproc.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    children = []

    def start(*arguments):
        """A real Python started in the sandbox with these arguments; its PID, once it runs what it was given."""
        child = subprocess.Popen([sys.executable, *arguments], cwd=root, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        children.append(child)
        assert child.stdout.readline() == b"up\n", arguments
        return child.pid

    yield types.SimpleNamespace(root=root, botproc=verifier, start=start)
    for child in children:
        child.kill()
        child.wait()
        child.stdout.close()
