# -*- coding: utf-8 -*-
import pytest
from pydantic import ValidationError

from swe.app.crons.models import CronRecheckSpec


def test_recheck_rule_normalizes_instruction():
    rule = CronRecheckSpec(delay_seconds=60, instruction="  check A  ")
    assert rule.instruction == "check A"


@pytest.mark.parametrize("delay", [True, "60", 59, 604801])
def test_recheck_rule_rejects_invalid_delay(delay):
    with pytest.raises(ValidationError):
        CronRecheckSpec(delay_seconds=delay, instruction="check A")
