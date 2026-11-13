"""Fake filesystem/image verification; no build or hardware tools run."""
import copy
import json
from pathlib import Path
import pytest
import duo_bundle as bundle
from duo_identity import write_new


def fixture(tmp_path,board='echoear'):
    flash=dict(flash_files={'0x10000':'dg-esp32.bin'},write_flash_args=[],extra_esptool_args=dict(chip=bundle.base.CHIPS[board]))
    (tmp_path/'dg-esp32.elf').write_bytes(b'synthetic ELF')
    (tmp_path/'dg-esp32.bin').write_bytes(b'synthetic app')
    write_new(tmp_path/'flasher_args.json',flash)
    hashes={p.name:bundle.base.digest(p) for p in tmp_path.iterdir()}
    m=dict(schema=1,profile='duo',board=board,target=bundle.base.CHIPS[board],firmware_commit='a'*40,
        aec_enabled=True,autostart=True,elf_sha256=hashes['dg-esp32.elf'],sha256=hashes,
        files={n:dict(sha256=h,bytes=(tmp_path/n).stat().st_size) for n,h in hashes.items()},flash_args=flash,
        config=dict(CONFIG_DG_DUO=1,CONFIG_DG_ECHOEAR_AEC=1,CONFIG_DG_DEMO_AUTOSTART=1,CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB=30),
        boot_ref_gain_db=30,expected_boot_line='MIC3_GAIN=0x1a analog_db=30')
    write_new(tmp_path/'manifest.json',m)
    return m


def check(tmp_path,board='echoear'):
    return bundle.verify_bundle(tmp_path,board,image_check=lambda path,board:dict(elf_sha256=bundle.base.digest(path/'dg-esp32.elf')))


@pytest.mark.parametrize('board',['echoear','p4xc'])
def test_keeps_duo_aec_autostart_and_flash_metadata(tmp_path,board):
    m=fixture(tmp_path,board);got,flash=check(tmp_path,board)
    assert got==dict(m,provenance_status='recorded-only') and got['profile']=='duo' and got['aec_enabled'] and got['autostart'] and flash==m['flash_args']


@pytest.mark.parametrize('bad',['profile','aec','autostart','duo','gain','boot','board','elf','commit','size','map','escape'])
def test_bad_bundle_never_passes(tmp_path,bad):
    m=fixture(tmp_path)
    if bad=='profile':m['profile']='aec-demo'
    if bad=='aec':m['aec_enabled']=False
    if bad=='autostart':m['autostart']=False
    if bad=='duo':m['config']['CONFIG_DG_DUO']=0
    if bad=='gain':m['boot_ref_gain_db']=31
    if bad=='boot':m['expected_boot_line']='wrong'
    if bad=='board':m['board']='p4xc'
    if bad=='elf':m['elf_sha256']='0'*64
    if bad=='commit':m['firmware_commit']='short'
    if bad=='size':m['files']['dg-esp32.bin']['bytes']+=1
    if bad=='map':m['flash_args']['flash_files']['0x20000']='unhashed.bin'
    if bad=='escape':m['sha256']['../escape.bin']='0'*64;m['files']['../escape.bin']=dict(sha256='0'*64,bytes=1)
    (tmp_path/'manifest.json').write_text(json.dumps(m))
    with pytest.raises((ValueError,OSError)):check(tmp_path)


def test_corrupt_bytes_and_unbound_images_rejected(tmp_path):
    fixture(tmp_path);(tmp_path/'dg-esp32.bin').write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='hash/size'):check(tmp_path)


def test_real_image_parser_rejects_fake_image_before_claiming_duo(tmp_path):
    fixture(tmp_path)
    with pytest.raises(ValueError,match='image header'):bundle.verify_bundle(tmp_path)


def test_image_verifier_requires_image_elf_binding(tmp_path):
    fixture(tmp_path)
    with pytest.raises(ValueError,match='ELF binding'):
        bundle.verify_bundle(tmp_path,image_check=lambda *args:dict(elf_sha256='0'*64))


def test_make_requires_build_lock_before_reading_inputs(tmp_path,monkeypatch):
    monkeypatch.delenv('DG_BUILD_LOCK_HELD',raising=False)
    with pytest.raises(ValueError,match='build_locked'):bundle.make_bundle(tmp_path,tmp_path/'out','echoear')


def test_frozen_adapter_routes_only_duo_and_always_restores(tmp_path,monkeypatch):
    import board_smoke
    fixture(tmp_path);seen=[]
    monkeypatch.setattr(bundle,'verify_bundle',lambda directory,board:('duo',board))
    original=bundle.base.verify_bundle;smoke_original=board_smoke.verify_bundle
    with pytest.raises(RuntimeError):
        with bundle.frozen_adapters():
            assert bundle.base.verify_bundle(tmp_path,'p4xc')==('duo','p4xc')
            with pytest.raises(ValueError):bundle.base.verify_bundle(tmp_path,'echoear',True)
            raise RuntimeError('test abort')
    assert bundle.base.verify_bundle is original and board_smoke.verify_bundle is smoke_original


def test_make_copies_only_explicit_image_inputs_and_safe_metadata(tmp_path,monkeypatch):
    build=tmp_path/'build';build.mkdir();out=tmp_path/'out'
    m=fixture(build)
    project=dict(project_path=str(bundle.ROOT),target='esp32s3',app_elf='dg-esp32.elf',app_bin='dg-esp32.bin',
                 project_version='abc123',config_file=str(tmp_path/'private-config-not-created'))
    write_new(build/'project_description.json',project)
    monkeypatch.setenv('DG_BUILD_LOCK_HELD','1')
    def check_output(command,**kwargs):
        return 'abc123\n' if command[1]=='describe' else 'a'*40+'\n' if command[1]=='rev-parse' else ''
    monkeypatch.setattr(bundle.subprocess,'check_output',check_output)
    checks=[]
    def enabled(path,key):checks.append((str(path),key));return True
    monkeypatch.setattr(bundle.base,'config_enabled',enabled)
    monkeypatch.setattr(bundle.base,'config_gain',lambda path:30)
    image=lambda path,board:dict(elf_sha256=bundle.base.digest(path/'dg-esp32.elf'))
    monkeypatch.setattr(bundle,'image_verify',image)
    verify=bundle.verify_bundle
    monkeypatch.setattr(bundle,'verify_bundle',lambda directory,board:verify(directory,board,image_check=image))
    result=bundle.make_bundle(build,out,'echoear')
    assert result['profile']=='duo' and result['aec_enabled'] and result['autostart']
    assert {p.name for p in out.iterdir()}=={'flasher_args.json','dg-esp32.elf','dg-esp32.bin','manifest.json'}
    assert {key for _,key in checks}=={'DG_DUO','DG_ECHOEAR_AEC','DG_DEMO_AUTOSTART','DG_BOARD_ECHOEAR'}
    assert not (tmp_path/'private-config-not-created').exists()
