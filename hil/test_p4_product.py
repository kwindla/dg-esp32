"""Storage policy tests: synthetic authenticated bundles, no bench/device IO."""
import hashlib
import importlib
import json
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest

import boards
import board_serial
import board_smoke
import make_bundle
import p4_product as product


def table(*, name='storage', offset=0x410000, size=0x400000, extra=()):
    rows=[('factory',0,0,0x10000,0x400000), (name,1,0x83,offset,size), *extra]
    data=b''.join(struct.pack('<2sBBLL16sL',b'\xaa\x50',kind,subtype,at,length,label.encode(),0)
                  for label,kind,subtype,at,length in rows)
    data+=b'\xeb\xeb'+b'\xff'*14+hashlib.md5(data).digest()
    return data+b'\xff'*(0xc00-len(data))


def bundle(tmp_path, **kw):
    root=tmp_path/'bundle';root.mkdir()
    files={'dg-esp32.elf':b'ELF fixture','dg-esp32.bin':b'APP',
           'bootloader/bootloader.bin':b'BOOT','partition_table/partition-table.bin':table(**kw)}
    flash=dict(write_flash_args=['--flash-size','32MB'],flash_files={
        '0x2000':'bootloader/bootloader.bin','0x8000':'partition_table/partition-table.bin','0x10000':'dg-esp32.bin'},
        **{'partition-table':dict(offset='0x8000',file='partition_table/partition-table.bin')})
    files['flasher_args.json']=json.dumps(flash).encode()
    for name,data in files.items():
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    hashes={name:hashlib.sha256(data).hexdigest() for name,data in files.items()}
    (root/'manifest.json').write_text(json.dumps(dict(schema=1,firmware_commit='0'*40,
        board='p4xc',target='esp32p4',autostart=False,aec_enabled=False,
        elf_sha256=hashes['dg-esp32.elf'],sha256=hashes)))
    return root


class FakeTool:
    def __init__(self, events, *, fail=None, data=None):self.events=events;self.fail=fail;self.data=data
    def __call__(self, command, **kwargs):
        self.events.append(command)
        assert kwargs['check'] and kwargs['timeout']==180 and kwargs['pass_fds']==(123,)
        assert kwargs['stderr']==subprocess.STDOUT
        phase=next(word for word in ('write-flash','verify-flash','erase-region','read-flash') if word in command)
        if phase==self.fail:raise subprocess.CalledProcessError(1,command)
        if phase=='read-flash':Path(command[-1]).write_bytes(b'\xff'*0x400000 if self.data is None else self.data)
        return SimpleNamespace(returncode=0)


@pytest.fixture
def fake(monkeypatch):
    events=[]
    monkeypatch.setattr(boards,'require_bench',lambda:123)
    monkeypatch.setattr(boards,'checked_esptool_python',lambda:'/fake/pinned-python')
    monkeypatch.setattr(board_smoke,'verify_flash_mac',lambda board:boards.BOARDS[board]['by_id'])
    monkeypatch.setattr(subprocess,'run',FakeTool(events))
    monkeypatch.setattr(board_serial,'open_board',lambda board,**kw:events.append('open-'+board))
    return events


def test_exact_arguments_and_authenticated_full_readback(tmp_path, fake):
    root=bundle(tmp_path);out=tmp_path/'out';out.mkdir()
    record=product.prepare_storage(root,out)
    base=['/fake/pinned-python','-m','esptool','--chip','esp32p4','--port',boards.BOARDS['p4xc']['by_id'],
          '--baud','921600','--after','no-reset']
    assert fake==[base+['verify-flash','0x2000',str(root/'bootloader/bootloader.bin'),
        '0x8000',str(root/'partition_table/partition-table.bin'),'0x10000',str(root/'dg-esp32.bin')],
        base+['erase-region','0x410000','0x400000'],
        base+['read-flash','0x410000','0x400000',str(out/'candidate-storage-readback.bin')]]
    assert record==json.loads((out/'candidate-storage-erase.json').read_text())
    assert record['partition']['name']=='storage' and record['verified_bytes']==0x400000
    assert record['flash_verified'] and record['erased'] and record['readback_verified']
    assert record['readback_sha256']==make_bundle.digest(out/'candidate-storage-readback.bin')
    assert record['partition_table_sha256']==make_bundle.digest(root/'partition_table/partition-table.bin')
    assert record['manifest_sha256']==make_bundle.digest(root/'manifest.json')


