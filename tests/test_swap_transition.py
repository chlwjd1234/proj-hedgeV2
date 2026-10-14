"""Failure atomicity and evidence gates for the separate transition API.

Numerical success assertions are independently supplied by program_parity's
raw SBF pre/post accounts. These tests exercise refusal paths and ownership.
"""
from copy import deepcopy
from dataclasses import replace

import pytest

from mdlmm.errors import Unavailable, Unsupported, ValidationError
from mdlmm.mechanics import Q64
from mdlmm.model import BIN_LIMIT_ORDER_FIELDS, Bin, FeeParameters, PoolState, VariableState
from mdlmm.swap_quote import SwapIntent
from mdlmm.swap_transition import PROGRAM_SHA256, simulate_swap


def _state():
    zero_lo = {key: 0 for key in BIN_LIMIT_ORDER_FIELDS}
    bins = {index: Bin(index, 0, 0, 0, 0, **zero_lo) for index in range(70)}
    bins[0] = replace(bins[0], amount_x=1_000_000, amount_y=1_000_000,
                      price=Q64, liquidity_supply=1_000_000 * Q64)
    bitmap = [0] * 16
    bitmap[8] = 1
    return PoolState(
        'So11111111111111111111111111111111111111112', 0, 10,
        FeeParameters(10_000, 30, 600, 5000, 0, 350_000, 2000,
                      function_type=2, collect_fee_mode=0),
        VariableState(0, 0, 0, 1000), bins,
        known_bin_arrays=[0], bin_array_bitmap=bitmap,
        protocol_fee_x=0, protocol_fee_y=0,
        binary_sha256=PROGRAM_SHA256, data_kind='SYNTHETIC', last_updated_at=0,
    )


@pytest.mark.parametrize('field', BIN_LIMIT_ORDER_FIELDS)
def test_missing_mutation_field_refuses_without_changing_the_input(field):
    source = _state()
    setattr(source.bins[0], field, None)
    before = deepcopy(source.to_dict())
    with pytest.raises(Unavailable):
        simulate_swap(source, SwapIntent.exact_in(10_000, True), 1001)
    assert source.to_dict() == before


@pytest.mark.parametrize('field', ['protocol_fee_x', 'protocol_fee_y', 'bin_array_bitmap'])
def test_missing_pool_evidence_cannot_be_invented(field):
    source = _state()
    setattr(source, field, None)
    with pytest.raises(Unavailable):
        simulate_swap(source, SwapIntent.exact_in(10_000, True), 1001)


def test_mismatched_program_or_missing_historical_binding_refuses():
    source = _state()
    source.binary_sha256 = '0' * 64
    with pytest.raises(Unsupported, match='binary'):
        simulate_swap(source, SwapIntent.exact_in(10_000, True), 1001)
    source.binary_sha256 = None
    source.data_kind = 'HISTORICAL'
    with pytest.raises(Unavailable, match='binary'):
        simulate_swap(source, SwapIntent.exact_in(10_000, True), 1001)


@pytest.mark.parametrize('target', ['protocol', 'growth', 'principal', 'scaled_liquidity'])
def test_mutation_overflow_or_zero_scaled_supply_is_atomic(target):
    source = _state()
    if target == 'protocol':
        source.protocol_fee_x = 2**64 - 1
    elif target == 'growth':
        source.bins[0].fee_x_stored = 2**128 - 1
    elif target == 'principal':
        source.bins[0].amount_x = 2**64 - 1
    else:
        source.bins[0].liquidity_supply = Q64 - 1
    before = deepcopy(source.to_dict())
    with pytest.raises(ValidationError):
        simulate_swap(source, SwapIntent.exact_in(10_000, True), 1001)
    assert source.to_dict() == before


def test_quote_and_post_state_are_separate_owned_results():
    source = _state()
    before = deepcopy(source.to_dict())
    result = simulate_swap(source, SwapIntent.exact_in(100_000, True, host_fee_present=True), 1001)
    assert source.to_dict() == before
    assert not hasattr(result.quote, 'state')
    assert result.intent.host_fee_present is True
    assert result.quote.intent.host_fee_present is False
    assert result.host_fee > 0
    assert result.historical_prestate_validated is False
    assert result.historical_runtime_feature_parity == 'UNVERIFIED'
    result.post_state.bins[0].amount_x = 0
    result.post_state.variable.volatility_accumulator = 123
    assert source.to_dict() == before


def test_new_transition_does_not_silently_handle_liquidity_mining():
    source = _state()
    source.parameters.function_type = 1
    with pytest.raises(Unsupported, match='LIMIT_ORDER'):
        simulate_swap(source, SwapIntent.exact_in(10_000, True), 1001)
