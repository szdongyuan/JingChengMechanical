from dataclasses import replace
import math

import pytest

from app.service.torque.runtime import TorqueRuntime
from app.service.torque.types import RuleSnapshot, TorqueConfig, TorqueCycle


@pytest.fixture
def cycle():
    return TorqueCycle(7, 100, '000123', ' M100 ',
                       TorqueConfig(7, True, 'model', 2, 20, 3, 40),
                       RuleSnapshot(9, {('model', 'M100'): 2.5}), 1)


def run(cycle, value=0.0, read_error=None, write_error=None):
    reads, writes, events = [], [], []
    def read(*address):
        reads.append(address)
        if read_error:
            raise read_error
        return value
    def write(*args):
        writes.append(args)
        if write_error:
            raise write_error
    result = TorqueRuntime().process(cycle, read_real=read, write_real=write, publish=events.append)
    return result, reads, writes, events


@pytest.mark.parametrize('value', [3.0, -3.0, 1e-40])
@pytest.mark.parametrize('rules', [{}, {('model', 'M100'): 2.5}])
def test_nonzero_plc_value_remains_authoritative_across_starts(cycle, value, rules):
    for trigger in (100, 200):
        result, reads, writes, events = run(replace(cycle, trigger_ns=trigger, rules=RuleSnapshot(9, rules)), value)
        assert (result.state, result.value, result.source, result.trigger_ns) == ('plc', value, 'plc', trigger)
        assert reads == [(2, 20)]
        assert writes == []
        assert [e.state for e in events] == ['reading', 'plc']


@pytest.mark.parametrize('zero', [0.0, -0.0])
@pytest.mark.parametrize('table_value', [2.5, 0.0, -2.5])
def test_zero_writes_table_value_once(cycle, zero, table_value):
    cycle = replace(cycle, rules=RuleSnapshot(9, {('model', 'M100'): table_value}))
    result, reads, writes, events = run(cycle, zero)
    assert (result.state, result.value, result.read_value) == ('table_sent', table_value, zero)
    assert reads == [(2, 20)]
    assert writes == [(3, 40, table_value)]
    assert [e.state for e in events] == ['reading', 'table_pending', 'table_sent']


@pytest.mark.parametrize('value', [math.nan, math.inf, -math.inf])
def test_nonfinite_never_falls_back(cycle, value):
    result, _, writes, _ = run(cycle, value)
    assert result.state == 'invalid_value'
    assert writes == []


def test_read_error_does_not_become_zero(cycle):
    result, _, writes, _ = run(cycle, read_error=OSError('offline'))
    assert result.state == 'read_error'
    assert result.read_value is None
    assert result.value is None
    assert 'offline' in result.detail
    assert writes == []


@pytest.mark.parametrize('key', ['', ' ', 'm100', 'M100-extra'])
def test_empty_or_inexact_key_is_unmatched(cycle, key):
    result, _, writes, _ = run(replace(cycle, model=key))
    assert result.state == 'unmatched'
    assert writes == []


def test_sn_matching_keeps_leading_zero_and_does_not_fall_back_to_model(cycle):
    cycle = replace(cycle, config=replace(cycle.config, match_type='sn'))
    assert run(cycle)[0].state == 'unmatched'
    cycle = replace(cycle, rules=RuleSnapshot(10, {('sn', '000123'): 8.0}))
    result, _, writes, _ = run(cycle)
    assert result.match_key == '000123'
    assert writes == [(3, 40, 8.0)]


def test_write_error_retains_table_value(cycle):
    result, _, writes, _ = run(cycle, write_error=OSError('write failed'))
    assert (result.state, result.value, result.source) == ('write_error', 2.5, 'table')
    assert (result.target_db, result.target_byte_offset) == (3, 40)
    assert len(writes) == 1


def test_invalid_inherited_addresses_skip_io(cycle):
    cycle = replace(cycle, config=replace(cycle.config, read_db_number=None, write_db_number=1, write_byte_offset=22))
    result, reads, writes, events = run(cycle)
    assert result.state == 'config_error'
    assert result.read_value is None
    assert reads == writes == []
    assert events == [result]


def test_disabled_does_nothing(cycle):
    result, reads, writes, events = run(replace(cycle, config=TorqueConfig(7)))
    assert result is None
    assert reads == writes == events == []
