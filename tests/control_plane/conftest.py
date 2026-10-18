"""Acknowledged receipt persistence for control-plane transport tests."""
import copy

import pytest


@pytest.fixture
def acknowledged_sink():
    class AcknowledgedSink:
        def __init__(self):
            self.events = []

        def record(self, event):
            self.record_required(event)

        def record_required(self, event):
            self.events.append(copy.deepcopy(event))

    return AcknowledgedSink()
