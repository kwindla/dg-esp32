#!/usr/bin/env python3
"""Step-15 closing metadata and adapters over the frozen applied-image tools."""
from contextlib import contextmanager
import copy
import sys

from p4_aec_applied import checked_recommendation, GAIN_SELECTION, require
from make_bundle import digest

DECISION='/home/khkramer/src/dg-esp32/proj-2026-10-30-2143/codex-jobs/decision-2026-11-03-close-15-and-pin.md'
DECISION_SHA256='1f66bbf766e69e6968bb966ff73bb61f36a5f43092d2f53692408d8748d2a39e'
V1='/home/khkramer/src/evidence/echoear/p4/step-15d-validation/coordinator-run-1/validation-report-01.json'
V1_SHA256='cc7f8ef7c5f91e73961517dfbb98750edea23ef69d04668ea6bec6d56d82c661'
PROVISIONAL_NOTE='provisional: bench speaker not final hardware'
CALIBRATION_STATUS=dict(gain='selected-single-trial',guard='provisional',delay='measured-diagnostic')
GUARD_VALIDATION=dict(basis='event-detection-only',physically_validated_in_window=False,
    deferred_until='final speaker hardware')
DELAY_PROVENANCE=dict(kind='measured-diagnostic',measured=True,delay_samples=1024,
    path='diagnostic reference -> AEC output',decision_file=DECISION,decision_sha256=DECISION_SHA256,
    evidence_file=V1,evidence_sha256=V1_SHA256,repetitions=3,minimum_correlation=0.99998,
    nearend_locator_gate='waived for this 1024-sample value only; future measurements retain the gate')


def labels(selection_delay):
    require(digest(DECISION)==DECISION_SHA256 and digest(V1)==V1_SHA256,
        'closing decision or V1 evidence changed')
    return copy.deepcopy(dict(calibration_status=CALIBRATION_STATUS,gain_selection=GAIN_SELECTION,
        guard_validation=GUARD_VALIDATION,delay_provenance=DELAY_PROVENANCE,
        selection_delay_provenance=selection_delay,
        provisional_notes={key:PROVISIONAL_NOTE for key in ('gain','guard','double_talk','nearend_level')}))


def calibration_labels(calibration,selection_delay):
    expected=labels(selection_delay)
    require(all(calibration.get(key)==value for key,value in expected.items()),
        'measured-diagnostic delay with decision/V1 references and provisional acoustic notes required')
    require(calibration.get('measured') is False and not any(key in calibration for key in
        ('gain_guard_measured','gain_measured','guard_measured','delay_measured')),
        'overall calibration must not claim full measured validation')
    require(type(calibration['gain_selection']['prompt_trials_per_gain']) is int and
        calibration['guard_validation']['physically_validated_in_window'] is False,
        'typed trial count/physical validation status required')


@contextmanager
def adapters():
    """Only replace metadata readers; preserve frozen acquisition/scoring gates."""
    import p4_aec_applied as applied
    import p4_aec_product as product
    import p4_aec_image_measure as image
    old_labels,old_gate,old_delay=applied.calibration_labels,product.applied_gate,image.image_delay
    def gate(manifest,path):
        result=old_gate(manifest,path)
        if path is not None:
            result.update(labels(checked_recommendation(path)['delay_provenance']))
        return result
    def delay(path,plan):
        historical=old_delay(path,plan)
        return dict(copy.deepcopy(DELAY_PROVENANCE),record_file=historical['record_file'],
            record_sha256=historical['record_sha256'],selection_delay_provenance=historical,
            applied_elf_sha256=plan['elf_sha256'])
    applied.calibration_labels=calibration_labels;product.applied_gate=gate;image.image_delay=delay
    try:yield
    finally:applied.calibration_labels=old_labels;product.applied_gate=old_gate;image.image_delay=old_delay


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    require(argv and argv[0] in ('product','image'),'expected product or image followed by frozen tool arguments')
    import p4_aec_product as product
    import p4_aec_image_measure as image
    tool=product if argv.pop(0)=='product' else image
    with adapters():return tool.main(argv)


if __name__=='__main__':raise SystemExit(main())
