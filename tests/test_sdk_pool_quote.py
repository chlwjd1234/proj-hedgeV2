"""Actual npm SDK quotes on labeled, controlled transformations of raw bytes.

These test vectors are synthetic, not a historical SOL/USDC pool or executable
SVM evidence. Original pinned fixture bytes and hashes are bundled. Only mint
identity/decimals, Clock, and explicitly requested test fields are transformed.
No model guard is relaxed and no post-state is inferred from SDK quotes.
"""
import base64
from copy import deepcopy
import json
from pathlib import Path
import struct

import pytest

from mdlmm.codec import (
    b58decode, build_pool_state, decode_bin_array, decode_lb_pair, find_program_address,
)
from mdlmm.errors import Unsupported, ValidationError
from mdlmm.model import CLOCK_ID, PROGRAM_ID, SOL_MINT, TOKEN_PROGRAM, USDC_MINT
from mdlmm.sdk import _identity, _requests, oracle
from mdlmm.swap_quote import SwapIntent
from mdlmm.util import read_json, sha256, write_json

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'tests/fixtures/sdk_quote_source_accounts.json'


@pytest.fixture
def sdk_root():
    if not (ROOT / 'scripts/sdk/node_modules/@meteora-ag/dlmm').exists():
        pytest.skip('actual pinned npm SDK not installed; run scripts/install_sdk.sh')
    if not (ROOT / 'external/dlmm-sdk/ts-client/src/dlmm/idl/idl.json').exists():
        pytest.skip('pinned full source IDL missing; run source-sync')
    return ROOT


def _capture(tmp_path, *, fee_mode=1, processed=False, mm_only=False, rounding=False):
    fixture = read_json(SOURCE)
    records = deepcopy(fixture['accounts'])
    originals = {label: bytearray(base64.b64decode(record['data_base64']))
                 for label, record in records.items()}
    for label, raw in originals.items():
        assert sha256(bytes(raw)) == records[label]['sha256']
    pair = originals['lb_pair']
    original_pair = decode_lb_pair(bytes(pair))
    # The original fixture is deliberately outside this project's token scope.
    assert original_pair['token_x_mint'] != SOL_MINT
    assert original_pair['token_y_mint'] != USDC_MINT
    pair[88:120] = b58decode(SOL_MINT)
    pair[120:152] = b58decode(USDC_MINT)
    transformations = ['pair token_x_mint/token_y_mint remapped to SOL/USDC',
                       'mint account identities and decimals remapped to 9/6',
                       'reserve token-account mint fields remapped consistently',
                       f'collect_fee_mode={fee_mode}', 'synthetic Clock and account wrappers']
    pair[36] = fee_mode
    for side, mint, decimals in (('x', SOL_MINT, 9), ('y', USDC_MINT, 6)):
        records['token_' + side + '_mint']['address'] = mint
        originals['token_' + side + '_mint'][44] = decimals
        originals['reserve_' + side][:32] = b58decode(mint)
    if processed or mm_only:
        for label in ('bin_array_1', 'bin_array_2'):
            raw = originals[label]
            # Pinned BinArray header=56, Bin stride=144, LO fields at112/120/128.
            for index in range(70):
                offset = 56 + index * 144
                opened = int.from_bytes(raw[offset+112:offset+120], 'little')
                if mm_only:
                    raw[offset+112:offset+136] = b'\0' * 24
                elif opened:
                    remaining = opened // 2
                    raw[offset+112:offset+120] = (opened - remaining).to_bytes(8, 'little')
                    raw[offset+120:offset+128] = remaining.to_bytes(8, 'little')
                    raw[offset+128:offset+136] = remaining.to_bytes(8, 'little')
                    raw[offset+136:offset+140] = (1).to_bytes(4, 'little')
        transformations.append('all LO quantities zero' if mm_only else
                               'half of each open-order quantity moved to processed capacity; age=1')
    if rounding:
        pair[8:10] = b'\0' * 2  # base_factor
        pair[16:20] = b'\0' * 4  # variable_fee_control
        raw = originals['bin_array_2']
        raw[56+16:56+32] = (10 << 64).to_bytes(16, 'little')
        transformations += ['base_factor=variable_fee_control=0', 'bin0.price=10*Q64']
    raw_accounts = {}
    for label, record in records.items():
        raw_accounts[record['address']] = {
            'owner': PROGRAM_ID if label in ('lb_pair', 'bin_array_1', 'bin_array_2') else TOKEN_PROGRAM,
            'data': [base64.b64encode(originals[label]).decode(), 'base64'],
            'lamports': 1, 'executable': False, 'rentEpoch': 0,
        }
    clock = {'slot': 448432756, 'epoch_start_timestamp': 1749999999,
             'epoch': 1038, 'leader_schedule_epoch': 1039, 'unix_timestamp': 1750000000}
    clock_raw = struct.pack('<QqQQq', *(clock[k] for k in (
        'slot', 'epoch_start_timestamp', 'epoch', 'leader_schedule_epoch', 'unix_timestamp')))
    raw_accounts[CLOCK_ID] = {'owner': 'Sysvar1111111111111111111111111111111111111',
                              'data': [base64.b64encode(clock_raw).decode(), 'base64'],
                              'lamports': 1, 'executable': False, 'rentEpoch': 0}
    pool = records['lb_pair']['address']
    bitmap, _ = find_program_address([b'bitmap', b58decode(pool)], PROGRAM_ID)
    raw_accounts[bitmap] = None
    state = build_pool_state(pool, decode_lb_pair(bytes(pair)),
                             [decode_bin_array(bytes(originals[label]))
                              for label in ('bin_array_1', 'bin_array_2')],
                             slot=clock['slot'], data_kind='SYNTHETIC')
    capture = {'pool': pool, 'slot': clock['slot'], 'clock': clock, 'state': state.to_dict(),
               'raw_accounts': raw_accounts, 'bitmap_extension_address': bitmap,
               'data_kind': 'SYNTHETIC',
               'provenance': {'kind': 'SYNTHETIC_SDK_VECTOR_CONTROLLED_MINT_REMAP',
                              'source_commit': fixture['source_commit'],
                              'source_fixture_manifest_sha256': sha256(SOURCE.read_bytes()),
                              'source_account_sha256': {k: v['sha256'] for k, v in fixture['accounts'].items()},
                              'transformations': transformations,
                              'historical_replay_parity': 'UNAVAILABLE',
                              'account_mutation_parity': 'UNAVAILABLE'}}
    path = tmp_path / 'capture.json'
    write_json(path, capture)
    return path


