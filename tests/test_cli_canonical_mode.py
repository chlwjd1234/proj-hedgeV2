"""Exercise the public CLI's diagnostic/strict boundary on raw instruction bytes."""
from __future__ import annotations

import json
import shutil
import struct
from pathlib import Path

import pytest

from mdlmm.cli import main
from mdlmm.codec import EVENT_FIELDS, b58decode, b58encode, discriminator
from mdlmm.model import PROGRAM_ID, SOL_MINT, USDC_MINT, TOKEN_PROGRAM
from mdlmm.normalize import EVENT_CPI_TAG
from mdlmm.util import read_json, read_jsonl


def _event_bytes(name, values):
    out = bytearray(EVENT_CPI_TAG + discriminator('event', name))
    for field, kind in EVENT_FIELDS[name]:
        value = values[field]
        if kind == 'pubkey':
            out.extend(b58decode(value))
        elif kind == 'bool':
            out.append(int(value))
        else:
            out.extend(value.to_bytes(int(kind[1:]) // 8, 'little', signed=kind[0] == 'i'))
    return b58encode(bytes(out))


def _block(pool, *, paired):
    keys = [b58encode(bytes([i]) * 32) for i in range(1, 16)]
    keys[0] = pool
    keys[6:8] = [SOL_MINT, USDC_MINT]
    keys[9] = PROGRAM_ID  # Anchor's omitted optional host account.
    keys[11:13] = [TOKEN_PROGRAM, TOKEN_PROGRAM]
    keys[14] = PROGRAM_ID
    common = dict(lb_pair=pool, **{'from': keys[10]}, start_bin_id=0, end_bin_id=0,
                  amount_in=100, amount_out=90, swap_for_y=True, protocol_fee=1,
                  host_fee=0, fee_bps=1)
    old = dict(common, fee=10)
    new = dict(common, amount_left=0, mm_fee=9, limit_order_fee=0,
               fees_on_input=True, fees_on_token_x=True)
    inner = [{'programId': PROGRAM_ID, 'accounts': [], 'data': _event_bytes('Swap', old),
              'stackHeight': 2}]
    if paired:
        inner.append({'programId': PROGRAM_ID, 'accounts': [],
                      'data': _event_bytes('Swap2Evt', new), 'stackHeight': 2})
    data = discriminator('global', 'swap') + (struct.pack('<QQ', 100, 1) if paired else b'')
    top = {'programId': PROGRAM_ID, 'accounts': keys if paired else [pool],
           'data': b58encode(data)}
    return {'blockTime': 1000, 'transactions': [{
        'transaction': {'signatures': ['SYNTHETIC_CLI_SIG'],
                        'message': {'accountKeys': [PROGRAM_ID, pool], 'instructions': [top]}},
        'meta': {'err': None, 'fee': 5000,
                 'innerInstructions': [{'index': 0, 'instructions': inner}]},
    }]}


@pytest.mark.parametrize('paired,strict,expected_code,expected_swaps', [
    (False, False, 0, 1), (False, True, 3, 0), (True, True, 0, 1),
])
def test_cli_preserves_blockers_and_distinguishes_strict_pairing(
        tmp_path, paired, strict, expected_code, expected_swaps):
    source = Path(__file__).parents[1]
    for directory in ('configs', 'reference'):
        shutil.copytree(source / directory, tmp_path / directory)
    pool = b58encode(bytes(range(32)))
    blocks = tmp_path / 'blocks'
    blocks.mkdir()
    (blocks / '20.json').write_text(json.dumps({'slot': 20, 'block': _block(pool, paired=paired)}))
    args = ['normalize', '--root', str(tmp_path), '--input', str(blocks), '--pool', pool]
    if strict:
        args.append('--require-canonical-swaps')
    assert main(args) == expected_code
    pointer = read_json(tmp_path / 'runs/normalize_latest.json')
    run = tmp_path / 'runs' / pointer['run_id']
    result = read_json(run / 'result.json')
    events = list(read_jsonl(run / 'events.jsonl.gz'))
    assert len(events) == expected_swaps
    assert result['strict_canonical_pass'] is (strict and paired)
    assert result['canonical_swaps'] == int(paired)
    assert result['scope'] == 'EVENT_DECODE_NOT_FACTUAL_REPLAY'
    assert result['historical_gate'] == 'UNAVAILABLE'
    blockers = read_json(run / 'blockers.json')
    assert bool(blockers) is (strict and not paired)