@pytest.mark.parametrize('kw,reason',[
    ({'name':'factory-data'},'named storage'),({'offset':0x420000},'offset/size mismatch'),
    ({'size':0x401000},'offset/size mismatch'),
    ({'extra':[('storage',1,0x83,0x900000,0x1000)]},'named storage'),
    ({'extra':[('overlap',1,2,0x800000,0x2000)]},'overlaps'),
])
def test_partition_refusals_before_any_tool_or_serial(tmp_path, fake, kw, reason):
    root=bundle(tmp_path,**kw)
    with pytest.raises(ValueError,match=reason),product.candidate_storage('p4xc',root,tmp_path/'out'):
        pytest.fail('invalid candidate entered live helper')
    assert fake==[]


def test_table_hash_and_md5_are_both_required(tmp_path, fake):
    root=bundle(tmp_path);path=root/'partition_table/partition-table.bin'
    data=bytearray(path.read_bytes());data[20]^=1;path.write_bytes(data)
    with pytest.raises(ValueError,match='hash mismatch'):product.storage_plan(root)
    manifest=json.loads((root/'manifest.json').read_text());manifest['sha256'][str(path.relative_to(root))]=make_bundle.digest(path)
    (root/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='MD5 mismatch'):product.storage_plan(root)
    assert fake==[]


def test_changed_plan_and_unpinned_port_refused(tmp_path, fake, monkeypatch):
    root=bundle(tmp_path);out=tmp_path/'out';out.mkdir();plan=product.storage_plan(root)
    plan['partition']['size']+=0x1000
    with pytest.raises(ValueError,match='plan changed'):product.prepare_storage(root,out,plan=plan)
    monkeypatch.setattr(board_smoke,'verify_flash_mac',lambda board:'/dev/ttyACM9')
    with pytest.raises(ValueError,match='pinned P4 by-id'):product.prepare_storage(root,out)
    assert fake==[]


@pytest.mark.parametrize('failure,data,phases,reason',[
    ('verify-flash',None,1,'CalledProcessError'),('erase-region',None,2,'CalledProcessError'),
    ('read-flash',None,3,'CalledProcessError'),(None,b'\xff'*128,3,'size mismatch'),
    (None,b'\xff'*(0x400000-1)+b'\x00',3,'not all 0xFF')])
def test_failure_blocks_boot_and_preserves_evidence(tmp_path, fake, monkeypatch, failure, data, phases, reason):
    root=bundle(tmp_path);out=tmp_path/'out';out.mkdir()
    original=board_serial.open_board
    monkeypatch.setattr(subprocess,'run',FakeTool(fake,fail=failure,data=data))
    with pytest.raises((ValueError,subprocess.CalledProcessError)):
        with product.candidate_storage('p4xc',root,out):
            board_serial.open_board('p4xc',log_path=out/'serial.log')
    assert len(fake)==phases and all(isinstance(event,list) for event in fake)
    assert board_serial.open_board is original
    record=json.loads((out/'candidate-storage-erase.json').read_text())
    assert not record['readback_verified'] and reason in record['error']


