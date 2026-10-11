"""Account representation tests; these do not assert LO swap/mutation parity.

The independent byte fixtures below use the pinned 144-byte Bin and 904-byte
LbPair layouts. They are synthetic layout vectors, not historical prestate.
Reference: MeteoraAg/dlmm-sdk 576919e3e4368e542c402f000b4264724f7f23ec,
ts-client/src/dlmm/idl/idl.json.
"""
from copy import deepcopy
import json
import struct

import pytest

from mdlmm.codec import (
    b58decode, b58encode, build_pool_state, decode_bin_array, decode_lb_pair,
    discriminator,
)
from mdlmm.errors import Unsupported, ValidationError
from mdlmm.mechanics import quote_exact_in
from mdlmm.model import (
    DEFAULT_PUBKEY, ECONOMIC_STATE_HASH_SCHEMA, FunctionType, PoolState,
    SOL_MINT, USDC_MINT, economic_state_hash,
)
from mdlmm.replay import compact_trace, expand_traces


def _unsigned(raw, offset, value, size):
    raw[offset:offset + size] = value.to_bytes(size, 'little')


@pytest.fixture
def decoded_accounts():
    pool = b58encode(bytes([9]) * 32)
    pair = bytearray(904)
    pair[:8] = discriminator('account', 'LbPair')
    struct.pack_into('<HHHH', pair, 8, 1000, 30, 600, 5000)
    struct.pack_into('<IIiiH', pair, 16, 40000, 350000, -100, 100, 1000)
    struct.pack_into('<q', pair, 56, 1000)
    struct.pack_into('<iH', pair, 76, 0, 10)
    pair[88:120], pair[120:152] = b58decode(SOL_MINT), b58decode(USDC_MINT)
    struct.pack_into('<QQ', pair, 216, (1 << 63) + 11, (1 << 63) + 13)
    # Both reward mints are the zero Pubkey; the 144-byte RewardInfo entries
    # begin at 264 and 408. Their absence must not be inferred for legacy data.
    bitmap = [(1 << 63) + i for i in range(16)]
    struct.pack_into('<16Q', pair, 584, *bitmap)
    struct.pack_into('<q', pair, 712, -17)

    array = bytearray(10136)
    array[:8] = discriminator('account', 'BinArray')
    array[24:56] = b58decode(pool)
    values = {
        'amount_x': 1_000_000_000,
        'amount_y': 1_000_000_000,
        'price': 1 << 64,
        'liquidity_supply': 1 << 90,
        'fulfilled_order_amount_x': (1 << 63) + 3,
        'fulfilled_order_amount_y': (1 << 63) + 5,
        'limit_order_fee_ask_side': (1 << 64) - 7,
        'limit_order_fee_bid_side': (1 << 64) - 9,
        'fee_x_stored': (1 << 100) + 11,
        'fee_y_stored': (1 << 100) + 13,
        'open_order_amount': 23,
        'total_processing_order_amount': 29,
        'processed_order_remaining_amount': 17,
        'order_age': (1 << 32) - 1,
        'limit_order_ask_side': 1,
    }
    layout = (
        ('amount_x', 0, 8), ('amount_y', 8, 8), ('price', 16, 16),
        ('liquidity_supply', 32, 16), ('fulfilled_order_amount_x', 48, 8),
        ('fulfilled_order_amount_y', 56, 8), ('limit_order_fee_ask_side', 64, 8),
        ('limit_order_fee_bid_side', 72, 8), ('fee_x_stored', 80, 16),
        ('fee_y_stored', 96, 16), ('open_order_amount', 112, 8),
        ('total_processing_order_amount', 120, 8),
        ('processed_order_remaining_amount', 128, 8), ('order_age', 136, 4),
        ('limit_order_ask_side', 140, 1),
    )
    for field, offset, size in layout:
        _unsigned(array, 56 + offset, values[field], size)
    return pool, decode_lb_pair(bytes(pair)), decode_bin_array(bytes(array)), values


@pytest.fixture
def captured_state(decoded_accounts):
    pool, pair, array, _ = decoded_accounts
    return build_pool_state(pool, pair, [array], slot=42, data_kind='SYNTHETIC')


def test_all_decoded_bin_and_pair_fields_survive_model_and_json(decoded_accounts, captured_state):
    _, pair, array, expected = decoded_accounts
    b = captured_state.bins[0]
    for name, value in expected.items():
        assert array['bins'][0][name] == value
        assert getattr(b, name) == value
    assert b.limit_order_fields_complete
    assert all(x.limit_order_fields_complete for x in captured_state.bins.values())
    assert captured_state.reward_mints == (DEFAULT_PUBKEY, DEFAULT_PUBKEY)
    assert captured_state.protocol_fee_x == (1 << 63) + 11
    assert captured_state.protocol_fee_y == (1 << 63) + 13
    assert captured_state.bin_array_bitmap == tuple(pair['bin_array_bitmap'])
    assert captured_state.last_updated_at == -17
    encoded = json.dumps(captured_state.to_dict())
    restored = PoolState.from_dict(json.loads(encoded))
    assert restored.to_dict() == captured_state.to_dict()
    assert restored.economic_hash() == captured_state.economic_hash()


