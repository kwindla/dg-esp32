"""Pinned aiortc 1.15.0 T3 flight-accounting repair (AEC demo step 6c).

RFC 4960 6.2.1(D), 6.3.3: retransmitted gap-ACKed data is outstanding again.
Do not change the installed library, retransmission policy or congestion logic.
"""
import hashlib
from pathlib import Path

import aiortc
from aiortc import rtcsctptransport
from loguru import logger

EXPECTED_VERSION = '1.15.0'
EXPECTED_SHA256 = 'd0d42fd9b568d44e687b564ca17bb384ea06c481bf8f38091ccb09d20be716c6'
try:
    SOURCE_SHA256 = hashlib.sha256(Path(rtcsctptransport.__file__).read_bytes()).hexdigest()
except OSError:
    SOURCE_SHA256 = None


def supported(version, source_sha256):
    return version == EXPECTED_VERSION and source_sha256 == EXPECTED_SHA256


# One startup guard shared with the opt-in tap; no filesystem work per chunk.
SUPPORTED = supported(aiortc.__version__, SOURCE_SHA256)
ENABLED = False
_original_t3 = rtcsctptransport.RTCSctpTransport._t3_expired


def _t3_expired(self):
    if ENABLED:
        for chunk in self._sent_queue:
            if not chunk._abandoned:
                chunk._acked = False
    return _original_t3(self)


def install():
    """Idempotent; cooperate with the tap in either import order."""
    global ENABLED
    if not SUPPORTED:
        logger.error('SCTP_FLIGHT_FIX disabled: unsupported aiortc={} source_sha256={}',
                     aiortc.__version__, SOURCE_SHA256)
        return False
    if ENABLED:
        return True
    current = rtcsctptransport.RTCSctpTransport._t3_expired
    # The tap calls this module's stable dispatcher, which also works before
    # installation. Keep that outer observer when it was installed first.
    if not getattr(current, '_dg_sctp_tap', False):
        if current is not _original_t3:
            logger.error('SCTP_FLIGHT_FIX disabled: unexpected timer wrapper')
            return False
        rtcsctptransport.RTCSctpTransport._t3_expired = _t3_expired
    ENABLED = True
    logger.info('SCTP_FLIGHT_FIX enabled aiortc={} source_sha256={}',
                aiortc.__version__, SOURCE_SHA256)
    return True
