"""Input/evidence guards for the staged foundation, not LO execution tests."""
from __future__ import annotations

import base64
import struct
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

import mdlmm.dataset as dataset_module
import mdlmm.prepare as prepare_module
from mdlmm.dataset import Dataset, ref_file, verify_preserved_account_fields
from mdlmm.errors import Unavailable, Unsupported, ValidationError
from mdlmm.mechanics import quote_exact_in
from mdlmm.model import (CLOCK_ID, DEFAULT_PUBKEY, ECONOMIC_STATE_HASH_SCHEMA,
                         Event, Order, SOL_MINT)
from mdlmm.replay import Replay, compare_states, compact_trace, expand_traces
from mdlmm.sdk import oracle
from mdlmm.util import write_json, write_jsonl


def canonical_event(state):
    q = quote_exact_in(state, 100_000, True, 1001, slot=11)
    return Event(
        Order(11, 0, 0, -1), 1_001_000, 'synthetic-swap', state.pool, 'SWAP',
        program_version=state.program_version, binary_sha256=state.binary_sha256,
        idl_hash=state.idl_hash, clock_unix_timestamp=1001,
        clock_source='SYNTHETIC_FIXTURE', data_kind=state.data_kind,
        payload={'amount_in': 100_000, 'swap_for_y': True,
                 'normalization_mode': 'CANONICAL_PAIRED',
                 'canonical_pair_verified': True, 'instruction_name': 'swap2',
                 'exact_out': False, 'host_fee_present': False},
        observed={'amount_in': q.amount_in, 'amount_out': q.amount_out,
                  'start_bin': q.start_bin, 'end_bin': q.end_bin,
                  'amount_left': q.amount_left, 'protocol_fee': q.protocol_fee,
                  'mm_fee': q.fee - q.protocol_fee, 'limit_order_fee': 0,
                  'host_fee': 0},
    )


def test_canonical_input_keeps_mm_execution_and_scope(state, registry):
    engine = Replay(state, registry)
    row = engine.step(canonical_event(state))
    assert row['quote']['amount_in'] == 100_000
    report = engine.report()
    assert report['mechanics_scope'] == 'MM_ONLY_EXACT_IN'
    assert 'LIMIT_ORDER_FILL_AND_STATE_MUTATION' in report['unsupported_semantics']
    assert 'HOST_REFERRAL_FEE_SPLIT' in report['unsupported_semantics']
    assert 'EXACT_OUT' in report['unsupported_semantics']
    assert report['full_program_byte_parity'] == 'UNAVAILABLE'
    assert 'protocol_fee_balance_mutation' in report['account_parity_excluded']


@pytest.mark.parametrize('presence', [True, None, 'missing'])
def test_host_account_does_not_become_supported_when_fee_rounds_to_zero(state, registry, presence):
    event = canonical_event(state)
    if presence == 'missing':
        event.payload.pop('host_fee_present')
    else:
        event.payload['host_fee_present'] = presence
    engine = Replay(state, registry)
    before = engine.state.economic_hash()
    with pytest.raises(Unsupported, match='host account'):
        engine.step(event)
    assert event.observed['host_fee'] == 0
    assert engine.state.economic_hash() == before


@pytest.mark.parametrize('name', ['swap_exact_out', 'swap_exact_out2',
                                  'swap_with_price_impact', 'swap_with_price_impact2'])
def test_recognized_instruction_does_not_imply_execution_support(state, registry, name):
    event = canonical_event(state)
    event.payload['instruction_name'] = name
    # The execution guard must also catch an inconsistent/missing exact_out flag.
    event.payload['exact_out'] = False
    with pytest.raises(Unsupported):
        Replay(state, registry).step(event)


def test_canonical_observed_input_must_match_consumption(state, registry):
    event = canonical_event(state)
    event.observed['amount_in'] += 1
    with pytest.raises(ValidationError, match='amount_in'):
        Replay(state, registry).step(event)


def test_nonzero_lo_fee_stays_unsupported(state, registry):
    event = canonical_event(state)
    event.observed['limit_order_fee'] = 1
    with pytest.raises(Unsupported, match='limit-order fee'):
        Replay(state, registry).step(event)


