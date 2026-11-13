#!/usr/bin/env python3
"""Compare generated config keys/status only. Never emit configuration values."""
import argparse
import json
from pathlib import Path
import re
import sys


class ConfigError(ValueError): pass


def settings(path):
    path = Path(path)
    # Refuse arbitrary sources, directories and aliases to private .env/local
    # files. Only generated sdkconfig* files are supported by this utility.
    if (not path.name.startswith('sdkconfig') or path.name == 'sdkconfig.local' or not path.is_file() or
        not path.resolve().name.startswith('sdkconfig') or path.resolve().name == 'sdkconfig.local'):
        raise ConfigError('Expected a generated file named sdkconfig*')
    values = {}
    try:
        with path.open() as stream:
            for number, line in enumerate(stream, 1):
                line = line.rstrip('\r\n')
                match = re.fullmatch(r'(CONFIG_[A-Z0-9_]+)=(.*)', line)
                disabled = re.fullmatch(r'# (CONFIG_[A-Z0-9_]+) is not set', line)
                if match: key, value = match.groups()
                elif disabled: key, value = disabled[1], 'n'
                elif not line.strip() or line.lstrip().startswith('#'): continue
                else: raise ConfigError(f'Malformed generated configuration at line {number}')
                # IDF emits some deprecated aliases twice. Match Kconfig's
                # last-assignment semantics without exposing either value.
                values[key] = value
    except (OSError, UnicodeError) as exc:
        raise ConfigError('Cannot read generated configuration') from exc
    return values


def compare(left, right):
    keys = set(left) | set(right)
    result = {status: [] for status in ('equal', 'differ', 'added', 'removed')}
    for key in sorted(keys):
        status = 'added' if key not in left else 'removed' if key not in right else 'equal' if left[key] == right[key] else 'differ'
        result[status].append(key)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('left', type=Path); parser.add_argument('right', type=Path, nargs='?')
    parser.add_argument('--expect-keys', help='comma-separated CONFIG_* keys; with two paths checks the right file')
    args = parser.parse_args(argv)
    if args.right is None and args.expect_keys is None: parser.error('Two paths or --expect-keys required')
    try:
        left = settings(args.left); right = settings(args.right) if args.right is not None else left
        if args.expect_keys is not None:
            keys = args.expect_keys.split(',')
            keys = [key if key.startswith('CONFIG_') else 'CONFIG_'+key for key in keys]
            if not keys or any(not re.fullmatch(r'CONFIG_[A-Z0-9_]+', key) for key in keys):
                raise ConfigError('Expected valid configuration key names')
            result = dict(present=sorted(set(keys)&set(right)), missing=sorted(set(keys)-set(right)))
            failed = bool(result['missing'])
        else:
            result = compare(left, right); failed = any(result[status] for status in ('differ', 'added', 'removed'))
        print(json.dumps(result, indent=2)); return int(failed)
    except ConfigError as exc:
        print('Invalid generated config input: '+str(exc), file=sys.stderr); return 2
    except (OSError, RuntimeError):
        # Controlled diagnostics only: never values, input lines or contents.
        print('Invalid generated config input or key request', file=sys.stderr); return 2


if __name__ == '__main__': raise SystemExit(main())
