#!/usr/bin/env python3
"""R3/R3W step-14 matrix with immediate bundles and empty 13b config diff.

Run only after build permission, through build_locked.sh in the IDF environment.
The existing baseline runner owns fresh configs, builds and lock restoration.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import capture_s3_baselines as baseline

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'hil'))
from make_bundle import make_bundle


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous',type=Path,required=True,help='accepted 13b baseline manifest')
    parser.add_argument('--references',type=Path,required=True,help='private 13b config reference paths and SHA256s')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--label',default='step-14')
    args=parser.parse_args(argv)
    if os.environ.get('DG_BUILD_LOCK_HELD')!='1':raise RuntimeError('Use scripts/build_locked.sh')
    subprocess.run(['git','diff','--exit-code','--quiet','HEAD','--','dependencies.lock'],cwd=ROOT,check=True)
    subprocess.run(['git','diff','--exit-code','--quiet','HEAD','--','dependencies.waveshare146.lock'],cwd=ROOT,check=True)
    previous=json.loads(args.previous.read_text());references=json.loads(args.references.read_text())
    if previous.get('complete') is not True:raise ValueError('complete accepted 13b baseline required')
    for variant in baseline.VARIANTS:
        if variant not in previous['variants']:raise ValueError('missing 13b variant: '+variant)
        if variant=='waveshare_146':continue
        row=references[variant]
        if baseline.sha(row['reference'])!=row['sha256']:raise ValueError('13b reference hash changed: '+variant)
    original=baseline.snapshot
    def snapshot(build,config,destination,normal,aec,demo):
        result=original(build,config,destination,normal,aec,demo)
        variant='waveshare_146' if build.name=='build-ws146' else ('aec-' if aec else '')+('demo' if demo else 'normal')
        if result['canonical_config_sha256']!=previous['variants'][variant]['canonical_config_sha256']:
            raise ValueError('config digest changed versus 13b: '+variant)
        if variant!='waveshare_146':
            with baseline.keydiff_name(config) as named:
                baseline.run([sys.executable,str(ROOT/'scripts/sdkconfig_keydiff.py'),
                    references[variant]['reference'],str(named)],destination/'keydiff-step13b.json')
            # Frozen make_bundle hashes root dependencies.lock. Canonical S3
            # lock state must be restored before taking the bundle snapshot.
            subprocess.run(['git','checkout','--','dependencies.lock'],cwd=ROOT,check=True)
            make_bundle(build,args.out/('bundle-echoear-'+variant),'echoear')
        return result
    baseline.snapshot=snapshot
    try:baseline.main([args.label])
    finally:
        baseline.snapshot=original
        # The resolver refreshes Waveshare's manifest_hash even with unchanged
        # versions. This step declares no manifest change; retain its pin.
        subprocess.run(['git','checkout','--','dependencies.waveshare146.lock'],cwd=ROOT,check=True)


if __name__=='__main__':main()