def test_scope_only_gates_candidate_once_and_restores_hook(tmp_path, fake):
    root=bundle(tmp_path);out=tmp_path/'out';out.mkdir();original=board_serial.open_board
    with product.candidate_storage('p4xc',root,out):
        board_serial.open_board('p4xc',log_path=out/'factory-restore-serial.log')
        board_serial.open_board('echoear',log_path=out/'serial.log')
        with product.candidate_storage('p4xc',root,out):
            board_serial.open_board('p4xc',log_path=out/'serial.log')
        board_serial.open_board('p4xc',log_path=out/'serial.log')
    assert fake[:2]==['open-p4xc','open-echoear'] and fake[-2:]==['open-p4xc','open-p4xc']
    assert len(fake)==7 and board_serial.open_board is original


def test_s3_path_does_not_even_require_a_bundle(tmp_path, fake):
    with product.candidate_storage('echoear',tmp_path/'missing',tmp_path/'out'):
        board_serial.open_board('echoear',log_path=tmp_path/'serial.log')
    assert fake==['open-echoear']


@pytest.mark.parametrize('helper',product.HELPERS)
@pytest.mark.parametrize('board',['p4xc','echoear'])
def test_cli_routes_every_helper_and_restores_argv(tmp_path, fake, monkeypatch, helper, board):
    root=bundle(tmp_path);out=tmp_path/'out';out.mkdir();previous=sys.argv
    mode=[] if 'board_smoke' in helper or 'headless' in helper else ['live']
    arguments=[*mode,'--board',board,'--bundle',str(root),'--out',str(out)]
    def main():
        assert sys.argv==[helper,*arguments]
        from board_serial import open_board
        open_board(board,log_path=out/'serial.log');return 7
    monkeypatch.setattr(importlib.import_module(product.HELPERS[helper]),'main',main)
    assert product.main([helper,*arguments])==7 and sys.argv is previous
    assert fake[-1]=='open-'+board
    assert len(fake)==(4 if board=='p4xc' else 1)


@pytest.mark.parametrize('name',['p4_display','p4_headless','p4_audio','p4_audio_fix4','p4_contention'])
def test_real_helpers_reach_gate_after_flash_before_boot(tmp_path, fake, monkeypatch, name):
    root=bundle(tmp_path);out=tmp_path/'out';out.mkdir()
    manifest,flash=make_bundle.verify_bundle(root,'p4xc')
    helper=importlib.import_module(name)
    class StopBeforeBoot(Exception):pass
    def stop(board,**kw):
        fake.append('before-boot');raise StopBeforeBoot
    monkeypatch.setattr(board_serial,'open_board',stop)
    args=SimpleNamespace(board='p4xc',bundle=root,out=out)
    io=SimpleNamespace(python='/fake/pinned-python',flash=lambda *args:fake.append('candidate-flash'))
    if name in ('p4_display','p4_contention'):
        monkeypatch.setattr(helper,'bot_identity',lambda *args:123)
        if name=='p4_contention':
            import alsa
            monkeypatch.setattr(alsa,'resolve_jabra',lambda:('fake-card','fake-device'))
            monkeypatch.setattr(alsa,'set_volumes',lambda *args:None)
        call=lambda:helper.acquire(args,io,manifest,{})
    elif name=='p4_headless':
        monkeypatch.setattr(helper,'verify_flash_mac',board_smoke.verify_flash_mac)
        call=lambda:helper.acquire(args,io,manifest,flash)
    else:
        # Both real audio IO classes inherit this candidate-open path.
        cls=helper.VoiceIO if name=='p4_audio_fix4' else helper.HardwareIO
        io=object.__new__(cls);io.args=args;io.out=out
        call=io.open
        fake.append('candidate-flash')
    with pytest.raises(StopBeforeBoot),product.candidate_storage('p4xc',root,out):call()
    assert fake[-1]=='before-boot'
    commands=[row for row in fake if isinstance(row,list)]
    assert any('verify-flash' in row for row in commands[:-2])
    assert commands[-2][-3:]==['erase-region','0x410000','0x400000']
    assert commands[-1][-4:-1]==['read-flash','0x410000','0x400000']
