"""Instruction-owned DLMM observations, in finalized getBlock execution order.

A Swap2Evt and its compatibility Swap describe one economic swap.  They are
paired inside one invocation, never by pool or by transaction-wide adjacency.
This module preserves evidence; it does not implement swap/LO state mutation.
Legacy single-Swap observations remain explicitly weaker than canonical pairs.
"""
from __future__ import annotations

import base64
from collections import Counter
from dataclasses import asdict
import re

from .codec import EVENT_FIELDS, Reader, b58decode, decode_event, IdlCodec, discriminator
from .errors import Unavailable, Unsupported, ValidationError
from .model import Event, Order, PROGRAM_ID

EVENT_CPI_TAG = bytes.fromhex('e445a52e51cb9a1d')
SWAP_INSTRUCTIONS = (
    'swap', 'swap2', 'swap_exact_out', 'swap_exact_out2',
    'swap_with_price_impact', 'swap_with_price_impact2',
)
SWAP_DISCRIMINATORS = {discriminator('global', n): n for n in SWAP_INSTRUCTIONS}
SWAP_EVENTS = {'Swap', 'Swap2Evt'}
SWAP_COMMON_FIELDS = (
    'lb_pair', 'from', 'start_bin_id', 'end_bin_id', 'swap_for_y',
    'fee_bps', 'amount_in', 'amount_out', 'protocol_fee', 'host_fee',
)
_ACCOUNT_PREFIX = (
    'lb_pair', 'bin_array_bitmap_extension', 'reserve_x', 'reserve_y',
    'user_token_in', 'user_token_out', 'token_x_mint', 'token_y_mint',
    'oracle', 'host_fee_in', 'user', 'token_x_program', 'token_y_program',
)
_LOG_INVOKE = re.compile(r'^Program (\S+) invoke \[([0-9]+)\]$')
_LOG_RETURN = re.compile(r'^Program (\S+) (?:success|failed:.*)$')


def account_keys(tx: dict, meta: dict) -> list[str]:
    raw = tx['message']['accountKeys']
    keys = [k if isinstance(k, str) else k['pubkey'] for k in raw]
    parsed_expanded = bool(raw and isinstance(raw[0], dict))
    loaded = meta.get('loadedAddresses') or {}
    if not parsed_expanded:
        keys += loaded.get('writable', []) + loaded.get('readonly', [])
    if tx['message'].get('addressTableLookups') and not loaded and not parsed_expanded:
        raise Unavailable('versioned transaction address table keys not resolved')
    return keys


def _program_id(ix: dict, keys: list[str]) -> str:
    program = ix.get('programId')
    if program is None:
        index = ix.get('programIdIndex')
        if type(index) is not int or not 0 <= index < len(keys):
            raise ValidationError('instruction program index unresolved')
        program = keys[index]
    if not isinstance(program, str):
        raise ValidationError('instruction program ID unresolved')
    return program


def resolve_ix(ix: dict, keys: list[str]) -> dict:
    program = _program_id(ix, keys)
    accounts = []
    for k in ix.get('accounts', []):
        if isinstance(k, str):
            accounts.append(k)
        elif type(k) is int and 0 <= k < len(keys):
            accounts.append(keys[k])
        else:
            raise ValidationError('instruction account index unresolved')
    data = ix.get('data')
    if not isinstance(data, str):
        raise Unsupported('jsonParsed instruction cannot substitute for raw instruction bytes')
    return {'program': program, 'accounts': accounts, 'raw': b58decode(data),
            'stack_height': ix.get('stackHeight')}