def test_decoded_mutable_lists_do_not_alias_state(decoded_accounts, captured_state):
    _, pair, array, _ = decoded_accounts
    original = deepcopy(captured_state.to_dict())
    pair['rewards'][0]['mint'] = SOL_MINT
    pair['bin_array_bitmap'][0] = 0
    array['bins'][0]['order_age'] = 0
    output = captured_state.to_dict()
    output['bins'][0]['fulfilled_order_amount_x'] = 0
    output['known_bin_arrays'].append(999)
    assert captured_state.to_dict() == original
    assert isinstance(output['reward_mints'], tuple)
    assert isinstance(output['bin_array_bitmap'], tuple)


@pytest.mark.parametrize('raw,rewards,expected', [
    (0, None, None),
    (0, (DEFAULT_PUBKEY, DEFAULT_PUBKEY), FunctionType.LIMIT_ORDER),
    (0, (SOL_MINT, DEFAULT_PUBKEY), FunctionType.LIQUIDITY_MINING),
    (0, (DEFAULT_PUBKEY, USDC_MINT), FunctionType.LIQUIDITY_MINING),
    (1, None, FunctionType.LIQUIDITY_MINING),
    (2, None, FunctionType.LIMIT_ORDER),
])
def test_effective_function_type_requires_reward_evidence(state, raw, rewards, expected):
    state.parameters.function_type = raw
    state.reward_mints = rewards
    assert state.effective_function_type == expected


def test_unknown_function_type_fails_closed(state):
    state.parameters.function_type = 3
    with pytest.raises(Unsupported, match='unknown pool function type'):
        _ = state.effective_function_type


def test_legacy_missing_fields_remain_unknown(captured_state):
    legacy = captured_state.to_dict()
    for name in ('reward_mints', 'protocol_fee_x', 'protocol_fee_y',
                 'bin_array_bitmap', 'last_updated_at'):
        legacy.pop(name)
    for b in legacy['bins'].values():
        for name in ('fulfilled_order_amount_x', 'fulfilled_order_amount_y',
                     'limit_order_fee_ask_side', 'limit_order_fee_bid_side', 'order_age'):
            b.pop(name)
    before = deepcopy(legacy)
    restored = PoolState.from_dict(legacy)
    assert legacy == before
    assert restored.reward_mints is None
    assert restored.effective_function_type is None
    assert restored.protocol_fee_x is None and restored.protocol_fee_y is None
    assert restored.bin_array_bitmap is None and restored.last_updated_at is None
    assert restored.bins[0].open_order_amount == 23  # Retain older known fields.
    assert restored.bins[0].fulfilled_order_amount_x is None
    assert not restored.bins[0].limit_order_fields_complete
    assert restored.economic_hash() == economic_state_hash(legacy)


def test_omitted_old_lo_fields_do_not_become_observed_zeros(state):
    data = state.to_dict()
    for b in data['bins'].values():
        for name in ('open_order_amount', 'processed_order_remaining_amount',
                     'total_processing_order_amount', 'limit_order_ask_side'):
            b.pop(name)
    restored = PoolState.from_dict(data)
    assert restored.bins[0].open_order_amount is None
    assert restored.bins[0].limit_order_ask_side is None
    assert not restored.bins[0].limit_order_fields_complete
    assert restored.economic_hash() == economic_state_hash(data)
    observed_zero = restored.to_dict()
    observed_zero['bins'][0]['open_order_amount'] = 0
    assert economic_state_hash(observed_zero) != restored.economic_hash()


@pytest.mark.parametrize('field,bits', [
    ('open_order_amount', 64), ('processed_order_remaining_amount', 64),
    ('total_processing_order_amount', 64), ('limit_order_ask_side', 8),
    ('fulfilled_order_amount_x', 64), ('fulfilled_order_amount_y', 64),
    ('limit_order_fee_ask_side', 64), ('limit_order_fee_bid_side', 64),
    ('order_age', 32), ('fee_x_stored', 128), ('fee_y_stored', 128),
])
def test_bin_program_integer_widths_and_types(state, field, bits):
    b = state.bins[0]
    setattr(b, field, (1 << bits) - 1)
    b.validate()
    for invalid in (-1, 1 << bits, True, 1.0, '1'):
        setattr(b, field, invalid)
        with pytest.raises(ValidationError, match=field):
            b.validate()


