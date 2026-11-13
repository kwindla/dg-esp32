"""Shared setup for the offline suites in this directory."""
import gc

import pytest


@pytest.fixture(scope="session", autouse=True)
def frozen_import_heap():
    """Keep full garbage collections short.

    Importing Pipecat and its dependencies leaves several hundred thousand
    long-lived objects behind. A generation-2 collection that walks them takes
    about 90 ms on the bench host: longer than the 80 ms transaction deadline
    some duo tests use, and the test it lands in moves whenever a test is
    added. Freezing the heap once the test modules are imported takes those
    objects out of every later collection.
    """
    gc.collect()
    gc.freeze()
    yield
    gc.unfreeze()