def _invocations(top: dict, inner: list[dict], keys: list[str], outer: int) -> list[dict]:
    """Reconstruct the CPI tree from stackHeight; an unknown edge stays unknown.

    Paths contain the outer index followed by actual inner-list indices of all
    ancestors.  Sibling swaps in the same router instruction therefore differ.
    Non-DLMM instructions still participate in the tree (including parsed ones).
    """
    nodes, stack = [], {}
    for index, ix in [(-1, top), *enumerate(inner)]:
        node = {'inner_index': index, 'outer_index': outer, 'parent': None,
                'path': None, 'stack_height': ix.get('stackHeight')}
        height = 1 if index == -1 else ix.get('stackHeight')
        if index == -1:
            provided = ix.get('stackHeight')
            if provided is not None and (type(provided) is not int or provided != 1):
                node['ancestry_error'] = 'outer instruction stackHeight must be 1'
            else:
                node['path'] = [outer]
        elif type(height) is not int or not 2 <= height <= 64:
            node['ancestry_error'] = 'CPI stackHeight missing or invalid'
            stack = {1: nodes[0]}
        else:
            stack = {h: n for h, n in stack.items() if h < height}
            parent = stack.get(height - 1)
            if parent is None or parent['path'] is None:
                node['ancestry_error'] = 'CPI stackHeight skips an unresolved parent'
            else:
                node['parent'] = parent
                node['path'] = parent['path'] + [index]
        if type(height) is int and 1 <= height <= 64:
            stack[height] = node
        try:
            node['program'] = _program_id(ix, keys)
            # Parsed foreign instructions are relevant to ancestry, not DLMM decoding.
            if node['program'] == PROGRAM_ID:
                node['resolved'] = resolve_ix(ix, keys)
        except (Unsupported, ValidationError) as exc:
            node['decode_error'] = str(exc)
        nodes.append(node)
    return nodes


def _location(node: dict) -> dict:
    return {'instruction_index': node['outer_index'],
            'inner_instruction_index': node['inner_index'],
            'stack_height': node['stack_height'], 'invocation_path': node['path']}


def _flatten_account_names(items: list[dict]) -> list[str]:
    names = []
    for item in items:
        if 'accounts' in item:
            names.extend(_flatten_account_names(item['accounts']))
        else:
            names.append(item['name'])
    return names


def _typed_instruction_pool(node: dict | None, idl: IdlCodec | None) -> str | None:
    """Locate the *lb_pair* account by its pinned IDL account name.

    The transaction's account-key membership is insufficient: router transactions
    may invoke several independent DLMM pools. Unknown/ambiguous instruction
    layouts must return None and remain fail-closed for state mutations.
    """
    if not isinstance(node, dict) or not isinstance(node.get('resolved'), dict):
        return None
    if 'swap' in node:
        return node['swap'].get('accounts', {}).get('lb_pair')
    decoded = node.get('decoded_mutation')
    if not isinstance(decoded, dict) or idl is None:
        return None
    name = decoded.get('name')
    declarations = [d for d in idl.idl.get('instructions', []) if d.get('name') == name]
    if len(declarations) != 1:
        return None
    accounts = node['resolved']['accounts']
    names = _flatten_account_names(declarations[0].get('accounts', []))
    indices = [i for i, n in enumerate(names) if n == 'lb_pair']
    if len(indices) != 1 or len(accounts) < len(names):
        return None
    return accounts[indices[0]]


