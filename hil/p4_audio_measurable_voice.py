#!/usr/bin/env python3
"""Add the measurable-harmonic voice gate around the frozen step-14 runner.

Coordinator-only live CLI: same arguments as p4_step14.py. The scoped dispatch
extends only acoustic reanalysis; old kinds and all restore/product/stack
checks execute their existing implementations. Importing this module does no IO.
"""
from contextlib import contextmanager
import json

import p4_audio as audio
import p4_audio_measurable as measurable
import p4_step14


@contextmanager
def gates():
    previous = audio.reanalysis_gate
    def dispatch(path, manifest):
        row = json.loads(path.read_text())
        if row.get('kind')=='offline-p4-measurable-harmonics':
            return measurable.reanalysis_gate(path,manifest)
        return previous(path,manifest)
    audio.reanalysis_gate = dispatch
    try: yield
    finally: audio.reanalysis_gate = previous


def main(argv=None):
    with gates():
        return p4_step14.main(argv)


if __name__=='__main__': raise SystemExit(main())
