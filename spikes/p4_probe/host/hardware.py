"""P4 runner preflight; validation/imports are host-only and never open serial."""
import fcntl
import os
import subprocess
from pathlib import Path

BENCH_LOCK = Path(f'/tmp/claude-{os.getuid()}/echoear-bench.lock')
WRITE_TIMEOUT = 1800
VERIFY_TIMEOUT = 1800
FLASH_TIMEOUT = 1800


def esptool_python():
    override = os.environ.get('ESPTOOL_PYTHON')
    if override: return override
    environment = os.environ.get('IDF_PYTHON_ENV_PATH')
    if environment: return str(Path(environment) / 'bin/python')
    return str(Path.home() / '.espressif/python_env/idf6.1_py3.12_env/bin/python')


def checked_esptool_python():
    python = esptool_python()
    result = subprocess.run([python, '-c', 'import esptool; print(esptool.__version__)'],
                            capture_output=True, text=True, timeout=10, check=True)
    if result.stdout.strip() != '5.5.0':
        raise RuntimeError('ESPTOOL_PYTHON must provide esptool 5.5.0')
    return python


def require_bench():
    if os.environ.get('DG_BUILD_LOCK_HELD') == '1':
        raise RuntimeError('Release the build lock before using the bench')
    try:
        fd = int(os.environ['DG_BENCH_LOCK_FD'])
        owned, expected = os.fstat(fd), BENCH_LOCK.stat()
        if (owned.st_dev, owned.st_ino) != (expected.st_dev, expected.st_ino):
            raise RuntimeError('DG_BENCH_LOCK_FD is not the bench lock file')
        # A fresh open must contend. An inherited env var alone proves nothing.
        with BENCH_LOCK.open('rb') as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                fcntl.flock(probe, fcntl.LOCK_UN)
                raise RuntimeError('Bench lock is not held')
        # Distinguish our inherited open-file description from another holder.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (KeyError, ValueError, OSError) as exc:
        raise RuntimeError('Run under hil/bench.py with its inherited bench lock') from exc
    return fd


def check_project_config(project, build):
    root = Path(project['project_path'])
    if (root / 'sdkconfig').exists():
        raise ValueError('Stale root sdkconfig exists; only -DSDKCONFIG=<build>/sdkconfig is supported')
    if Path(project['config_file']).absolute() != (build / 'sdkconfig').absolute():
        raise ValueError('Only -DSDKCONFIG=<build>/sdkconfig builds are supported')