def _decode_swap(node: dict, idl: IdlCodec | None) -> dict:
    """Decode actual arguments.  Only the three fixed legacy layouts have a fallback.

    The pinned *2 layouts contain RemainingAccountsInfo; a supplied IDL must
    decode that tail completely.  Failed IDL decoding never falls back silently.
    """
    resolved = node['resolved']
    raw, accounts = resolved['raw'], resolved['accounts']
    name = SWAP_DISCRIMINATORS[raw[:8]]
    modern = name.endswith('2')
    fixed = list(_ACCOUNT_PREFIX) + (['memo_program'] if modern else []) + ['event_authority', 'program']
    # Historical synthetic tests contained only a discriminator and pool.  Keep
    # this exact shape as event-only evidence, never as decoded instruction input.
    if idl is None and name == 'swap' and len(raw) == 8 and len(accounts) == 1:
        return {'name': name, 'args': None, 'accounts': {'lb_pair': accounts[0]},
                'remaining_accounts': [], 'decode_basis': 'LEGACY_DISCRIMINATOR_ONLY'}
    if idl is not None:
        decoded = idl.decode_named('instructions', raw)
        if decoded['name'] != name:
            raise Unsupported('IDL swap name/discriminator differs from supported instruction')
        entry = next(i for i in idl.idl['instructions'] if i['name'] == name)
        if _flatten_account_names(entry.get('accounts', [])) != fixed:
            raise Unsupported('IDL swap account schema differs from supported layout')
        args = decoded['data']
        basis = 'SUPPLIED_IDL'
    else:
        if modern:
            raise Unsupported(f'{name} requires a supplied IDL for RemainingAccountsInfo')
        r = Reader(raw, 8)
        if name == 'swap':
            args = {'amount_in': r.integer(64), 'min_amount_out': r.integer(64)}
        elif name == 'swap_exact_out':
            args = {'max_in_amount': r.integer(64), 'out_amount': r.integer(64)}
        else:
            args = {'amount_in': r.integer(64)}
            tag = r.integer(8)
            if tag not in (0, 1):
                raise ValidationError('invalid price-impact active_id option tag')
            args['active_id'] = r.integer(32, True) if tag else None
            args['max_price_impact_bps'] = r.integer(16)
        if r.offset != len(raw):
            raise ValidationError(f'{name} instruction has trailing argument bytes')
        basis = 'BOUNDED_LEGACY_LAYOUT'
    expected = ({'max_in_amount': (64, False), 'out_amount': (64, False)}
                if name.startswith('swap_exact_out') else
                {'amount_in': (64, False), 'active_id': (32, True), 'max_price_impact_bps': (16, False)}
                if name.startswith('swap_with_price_impact') else
                {'amount_in': (64, False), 'min_amount_out': (64, False)})
    if set(args) != set(expected) | ({'remaining_accounts_info'} if modern else set()):
        raise ValidationError('decoded swap argument names do not match supported instruction')
    for key, (bits, signed) in expected.items():
        value = args[key]
        if key == 'active_id' and value is None:
            continue
        lower, upper = (-(1 << (bits - 1)), 1 << (bits - 1)) if signed else (0, 1 << bits)
        if type(value) is not int or not lower <= value < upper:
            raise ValidationError(f'invalid decoded swap argument {key}')
    if len(accounts) < len(fixed):
        raise ValidationError(f'{name} requires all {len(fixed)} fixed instruction accounts')
    if any(len(b58decode(key)) != 32 for key in accounts):
        raise ValidationError('swap account is not a 32-byte public key')
    named = dict(zip(fixed, accounts))
    if named['program'] != PROGRAM_ID:
        raise ValidationError('swap event-CPI program account differs from DLMM')
    # Anchor optional accounts use the program ID sentinel; zero fee is unrelated.
    if named['host_fee_in'] == PROGRAM_ID:
        named['host_fee_in'] = None
    return {'name': name, 'args': args, 'accounts': named,
            'remaining_accounts': accounts[len(fixed):], 'decode_basis': basis}


def _event_to_normalized(name: str, fields: dict) -> tuple[str, dict, dict]:
    if name in SWAP_EVENTS:
        out = {'start_bin': fields['start_bin_id'], 'end_bin': fields['end_bin_id'],
               'amount_out': fields['amount_out']}
        payload = {'amount_in': fields['amount_in'], 'swap_for_y': fields['swap_for_y'],
                   'instruction_flavor': name}
        if name == 'Swap':
            out.update({k: fields[k] for k in ('fee', 'protocol_fee', 'host_fee')})
        else:
            out.update({k: fields[k] for k in ('amount_left', 'mm_fee', 'protocol_fee',
                        'limit_order_fee', 'host_fee', 'fees_on_input', 'fees_on_token_x')})
        return 'SWAP', payload, out
    if name in ('FeeParameterUpdate', 'DynamicFeeParameterUpdate'):
        return 'FEE_PARAMETERS', {k: v for k, v in fields.items() if k != 'lb_pair'}, {}
    if name == 'LbPairCreate':
        return 'POOL_CREATE', fields, {}
    if name == 'CompositionFee':
        return 'COMPOSITION_FEE', fields, {}
    return 'OBSERVED_EVENT_REQUIRES_STATE', {'name': name, 'fields': fields}, {}


