#!/usr/bin/env python3
"""The R4 gate must reject a real shared-source mutation, not a missing tree."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='board-neutral-', dir=os.environ.get('TMPDIR', str(Path.home()/'.cache/gap3-tmp'))) as temp:
    root = Path(temp)
    # No firmware/config files are copied or read; symlink unchanged components.
    (root/'components').symlink_to(ROOT/'components', target_is_directory=True)
    shutil.copytree(ROOT/'main', root/'main', ignore=shutil.ignore_patterns('test_host'))
    command = [str(ROOT/'scripts/check_board_neutral.sh'), '--root', str(root)]
    control = subprocess.run(command, capture_output=True, text=True)
    assert control.returncode == 0, control.stdout+control.stderr
    source = root/'main/main.cpp'
    source.write_text(source.read_text()+'\n#if CONFIG_DG_BOARD_ECHOEAR\n#endif\n')
    mutant = subprocess.run(command, capture_output=True, text=True)
    assert mutant.returncode == 1 and 'main/main.cpp:' in mutant.stderr, mutant.stdout+mutant.stderr
    print('R4 mutation killed: #if CONFIG_DG_BOARD_ECHOEAR in main/main.cpp rejected; control passes.')

# Isolated real files: gitignore and hidden paths must never hide violations.
with tempfile.TemporaryDirectory(prefix='board-neutral-hidden-', dir=os.environ.get('TMPDIR', str(Path.home()/'.cache/gap3-tmp'))) as temp:
    root=Path(temp);(root/'main').mkdir();(root/'components').mkdir();(root/'.git').mkdir()
    (root/'.gitignore').write_text('main/ignored.cpp\n')
    (root/'main/Kconfig.projbuild').write_text('# CONFIG_DG_BOARD_ECHOEAR\n')
    command=[str(ROOT/'scripts/check_board_neutral.sh'),'--root',str(root)]
    assert subprocess.run(command,capture_output=True).returncode==0
    for name in ('ignored.cpp','.hidden.cpp','Kconfig.cpp','Kconfig_fake.h','Kconfig.projbuild.cpp'):
        source=root/'main'/name;source.write_text('#if CONFIG_DG_BOARD_ECHOEAR\n#endif\n')
        if name in ('ignored.cpp','.hidden.cpp'):
            ordinary=subprocess.run(['rg','-n','CONFIG_DG_BOARD_',str(source.parent)],capture_output=True,text=True)
            assert name not in ordinary.stdout, ordinary.stdout
        result=subprocess.run(command,capture_output=True,text=True)
        assert result.returncode==1 and name+':' in result.stderr, result.stdout+result.stderr
        source.unlink()
    print('R4 rejects hidden, ignored and disguised Kconfig source files.')
