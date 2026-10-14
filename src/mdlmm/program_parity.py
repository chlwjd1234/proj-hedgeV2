"""Offline verification of frozen synthetic DLMM program executions.

Model checks decode original transaction/account/event bytes, project the
modeled fields into copies of the original account buffers, and compare whole
buffers. Optional SVM checks execute the original transactions against the
supplied, hash-bound program. Neither check validates a historical prestate,
historical instruction coverage, or historical runtime feature equivalence.

The SDK fixture has noncanonical mints. Model checks explicitly alias only
their labels to SOL/USDC; program execution always uses the unmodified bytes.
"""
from __future__ import annotations

import argparse
import base64
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
import gzip
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import sys

from .codec import (
    IdlCodec, Reader, b58decode, b58encode, build_pool_state,
    decode_bin_array, decode_lb_pair, discriminator, find_program_address,
)
from .errors import ResearchError, Unavailable, Unsupported, ValidationError
from .model import Bin, PROGRAM_ID, SOL_MINT, TOKEN_PROGRAM, USDC_MINT
from .swap_quote import BitmapExtension, SwapIntent
from .swap_transition import PROGRAM_SHA256, simulate_swap

SCHEMA = 'MDLMM_PROGRAM_CASES_V1'
IDL_SHA256 = '045c0f4af044be046b6a14b350077e4242e8ac026b64a855803dab30fcdd8b35'
CASE_JSON_SHA256 = 'bb3fc90f25f6673a62d9db5f836ea1252e35a1c0deb80ab79a44a2b23f288254'
SOLDERS_VERSION = '0.27.1'
EVENT_CPI_TAG = bytes.fromhex('e445a52e51cb9a1d')
SUPPORTED_SWAPS = frozenset(('swap', 'swap2', 'swap_exact_out', 'swap_exact_out2'))
MODEL_SCOPE = 'SYNTHETIC_FOUR_VARIANT_PAIR_BINARRAY_TOKEN_AND_EVENT_PARITY'
SVM_SCOPE = 'SYNTHETIC_ORIGINAL_TRANSACTION_PROGRAM_REEXECUTION'

_BIN_LAYOUT = (
    ('amount_x', 0, 64), ('amount_y', 8, 64), ('price', 16, 128),
    ('liquidity_supply', 32, 128), ('fulfilled_order_amount_x', 48, 64),
    ('fulfilled_order_amount_y', 56, 64), ('limit_order_fee_ask_side', 64, 64),
    ('limit_order_fee_bid_side', 72, 64), ('fee_x_stored', 80, 128),
    ('fee_y_stored', 96, 128), ('open_order_amount', 112, 64),
    ('total_processing_order_amount', 120, 64),
    ('processed_order_remaining_amount', 128, 64), ('order_age', 136, 32),
    ('limit_order_ask_side', 140, 8),
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _b64(value, label: str) -> bytes:
    if not isinstance(value, str):
        raise ValidationError(f'{label} must contain base64 text')
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f'{label} has invalid base64') from exc


def _key(value, label: str) -> str:
    if not isinstance(value, str) or len(b58decode(value)) != 32:
        raise ValidationError(f'{label} must be a 32-byte public key')
    return value


def _uint(value, bits: int, label: str) -> int:
    if type(value) is not int or not 0 <= value < (1 << bits):
        raise ValidationError(f'{label} must be integer u{bits}')
    return value


def _read_artifact(path: str | Path, maximum: int) -> tuple[bytes, str]:
    path = Path(path)
    compressed = path.read_bytes()
    if len(compressed) > maximum:
        raise ValidationError('artifact exceeds the verification size bound')
    if path.suffix == '.gz':
        with gzip.open(path, 'rb') as stream:
            raw = stream.read(maximum + 1)
    else:
        raw = compressed
    if len(raw) > maximum:
        raise ValidationError('expanded artifact exceeds the verification size bound')
    return raw, _sha(compressed)


def _canonical_case_sha(case: dict) -> str:
    return _sha((json.dumps(case, indent=2, sort_keys=True) + '\n').encode())


