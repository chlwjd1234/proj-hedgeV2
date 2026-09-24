"""Finalized block order and Anchor CPI event normalization.

Raw logs are NOT silently substituted for instruction-located events. Transactions
with missing event location, unrecognized mutations, or failed execution remain
visible as blockers/diagnostics. No signature-lexicographic transaction ordering.
"""
from __future__ import annotations
import base64
from dataclasses import asdict
from .codec import b58decode, decode_event, IdlCodec, discriminator
from .errors import Unavailable, Unsupported, ValidationError
from .model import Event, Order, PROGRAM_ID

EVENT_CPI_TAG = bytes.fromhex('e445a52e51cb9a1d')


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


def resolve_ix(ix: dict, keys: list[str]) -> dict:
    program = ix.get('programId')
    if program is None:
        index = ix.get('programIdIndex')
        if not isinstance(index, int) or not 0 <= index < len(keys):
            raise ValidationError('instruction program index unresolved')
        program = keys[index]
    accounts = []
    for k in ix.get('accounts', []):
        if isinstance(k, str):
            accounts.append(k)
        elif isinstance(k, int) and 0 <= k < len(keys):
            accounts.append(keys[k])
        else:
            raise ValidationError('instruction account index unresolved')
    data = ix.get('data')
    if not isinstance(data, str):
        raise Unsupported('jsonParsed instruction cannot substitute for raw instruction bytes')
    return {'program': program, 'accounts': accounts, 'raw': b58decode(data),
            'stack_height': ix.get('stackHeight')}


def _event_to_normalized(name: str, fields: dict) -> tuple[str, dict, dict]:
    if name in ('Swap', 'Swap2Evt'):
        out = {'start_bin': fields['start_bin_id'], 'end_bin': fields['end_bin_id'],
               'amount_out': fields['amount_out']}
        payload = {'amount_in': fields['amount_in'], 'swap_for_y': fields['swap_for_y'],
                   'instruction_flavor': name}
        if name == 'Swap':
            out.update({'fee': fields['fee'], 'protocol_fee': fields['protocol_fee'],
                        'host_fee': fields['host_fee']})
        else:
            out.update({'amount_left': fields['amount_left'], 'mm_fee': fields['mm_fee'],
                        'protocol_fee': fields['protocol_fee'], 'limit_order_fee': fields['limit_order_fee'],
                        'host_fee': fields['host_fee'], 'fees_on_input': fields['fees_on_input'],
                        'fees_on_token_x': fields['fees_on_token_x']})
        return 'SWAP', payload, out
    if name in ('FeeParameterUpdate', 'DynamicFeeParameterUpdate'):
        return 'FEE_PARAMETERS', {k: v for k, v in fields.items() if k != 'lb_pair'}, {}
    if name == 'LbPairCreate':
        return 'POOL_CREATE', fields, {}
    if name == 'CompositionFee':
        return 'COMPOSITION_FEE', fields, {}
    # Events such as AddLiquidity are aggregate amounts, NOT per-bin transitions.
    return 'OBSERVED_EVENT_REQUIRES_STATE', {'name': name, 'fields': fields}, {}