def _swap_row(owner: dict, events: list[dict], require_canonical: bool) -> tuple[dict, dict, dict]:
    """Validate one invocation atomically; return the chosen source and payload."""
    info = owner['swap']
    if owner.get('swap_error'):
        raise ValidationError(owner['swap_error'])
    if any(e.get('event_error') for e in events):
        raise ValidationError('swap invocation contains an undecodable event')
    observations = [e for e in events if e.get('event', {}).get('name') in SWAP_EVENTS]
    counts = Counter(e['event']['name'] for e in observations)
    canonical = counts == {'Swap': 1, 'Swap2Evt': 1}
    legacy = counts == {'Swap': 1} and not require_canonical
    if not canonical and not legacy:
        raise ValidationError('swap instruction requires one Swap2Evt and one compatibility Swap; '
                              f'found {dict(counts)}')
    if canonical and (info['args'] is None or owner['path'] is None or
                      any(e['path'] is None for e in observations)):
        raise ValidationError('canonical swap requires decoded arguments/accounts and proven CPI ancestry')
    rich = next((e for e in observations if e['event']['name'] == 'Swap2Evt'), None)
    compat = next(e for e in observations if e['event']['name'] == 'Swap')
    selected = rich or compat
    fields = selected['event']['data']
    if canonical:
        other = compat['event']['data']
        different = [k for k in SWAP_COMMON_FIELDS if fields[k] != other[k]]
        if different:
            raise ValidationError('Swap2Evt/Swap common fields differ: ' + ','.join(different))
    if fields['lb_pair'] != info['accounts']['lb_pair']:
        raise ValidationError('swap event pool differs from actual instruction lb_pair')
    if info['args'] is not None and fields['from'] != info['accounts']['user']:
        raise ValidationError('swap event from differs from actual instruction user')
    _, payload, observed = _event_to_normalized(selected['event']['name'], fields)
    args = info['args']
    exact_out = info['name'].startswith('swap_exact_out')
    if args is not None:
        # Event amounts are observations, not a substitute for requested inputs.
        payload.pop('amount_in')
        if not exact_out:
            payload['amount_in'] = args['amount_in']
        else:
            payload.update({'max_in_amount': args['max_in_amount'], 'out_amount': args['out_amount']})
    observed['amount_in'] = fields['amount_in']
    mode = ('CANONICAL_PAIRED' if canonical else 'LEGACY_SINGLE_SWAP'
            if args is not None else 'LEGACY_EVENT_ONLY')
    host_known = args is not None
    payload.update({
        'normalization_mode': mode, 'canonical_pair_verified': canonical,
        'instruction_name': info['name'], 'exact_out': exact_out,
        'instruction_args': args, 'instruction_decode_basis': info['decode_basis'],
        'instruction_accounts': info['accounts'], 'remaining_accounts': info['remaining_accounts'],
        'instruction_data_hex': owner['resolved']['raw'].hex(),
        'instruction_location': _location(owner),
        'host_fee_account': info['accounts'].get('host_fee_in'),
        'host_fee_present': info['accounts'].get('host_fee_in') is not None if host_known else None,
        'input_evidence': 'ACTUAL_INSTRUCTION_ARGS' if args is not None else 'LEGACY_EVENT_ONLY',
        'event_sources': [{'name': e['event']['name'], **_location(e)} for e in observations],
        'compatibility_swap': compat['event']['data'] if canonical else None,
    })
    return selected, payload, observed