def _account_raw(account: dict | None, label: str) -> bytes:
    if not isinstance(account, dict):
        raise Unavailable(f'{label} account is absent')
    raw = _b64(account.get('data_base64'), label)
    if _sha(raw) != account.get('data_sha256'):
        raise ValidationError(f'{label} account data hash mismatch')
    _key(account.get('owner'), f'{label} owner')
    _uint(account.get('lamports'), 64, f'{label} lamports')
    _uint(account.get('rent_epoch'), 64, f'{label} rent_epoch')
    if type(account.get('executable')) is not bool:
        raise ValidationError(f'{label} executable must be boolean')
    return raw


@dataclass(frozen=True)
class ProgramBundle:
    cases: tuple[dict, ...]
    idl: dict
    program_bytes: bytes
    case_sources: dict
    metadata: dict
    case_json_sha256: str
    case_archive_sha256: str
    program_archive_sha256: str

    @property
    def codec(self) -> IdlCodec:
        return IdlCodec(self.idl)


def load_bundle(cases_path: str | Path, program_path: str | Path) -> ProgramBundle:
    """Load the pinned v1 corpus and program, without importing an SVM runtime."""
    raw, archive_hash = _read_artifact(cases_path, 128 * 1024 * 1024)
    if _sha(raw) != CASE_JSON_SHA256:
        raise ValidationError('case bundle does not match the pinned v1 JSON hash')
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get('schema') != SCHEMA:
        raise ValidationError('unsupported program-case bundle schema')
    if (data.get('program_sha256') != PROGRAM_SHA256
            or data.get('program_id') != PROGRAM_ID
            or data.get('data_kind') != 'SYNTHETIC'):
        raise ValidationError('program-case identity or synthetic scope mismatch')
    if data.get('solders_version') != SOLDERS_VERSION:
        raise ValidationError('bundle runtime version differs from the pinned runtime')
    idl_raw = _b64(data.get('idl_raw_base64'), 'bundled IDL')
    if _sha(idl_raw) != IDL_SHA256 or data.get('idl_sha256') != IDL_SHA256:
        raise ValidationError('bundled IDL hash mismatch')
    idl = json.loads(idl_raw)
    if idl.get('address') != PROGRAM_ID:
        raise ValidationError('bundled IDL program address mismatch')
    program, program_archive_hash = _read_artifact(program_path, 16 * 1024 * 1024)
    if _sha(program) != PROGRAM_SHA256 or len(program) != data.get('program_length'):
        raise ValidationError('supplied program does not match the d296 binary binding')
    cases, sources = data.get('cases'), data.get('case_sources')
    if not isinstance(cases, list) or not cases or not isinstance(sources, dict):
        raise ValidationError('program bundle has no complete source-indexed cases')
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get('case_id'), str):
            raise ValidationError('invalid program case')
        case_id = case['case_id']
        if case_id in ids:
            raise ValidationError('duplicate program case ID')
        ids.add(case_id)
        source = sources.get(case_id)
        if (not isinstance(source, dict)
                or source.get('serialization') != 'json.dumps(indent=2,sort_keys=True)+newline'
                or source.get('sha256') != _canonical_case_sha(case)):
            raise ValidationError(f'{case_id}: original capture hash mismatch')
        if case.get('execution_status') not in ('EXECUTED', 'ERROR'):
            raise ValidationError(f'{case_id}: missing execution status')
        if case.get('provenance', {}).get('program_sha256') != PROGRAM_SHA256:
            raise ValidationError(f'{case_id}: captured binary binding mismatch')
        pre, post = case.get('pre_accounts'), case.get('post_accounts')
        if not isinstance(pre, dict) or not isinstance(post, dict) or set(pre) != set(post):
            raise ValidationError(f'{case_id}: pre/post account inventory mismatch')
        for phase, accounts in (('pre', pre), ('post', post)):
            for address, account in accounts.items():
                _key(address, f'{case_id} account')
                if account is not None:
                    _account_raw(account, f'{case_id} {phase} {address}')
    if set(sources) != ids:
        raise ValidationError('source index does not exactly cover cases')
    metadata = {k: v for k, v in data.items() if k not in ('cases', 'case_sources', 'idl_raw_base64')}
    return ProgramBundle(tuple(cases), idl, program, sources, metadata, _sha(raw),
                         archive_hash, program_archive_hash)


