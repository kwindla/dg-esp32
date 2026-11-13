"""Pure R7 replay from raw restore, boot and storage witnesses."""
import re
from pathlib import Path

from p4_parity import load,digest,require,RESTORE_SHA256,PIN

S3_RESTORE_ELF='55152f25dcec0e164c8a087d7f5962dd81191e2cdd3f41ac882a58a0a57a626e'

FATAL=r'Guru Meditation|panic(?:ked|\W|$)|Stack canary|abort\(\) was called|assert(?:ion)? failed|Brownout|GPIO ownership conflict|Virtual address not enough'


def reject_fatal(lines,capture='restore'):
    require(not any(re.search(FATAL,line,re.I) for line in lines),'fatal diagnostic in '+capture+' capture')


def storage_commands(plan,destination,python):
    from boards import BOARDS
    from make_bundle import local_file
    base=[python,'-m','esptool','--chip','esp32p4','--port',BOARDS['p4xc']['by_id'],
        '--baud','921600','--after','no-reset']
    files=[value for offset,name in plan['flash']['flash_files'].items() for value in (offset,str(local_file(Path(plan['bundle']),name)))]
    offset,size=hex(plan['partition']['offset']),hex(plan['partition']['size'])
    return [base+['verify-flash',*files],base+['erase-region',offset,size],base+['read-flash',offset,size,str(destination)]]


def storage_logs(plan,commands,logs):
    """Esptool 5.5's terminal success records, not summary flags alone."""
    from make_bundle import local_file
    for text in logs:
        require(not re.search(FATAL+r'|\bFAILED\b|Traceback|A fatal error|\bError:',text,re.I),
            'failed storage operation output')
        require(text.rstrip().endswith('Staying in bootloader.'),'truncated storage operation log')
    verified=re.findall(r"Verifying (0x[0-9a-f]+) \((\d+)\) bytes at (0x[0-9a-f]+) in flash against '([^']+)'\.\.\.\s*Verification successful \(digest matched\)\.",logs[0],re.I)
    expected=[(local_file(Path(plan['bundle']),name).stat().st_size,int(offset,16),str(local_file(Path(plan['bundle']),name)))
        for offset,name in plan['flash']['flash_files'].items()]
    require([(int(h,16),int(n),int(offset,16),name) for h,n,offset,name in verified]==
        [(size,size,offset,name) for size,offset,name in expected],'flash verification success/input evidence differs')
    require(len(re.findall(r'Flash memory region erased successfully in [0-9.]+ seconds\.',logs[1]))==1,
        'storage erase completion missing')
    read=re.findall(r"Read (\d+) bytes from (0x[0-9a-f]+) in [0-9.]+ seconds .* to '([^']+)'\.",logs[2],re.I)
    require(len(read)==1 and (int(read[0][0]),int(read[0][1],16),read[0][2])==
        (plan['partition']['size'],plan['partition']['offset'],commands[2][-1]),'storage readback completion/destination differs')


def storage_proof(directory,bundle,sources):
    from p4_product import storage_plan
    from boards import BOARDS
    directory=Path(directory);path=directory/'candidate-storage-erase.json';row=load(path)
    plan=storage_plan(Path(bundle))
    require(all(row.get(k)==plan[k] for k in ('elf_sha256','manifest_sha256','partition_table',
        'partition_table_sha256','partition')),'storage identity/partition differs')
    require(row.get('port')==BOARDS['p4xc']['by_id'] and not row.get('error') and
        all(row.get(k) is True for k in ('flash_verified','erased','readback_verified')),
        'storage erase/verify/readback incomplete')
    require(row.get('readback_file')=='candidate-storage-readback.bin','unexpected storage readback path')
    readback=directory/row['readback_file'];data=readback.read_bytes();size=plan['partition']['size']
    require(row.get('verified_bytes')==size and len(data)==size and data.count(255)==size and
        digest(readback)==row.get('readback_sha256'),'storage readback bytes/hash differ')
    commands=row.get('commands');require(isinstance(commands,list) and len(commands)==3,'three ordered storage commands required')
    python=commands[0][0]
    require(isinstance(python,str) and Path(python).is_absolute() and Path(python).name in ('python','python3') and
        Path(python).is_file(),'storage interpreter binding differs')
    destination=Path(commands[2][-1])
    require(destination.is_absolute() and destination.name==readback.name,'storage readback destination differs')
    require(commands==storage_commands(plan,destination,python),'storage operation/order/arguments differ')
    sources[str(path.resolve())]=digest(path);sources[str(readback.resolve())]=digest(readback)
    logs=[]
    for phase in ('verify','erase','readback'):
        log=directory/('candidate-storage-'+phase+'.log')
        require(log.is_file() and log.stat().st_size>0,'missing storage operation log')
        logs.append(log.read_text())
        sources[str(log.resolve())]=digest(log)
    storage_logs(plan,commands,logs)
    if destination.resolve()!=readback.resolve():
        # Read-only copies may retain absolute paths. Authenticate every byte
        # against its retained origin; never accept a detached FF file alone.
        for local in (path,readback,*(directory/('candidate-storage-'+p+'.log') for p in ('verify','erase','readback'))):
            origin=destination.parent/local.name
            require(origin.is_file() and digest(origin)==digest(local),'copied storage witness differs from retained origin')
            sources[str(origin.resolve())]=digest(origin)
    return dict(elf_sha256=plan['elf_sha256'],partition_table_sha256=plan['partition_table_sha256'],
        verified_bytes=size,readback_sha256=digest(readback))