def normalize_block(slot: int, block: dict, pools: set[str], *, raw_hash: str | None,
                    idl: IdlCodec | None = None, registry_entry: dict | None = None,
                    clock_unix_timestamp: int | None = None) -> dict:
    if block.get('blockTime') is None:
        raise Unavailable('blockTime missing; cannot time-align economics')
    rows, blockers, failed = [], [], []
    txs = block.get('transactions')
    if txs is None:
        raise Unavailable('getBlock full transaction list required')
    binding = registry_entry or {}
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
        inner = {x['index']: x['instructions'] for x in meta.get('innerInstructions') or []}
        located_count = 0
        for outer_index, top in enumerate(tx['message']['instructions']):
            sequence = [(-1, top)] + list(enumerate(inner.get(outer_index, [])))
            outer_events = []
            dlmm_mutations = []
            for inner_index, ix in sequence:
                try:
                    resolved = resolve_ix(ix, keys)
                except Unsupported as exc:
                    blockers.append({'signature': signature, 'instruction': outer_index,
                                     'reason': str(exc)})
                    continue
                if resolved['program'] != PROGRAM_ID:
                    continue
                raw = resolved['raw']
                if raw.startswith(EVENT_CPI_TAG):
                    try:
                        event = decode_event(raw[8:], idl)
                    except (Unsupported, ValidationError) as exc:
                        blockers.append({'signature': signature, 'instruction': outer_index,
                                         'inner_instruction': inner_index, 'reason': str(exc)})
                        continue
                    fields, name = event['data'], event['name']
                    pool = fields.get('lb_pair')
                    if not pool and len(relevant) == 1:
                        pool = next(iter(relevant))
                    if pool not in pools:
                        blockers.append({'signature': signature, 'reason': 'event pool attribution unresolved'})
                        continue
                    kind, payload, observed = _event_to_normalized(name, fields)
                    e = Event(Order(slot, tx_index, outer_index, inner_index), block['blockTime']*1000,
                              signature, pool, kind, program_version=binding.get('version'),
                              binary_sha256=binding.get('binary_sha256'), idl_hash=binding.get('idl_sha256'),
                              clock_unix_timestamp=clock_unix_timestamp,
                              clock_source='CAPTURED_SYSVAR' if clock_unix_timestamp is not None else None,
                              observed=observed, payload=payload, raw_sha256=raw_hash)
                    outer_events.append(e)
                    located_count += 1
                else:
                    name = None
                    decoded = None
                    if idl:
                        try:
                            decoded = idl.decode_named('instructions', raw)
                            name = decoded['name']
                        except (Unsupported, ValidationError):
                            pass
                    dlmm_mutations.append({'name': name, 'raw_discriminator': raw[:8].hex(),
                                           'inner_instruction': inner_index, 'decoded': decoded,
                                           'accounts': resolved['accounts']})
            swap_instruction_count=sum(m['name'] in ('swap','swap2') or m['raw_discriminator'] in {
                discriminator('global',n).hex() for n in ('swap','swap2')} for m in dlmm_mutations)
            if swap_instruction_count != sum(e.kind=='SWAP' for e in outer_events):
                blockers.append({'signature':signature,'instruction':outer_index,
                                 'reason':'swap instruction/event count mismatch; missing event cannot be assumed harmless'})
            # Event vs instruction coverage: an unknown mutation is not cleared
            # just because some *other* swap in this transaction emitted an event.
            harmless = {'initialize_position', 'initialize_position2', 'close_position', 'close_position2'}
            for m in dlmm_mutations:
                name = m['name']
                disc = m['raw_discriminator']
                exact_in = name in ('swap', 'swap2') or disc in {
                    discriminator('global', n).hex() for n in ('swap','swap2')}
                if exact_in and any(e.kind == 'SWAP' for e in outer_events):
                    continue
                if name in harmless:
                    continue
                blockers.append({'signature': signature, 'instruction': outer_index,
                                 'inner_instruction': m['inner_instruction'], 'mutation': m,
                                 'reason': 'mutation requires independently observed state transition or SVM'})
            rows.extend(asdict(e) for e in outer_events)
        logs = [x for x in meta.get('logMessages') or [] if x.startswith('Program data: ')]
        if logs and not located_count:
            blockers.append({'signature': signature,
                             'reason': 'log-only events: instruction location unresolved; retained raw, not reordered'})
    rows.sort(key=lambda r: Order(**r['order']))
    return {'events': rows, 'blockers': blockers, 'failed_transactions': failed,
            'slot': slot, 'transaction_order_basis': 'GETBLOCK_LIST_INDEX',
            'fee_cost_note': 'failed tx fees are observed network costs, not automatically our LP costs'}