def _shortvec(reader: Reader) -> int:
    value = 0
    for index in range(3):
        byte = reader.integer(8)
        if index == 2 and byte > 3:
            raise ValidationError('transaction shortvec exceeds u16')
        value |= (byte & 127) << (index * 7)
        if byte < 128:
            if index and byte == 0:
                raise ValidationError('noncanonical transaction shortvec')
            return value
    raise ValidationError('unterminated transaction shortvec')


def _transaction_instruction(case: dict, codec: IdlCodec) -> dict:
    """Decode the captured legacy transaction without an optional dependency."""
    reader = Reader(_b64(case.get('transaction_base64'), 'original transaction'))
    signatures = _shortvec(reader)
    reader.take(64 * signatures)
    required, readonly_signed, readonly_unsigned = [reader.integer(8) for _ in range(3)]
    if required & 128:
        raise Unsupported('model corpus parser is bounded to original legacy transactions')
    count = _shortvec(reader)
    if (not 0 < count <= 256 or signatures != required or required > count
            or readonly_signed > required or readonly_unsigned > count - required):
        raise ValidationError('malformed transaction account header')
    keys = [reader.key() for _ in range(count)]
    if len(set(keys)) != len(keys):
        raise ValidationError('duplicate transaction message account key')
    reader.take(32)  # Original recent blockhash is retained for SVM execution.
    instruction_count = _shortvec(reader)
    if instruction_count != 1:
        raise Unsupported('model corpus requires exactly one original instruction')
    program_index = reader.integer(8)
    indices = list(reader.take(_shortvec(reader)))
    payload = reader.take(_shortvec(reader))
    if reader.offset != len(reader.data) or max([program_index, *indices]) >= count:
        raise ValidationError('invalid or trailing transaction instruction data')
    if keys[program_index] != PROGRAM_ID:
        raise Unsupported('original transaction does not invoke the bound DLMM program')
    decoded = codec.decode_named('instructions', payload)
    spec = next(i for i in codec.idl['instructions'] if i['name'] == decoded['name'])
    if len(indices) < len(spec['accounts']):
        raise ValidationError('original instruction omits required account positions')
    accounts = [keys[i] for i in indices]
    recorded = case.get('instruction', {})
    if (_b64(recorded.get('data_base64'), 'recorded instruction') != payload
            or [a.get('pubkey') for a in recorded.get('accounts', [])] != accounts
            or recorded.get('name') != decoded['name']):
        raise ValidationError('recorded instruction disagrees with original transaction bytes')
    roles = {item['name']: accounts[i] for i, item in enumerate(spec['accounts'])}
    for index, item in enumerate(spec['accounts']):
        key_index = indices[index]
        if item.get('signer') and key_index >= required:
            raise ValidationError('required instruction authority is not a transaction signer')
        if 'address' in item and accounts[index] != item['address']:
            raise ValidationError('instruction uses the wrong pinned program account')
    return {'name': decoded['name'], 'args': decoded['data'], 'roles': roles,
            'remaining_accounts': accounts[len(spec['accounts']):], 'keys': keys,
            'accounts': accounts, 'payload': payload}


def _decoded_events(bundle: ProgramBundle, case: dict) -> list[dict]:
    if case.get('event_decode_errors'):
        raise ValidationError('capture contains unparsed program event evidence')
    result = []
    for item in case.get('events', []):
        raw = _b64(item.get('raw_cpi_data_base64'), 'original event CPI')
        if not raw.startswith(EVENT_CPI_TAG):
            raise ValidationError('captured event lacks the Anchor event-CPI tag')
        decoded = bundle.codec.decode_named('events', raw[8:])
        if decoded.get('name') != item.get('name') or decoded.get('data') != item.get('data'):
            raise ValidationError('decoded event metadata contradicts its original CPI bytes')
        result.append({**decoded, 'raw': raw})
    return result


