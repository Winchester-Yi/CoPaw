# -*- coding: utf-8 -*-
"""Budget traversal must stay finite without rejecting shared JSON values."""

import subprocess
import sys
from datetime import datetime

from swe.tracing.models import EventType, Span
from swe.tracing.writer import _estimate_bytes


def test_repeated_containers_are_counted_for_each_json_occurrence():
    shared = ["value" * 100]
    span = Span(
        span_id="s",
        trace_id="t",
        source_id="source",
        name="tool",
        start_time=datetime.now(),
        event_type=EventType.TOOL_CALL_START,
        tool_input={"a": shared},
    )
    first = _estimate_bytes(span)
    span.tool_input["b"] = shared
    assert _estimate_bytes(span) - first > 7000


def test_cyclic_input_does_not_spin_in_budget_traversal():
    # Bound the old infinite loop without leaving a stuck test thread behind.
    code = """
from datetime import datetime
from swe.tracing.models import EventType, Span
from swe.tracing.writer import _estimate_bytes
value = []
value.append(value)
span = Span(span_id='s', trace_id='t', source_id='source', name='tool',
    start_time=datetime.now(), event_type=EventType.TOOL_CALL_START,
    tool_input={'cycle': value})
try:
    _estimate_bytes(span)
except ValueError:
    pass
else:
    raise AssertionError('circular input was not rejected')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        timeout=2,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
