"""Host-only immutable review bundle for the separate link soak; never launch."""
import argparse
import json
import re
import shlex
import subprocess
from pathlib import Path
from echoear_aec_bench import ROOT,sha,write
from echoear_aec_link_soak import validate

ESPTOOL_PYTHON='/home/khkramer/.espressif/python_env/idf6.1_py3.12_env/bin/python'
LOCKED_BSSID='b4:20:46:f4:ea:e5'
DECISIONS_10=Path('/home/khkramer/src/dg-esp32/proj-2026-10-15-0645/codex-jobs/step-9-coordinator-decisions-10.md')


def runner_command(digest, declaration, output):
    return ['uv','run','--frozen','--offline','--with','pyserial','--with','pyelftools','--with','idf-component-manager==3.1.2','python','../hil/echoear_aec_campaign.py','--link-soak','--declaration-sha256',digest,str(declaration),str(output)]


def launch_script(digest, declaration, output, precondition=None):
    """Generate only; neither freeze nor validation runs environment queries."""
    lines=['#!/usr/bin/env bash','# Prepared only; NOT EXECUTED. No retry/resume.',
           'set -euo pipefail','cd '+shlex.quote(str(ROOT/'server')),
           'readonly soak_output='+shlex.quote(str(output)),
           'if [[ -e "$soak_output" || -L "$soak_output" ]]; then',
           "  printf '%s\\n' 'Refusing existing soak output' >&2",'  exit 2','fi',
           "printf '%s  %s\\n' "+shlex.quote(digest)+' '+shlex.quote(str(declaration))+' | sha256sum --check --status',
           'export ESPTOOL_PYTHON='+shlex.quote(ESPTOOL_PYTHON),
           'export PYTHONUNBUFFERED=1','export PYTHONPATH=../hil:.']
    if precondition is not None:
        if (precondition.get('interface'),precondition.get('bssid'))!=('wlp8s0',LOCKED_BSSID) or not precondition.get('connection'):
            raise ValueError('Expected host BSSID lock precondition')
        profile=shlex.quote(precondition['connection'])
        if precondition.get('rule')=='host-environment-v8.5':
            from echoear_aec_host_gate import V85_POLICY
            if any(precondition.get(k)!=V85_POLICY[k] for k in ('interface','connection','connection_uuid','bssid')):
                raise ValueError('Expected declared v8.5 active profile identity')
            # The runner's structured guard repeats these checks and retains
            # raw queries at every boundary; this shell guard precedes runner exec.
            expected=shlex.quote(precondition['connection']+'\n'+precondition['connection_uuid'])
            query='nmcli --escape no -g GENERAL.CONNECTION,GENERAL.CON-UUID device show wlp8s0'
            lines += ['active_profile_before="$('+query+')"',
                      'if [[ "$active_profile_before" != '+expected+' ]]; then',
                      "  printf '%s\\n' 'Wrong active NetworkManager profile or UUID' >&2",'  exit 2','fi']
            profile='uuid '+shlex.quote(precondition['connection_uuid'])
        lines += ['configured_bssid="$(nmcli --escape no -g 802-11-wireless.bssid connection show '+profile+')"',
                  'if [[ "${configured_bssid,,}" != '+LOCKED_BSSID+' ]]; then',
                  "  printf '%s\\n' 'Host connection BSSID lock missing or wrong' >&2",'  exit 2','fi',
                  'host_link="$(iw dev wlp8s0 link)"',
                  "associated_bssid=\"$(printf '%s\\n' \"$host_link\" | awk '$1 == \"Connected\" && $2 == \"to\" {print tolower($3)}')\"",
                  'if [[ "$associated_bssid" != '+LOCKED_BSSID+' ]]; then',
                  "  printf '%s\\n' 'Host live BSSID association missing or wrong' >&2",'  exit 2','fi']
    if precondition is not None and precondition.get('rule')=='host-environment-v8.5':
        lines += ['active_profile_after="$('+query+')"',
                  'if [[ "$active_profile_after" != "$active_profile_before" ]]; then',
                  "  printf '%s\\n' 'Active NetworkManager profile changed during guard' >&2",'  exit 2','fi']
    lines += ['# Runner owns per-hold flock and signal-driven normal-image/mixer restore.',
              '# For stopping, identify the Python child using stop-instructions.txt.',
              'exec '+shlex.join(runner_command(digest,declaration,output))]
    return '\n'.join(lines)+'\n'


def validate_launch_script(script, digest, declaration, output, precondition=None):
    if 'export ESPTOOL_PYTHON='+shlex.quote(ESPTOOL_PYTHON)+'\n' not in script:
        raise ValueError('Launch script missing required ESPTOOL_PYTHON export')
    if script!=launch_script(digest,declaration,output,precondition):
        raise ValueError('Launch script differs from frozen guarded command')
    subprocess.run(['bash','-n'],input=script,text=True,check=True,capture_output=True)
    return dict(passed=True,hardware_access=False,environment_queries_executed=False,
                esptool_export=True,launch_status='NOT EXECUTED')


def validate_bundle(destination):
    destination=Path(destination).resolve();declaration=destination/'declaration.json'
    p=json.loads(declaration.read_text());digest=sha(declaration)
    if (destination/'declaration.sha256').read_text()!=digest+'  declaration.json\n':
        raise ValueError('Declaration digest mismatch')
    result=validate(p,p['output'])
    result['launch_script']=validate_launch_script((destination/'launch.sh').read_text(),digest,
        declaration,p['output'],p.get('environment_precondition'))
    return result