def test_historical_replay_cannot_use_legacy_unpaired_events(state, registry):
    state.data_kind = 'HISTORICAL'
    event = canonical_event(state)
    event.payload['canonical_pair_verified'] = False
    event.payload['normalization_mode'] = 'LEGACY_SINGLE_SWAP'
    with pytest.raises(Unsupported, match='canonical'):
        Replay(state, registry).step(event)


def test_unknown_historical_lo_liquidity_is_not_observed_zero(state, registry):
    state.data_kind = 'HISTORICAL'
    for b in state.bins.values():
        b.open_order_amount = b.processed_order_remaining_amount = b.total_processing_order_amount = 0
    state.bins[0].processed_order_remaining_amount = None
    event = canonical_event(state)
    with pytest.raises(Unavailable, match='unknown LO liquidity'):
        Replay(state, registry).step(event)
    state.bins[0].processed_order_remaining_amount = 0
    assert Replay(state, registry).step(event)['state_continuity'] == 'PASS'


@pytest.mark.parametrize('field,wrong', [
    ('trace_format', 'COMPACT_STATE_DELTAS_V1'),
    ('economic_hash_schema', 'MDLMM_ECONOMIC_STATE_V1'),
    ('economic_hash_schema', None),
])
def test_old_or_unmarked_trace_schema_is_not_silently_rehashed(state, registry, field, wrong):
    row = compact_trace(Replay(state, registry).step(canonical_event(state)))
    assert row['trace_format'] == 'COMPACT_STATE_DELTAS_V2'
    assert row['economic_hash_schema'] == ECONOMIC_STATE_HASH_SCHEMA
    if wrong is None:
        row.pop(field)
    else:
        row[field] = wrong
    with pytest.raises(Unsupported, match='not V2'):
        list(expand_traces([row], state, verify_hashes=False))


def test_new_lo_field_tampering_breaks_compact_continuity(state, registry):
    state.bins[0].order_age = 8
    row = compact_trace(Replay(state, registry).step(canonical_event(state)))
    row['state_patch']['bins']['0']['order_age'] = 9
    with pytest.raises(ValidationError, match='post-state continuity'):
        list(expand_traces([row], state))


@pytest.mark.parametrize('field,observed,claimed', [
    ('reward_mints', (DEFAULT_PUBKEY, DEFAULT_PUBKEY), (SOL_MINT, DEFAULT_PUBKEY)),
    ('protocol_fee_x', 23, 24),
    ('protocol_fee_y', 29, 30),
    ('bin_array_bitmap', tuple([0] * 16), tuple([1] + [0] * 15)),
    ('last_updated_at', 1000, 1001),
])
def test_preserved_pool_claims_are_checked_separately_from_mm_parity(state, field, observed, claimed):
    decoded = deepcopy(state)
    captured = deepcopy(state)
    setattr(decoded, field, observed)
    setattr(captured, field, claimed)
    # The old scope is intentionally too narrow to certify these fields.
    assert compare_states(captured, decoded)['equal']
    with pytest.raises(ValidationError, match='raw account preserved field mismatch'):
        verify_preserved_account_fields(captured, decoded)


@pytest.mark.parametrize('field', ['fulfilled_order_amount_x', 'fulfilled_order_amount_y',
                                   'limit_order_fee_ask_side', 'limit_order_fee_bid_side', 'order_age'])
def test_preserved_lo_claims_are_checked_against_decoded_accounts(state, field):
    decoded = deepcopy(state)
    captured = deepcopy(state)
    setattr(decoded.bins[0], field, 41)
    setattr(captured.bins[0], field, 42)
    assert compare_states(captured, decoded)['equal']
    with pytest.raises(ValidationError, match=field):
        verify_preserved_account_fields(captured, decoded)


def test_missing_preserved_capture_fields_stay_unknown(state):
    decoded = deepcopy(state)
    decoded.protocol_fee_x = 99
    decoded.bins[0].order_age = 11
    verify_preserved_account_fields(state, decoded)
    assert state.protocol_fee_x is None
    assert state.bins[0].order_age is None


@pytest.mark.parametrize('extra', [{'exact_out': True}, {'amount_out': '12'},
                                  {'host_fee_present': True}])
def test_sdk_exact_in_adapter_does_not_ignore_other_path_requests(tmp_path, extra):
    with pytest.raises(Unsupported, match='only amount_in/swap_for_y'):
        oracle(tmp_path, tmp_path / 'unused_capture.json', tmp_path / 'out',
               [{'amount_in': '100000', 'swap_for_y': True, **extra}])