def _pre(case: dict, address: str, *, owner: str | None = None) -> bytes:
    account = case['pre_accounts'].get(address)
    raw = _account_raw(account, f'pre {address}')
    if owner is not None and account['owner'] != owner:
        raise ValidationError('original account has the wrong owner')
    return raw


def _token(case: dict, address: str) -> tuple[str, int]:
    raw = _pre(case, address, owner=TOKEN_PROGRAM)
    if len(raw) != 165 or raw[108] != 1:
        raise Unsupported('model corpus requires initialized classic SPL token accounts')
    return b58encode(raw[:32]), int.from_bytes(raw[64:72], 'little')


def _write(raw: bytearray, offset: int, bits: int, value, *, signed=False) -> None:
    if value is None or type(value) is not int:
        raise Unavailable('projected program field lacks exact integer evidence')
    try:
        raw[offset:offset + bits // 8] = value.to_bytes(bits // 8, 'little', signed=signed)
    except OverflowError as exc:
        raise ValidationError('projected program field overflows its account width') from exc


def _assert_bytes(actual: bytes, expected: bytes, label: str) -> None:
    if actual != expected:
        offset = next((i for i, (a, b) in enumerate(zip(actual, expected)) if a != b),
                      min(len(actual), len(expected)))
        raise ValidationError(f'{label}: raw post-account mismatch at byte {offset}')


def _compare_projected_account(case: dict, address: str, projected: bytes, label: str) -> None:
    pre, post = case['pre_accounts'][address], case['post_accounts'].get(address)
    expected = _account_raw(post, f'post {address}')
    _assert_bytes(projected, expected, label)
    for field in ('owner', 'executable', 'rent_epoch', 'lamports'):
        if post[field] != pre[field]:
            raise ValidationError(f'{label}: unmodeled account metadata change in {field}')


def check_model_case(bundle: ProgramBundle, case: dict) -> dict:
    """Verify one supported successful case; failures/other methods are explicit.

    Decoded snapshots in the archival capture are documentary and are never
    used as expected model values. The oracle is original post-account and CPI
    bytes. Validation errors propagate so the caller cannot report them PASS.
    """
    base = {'case_id': case['case_id'], 'scope': MODEL_SCOPE,
            'historical_gate': 'UNAVAILABLE', 'full_historical_exactness': False}
    instruction = _transaction_instruction(case, bundle.codec)
    if case.get('execution_status') != 'EXECUTED':
        return {**base, 'status': 'UNAVAILABLE', 'modeled': False,
                'reason': 'failed program case is not certified by a typed amount intent',
                'instruction_name': instruction['name']}
    if instruction['name'] not in SUPPORTED_SWAPS:
        return {**base, 'status': 'UNAVAILABLE', 'modeled': False,
                'reason': 'instruction mutation or price-impact constraint is outside this model',
                'instruction_name': instruction['name']}
    original_hash = _canonical_case_sha(case)
    roles, args = instruction['roles'], dict(instruction['args'])
    pool = roles['lb_pair']
    pair_raw = _pre(case, pool, owner=PROGRAM_ID)
    pair = decode_lb_pair(pair_raw)
    for name in ('reserve_x', 'reserve_y', 'token_x_mint', 'token_y_mint', 'oracle'):
        if roles[name] != pair[name]:
            raise ValidationError(f'actual instruction {name} does not match pool bytes')
    if roles['program'] != PROGRAM_ID:
        raise ValidationError('wrong event-CPI program account')
    authority, _ = find_program_address([b'__event_authority'], PROGRAM_ID)
    if roles['event_authority'] != authority:
        raise ValidationError('wrong event-CPI authority account')
    if roles['token_x_program'] != TOKEN_PROGRAM or roles['token_y_program'] != TOKEN_PROGRAM:
        raise Unsupported('model corpus excludes Token-2022 account semantics')
    input_mint, _ = _token(case, roles['user_token_in'])
    output_mint, _ = _token(case, roles['user_token_out'])
    raw_x, raw_y = pair['token_x_mint'], pair['token_y_mint']
    if (input_mint, output_mint) == (raw_x, raw_y):
        direction = True
    elif (input_mint, output_mint) == (raw_y, raw_x):
        direction = False
    else:
        raise ValidationError('actual token-account mints do not establish swap direction')
    for side, mint in (('x', raw_x), ('y', raw_y)):
        if _token(case, roles['reserve_' + side])[0] != mint:
            raise ValidationError('pool reserve mint mismatch')
    mode = pair['parameters']['collect_fee_mode']
    if mode not in (0, 1):
        raise Unsupported('unknown collect fee mode')
    fee_on_input = mode == 0 or not direction
    fee_mint = input_mint if fee_on_input else output_mint
    host = roles['host_fee_in'] != PROGRAM_ID
    if host and _token(case, roles['host_fee_in'])[0] != fee_mint:
        raise ValidationError('actual host account has the wrong fee-token mint')
    if (case['instruction'].get('swap_for_y') is not direction
            or case['instruction'].get('host') is not host):
        raise ValidationError('recorded direction/host metadata contradicts actual accounts')
    if args.pop('remaining_accounts_info', {'slices': []}) != {'slices': []}:
        raise Unsupported('model corpus excludes additional transfer-hook account slices')
    intent = (SwapIntent.exact_out(args['out_amount'], direction,
                                   max_in_amount=args['max_in_amount'], host_fee_present=host)
              if 'exact_out' in instruction['name'] else
              SwapIntent.exact_in(args['amount_in'], direction,
                                  min_amount_out=args['min_amount_out'], host_fee_present=host))
    arrays_by_address = {}
    for address in instruction['remaining_accounts']:
        raw = _pre(case, address, owner=PROGRAM_ID)
        arrays_by_address[address] = decode_bin_array(raw)
    if len(arrays_by_address) != len(instruction['remaining_accounts']):
        raise ValidationError('duplicate remaining BinArray account')
    extension_address = roles['bin_array_bitmap_extension']
    extension = None
    if extension_address != PROGRAM_ID:
        extension = BitmapExtension.from_bytes(_pre(case, extension_address, owner=PROGRAM_ID))
    # Labels only: raw pair, mint accounts and all execution bytes stay intact.
    aliased_pair = dict(pair, token_x_mint=SOL_MINT, token_y_mint=USDC_MINT)
    state = build_pool_state(pool, aliased_pair, list(arrays_by_address.values()),
                             slot=case['clock']['slot'], data_kind='SYNTHETIC')
    state.binary_sha256 = PROGRAM_SHA256
    state.idl_hash = IDL_SHA256
    source_copy = deepcopy(state.to_dict())
    transition = simulate_swap(state, intent, case['clock']['unix_timestamp'],
                                bitmap_extension=extension,
                                extension_known=extension is not None)
    if state.to_dict() != source_copy:
        raise ValidationError('model mutated caller-owned prestate (including MM fee growth)')
    checked_accounts = []
    for address, array in arrays_by_address.items():
        post_raw = _account_raw(case['post_accounts'].get(address), f'post {address}')
        decoded_post = decode_bin_array(post_raw)
        projected = bytearray(_pre(case, address, owner=PROGRAM_ID))
        for index, expected_bin in enumerate(decoded_post['bins']):
            model_bin = transition.post_state.bins[array['bins'][index]['bin_id']]
            for field in Bin.__dataclass_fields__:
                if getattr(model_bin, field) != expected_bin[field]:
                    raise ValidationError(f'Bin {model_bin.bin_id} {field}: raw post-state mismatch')
            for field, offset, bits in _BIN_LAYOUT:
                _write(projected, 56 + 144 * index + offset, bits, getattr(model_bin, field))
        _compare_projected_account(case, address, bytes(projected), f'BinArray {array["index"]}')
        checked_accounts.append(address)
    projected_pair = bytearray(pair_raw)
    post_state = transition.post_state
    for field, offset, bits, signed in (
            ('volatility_accumulator', 40, 32, False), ('volatility_reference', 44, 32, False),
            ('index_reference', 48, 32, True), ('last_update_timestamp', 56, 64, True)):
        _write(projected_pair, offset, bits, getattr(post_state.variable, field), signed=signed)
    _write(projected_pair, 76, 32, post_state.active_id, signed=True)
    _write(projected_pair, 216, 64, post_state.protocol_fee_x)
    _write(projected_pair, 224, 64, post_state.protocol_fee_y)
    for index, word in enumerate(post_state.bin_array_bitmap):
        _write(projected_pair, 584 + 8 * index, 64, word)
    _write(projected_pair, 712, 64, post_state.last_updated_at, signed=True)
    _compare_projected_account(case, pool, bytes(projected_pair), 'LbPair')
    checked_accounts.append(pool)
    delta = defaultdict(int)
    delta[roles['user_token_in']] -= transition.amount_in
    delta[roles['user_token_out']] += transition.amount_out
    reserve_in = roles['reserve_x' if direction else 'reserve_y']
    reserve_out = roles['reserve_y' if direction else 'reserve_x']
    delta[reserve_in] += transition.amount_in
    delta[reserve_out] -= transition.amount_out
    if host:
        delta[roles['host_fee_in']] += transition.host_fee
        delta[roles['reserve_x' if transition.fee_token == 'x' else 'reserve_y']] -= transition.host_fee
    token_accounts = []
    for address, account in case['pre_accounts'].items():
        if account is None:
            continue
        raw = _account_raw(account, f'pre {address}')
        if account['owner'] == TOKEN_PROGRAM and len(raw) == 165:
            projected = bytearray(raw)
            _write(projected, 64, 64, int.from_bytes(raw[64:72], 'little') + delta.pop(address, 0))
            _compare_projected_account(case, address, bytes(projected), 'SPL token account')
            checked_accounts.append(address)
            token_accounts.append(address)
        elif (address not in checked_accounts and address != roles['oracle']
              and account['owner'] in (PROGRAM_ID, TOKEN_PROGRAM)):
            _compare_projected_account(case, address, raw, 'unmodified program or mint account')
            checked_accounts.append(address)
    if any(delta.values()):
        raise ValidationError('predicted token change lacks a captured token account')
    events = _decoded_events(bundle, case)
    if [event['name'] for event in events] != ['Swap', 'Swap2Evt']:
        raise ValidationError('successful modeled swap does not have its exact ordered event pair')
    compatibility, rich = events[0]['data'], events[1]['data']
    for event in (compatibility, rich):
        if event['lb_pair'] != pool or event['from'] != roles['user']:
            raise ValidationError('raw event identifies a different pool or user')
    expected = transition.event_amounts()
    for field, value in expected.items():
        if rich.get(field) != value:
            raise ValidationError(f'Swap2Evt {field}: raw event differs from model')
    for field in ('start_bin_id', 'end_bin_id', 'amount_in', 'amount_out',
                  'swap_for_y', 'protocol_fee', 'host_fee', 'fee_bps'):
        if compatibility[field] != rich[field]:
            raise ValidationError(f'compatibility Swap {field} differs from Swap2Evt')
    if compatibility['fee'] != transition.base_trading_fee + transition.rounding_protocol_fee:
        raise ValidationError('compatibility Swap fee differs from actual trading and rounding fees')
    if _canonical_case_sha(case) != original_hash:
        raise ValidationError('verification altered the original capture')
    unmodeled = []
    for address, pre_account in case['pre_accounts'].items():
        if address in checked_accounts:
            continue
        post_account = case['post_accounts'][address]
        unmodeled.append({
            'address': address, 'label': case.get('account_labels', {}).get(address),
            'reason': ('Oracle state is outside PoolState; checked only by full SVM replay'
                       if address == roles['oracle'] else
                       'runtime account creation, rent or transaction fees are outside the model'),
            'pre_data_sha256': pre_account.get('data_sha256') if pre_account else None,
            'post_data_sha256': post_account.get('data_sha256') if post_account else None,
            'account_changed': pre_account != post_account,
        })
    return {**base, 'status': 'PASS', 'modeled': True,
            'instruction_name': instruction['name'], 'actual_intent': intent.to_request(),
            'raw_original_mints': {'x': raw_x, 'y': raw_y},
            'synthetic_model_mint_labels': {'x': SOL_MINT, 'y': USDC_MINT},
            'raw_execution_bytes_modified': False, 'checked_account_count': len(checked_accounts),
            'checked_bin_count': 70 * len(arrays_by_address),
            'checked_token_account_count': len(token_accounts),
            'unmodeled_account_metadata': [a for a in case['pre_accounts'] if a not in checked_accounts],
            'unmodeled_accounts': unmodeled,
            'amount_in': transition.amount_in, 'amount_out': transition.amount_out,
            'amount_left': transition.amount_left,
            'protocol_fee': transition.protocol_fee,
            'sdk_protocol_fee_scalar': transition.quote.sdk_protocol_fee,
            'rounding_protocol_fee': transition.rounding_protocol_fee,
            'unallocated_output_surplus': transition.unallocated_output_surplus}


def _require_solders() -> None:
    try:
        version = importlib.metadata.version('solders')
    except importlib.metadata.PackageNotFoundError as exc:
        raise Unavailable('SVM verification requires solders==0.27.1; it was not run') from exc
    if version != SOLDERS_VERSION:
        raise Unavailable(f'SVM verification requires solders==0.27.1, found {version}')


def reexecute_case(bundle: ProgramBundle, case: dict) -> dict:
    """Replay all original bytes in a fresh pinned runtime, including failures."""
    _require_solders()
    # Optional dependency: importing this module and normal model tests do not
    # load solders or silently skip requested runtime checks.
    from solders.account import Account
    from solders.clock import Clock
    from solders.litesvm import LiteSVM
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    from solders.transaction_metadata import FailedTransactionMetadata

    svm = LiteSVM().with_sigverify(False).with_blockhash_check(False)
    program_key = Pubkey.from_string(PROGRAM_ID)
    svm.add_program(program_key, bundle.program_bytes)
    for address, account in case['pre_accounts'].items():
        key = Pubkey.from_string(address)
        if account is None:
            if svm.get_account(key) is not None:
                raise ValidationError('fresh runtime unexpectedly supplies an absent pre-account')
            continue
        raw = _account_raw(account, f'pre {address}')
        svm.set_account(key, Account(account['lamports'], raw, Pubkey.from_string(account['owner']),
                                     account['executable'], account['rent_epoch']))
    svm.set_clock(Clock(**case['clock']))
    transaction = VersionedTransaction.from_bytes(_b64(case['transaction_base64'], 'original transaction'))
    result = svm.send_transaction(transaction)
    failed = isinstance(result, FailedTransactionMetadata)
    status = 'ERROR' if failed else 'EXECUTED'
    error = str(result.err()) if failed else None
    if status != case['execution_status'] or error != case.get('error'):
        raise ValidationError(f'{case["case_id"]}: program status/error differs: {status}, {error}')
    for address, expected in case['post_accounts'].items():
        actual = svm.get_account(Pubkey.from_string(address))
        if expected is None:
            if actual is not None:
                raise ValidationError('program created an account expected to remain absent')
            continue
        if actual is None:
            raise ValidationError('program post-account unexpectedly absent')
        _assert_bytes(bytes(actual.data), _account_raw(expected, f'post {address}'), f'SVM {address}')
        values = {'owner': str(actual.owner), 'lamports': actual.lamports,
                  'executable': actual.executable, 'rent_epoch': actual.rent_epoch}
        if any(values[k] != expected[k] for k in values):
            raise ValidationError(f'SVM {address}: post-account metadata mismatch')
    meta = result.meta() if failed else result
    keys = list(transaction.message.account_keys)
    actual_events = []
    for group in meta.inner_instructions():
        for inner in group:
            compiled = inner.instruction
            if callable(compiled):
                compiled = compiled()
            if keys[compiled.program_id_index] == program_key:
                raw = bytes(compiled.data)
                if not raw.startswith(EVENT_CPI_TAG):
                    raise ValidationError('unrecognized self-CPI in program reexecution')
                actual_events.append(raw)
    expected_events = [event['raw'] for event in _decoded_events(bundle, case)]
    if actual_events != expected_events:
        raise ValidationError('program reexecution emitted different ordered CPI bytes')
    custom = re.search(r'Custom\((\d+)\)', error or '')
    return {'case_id': case['case_id'], 'status': 'PASS', 'scope': SVM_SCOPE,
            'execution_status': status, 'custom_error_code': int(custom[1]) if custom else None,
            'post_accounts_compared': len(case['post_accounts']), 'ordered_cpi_events_compared': len(actual_events),
            'solders_version': SOLDERS_VERSION, 'program_sha256': PROGRAM_SHA256,
            'signature_verification': False, 'blockhash_check': False,
            'historical_gate': 'UNAVAILABLE', 'full_historical_exactness': False}


def verify_bundle(bundle: ProgramBundle, *, run_svm: bool = False) -> dict:
    """Aggregate bounded model parity and, when requested, whole-program replay."""
    model_results, svm_results, errors = [], [], []
    for case in bundle.cases:
        try:
            model_results.append(check_model_case(bundle, case))
        except (ResearchError, ValueError, KeyError, TypeError) as exc:
            errors.append({'case_id': case['case_id'], 'scope': MODEL_SCOPE, 'error': str(exc)})
    modeled = [row for row in model_results if row.get('modeled')]
    not_modeled = [row for row in model_results if not row.get('modeled')]
    model_status = 'FAIL' if errors else 'PASS' if modeled else 'UNAVAILABLE'
    svm_status = 'NOT_REQUESTED'
    if run_svm:
        try:
            _require_solders()
        except Unavailable as exc:
            svm_status = 'UNAVAILABLE'
            errors.append({'scope': SVM_SCOPE, 'error': str(exc)})
        else:
            for case in bundle.cases:
                try:
                    svm_results.append(reexecute_case(bundle, case))
                except (ResearchError, ValueError, KeyError, TypeError) as exc:
                    errors.append({'case_id': case['case_id'], 'scope': SVM_SCOPE, 'error': str(exc)})
            svm_status = 'PASS' if len(svm_results) == len(bundle.cases) else 'FAIL'
    status = ('FAIL' if model_status == 'FAIL' or svm_status == 'FAIL' else
              'UNAVAILABLE' if model_status == 'UNAVAILABLE' or svm_status == 'UNAVAILABLE' else 'PASS')
    return {'schema': 'MDLMM_PROGRAM_PARITY_V1', 'status': status,
            'program_vector_status': model_status, 'scope': MODEL_SCOPE,
            'case_count': len(bundle.cases), 'model_case_count': len(modeled),
            'model_cases': modeled, 'not_modeled_cases': not_modeled,
            'svm_reexecution_status': svm_status, 'svm_reexecution_count': len(svm_results),
            'svm_cases': svm_results, 'errors': errors, 'data_kind': 'SYNTHETIC',
            'case_json_sha256': bundle.case_json_sha256, 'case_archive_sha256': bundle.case_archive_sha256,
            'program_sha256': PROGRAM_SHA256, 'program_archive_sha256': bundle.program_archive_sha256,
            'idl_sha256': IDL_SHA256, 'solders_version_required': SOLDERS_VERSION,
            'historical_gate': 'UNAVAILABLE', 'full_historical_exactness': False,
            'historical_prestate_validated': False, 'historical_runtime_feature_parity': 'UNVERIFIED'}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', required=True, type=Path, help='pinned case JSON or JSON.gz')
    parser.add_argument('--program', required=True, type=Path, help='d296 SBF .so or .so.gz')
    parser.add_argument('--run-svm', action='store_true', help='require solders 0.27.1 and reexecute every original case')
    args = parser.parse_args(argv)
    try:
        report = verify_bundle(load_bundle(args.cases, args.program), run_svm=args.run_svm)
    except (ResearchError, ValueError, OSError, KeyError, TypeError) as exc:
        report = {'status': 'UNAVAILABLE' if isinstance(exc, Unavailable) else 'FAIL',
                  'error': str(exc), 'historical_gate': 'UNAVAILABLE', 'full_historical_exactness': False}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report['status'] == 'PASS' else 2


if __name__ == '__main__':
    sys.exit(main())