def restore_proof(directory,summary,plan=None):
    from board_smoke import boot_check
    from p4_step14 import restore_record
    directory=Path(directory).resolve();raw=directory/'restore.json';restore=load(raw)
    require(restore==summary.get('restore') and restore.get('clean') is True,'raw restore/summary conflict or failure')
    sources={str(raw):digest(raw)};board=summary['board']
    record_path=Path(plan['restore_record']) if plan else PIN/'restore-of-record.json'
    expected_sha=plan['restore_record_sha256'] if plan else RESTORE_SHA256
    require(expected_sha==RESTORE_SHA256,'wrong restore record hash')
    record=restore_record(record_path,expected_sha)
    sources[str(record_path.resolve())]=digest(record_path)
    p4_manifest=Path(record['bundle'])/'manifest.json';sources[str(p4_manifest)]=digest(p4_manifest)
    sources[record['headroom']]=digest(record['headroom'])
    if board=='p4xc':
        require(restore.get('action')=='p4-product-of-record' and restore.get('attempts')==[],
            'factory/retried P4 restore cannot pass')
        manifest=load(p4_manifest);expected=record['elf_sha256'];serial=directory/'restore-serial.log'
        lines=serial.read_text().splitlines()
        reject_fatal(lines)
        starts=[i for i,line in enumerate(lines) if 'elf_sha256=' in line]
        require(starts,'restored P4 boot missing')
        boot_check(lines[starts[-1]:],manifest,board)
        folders=sorted(directory.glob('restore-storage-*'))
        require(len(folders)==1 and folders[0].is_dir(),'one clean P4 restore storage proof required')
        storage=storage_proof(folders[0],record['bundle'],sources)
        # Candidate storage is separately bound to the image under test.
        require(summary.get('bundle'),'candidate bundle binding missing')
        candidate_storage=storage_proof(directory,summary['bundle'],sources)
    else:
        require(board=='echoear' and restore.get('restored_elf_sha256')==S3_RESTORE_ELF and
            len(restore.get('attempts',[]))==1,'wrong or retried S3 restore')
        attempt=restore['attempts'][0]
        require(attempt.get('label')=='restore-1' and attempt.get('error') is None and
            attempt.get('restored_elf_sha256')==S3_RESTORE_ELF,'invalid S3 restore attempt')
        expected=S3_RESTORE_ELF;serial=directory/'restore-1-serial.log';lines=serial.read_text().splitlines()
        reject_fatal(lines)
        from echoear_restore_verify import boot_proof
        require(boot_proof(serial,expected)['passed'] is True,'S3 restored boot proof failed')
        from echoear_aec_bench import NORMAL,verify_bundle
        require(Path(restore['image']).resolve()==NORMAL.resolve(),'S3 restore image selection differs')
        source=Path(restore['image'])/'manifest.json';manifest=load(source)
        retained=directory/'restore-1-image.json'
        hashes=verify_bundle(NORMAL,expected)
        require(load(retained)==dict(elf_sha256=expected,files=hashes,boot_ref_gain_db=None) and
            manifest['elf_sha256']==expected,'S3 restore image binding differs')
        sources[str(source)]=digest(source);sources[str(retained)]=digest(retained)
        for name,row in manifest.get('files',manifest.get('sha256',{})).items():
            file=Path(restore['image'])/name
            require(digest(file)==(row if isinstance(row,str) else row['sha256']),
                'S3 restore artifact changed');sources[str(file)]=digest(file)
        storage=candidate_storage=dict(applicable=False,reason='P4-only named-storage policy')
    hashes=[m[1] for line in lines for m in [re.search(r'elf_sha256=([0-9a-f]{64})',line)] if m]
    require(hashes and all(value==expected for value in hashes),'conflicting/wrong restored ELF')
    sources[str(serial)]=digest(serial)
    return dict(restored_elf_sha256=expected,restore_record_sha256=expected_sha,
        storage=storage,candidate_storage=candidate_storage,sources=sources)
