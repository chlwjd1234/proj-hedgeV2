"""Intent and traversal tests; no historical or account-mutation evidence."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest

from mdlmm.codec import b58decode, discriminator
from mdlmm.errors import InsufficientLiquidity, Unavailable, Unsupported, ValidationError
from mdlmm.mechanics import Q64
from mdlmm.model import Bin, Event, FeeParameters, Order, PoolState, VariableState, SOL_MINT
from mdlmm.swap_math import U64_MAX
from mdlmm.swap_quote import BitmapExtension, SwapIntent, SwapMode, quote_swap


def _pool(indices=(0, -1), *, active=0, mode=0, function_type=2):
    words = [0] * 16
    for index in indices:
        if -512 <= index <= 511:
            bit = index + 512
            words[bit // 64] |= 1 << (bit % 64)
    bins = {i: Bin(i, 0, 0, 0, 0, open_order_amount=0,
                    processed_order_remaining_amount=0, limit_order_ask_side=0)
            for index in indices for i in range(index * 70, index * 70 + 70)}
    parameters = FeeParameters(1000, 30, 600, 5000, 40000, 350000, 2000,
                               function_type=function_type, collect_fee_mode=mode,
                               min_bin_id=-443636, max_bin_id=443636)
    return PoolState(SOL_MINT, active, 10, parameters,
                     VariableState(0, 0, active, 1000), bins,
                     known_bin_arrays=list(indices), bin_array_bitmap=tuple(words),
                     slot=12, data_kind='SYNTHETIC')


def _fund(pool, bin_id=0, *, amount=1_000_000, opened=0, processed=0, side=0, price=Q64):
    pool.bins[bin_id] = replace(pool.bins[bin_id], amount_x=amount, amount_y=amount,
                                liquidity_supply=amount << 64, price=price,
                                open_order_amount=opened,
                                processed_order_remaining_amount=processed,
                                limit_order_ask_side=side)
    return pool


@pytest.mark.parametrize('direction', [True, False])
@pytest.mark.parametrize('fee_mode', [0, 1])
@pytest.mark.parametrize('mode', [SwapMode.EXACT_IN, SwapMode.EXACT_OUT])
def test_pool_quotes_mixed_layers_without_mutating_any_source(direction, fee_mode, mode):
    source = _fund(_pool(mode=fee_mode), amount=100, processed=200, opened=400,
                   side=0 if direction else 1)
    intent = (SwapIntent.exact_in(450, direction, min_amount_out=400)
              if mode == SwapMode.EXACT_IN else
              SwapIntent.exact_out(450, direction, max_in_amount=500))
    before = deepcopy(source.to_dict())
    result = quote_swap(source, intent, 1001)
    assert source.to_dict() == before
    assert not hasattr(result, 'state')
    assert not hasattr(result.reference, 'state')
    assert result.start_bin == result.end_bin == 0
    assert result.reference.source_economic_hash == source.economic_hash()
    assert result.reference.source_last_update_timestamp == 1000
    assert result.reference.bitmap_extension_evidence == 'UNKNOWN'
    assert [x.source for x in result.fills[0].quote.layers] == ['MM', 'PROCESSED_LO', 'OPEN_LO']
    assert all(x.net_amount_in for x in result.fills[0].quote.layers)
    assert result.trading_fee == result.fee_split.total_trading_fee
    assert result.sdk_fee == result.fee_split.mm_lp + result.fee_split.lo_maker
    assert result.fee_token == ('x' if direction and fee_mode == 0 else 'y')
    if mode == SwapMode.EXACT_IN:
        assert result.amount_in == 450 and result.input_remaining == 0
        assert result.output_remaining is result.input_cap_remaining is None
    else:
        assert result.amount_out == 450 and result.output_remaining == 0
        assert result.input_remaining is None
        assert result.input_cap_remaining == 500 - result.amount_in
    with pytest.raises(FrozenInstanceError):
        result.amount_in = 3


def test_exact_out_keeps_sdk_output_rounding_separate_from_fee_token():
    source = _fund(_pool(), price=10 * Q64)
    source.parameters.base_factor = source.parameters.variable_fee_control = 0
    result = quote_swap(source, SwapIntent.exact_out(1, True, max_in_amount=7), 1001)
    assert (result.amount_in, result.amount_out) == (1, 1)
    assert result.trading_fee == result.sdk_fee == result.fee_split.protocol_fee == 0
    assert result.sdk_protocol_fee == result.rounding_output_delta == 9
    assert result.sdk_protocol_rounding_delta == 9
    assert result.fee_token == 'x' and result.output_token == 'y'
    assert result.input_cap_remaining == 6


@pytest.mark.parametrize('intent', [SwapIntent.exact_in(100, True, min_amount_out=100),
                                  SwapIntent.exact_out(100, True, max_in_amount=100)])
def test_actual_instruction_bounds_are_enforced(intent):
    with pytest.raises(ValidationError, match='actual.*constraint'):
        quote_swap(_fund(_pool()), intent, 1001)


@pytest.mark.parametrize('name, args, mode', [
    ('swap', {'amount_in': 123, 'min_amount_out': 45}, SwapMode.EXACT_IN),
    ('swap2', {'amount_in': 124, 'min_amount_out': 46}, SwapMode.EXACT_IN),
    ('swap_exact_out', {'out_amount': 47, 'max_in_amount': 125}, SwapMode.EXACT_OUT),
    ('swap_exact_out2', {'out_amount': 48, 'max_in_amount': 126}, SwapMode.EXACT_OUT),
])
def test_actual_intent_does_not_use_observed_input_as_instruction(name, args, mode):
    encoded_args = {**args, **({'remaining_accounts_info': {'slices': []}} if name.endswith('2') else {})}
    event = Event(Order(12, 0, 0), 1001000, 'synthetic', SOL_MINT, 'SWAP',
                  observed={'amount_in': 99, 'amount_out': 42},
                  payload={'normalization_mode': 'CANONICAL_PAIRED',
                           'canonical_pair_verified': True, 'instruction_name': name,
                           'exact_out': mode == SwapMode.EXACT_OUT, 'instruction_args': encoded_args,
                           'swap_for_y': True, 'host_fee_present': False})
    intent = SwapIntent.from_event(event)
    assert intent.mode == mode
    assert SwapIntent.from_request(intent.to_request()) == intent
    for key, value in args.items():
        assert getattr(intent, key) == value
    if mode == SwapMode.EXACT_OUT:
        assert intent.amount_in is None
    if name.endswith('2'):
        event.payload['instruction_args']['remaining_accounts_info'] = {'slices': [{'accounts_type': 0, 'length': 1}]}
        with pytest.raises(Unsupported, match='remaining-account slices'):
            SwapIntent.from_event(event)
    event.payload['instruction_name'] = 'swap_with_price_impact2'
    with pytest.raises(Unsupported, match='price-impact'):
        SwapIntent.from_event(event)
    event.payload['canonical_pair_verified'] = False
    with pytest.raises(Unavailable, match='canonical'):
        SwapIntent.from_event(event)


def test_host_intent_is_preserved_but_sdk_quote_cannot_ignore_it():
    host = SwapIntent.exact_in(100, True, host_fee_present=True)
    assert host.host_fee_present is True
    with pytest.raises(Unsupported, match='host/referral'):
        quote_swap(_fund(_pool()), host, 1001)
    with pytest.raises(Unavailable, match='unknown'):
        SwapIntent.exact_in(100, True, host_fee_present=None)


@pytest.mark.parametrize('args', [
    {'amount_in': True}, {'amount_in': 0}, {'amount_in': U64_MAX + 1},
    {'amount_in': 3, 'swap_for_y': 1}, {'amount_in': 3, 'min_amount_out': -1},
])
def test_intent_rejects_non_program_amounts(args):
    with pytest.raises(ValidationError):
        SwapIntent.exact_in(**{'swap_for_y': True, **args})


@pytest.mark.parametrize('elapsed,index_ref,vol_ref', [(29, 5, 80000), (30, 0, 50000),
                                                     (599, 0, 50000), (600, 0, 0)])
def test_reference_thresholds_are_quote_metadata_only(elapsed, index_ref, vol_ref):
    source = _fund(_pool())
    source.variable = VariableState(100000, 80000, 5, 1000)
    result = quote_swap(source, SwapIntent.exact_in(100, True), 1000 + elapsed)
    assert result.reference.index_reference == index_ref
    assert result.reference.volatility_reference == vol_ref
    assert result.fills[0].volatility_accumulator == vol_ref + abs(index_ref) * 10000
    assert source.variable == VariableState(100000, 80000, 5, 1000)


def test_bitmap_gap_crosses_to_next_observed_array_and_updates_dynamic_fee():
    source = _fund(_pool(indices=(0, -2)), amount=100)
    _fund(source, -71, amount=1_000_000)
    result = quote_swap(source, SwapIntent.exact_in(1000, True), 1001)
    assert [f.quote.bin_id for f in result.fills] == [0, -71]
    assert result.reference.visited_bin_arrays == (0, -2)
    assert result.fills[-1].volatility_accumulator == 350000
    assert result.fills[-1].variable_rate > result.fills[0].variable_rate
    assert result.end_bin == -71


def test_bitmap_selected_account_cannot_be_skipped_for_a_later_supplied_account():
    source = _fund(_pool(indices=(0, -1, -2)), amount=100)
    source.known_bin_arrays.remove(-1)
    _fund(source, -71)
    with pytest.raises(Unavailable, match='-1 is not captured'):
        quote_swap(source, SwapIntent.exact_in(1000, True), 1001)


def test_internal_bitmap_and_full_selected_account_are_required():
    source = _fund(_pool())
    source.bin_array_bitmap = None
    source.complete_bin_map = True
    with pytest.raises(Unavailable, match='internal.*bitmap'):
        quote_swap(source, SwapIntent.exact_in(100, True), 1001)
    source = _fund(_pool())
    del source.bins[69]  # Even an unvisited bin is part of the captured account.
    with pytest.raises(ValidationError, match='incomplete'):
        quote_swap(source, SwapIntent.exact_in(100, True), 1001)


def test_empty_but_present_array_does_not_prove_absent_extension():
    source = _pool(indices=(0,))
    with pytest.raises(Unavailable, match='unknown.*extension'):
        quote_swap(source, SwapIntent.exact_in(100, True), 1001)
    with pytest.raises(InsufficientLiquidity):
        quote_swap(source, SwapIntent.exact_in(100, True), 1001, extension_known=True)


@pytest.mark.parametrize('unknown', ['function', 'order', 'price'])
def test_unknown_quote_evidence_is_not_defaulted_to_observed_zero(unknown):
    source = _fund(_pool())
    if unknown == 'function':
        source.parameters.function_type = 0
    elif unknown == 'order':
        source.bins[0].open_order_amount = None
    else:
        source.bins[0].price = 0
    with pytest.raises(Unavailable):
        quote_swap(source, SwapIntent.exact_in(100, True), 1001)


def test_proven_lm_mode_does_not_require_irrelevant_lo_fields():
    source = _fund(_pool(function_type=1))
    source.bins[0].open_order_amount = source.bins[0].limit_order_ask_side = None
    assert quote_swap(source, SwapIntent.exact_in(100, True), 1001).amount_in == 100


def _extension(indices):
    sides = [[[0] * 8 for _ in range(12)] for _ in range(2)]
    for index in indices:
        offset = index if index >= 512 else -index - 1
        sides[int(index < 0)][offset // 512 - 1][(offset % 512) // 64] |= 1 << (offset % 64)
    raw = discriminator('account', 'BinArrayBitmapExtension') + b58decode(SOL_MINT)
    raw += b''.join(word.to_bytes(8, 'little') for side in sides for row in side for word in row)
    return BitmapExtension.from_bytes(raw)


@pytest.mark.parametrize('index,direction', [(-513, True), (512, False), (-6337, False), (6337, True)])
def test_extension_layout_and_directional_traversal(index, direction):
    # At extreme negative bin IDs use an explicitly populated price; quote
    # traversal need not derive an unsupported zero/tiny price.
    source = _pool(indices=(index,), active=index * 70)
    _fund(source, index * 70)
    extension = _extension([index])
    assert extension.initialized_indices() == (index,)
    result = quote_swap(source, SwapIntent.exact_in(100, direction), 1001,
                        bitmap_extension=extension)
    assert result.reference.bitmap_extension_evidence == 'PRESENT'
    assert result.reference.visited_bin_arrays == (index,)


def test_absent_extension_does_not_allow_outside_cursor_to_jump_inward():
    source = _fund(_pool(indices=(511, 512), active=512 * 70), 511 * 70 + 69)
    with pytest.raises(InsufficientLiquidity, match='no bitmap extension'):
        quote_swap(source, SwapIntent.exact_in(100, True), 1001, extension_known=True)
    result = quote_swap(source, SwapIntent.exact_in(100, True), 1001,
                        bitmap_extension=_extension([]))
    assert result.end_bin == 511 * 70 + 69


def test_both_ends_of_extension_decode_without_sign_aliasing():
    indices = [-6656, -513, 512, 6655]
    assert _extension(indices).initialized_indices() == tuple(indices)
    with pytest.raises(Unsupported, match='layout'):
        BitmapExtension.from_bytes(b'\0' * 1576)


def test_dust_quote_keeps_nominal_fee_unallocated_as_official_helper_does():
    result = quote_swap(_fund(_pool()), SwapIntent.exact_in(1, True), 1001)
    assert result.amount_in == 1 and result.amount_out == 0
    assert result.trading_fee == result.fee_split.unallocated_trading_fee == 1
    assert result.sdk_fee == result.sdk_protocol_fee == 0


@pytest.mark.parametrize('field,lower,upper', [
    ('volatility_accumulator', 0, 1 << 32),
    ('volatility_reference', 0, 1 << 32),
    ('index_reference', -(1 << 31), 1 << 31),
    ('last_update_timestamp', -(1 << 63), 1 << 63),
])
@pytest.mark.parametrize('invalid', ['underflow', 'overflow', 'bool', 'float', 'string'])
def test_variable_state_rejected_before_reference_or_fee_math(monkeypatch, field, lower, upper, invalid):
    source = _fund(_pool())
    value = {'underflow': lower - 1, 'overflow': upper, 'bool': True,
             'float': 1.0, 'string': '1'}[invalid]
    setattr(source.variable, field, value)
    before = deepcopy(source.to_dict())
    def no_math(*args):
        pytest.fail('invalid program state reached reference arithmetic')
    monkeypatch.setattr('mdlmm.swap_quote.update_references', no_math)
    with pytest.raises(ValidationError, match='variable[.]' + field):
        quote_swap(source, SwapIntent.exact_in(1_000_000, True), 1001)
    assert source.to_dict() == before


def test_signed_and_unsigned_variable_width_endpoints_remain_valid():
    fields = [('volatility_accumulator', 0, (1 << 32) - 1),
              ('volatility_reference', 0, (1 << 32) - 1),
              ('index_reference', -(1 << 31), (1 << 31) - 1),
              ('last_update_timestamp', -(1 << 63), (1 << 63) - 1)]
    for field, lower, upper in fields:
        for value in (lower, upper):
            source = _fund(_pool())
            setattr(source.variable, field, value)
            timestamp = value if field == 'last_update_timestamp' else 1001
            assert quote_swap(source, SwapIntent.exact_in(1000, True), timestamp).amount_in == 1000


@pytest.mark.parametrize('field,invalid', [
    ('base_factor', 1 << 16), ('filter_period', 1 << 16),
    ('decay_period', 1 << 16), ('reduction_factor', 1 << 16),
    ('protocol_share', 1 << 16), ('variable_fee_control', 1 << 32),
    ('max_volatility_accumulator', 1 << 32), ('base_fee_power_factor', 1 << 8),
    ('function_type', 1 << 8), ('collect_fee_mode', 1 << 8),
    ('min_bin_id', -(1 << 31) - 1), ('max_bin_id', 1 << 31),
    ('base_factor', True), ('variable_fee_control', 1.0),
    ('base_fee_power_factor', 10 ** 100),
])
def test_fee_parameter_widths_fail_before_exponentiation_or_reference_math(monkeypatch, field, invalid):
    source = _fund(_pool())
    setattr(source.parameters, field, invalid)
    def no_math(*args):
        pytest.fail('invalid fee parameter reached reference arithmetic')
    monkeypatch.setattr('mdlmm.swap_quote.update_references', no_math)
    with pytest.raises(ValidationError, match='parameters[.]' + field):
        quote_swap(source, SwapIntent.exact_in(1000, True), 1001)


@pytest.mark.parametrize('field,invalid', [('active_id', True), ('active_id', 1 << 31),
                                         ('bin_step', True), ('bin_step', 1 << 16)])
def test_pool_arithmetic_scalars_are_program_integers(field, invalid):
    source = _fund(_pool())
    setattr(source, field, invalid)
    with pytest.raises(ValidationError, match=field):
        quote_swap(source, SwapIntent.exact_in(1000, True), 1001)
