#!/usr/bin/env python3
"""Seal independent item4 and preserve C's failed-monitor calibration evidence."""
import json,time
from pathlib import Path
from step9a_r3b_40_campaign import finish,listeners_closed
from step9a_r3b_analyze import analyze
from step9a_r2_diagnose import save
OUT=Path('/home/khkramer/src/evidence/step9a/r3')
while not (OUT/'r3b-D40/result.json').exists() or not listeners_closed():time.sleep(1)
rows=[json.loads((OUT/name/'analysis.json').read_text()) for name in ('r3b-A40-continued','r3b-B40','r3b-C40')]
try:
    rows.append(finish(4,'r3b-D40'))
    assert rows[2]['whole_boot_hardware_errors']==rows[2]['context_count']==3 and rows[2]['cycle_count']==20 and rows[2]['all_contexts_captured']
    assert rows[2]['power']['required_cycles']==40
    save(OUT/'r3b-40-summary.json',{'completed':True,'cells':rows,'C_original_monitor_invalid_retained':True,'C_primary_calibration_note':'complete3/20 cycle hardwarecount/context; failed adjacentcleanupPERF remainsfailed, not clean','positive_controls':[r['cell'] for r in rows if r['whole_boot_hardware_errors']]})
except Exception as exc:save(OUT/'r3b-item4-blocked.json',{'reason':str(exc)});raise