@pytest.fixture
def blocked_export(tmp_path, state):
    """Synthetic transport/evidence fixture; never an on-chain parity oracle."""
    event = Event(Order(11, 0, 0, -1), 1_001_000, 'blocked-swap', state.pool,
                  'OBSERVED_LIQUIDITY', clock_unix_timestamp=1001)
    clock = struct.pack('<QqQQq', 11, 1000, 1, 1, 1001)
    write_json(tmp_path / 'clock.json', {
        'pubkey': CLOCK_ID, 'source_receipt': 'SYNTHETIC_TEST_ONLY',
        'account': {'data': [base64.b64encode(clock).decode(), 'base64']},
    })
    write_json(tmp_path / 'clocks.json', {'clocks': [
        {'slot': 11, 'capture': ref_file(tmp_path, 'clock.json')},
    ]})
    write_json(tmp_path / 'block.json', {'slot': 11, 'block': {}})
    write_json(tmp_path / 'slots.json', {'result': [11]})
    write_json(tmp_path / 'registry.json', {'entries': []})
    coverage = {
        'complete': True, 'missing': [], 'source_kind': 'FINALIZED_FULL_BLOCK_SCAN',
        'blocks': [ref_file(tmp_path, 'block.json')], 'produced_slots': [11],
        'slot_list_receipt': ref_file(tmp_path, 'slots.json'),
        'clocks': ref_file(tmp_path, 'clocks.json'), 'event_ids': [event.id],
        'resolved_mutations': [['blocked-swap', 0, -1]],
    }
    write_json(tmp_path / 'coverage.json', coverage)
    write_jsonl(tmp_path / 'transitions.jsonl', [asdict(event)])
    write_json(tmp_path / 'plan.json', {
        'data_kind': 'HISTORICAL', 'program_registry': 'registry.json',
        'segments': [{'id': 'segment', 'pool': state.pool,
                      'coverage': 'coverage.json', 'clocks': 'clocks.json',
                      'observed_transitions': 'transitions.jsonl'}],
    })
    blocker = {'signature': 'blocked-swap', 'instruction': 0, 'inner_instruction': -1,
               'blocker_kind': 'SWAP_CANONICALIZATION',
               'resolution_policy': 'RENORMALIZE_OR_REJECT',
               'reason': 'missing canonical economic event'}
    return tmp_path, event, blocker


def test_archival_prepare_cannot_replace_a_missing_swap_with_liquidity(blocked_export, monkeypatch):
    root, _, blocker = blocked_export
    called = []
    registry = SimpleNamespace(resolve=lambda slot: {'idl_file': 'test-idl.json'})
    monkeypatch.setattr(prepare_module, 'VersionRegistry', lambda *args, **kwargs: registry)
    monkeypatch.setattr(prepare_module.IdlCodec, 'load', lambda path: object())

    def normalize(*args, **kwargs):
        called.append(kwargs['require_canonical_swaps'])
        return {'events': [], 'blockers': [blocker]}

    monkeypatch.setattr(prepare_module, 'normalize_block', normalize)
    with pytest.raises(Unavailable, match='canonical normalization must be repaired'):
        prepare_module.prepare_historical(root / 'plan.json')
    assert called == [True]
    assert not (root / 'dataset.json').exists()


def test_historical_coverage_cannot_liquidity_patch_a_canonical_failure(blocked_export, monkeypatch):
    root, event, blocker = blocked_export
    dataset = Dataset.__new__(Dataset)
    dataset.kind = 'HISTORICAL'
    dataset.root = root
    dataset.file = lambda ref: root / ref['path']
    dataset.registry = SimpleNamespace(resolve=lambda slot: {'idl_file': 'test-idl.json'})
    called = []
    monkeypatch.setattr(dataset_module.IdlCodec, 'load', lambda path: object())

    def normalize(*args, **kwargs):
        called.append(kwargs['require_canonical_swaps'])
        return {'events': [], 'blockers': [blocker]}

    monkeypatch.setattr(dataset_module, 'normalize_block', normalize)
    segment = {'pool': event.pool, 'coverage': ref_file(root, 'coverage.json')}
    with pytest.raises(Unavailable, match='cannot be cleared by a liquidity patch'):
        dataset.verify_coverage(segment, [event])
    assert called == [True]