@pytest.mark.parametrize('field', ['protocol_fee_x', 'protocol_fee_y'])
def test_protocol_counters_are_optional_u64(state, field):
    setattr(state, field, (1 << 64) - 1)
    state.validate()
    for invalid in (-1, 1 << 64, True, 1.0):
        setattr(state, field, invalid)
        with pytest.raises(ValidationError, match=field):
            state.validate()


def test_pair_timestamp_is_optional_signed_i64(state):
    for valid in (None, -(1 << 63), (1 << 63) - 1):
        state.last_updated_at = valid
        state.validate()
    for invalid in (-(1 << 63) - 1, 1 << 63, True, 1.0):
        state.last_updated_at = invalid
        with pytest.raises(ValidationError, match='last_updated_at'):
            state.validate()


@pytest.mark.parametrize('value', [[], [0] * 15, [0] * 17, '0' * 16])
def test_bitmap_shape_fails_closed(state, value):
    data = state.to_dict()
    data['bin_array_bitmap'] = value
    with pytest.raises(ValidationError, match='bin_array_bitmap'):
        PoolState.from_dict(data)


@pytest.mark.parametrize('value', [-1, 1 << 64, True, 1.0])
def test_bitmap_word_width_fails_closed(state, value):
    data = state.to_dict()
    data['bin_array_bitmap'] = [0] * 15 + [value]
    with pytest.raises(ValidationError, match='bin_array_bitmap'):
        PoolState.from_dict(data)


@pytest.mark.parametrize('value', [[], [DEFAULT_PUBKEY], [DEFAULT_PUBKEY] * 3,
                                    [DEFAULT_PUBKEY, '1'], [DEFAULT_PUBKEY, '0' * 32]])
def test_invalid_reward_evidence_fails_closed(state, value):
    data = state.to_dict()
    data['reward_mints'] = value
    with pytest.raises(ValidationError):
        PoolState.from_dict(data)


@pytest.mark.parametrize('field', [
    'fulfilled_order_amount_x', 'fulfilled_order_amount_y',
    'limit_order_fee_ask_side', 'limit_order_fee_bid_side', 'order_age',
    'total_processing_order_amount', 'limit_order_ask_side',
])
def test_preserved_bin_fields_participate_in_versioned_hash(captured_state, field):
    assert ECONOMIC_STATE_HASH_SCHEMA == 'MDLMM_ECONOMIC_STATE_V2'
    before = captured_state.economic_hash()
    b = captured_state.bins[0]
    setattr(b, field, getattr(b, field) - 1)
    assert captured_state.economic_hash() != before


@pytest.mark.parametrize('field,value', [
    ('reward_mints', (SOL_MINT, DEFAULT_PUBKEY)),
    ('protocol_fee_x', 0), ('protocol_fee_y', 0),
    ('bin_array_bitmap', (0,) * 16), ('last_updated_at', 0),
])
def test_preserved_pair_fields_participate_in_hash(captured_state, field, value):
    before = captured_state.economic_hash()
    setattr(captured_state, field, value)
    assert captured_state.economic_hash() != before


def test_existing_mm_fee_growth_and_provenance_exclusions_remain(captured_state):
    before = captured_state.economic_hash()
    captured_state.bins[0].fee_x_stored += 1
    captured_state.bins[0].fee_y_stored += 1
    captured_state.slot += 1
    captured_state.binary_sha256 = 'new provenance label'
    assert captured_state.economic_hash() == before


def test_mm_quote_clone_preserves_source_and_does_not_alias_new_fields(captured_state):
    # Remove live LO liquidity only for this MM-only clone test. This does not
    # validate a fill of the synthetic fixture's original limit orders.
    for name in ('open_order_amount', 'processed_order_remaining_amount',
                 'total_processing_order_amount'):
        setattr(captured_state.bins[0], name, 0)
    before = deepcopy(captured_state.to_dict())
    quote = quote_exact_in(captured_state, 100_000, True, 1001)
    assert captured_state.to_dict() == before
    quote.state.bins[0].fulfilled_order_amount_x = 0
    quote.state.protocol_fee_x = 0
    quote.state.reward_mints = (SOL_MINT, DEFAULT_PUBKEY)
    quote.state.bin_array_bitmap = (0,) * 16
    assert captured_state.to_dict() == before


def test_compact_trace_roundtrip_retains_new_state_fields(captured_state):
    post = PoolState.from_dict(captured_state.to_dict())
    post.bins[0].fulfilled_order_amount_x += 1
    post.protocol_fee_y += 1
    post.bin_array_bitmap = (0,) + post.bin_array_bitmap[1:]
    row = {'pre': captured_state.to_dict(), 'post': post.to_dict()}
    compressed = json.loads(json.dumps(compact_trace(row)))
    restored = list(expand_traces([compressed], captured_state))[0]
    assert PoolState.from_dict(restored['post']).to_dict() == post.to_dict()