def normalize_block(slot: int, block: dict, pools: set[str], *, raw_hash: str | None,
                    idl: IdlCodec | None = None, registry_entry: dict | None = None,
                    clock_unix_timestamp: int | None = None,
                    require_canonical_swaps: bool = False) -> dict:
    """Normalize observations; blockers always disqualify complete reconstruction.

    D1 callers must request require_canonical_swaps=True.  A compatibility run
    never reports strict_canonical_pass, even if every observed swap was paired.
    This is a normalization check, not SDK/account/historical execution parity.
    """
    if block.get('blockTime') is None:
        raise Unavailable('blockTime missing; cannot time-align economics')
    if type(require_canonical_swaps) is not bool:
        raise ValidationError('require_canonical_swaps must be boolean')
    rows, blockers, failed = [], [], []
    suppressed_swap_rows = 0
    txs = block.get('transactions')
    if txs is None:
        raise Unavailable('getBlock full transaction list required')
    binding = registry_entry or {}
    # A selected IDL must not erase recognition of other known DLMM mutations.
    known_event_discriminators = {discriminator('event', name) for name in EVENT_FIELDS}
    if idl is not None:
        known_event_discriminators.update(
            bytes(e.get('discriminator', discriminator('event', e['name'])))
            for e in idl.idl.get('events', []))

    for tx_index, item in enumerate(txs):
        tx, meta = item.get('transaction'), item.get('meta')
        if not isinstance(tx, dict) or meta is None:
            raise Unavailable('raw JSON transaction and metadata required')
        signature = tx.get('signatures', [None])[0]
        if not signature:
            raise ValidationError('missing transaction signature')
        keys = account_keys(tx, meta)
        relevant = pools.intersection(keys)
        if not relevant:
            continue
        if meta.get('err') is not None:
            failed.append({'slot': slot, 'transaction_index': tx_index, 'signature': signature,
                           'fee_lamports': meta.get('fee'), 'error': meta['err']})
            continue
        transaction_row_start, transaction_blocker_start = len(rows), len(blockers)

        def block_at(node: dict, reason: str, **detail):
            blockers.append({'signature': signature, 'instruction': node['outer_index'],
                             'inner_instruction': node['inner_index'], 'reason': reason,
                             'blocker_kind': 'NORMALIZATION_INTEGRITY',
                             'resolution_policy': 'RENORMALIZE_OR_REJECT', **detail})

        def append_row(node: dict, pool: str, kind: str, payload: dict, observed: dict):
            e = Event(Order(slot, tx_index, node['outer_index'], node['inner_index']),
                      block['blockTime'] * 1000, signature, pool, kind,
                      program_version=binding.get('version'), binary_sha256=binding.get('binary_sha256'),
                      idl_hash=binding.get('idl_sha256'), clock_unix_timestamp=clock_unix_timestamp,
                      clock_source='CAPTURED_SYSVAR' if clock_unix_timestamp is not None else None,
                      observed=observed, payload=payload, raw_sha256=raw_hash)
            rows.append(asdict(e))

        inner = {}
        for entry in meta.get('innerInstructions') or []:
            index = entry.get('index')
            if type(index) is not int or not 0 <= index < len(tx['message']['instructions']):
                raise ValidationError('inner instruction outer index unresolved')
            if index in inner:
                raise ValidationError('duplicate inner instruction group')
            inner[index] = entry['instructions']
        located_raw = Counter()
        for outer_index, top in enumerate(tx['message']['instructions']):
            nodes = _invocations(top, inner.get(outer_index, []), keys, outer_index)
            mutations, event_nodes = [], []
            for node in nodes:
                if node.get('decode_error'):
                    block_at(node, node['decode_error'])
                    continue
                if node.get('program') != PROGRAM_ID:
                    continue
                raw = node['resolved']['raw']
                if raw.startswith(EVENT_CPI_TAG):
                    event_nodes.append(node)
                    try:
                        node['event'] = decode_event(raw[8:], idl)
                        located_raw[raw[8:]] += 1
                    except (Unsupported, ValidationError, KeyError, TypeError) as exc:
                        node['event_error'] = str(exc)
                        block_at(node, 'event decoding failed: ' + str(exc))
                else:
                    mutations.append(node)
                    node['owned_events'] = []
                    name = SWAP_DISCRIMINATORS.get(raw[:8])
                    if name:
                        try:
                            node['swap'] = _decode_swap(node, idl)
                        except (Unsupported, ValidationError, KeyError, TypeError, StopIteration) as exc:
                            node['swap_error'] = str(exc)
                            # Preserve recognition and pool attribution, but never a valid row.
                            node['swap'] = {'name': name, 'args': None,
                                            'accounts': {'lb_pair': next(iter(node['resolved']['accounts']), None)}}
                    else:
                        decoded = None
                        if idl is not None:
                            try:
                                decoded = idl.decode_named('instructions', raw)
                            except (Unsupported, ValidationError):
                                pass
                        node['decoded_mutation'] = decoded

            # The compatibility escape is limited to ONE legacy event under ONE
            # top-level swap.  Never use it to join a rich pair or multiple CPIs.
            legacy_owner = (mutations[0] if not require_canonical_swaps and len(mutations) == 1
                            and mutations[0]['inner_index'] == -1 and 'swap' in mutations[0]
                            and len(event_nodes) == 1 and event_nodes[0].get('event', {}).get('name') == 'Swap'
                            else None)
            for node in event_nodes:
                owner = node['parent']
                if owner is None and legacy_owner is not None:
                    owner = legacy_owner
                if owner is not None and 'owned_events' in owner:
                    owner['owned_events'].append(node)
                else:
                    block_at(node, 'event has no unambiguous owning DLMM instruction')
                event = node.get('event')
                if not event or event['name'] in SWAP_EVENTS:
                    continue
                fields, name = event['data'], event['name']
                declared_pool = fields.get('lb_pair')
                owner_pool = _typed_instruction_pool(owner, idl)
                owner_accounts = (owner.get('resolved', {}).get('accounts', [])
                                  if isinstance(owner, dict) else [])
                # A decoded event from another pool in the same router tx is
                # irrelevant ONLY if its owning instruction proves that scope.
                # Do not infer pool ownership from membership in tx account keys.
                if (declared_pool is not None and owner_pool is not None
                        and declared_pool != owner_pool):
                    block_at(node, 'event/owning-instruction lb_pair disagreement')
                    continue
                if ((declared_pool is not None and declared_pool not in pools)
                        or (declared_pool is None and owner_pool is not None
                            and owner_pool not in pools)):
                    if (owner_pool is not None and owner_pool not in pools
                            and not pools.intersection(owner_accounts)):
                        continue  # Independently proved non-target instruction/event.
                    block_at(node, 'foreign event cannot be safely scoped to its owner')
                    continue
                pool = declared_pool if declared_pool is not None else owner_pool
                if pool is None and len(relevant) == 1:
                    pool = next(iter(relevant))
                if pool not in pools:
                    block_at(node, 'event pool attribution unresolved')
                    continue
                kind, payload, observed = _event_to_normalized(name, fields)
                append_row(node, pool, kind, payload, observed)

            for node in mutations:
                if 'swap' in node:
                    pool = node['swap']['accounts']['lb_pair']
                    # A decoded swap of another pool is outside this requested stream.
                    # An event claiming our pool is still a mismatch, not silently dropped.
                    claims_target = any(e.get('event', {}).get('data', {}).get('lb_pair') in pools
                                        for e in node['owned_events'])
                    if pool not in pools and not claims_target and not node.get('swap_error'):
                        continue
                    try:
                        selected, payload, observed = _swap_row(node, node['owned_events'], require_canonical_swaps)
                        if pool not in pools:
                            raise ValidationError('swap instruction pool attribution unresolved')
                        append_row(selected, pool, 'SWAP', payload, observed)
                    except (Unsupported, ValidationError, KeyError, TypeError) as exc:
                        block_at(node, str(exc), mutation=node['swap']['name'],
                                 blocker_kind='SWAP_CANONICALIZATION')
                else:
                    decoded = node.get('decoded_mutation')
                    name = decoded['name'] if decoded else None
                    mutation_pool = _typed_instruction_pool(node, idl)
                    claimed_target = any(e.get('event', {}).get('data', {}).get('lb_pair') in pools
                                         for e in node['owned_events'])
                    # Only a successfully decoded, IDL-named *lb_pair* can prove
                    # that a mutation belongs exclusively to another pool.
                    # An unknown instruction, or any overlap with the target
                    # pool's account list, remains a hard state-coverage blocker.
                    if mutation_pool is not None and mutation_pool not in pools:
                        if claimed_target or pools.intersection(node['resolved']['accounts']):
                            block_at(node, 'foreign mutation unexpectedly references the target pool')
                        continue
                    harmless = {'initialize_position', 'initialize_position2', 'close_position', 'close_position2'}
                    if name in harmless:
                        continue
                    block_at(node, 'mutation requires independently observed state transition or SVM',
                             blocker_kind='STATE_MUTATION', resolution_policy='OBSERVED_STATE_OR_SVM',
                             mutation={'name': name, 'raw_discriminator': node['resolved']['raw'][:8].hex(),
                                       'accounts': node['resolved']['accounts'], 'decoded': decoded})

        # A mirrored log is diagnostic evidence for the SAME CPI event.  An
        # unmatched known event log is a coverage blocker even if other CPIs exist.
        log_seen = Counter()
        log_programs = []
        for log in meta.get('logMessages') or []:
            invocation = _LOG_INVOKE.fullmatch(log)
            if invocation:
                program, depth = invocation.group(1), int(invocation.group(2))
                if 1 <= depth <= 64:
                    log_programs = log_programs[:depth - 1]
                    log_programs.extend([None] * (depth - 1 - len(log_programs)))
                    log_programs.append(program)
                else:
                    log_programs = []
                continue
            returned = _LOG_RETURN.fullmatch(log)
            if returned:
                if log_programs and log_programs[-1] == returned.group(1):
                    log_programs.pop()
                else:
                    log_programs = []
                continue
            if not log.startswith('Program data: '):
                continue
            emitting_program = log_programs[-1] if log_programs else None
            if emitting_program is not None and emitting_program != PROGRAM_ID:
                continue
            try:
                raw = base64.b64decode(log[len('Program data: '):], validate=True)
            except (ValueError, TypeError):
                if require_canonical_swaps:
                    blockers.append({'signature': signature, 'reason': 'malformed Program data log',
                                     'blocker_kind': 'NORMALIZATION_INTEGRITY',
                                     'resolution_policy': 'RENORMALIZE_OR_REJECT'})
                continue
            if (emitting_program != PROGRAM_ID and raw[:8] not in known_event_discriminators
                    and raw not in located_raw):
                if require_canonical_swaps:
                    blockers.append({'signature': signature,
                                     'reason': 'unmatched Program data log has unresolved program/event ownership',
                                     'blocker_kind': 'NORMALIZATION_INTEGRITY',
                                     'resolution_policy': 'RENORMALIZE_OR_REJECT'})
                continue
            log_seen[raw] += 1
            if log_seen[raw] > located_raw[raw]:
                blockers.append({'signature': signature,
                                 'reason': 'log-only or extra DLMM event: instruction location unresolved',
                                 'blocker_kind': 'NORMALIZATION_INTEGRITY',
                                 'resolution_policy': 'RENORMALIZE_OR_REJECT'})

        # Solana transactions are atomic.  Once an event/argument/ownership gap
        # is found, a subset of its swaps must not escape as a usable canonical
        # stream.  Keep other write observations and all blockers for diagnosis;
        # an observed-state proof can address only explicitly classified mutations.
        if any(b.get('resolution_policy') == 'RENORMALIZE_OR_REJECT'
               for b in blockers[transaction_blocker_start:]):
            transaction_rows = rows[transaction_row_start:]
            suppressed_swap_rows += sum(r['kind'] == 'SWAP' for r in transaction_rows)
            rows[transaction_row_start:] = [r for r in transaction_rows if r['kind'] != 'SWAP']

    rows.sort(key=lambda r: Order(**r['order']))
    swaps = [r for r in rows if r['kind'] == 'SWAP']
    canonical_count = sum(r['payload']['canonical_pair_verified'] for r in swaps)
    return {'events': rows, 'blockers': blockers, 'failed_transactions': failed,
            'slot': slot, 'transaction_order_basis': 'GETBLOCK_LIST_INDEX',
            'require_canonical_swaps': require_canonical_swaps,
            'canonical_swap_count': canonical_count, 'legacy_swap_count': len(swaps) - canonical_count,
            'suppressed_swap_rows': suppressed_swap_rows,
            'strict_canonical_pass': bool(require_canonical_swaps and swaps and not blockers
                                          and canonical_count == len(swaps)),
            'fee_cost_note': 'failed tx fees are observed network costs, not automatically our LP costs'}