@pytest.mark.parametrize('fee_mode', [0, 1])
@pytest.mark.parametrize('processed', [False, True])
def test_actual_pinned_sdk_both_modes_directions_and_order_layers(tmp_path, sdk_root, fee_mode, processed):
    capture = _capture(tmp_path, fee_mode=fee_mode, processed=processed)
    requests = [SwapIntent.exact_in(1_000_000, True),
                SwapIntent.exact_in(1_000_000, False),
                SwapIntent.exact_in(13_400_000_000, True),
                SwapIntent.exact_in(8_000_000_000_000, False),
                SwapIntent.exact_out(1_000_000, True),
                SwapIntent.exact_out(1_000_000, False),
                SwapIntent.exact_out(13_000_000_000, True),
                SwapIntent.exact_out(7_000_000_000_000, False)]
    report = oracle(sdk_root, capture, tmp_path / 'parity', requests)
    assert report['status'] == 'PASS' and report['bins'] == 140
    assert len(report['quotes']) == len(requests)
    assert report['scope'] == 'MM_LIMIT_ORDER_EXACT_IN_OUT_QUOTE_AND_BIN_DECODE_ONLY'
    assert report['state_mutation_parity'] == 'UNAVAILABLE'
    assert 'HISTORICAL_REPLAY_PARITY' in report['not_claimed']
    assert any(q['lo_maker_fee'] > 0 and q['mm_lp_fee'] > 0 for q in report['quotes'])
    assert any(len(q['bin_ids']) > 1 for q in report['quotes'])


@pytest.mark.parametrize('fee_mode', [0, 1])
def test_actual_sdk_exact_out_scalar_does_not_become_an_account_fee(tmp_path, sdk_root, fee_mode):
    capture = _capture(tmp_path, fee_mode=fee_mode, rounding=True)
    report = oracle(sdk_root, capture, tmp_path / 'parity', [SwapIntent.exact_out(1, True)])
    quote = report['quotes'][0]
    assert quote['amount_in'] == quote['amount_out'] == 1
    assert quote['sdk_user_fee'] == quote['trading_fee'] == quote['mm_protocol_fee'] == 0
    assert quote['sdk_protocol_fee_scalar'] == quote['rounding_output_delta'] == 9
    assert quote['fee_token'] == ('x' if fee_mode == 0 else 'y')
    assert quote['output_token'] == 'y'


def test_legacy_two_key_mm_oracle_behavior_remains_available(tmp_path, sdk_root):
    capture = _capture(tmp_path, mm_only=True)
    requests = [{'amount_in': '1000000', 'swap_for_y': direction} for direction in (True, False)]
    report = oracle(sdk_root, capture, tmp_path / 'parity', requests)
    assert report['scope'] == 'MM_EXACT_IN_QUOTE_AND_BIN_DECODE_ONLY'
    for q in report['quotes']:
        assert q['total_trader_fee'] == q['lp_user_fee'] + q['protocol_fee']


def test_sdk_compares_raw_clock_to_timestamp_used_by_quote(tmp_path, sdk_root):
    path = _capture(tmp_path)
    capture = read_json(path)
    capture['clock']['unix_timestamp'] += 1
    write_json(path, capture)
    with pytest.raises(ValidationError, match='raw Clock'):
        oracle(sdk_root, path, tmp_path / 'parity', [SwapIntent.exact_in(100, True)])


def test_wire_format_preserves_u64_precision_and_rejects_host():
    amount = (1 << 64) - 1
    wire, intents = _requests([SwapIntent.exact_out(5, False, max_in_amount=amount)])
    assert wire[0]['max_in_amount'] == str(amount)
    assert SwapIntent.from_request(wire[0]) == intents[0]
    with pytest.raises(Unsupported, match='host/referral'):
        _requests([SwapIntent.exact_in(100, True, host_fee_present=True)])


@pytest.mark.parametrize('tamper', ['omit', 'duplicate', 'reorder', 'request'])
def test_sdk_response_cannot_pass_for_subset_or_other_requests(tamper):
    wire, _ = _requests([SwapIntent.exact_in(100, True), SwapIntent.exact_out(90, False)])
    answer = {'quotes': [{'request_index': i, 'request': deepcopy(request), 'quote': {}}
                          for i, request in enumerate(wire)]}
    if tamper == 'omit':
        answer['quotes'].pop()
    elif tamper == 'duplicate':
        answer['quotes'][1] = answer['quotes'][0]
    elif tamper == 'reorder':
        answer['quotes'].reverse()
    else:
        answer['quotes'][0]['request']['amount_in'] = '99'
    with pytest.raises(ValidationError, match='request'):
        _identity(answer, wire)
