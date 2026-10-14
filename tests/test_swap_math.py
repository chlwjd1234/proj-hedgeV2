"""Amount-only parity with independently executed official TypeScript helpers.

The frozen four-scalar vectors below were produced with real exported
swapExactInQuoteAtBin / swapExactOutQuoteAtBin from @meteora-ag/dlmm 1.9.14,
Node 24.19.0, on 2026-10-11. Source pin:
576919e3e4368e542c402f000b4264724f7f23ec. The generator supplied binStep=1,
baseFactor=rate/10, baseFeePowerFactor=0 and variableFeeControl=0. These are
SDK quote parity vectors, not on-chain execution or post-state evidence.
"""
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from mdlmm.errors import Unavailable, ValidationError
from mdlmm.model import Bin
from mdlmm.swap_math import (
    MAX_FEE_RATE, Q64, U64_MAX, max_amount_out,
    quote_exact_in_at_bin, quote_exact_out_at_bin, split_fee,
)


# name, exact kind, direction, LO enabled, input fee, request, price,
# MM output capacity, processed capacity, open capacity, LO side, rate,
# protocol share, (official amountIn, amountOut, fee, protocolFee).
_SDK_VECTORS = [
    ('in_mixed_x_input', 'in', True, True, True, 1500, Q64*2, 500, 600, 700, 0, 10_000_000, 1000, (910, 1800, 6, 4)),
    ('in_mixed_y_input', 'in', False, True, True, 1500, Q64//2, 500, 600, 700, 1, 10_000_000, 1000, (910, 1800, 6, 4)),
    ('in_mixed_x_output', 'in', True, True, False, 1500, Q64*2, 500, 600, 700, 0, 10_000_000, 1000, (900, 1782, 11, 7)),
    ('in_mixed_y_output', 'in', False, True, False, 1500, Q64//2, 500, 600, 700, 1, 10_000_000, 1000, (900, 1782, 11, 7)),
    ('in_wrong_side', 'in', True, True, True, 1500, Q64*2, 500, 600, 700, 1, 10_000_000, 1000, (253, 500, 3, 0)),
    ('in_no_lo', 'in', True, False, True, 1500, Q64*2, 500, 600, 700, 0, 10_000_000, 1000, (253, 500, 3, 0)),
    ('in_zero_net_dust', 'in', True, False, True, 1, Q64, 1000, 0, 0, 0, 10_000_000, 1000, (1, 0, 0, 0)),
    ('in_output_dust', 'in', True, True, False, 1, Q64, 0, 1000, 0, 0, 10_000_000, 1000, (1, 0, 0, 1)),
    ('in_empty', 'in', True, True, True, 25, Q64, 0, 0, 0, 0, 10_000_000, 1000, (0, 0, 0, 0)),
    ('in_zero', 'in', True, True, True, 0, Q64, 5, 6, 7, 0, 10_000_000, 1000, (0, 0, 0, 0)),
    ('in_mm_ceil_favored', 'in', True, True, True, 100, Q64, 1, 500, 500, 0, 10_000_000, 1000, (100, 99, 1, 0)),
    ('out_mixed_x_input', 'out', True, True, True, 1500, Q64*2, 500, 600, 700, 0, 10_000_000, 1000, (758, 1500, 5, 3)),
    ('out_mixed_y_input', 'out', False, True, True, 1500, Q64//2, 500, 600, 700, 1, 10_000_000, 1000, (758, 1500, 5, 3)),
    ('out_mixed_x_output', 'out', True, True, False, 1500, Q64*2, 500, 600, 700, 0, 10_000_000, 1000, (758, 1500, 11, 5)),
    ('out_mixed_y_output', 'out', False, True, False, 1500, Q64//2, 500, 600, 700, 1, 10_000_000, 1000, (758, 1500, 11, 5)),
    ('out_rounding_one', 'out', True, False, True, 1, Q64*2, 100, 0, 0, 0, 0, 1000, (1, 1, 0, 0)),
    ('out_rounding_nine', 'out', True, False, True, 1, Q64*10, 100, 0, 0, 0, 0, 1000, (1, 1, 0, 9)),
    ('out_rounding_fee_input', 'out', True, False, True, 1, Q64*10, 100, 0, 0, 0, 10_000_000, 1000, (2, 1, 1, 9)),
    ('out_rounding_fee_output', 'out', True, False, False, 1, Q64*10, 100, 0, 0, 0, 10_000_000, 1000, (1, 1, 1, 8)),
    ('out_layerwise_ceil', 'out', True, True, True, 7, Q64*3, 1, 1, 100, 0, 0, 1000, (4, 7, 0, 0)),
    ('out_deplete_equal', 'out', True, True, True, 18, Q64, 5, 6, 7, 0, 10_000_000, 1000, (19, 18, 1, 0)),
    ('out_deplete_excess', 'out', True, True, True, 30, Q64, 5, 6, 7, 0, 10_000_000, 1000, (19, 18, 1, 0)),
    ('out_empty', 'out', True, True, True, 1, Q64, 0, 0, 0, 0, 10_000_000, 1000, (0, 0, 0, 0)),
    ('out_zero', 'out', True, True, True, 0, Q64, 5, 6, 7, 0, 10_000_000, 1000, (0, 0, 0, 0)),
    ('in_max_fee', 'in', True, True, False, 12345, Q64*7//3, 1111, 2222, 3333, 0, 100_000_000, 10000, (2859, 5999, 277, 390)),
    ('in_u64_exact', 'in', True, False, True, U64_MAX, Q64, U64_MAX, 0, 0, 0, 0, 1000, (U64_MAX, U64_MAX, 0, 0)),
]


def _bin(mm=500, processed=600, opened=700, side=0, price=Q64*2):
    return Bin(0, mm, mm, price, 10**18, open_order_amount=opened,
               processed_order_remaining_amount=processed, limit_order_ask_side=side)


def _kwargs(**changes):
    return dict(swap_for_y=True, support_limit_order=True, fee_on_input=True,
                trade_fee_numerator=10_000_000, protocol_share=1000) | changes


@pytest.mark.parametrize('vector', _SDK_VECTORS, ids=[v[0] for v in _SDK_VECTORS])
def test_official_helper_frozen_vectors(vector):
    (_, kind, direction, lo, input_fee, request, price, mm, processed,
     opened, side, rate, share, expected) = vector
    source = _bin(mm, processed, opened, side, price)
    before = deepcopy(asdict(source))
    fn = quote_exact_in_at_bin if kind == 'in' else quote_exact_out_at_bin
    quote = fn(source, request, **_kwargs(swap_for_y=direction,
        support_limit_order=lo, fee_on_input=input_fee,
        trade_fee_numerator=rate, protocol_share=share))
    assert (quote.amount_in, quote.amount_out, quote.sdk_fee, quote.sdk_protocol_fee) == expected
    assert asdict(source) == before
    assert quote.net_amount_in == sum(layer.net_amount_in for layer in quote.layers)
    assert quote.gross_amount_out == sum(layer.gross_amount_out for layer in quote.layers)
    assert quote.trading_fee == quote.fee_split.total_trading_fee == quote.rust_sdk_fee
    assert quote.sdk_fee == quote.fee_split.user_fee
    assert quote.sdk_protocol_fee == quote.fee_split.protocol_fee + quote.sdk_protocol_rounding_delta
    assert quote.requoted_amount_out == quote.amount_out + quote.rounding_output_delta
    if input_fee:
        assert quote.amount_in == quote.net_amount_in + quote.trading_fee
        assert quote.requoted_amount_out == quote.gross_amount_out
    else:
        assert quote.amount_in == quote.net_amount_in
        assert quote.requoted_amount_out == quote.gross_amount_out - quote.trading_fee


def test_source_split_keeps_mm_lp_and_lo_maker_separate():
    fees = split_fee(101, 2500, 2, 7)
    assert (fees.mm_lp, fees.mm_protocol, fees.lo_maker, fees.lo_protocol) == (22, 7, 36, 36)
    assert fees.user_fee == 58
    assert fees.protocol_fee == 43
    assert fees.user_fee != fees.mm_lp
    assert fees.mm_fee == 29  # ceil(101*2/7), before MM protocol share.
    assert fees.lo_fee == 72
    assert fees.total_trading_fee == 101


@pytest.mark.parametrize('fee,mm,total,expected', [
    (1, 1, 99, (1, 0, 0, 0)),
    (1, 0, 99, (0, 0, 0, 1)),
    (3, 0, 99, (0, 0, 1, 2)),
    (0, 50, 99, (0, 0, 0, 0)),
])
def test_split_rounding_favors_mm_then_lo_protocol(fee, mm, total, expected):
    result = split_fee(fee, 1000, mm, total)
    assert (result.mm_lp, result.mm_protocol, result.lo_maker, result.lo_protocol) == expected


@pytest.mark.parametrize('share,expected', [(0, (11, 0)), (10000, (0, 11))])
def test_mm_protocol_share_boundaries(share, expected):
    fees = split_fee(11, share, 99, 99)
    assert (fees.mm_lp, fees.mm_protocol) == expected
    assert fees.lo_fee == 0


def test_depletion_recomputes_consumed_gross_input_and_layer_order():
    quote = quote_exact_in_at_bin(_bin(), 1500, **_kwargs())
    assert [(layer.source, layer.net_amount_in, layer.gross_amount_out)
            for layer in quote.layers] == [
        ('MM', 250, 500), ('PROCESSED_LO', 300, 600), ('OPEN_LO', 350, 700),
    ]
    assert (quote.amount_in, quote.net_amount_in, quote.trading_fee) == (910, 900, 10)
    assert (quote.fee_split.mm_lp, quote.fee_split.mm_protocol,
            quote.fee_split.lo_maker, quote.fee_split.lo_protocol) == (3, 0, 3, 4)


@pytest.mark.parametrize('requested_amount,expected', [
    (100, [(100, 200), (0, 0), (0, 0)]),
    (400, [(250, 500), (150, 300), (0, 0)]),
    (700, [(250, 500), (300, 600), (150, 300)]),
])
def test_partial_fill_stops_at_current_layer(requested_amount, expected):
    quote = quote_exact_in_at_bin(_bin(), requested_amount, **_kwargs(trade_fee_numerator=0))
    assert [(layer.net_amount_in, layer.gross_amount_out) for layer in quote.layers] == expected


@pytest.mark.parametrize('direction,side', [(True, 1), (False, 0)])
def test_wrong_side_orders_are_not_available(direction, side):
    source = _bin(side=side, price=Q64)
    assert max_amount_out(source, swap_for_y=direction, support_limit_order=True) == 500
    quote = quote_exact_in_at_bin(source, 1500, **_kwargs(swap_for_y=direction))
    assert all(layer.available_amount_out == 0 for layer in quote.layers[1:])
    assert quote.fee_split.lo_fee == 0


def test_exact_out_ceil_is_applied_separately_per_source_layer():
    quote = quote_exact_out_at_bin(_bin(1, 1, 100, price=Q64*3), 7,
                                   **_kwargs(trade_fee_numerator=0))
    assert quote.amount_in == 4  # ceil(1/3) + ceil(1/3) + ceil(5/3), not ceil(7/3).
    assert [(layer.net_amount_in, layer.gross_amount_out) for layer in quote.layers] == [
        (1, 1), (1, 1), (2, 6),
    ]
    assert quote.requested_amount_out == quote.amount_out == 7
    assert quote.requoted_amount_out == quote.gross_amount_out == 8
    assert quote.rounding_output_delta == 1
    assert quote.sdk_protocol_rounding_delta == quote.sdk_protocol_fee == 0


@pytest.mark.parametrize('price,delta,protocol', [(Q64*2, 1, 0), (Q64*3, 2, 2), (Q64*10, 9, 9)])
def test_exact_out_output_delta_is_not_a_trading_fee(price, delta, protocol):
    quote = quote_exact_out_at_bin(_bin(100, price=price), 1,
        **_kwargs(support_limit_order=False, trade_fee_numerator=0))
    assert quote.amount_in == quote.amount_out == 1
    assert quote.requoted_amount_out == 1 + delta
    assert quote.rounding_output_delta == delta
    assert quote.sdk_protocol_fee == protocol
    assert quote.trading_fee == quote.fee_split.protocol_fee == quote.sdk_fee == 0
    assert quote.rust_sdk_fee == 0


@pytest.mark.parametrize('requested_amount', [18, 30])
def test_exact_out_greater_or_equal_capacity_uses_drain_fallback(requested_amount):
    quote = quote_exact_out_at_bin(_bin(5, 6, 7, price=Q64), requested_amount, **_kwargs())
    assert quote.used_full_bin_fallback
    assert quote.requested_amount_out == requested_amount
    assert quote.amount_out == quote.requoted_amount_out == 18
    assert quote.rounding_output_delta == 0
    assert quote.amount_in == 19


def test_input_fee_dust_is_recorded_without_inventing_a_recipient():
    quote = quote_exact_in_at_bin(_bin(price=Q64), 1, **_kwargs())
    assert quote.amount_in == quote.trading_fee == quote.rust_sdk_fee == 1
    assert quote.net_amount_in == quote.amount_out == quote.sdk_fee == quote.sdk_protocol_fee == 0
    assert quote.fee_split.unallocated_trading_fee == 1
    assert quote.fee_split.user_fee == quote.fee_split.protocol_fee == 0


@pytest.mark.parametrize('missing', ['open_order_amount', 'processed_order_remaining_amount', 'limit_order_ask_side'])
@pytest.mark.parametrize('fn', [quote_exact_in_at_bin, quote_exact_out_at_bin])
def test_lo_mode_requires_relevant_observed_fields_even_for_empty_or_wrong_side(missing, fn):
    source = replace(_bin(0, 0, 0, side=1), **{missing: None})
    with pytest.raises(Unavailable, match=missing):
        fn(source, 0, **_kwargs())
    with pytest.raises(Unavailable, match=missing):
        max_amount_out(source, swap_for_y=True, support_limit_order=True)


def test_mm_mode_accepts_unknown_lo_and_quote_does_not_require_mutation_fields():
    legacy = Bin(0, 500, 500, Q64, 10**18)
    assert quote_exact_in_at_bin(legacy, 100, **_kwargs(support_limit_order=False)).amount_out == 99
    source = _bin()
    assert source.order_age is None and source.fulfilled_order_amount_x is None
    assert quote_exact_in_at_bin(source, 1000, **_kwargs()).amount_out == 1800


def test_results_are_frozen_detached_amounts_without_a_post_state():
    source = _bin()
    quote = quote_exact_in_at_bin(source, 1000, **_kwargs())
    snapshot = asdict(quote)
    source.amount_y = 0
    source.open_order_amount = 0
    assert asdict(quote) == snapshot
    assert not hasattr(quote, 'state')
    assert isinstance(quote.layers, tuple)
    with pytest.raises(FrozenInstanceError):
        quote.amount_out = 0
    with pytest.raises(FrozenInstanceError):
        quote.layers[0].gross_amount_out = 0
    with pytest.raises(FrozenInstanceError):
        quote.fee_split.mm_lp = 0


@pytest.mark.parametrize('fn', [quote_exact_in_at_bin, quote_exact_out_at_bin])
@pytest.mark.parametrize('amount', [-1, U64_MAX+1, 1.0, True, None])
def test_request_is_an_exact_u64(fn, amount):
    with pytest.raises(ValidationError, match='integer u64'):
        fn(_bin(), amount, **_kwargs())


@pytest.mark.parametrize('changes', [
    {'trade_fee_numerator': -1}, {'trade_fee_numerator': MAX_FEE_RATE+1},
    {'trade_fee_numerator': 1.0}, {'trade_fee_numerator': True},
    {'protocol_share': -1}, {'protocol_share': 10001}, {'protocol_share': True},
    {'swap_for_y': 1}, {'support_limit_order': 0}, {'fee_on_input': 1},
])
def test_malformed_quote_modes_and_fees_fail_closed(changes):
    with pytest.raises(ValidationError):
        quote_exact_in_at_bin(_bin(), 100, **_kwargs(**changes))


@pytest.mark.parametrize('changes', [
    {'amount_y': U64_MAX+1}, {'open_order_amount': -1},
    {'processed_order_remaining_amount': True}, {'price': 1 << 128},
    {'limit_order_ask_side': 2}, {'bin_id': 1 << 31},
])
def test_malformed_bin_fields_fail_closed(changes):
    with pytest.raises(ValidationError):
        quote_exact_in_at_bin(replace(_bin(), **changes), 100, **_kwargs())


def test_zero_price_can_be_used_for_capacity_only():
    source = _bin(price=0)
    assert max_amount_out(source, swap_for_y=True, support_limit_order=True) == 1800
    with pytest.raises(ValidationError, match='positive Q64 price'):
        quote_exact_in_at_bin(source, 1, **_kwargs())


def test_aggregate_capacity_overflow_rejects_ts_rust_divergent_domain():
    source = _bin(U64_MAX, 1, 0, price=Q64)
    with pytest.raises(ValidationError, match='aggregate output capacity'):
        quote_exact_in_at_bin(source, 1, **_kwargs())
    with pytest.raises(ValidationError, match='aggregate output capacity'):
        max_amount_out(source, swap_for_y=True, support_limit_order=True)
    assert max_amount_out(source, swap_for_y=True, support_limit_order=False) == U64_MAX


def test_required_input_overflow_does_not_use_unbounded_python_amounts():
    with pytest.raises(ValidationError, match='required net input'):
        quote_exact_in_at_bin(_bin(U64_MAX, 0, 0, price=1), 1, **_kwargs())
    with pytest.raises(ValidationError, match='fee-inclusive amount'):
        quote_exact_out_at_bin(_bin(U64_MAX, 0, 0, price=Q64), U64_MAX,
                               **_kwargs(fee_on_input=False))


@pytest.mark.parametrize('values', [
    (1, 1000, 2, 1), (1, 1000, 1, 0), (True, 1000, 1, 1),
    (1, 1000, 0, U64_MAX+1), (1, 10001, 1, 1),
])
def test_invalid_split_inputs_fail_closed(values):
    with pytest.raises(ValidationError):
        split_fee(*values)