def runner_pattern(digest, declaration, output):
    # POSIX ERE, anchored at the interpreter: uv's argv contains python too.
    literal=lambda s:re.sub(r'([][(){}.^$*+?|\\])',r'\\\1',str(s))
    return (r'^([^ ]*/)?python([0-9.]+)? [^ ]*echoear_aec_campaign[.]py '
            r'--link-soak --declaration-sha256 '+literal(digest)+' '+
            literal(declaration)+' '+literal(output)+'$')


def freeze(destination, output, *, connection=None):
    destination=Path(destination).resolve();output=Path(output).resolve()
    if destination.exists() or output.exists():raise FileExistsError('Fresh freeze and nonexistent run output required')
    template=ROOT/'hil/plans/echoear-aec-link-soak-v1.json'
    p=json.loads(template.read_text());p.update(status='declared',output=str(output))
    if connection is not None:
        # Keep the experiment protocol and runner unchanged; identify this rerun
        # separately from the protocol's historical candidate-link-soak-01 name.
        p['run_id']='link-soak-02'
        p['environment_precondition']=dict(connection=connection,interface='wlp8s0',bssid=LOCKED_BSSID,
            checks=['nmcli saved connection BSSID lock','iw live associated BSSID'],
            policy='Launch refuses either mismatch or command failure; no network changes; prelaunch check only.')
        p['environment_context']='Coordinator decisions10: host BSSID lock applied 2026-10-17 11:16:38 to suppress background scans. Identical rerun measures post-fix device missing/late-burst base rate. Context supplied by coordinator, not independently measured during this host-only freeze.'
        p['source_paths'].extend([str(DECISIONS_10),str(DECISIONS_10.with_name('fable-step-9-link-soak-02-review.md')),str(DECISIONS_10.with_name('step-9-coordinator-decisions-11.md'))])
        from host_wifi_diagnostics import POLICY
        p['host_wifi_diagnostics']=POLICY
    # Bind all local Python helpers, including serial/cleanup/support imports,
    # plus runtime locks, template and mode documentation. Tests are bound too.
    files=[f for pattern in ('hil/*.py','server/*.py') for f in ROOT.glob(pattern)]
    files += [ROOT/n for n in ('server/uv.lock','server/pyproject.toml','hil/plans/echoear-aec-link-soak-v1.json','docs/echoear-aec-link-soak.md')]
    p['code_sha256']={str(f.relative_to(ROOT)):sha(f) for f in sorted(files)}
    p['inputs']=[dict(path=name,sha256=sha(name)) for name in p.pop('source_paths')]
    p['host_commit']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    result=validate(p,output)
    destination.mkdir(parents=True,exist_ok=False)
    declaration=destination/'declaration.json';write(declaration,p);digest=sha(declaration)
    (destination/'declaration.sha256').write_text(digest+'  declaration.json\n')
    script=launch_script(digest,declaration,output,p.get('environment_precondition'))
    launch=destination/'launch.sh';launch.write_text(script);launch.chmod(0o755)
    result['launch_script']=validate_launch_script(script,digest,declaration,output,p.get('environment_precondition'))
    write(destination/'validator.json',result)
    (destination/'validator.log').write_text('PASS: declaration and launch script validated host-only; no hardware or environment queries; launch NOT EXECUTED\n')
    (destination/'launch-command.txt').write_text('NOT EXECUTED\n'+shlex.quote(str(launch))+'\n')
    (destination/'stop-instructions.txt').write_text(
        '# Instructions only; NOT EXECUTED. Identify THIS Python runner.\n'
        '# A saved launch PID may identify nohup/uv: never signal that wrapper PID.\n'
        '# The interpreter anchor excludes uv even though its arguments contain python.\n'
        'pgrep -af '+shlex.quote(runner_pattern(digest,declaration,output))+'\n'
        '# After identifying that exact Python command, send SIGTERM to its PID:\n'
        '# kill -TERM <python-pid>\n'
        '# Wait for normal-image restore/hash verification, mixer equality and lock_released in result.json.\n'
        '# Do not kill -9, signal the wrapper or force-release flock. Restoration failure retains flock for operator recovery.\n'
        '# No other agent may use the bench during the hold.\n')
    write(destination/'code-manifest.json',p['code_sha256'])
    write(destination/'freeze-result.json',dict(passed=True,qualification=False,launch_status='NOT EXECUTED',declaration_sha256=digest,output_exists=output.exists()))
    print(json.dumps(dict(declaration=str(declaration),sha256=digest,validator='PASS',launch='NOT EXECUTED'),indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('destination',type=Path);parser.add_argument('output',type=Path,nargs='?')
    parser.add_argument('--connection',help='Prepare soak-02 with the declared host BSSID lock on this NM profile')
    parser.add_argument('--validate-bundle',action='store_true',help='Read-only declaration and launch-script validation; no environment commands')
    args=parser.parse_args()
    if args.validate_bundle:print(json.dumps(validate_bundle(args.destination),indent=2))
    elif args.output is None:parser.error('output required for freeze')
    else:freeze(args.destination,args.output,connection=args.connection)
