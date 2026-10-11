"""Factual, ordered economic-state replay with explicit evidence levels."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Iterator

from .errors import Unavailable, Unsupported, ValidationError
from .mechanics import quote_exact_in
from .model import (Event, PoolState, Bin, PROGRAM_ID, Level, Order, economic_state_hash,
                    ECONOMIC_STATE_HASH_SCHEMA)
from .util import digest, hash_file, read_json, safe_path


MM_ACCOUNT_PARITY_SCOPE = 'MM_RESERVES_SUPPLY_PRICE_ACTIVE_FEE_PARAMETERS_REFERENCE'
MM_ACCOUNT_PARITY_EXCLUDED = (
    'fee_growth_rounding', 'oracle_ring', 'reward_counters', 'full_account_bytes',
    'limit_order_fulfilled_amounts', 'limit_order_fee_counters', 'limit_order_order_age',
    'limit_order_total_processing_amount_and_side', 'reward_initialization_mutation',
    'protocol_fee_balance_mutation', 'bin_array_bitmap_mutation', 'last_updated_at_mutation',
)
MM_UNSUPPORTED_SEMANTICS = (
    'LIMIT_ORDER_FILL_AND_STATE_MUTATION', 'EXACT_OUT', 'HOST_REFERRAL_FEE_SPLIT',
    'PRICE_IMPACT_INSTRUCTION_BOUNDS', 'PROTOCOL_REWARD_BITMAP_COUNTER_MUTATION',
)


class VersionRegistry:
    def __init__(self, entries: list[dict], root: Path, *, synthetic: bool = False):
        self.entries, self.root, self.synthetic = entries, root, synthetic
        intervals = sorted(entries, key=lambda e: e['start_slot'])
        last_end = -1
        for entry in intervals:
            if entry['start_slot'] < last_end or entry['end_slot'] <= entry['start_slot']:
                raise ValidationError('overlapping/invalid program-version intervals')
            last_end = entry['end_slot']
            if entry.get('program_id') != PROGRAM_ID:
                raise ValidationError('registry program mismatch')
            if synthetic:
                if entry.get('provenance') != 'SYNTHETIC_FIXTURE':
                    raise ValidationError('synthetic registry must say so')
                continue
            if not entry.get('version') or entry.get('version') == 'UNKNOWN':
                raise Unavailable('historical program semantic version UNKNOWN')
            for name, key in (('binary_file', 'binary_sha256'), ('idl_file', 'idl_sha256'),
                              ('binding_evidence_file', 'binding_evidence_sha256')):
                if not entry.get(name) or not entry.get(key):
                    raise Unavailable(f'program registry requires {name} and {key}')
                p = safe_path(root, entry[name])
                if not p.exists():
                    raise Unavailable(f'program evidence missing: {entry[name]}')
                if hash_file(p) != entry[key]:
                    raise ValidationError(f'program evidence hash mismatch: {name}')
            idl = read_json(safe_path(root, entry['idl_file']))
            if idl.get('address') != PROGRAM_ID or idl.get('metadata', {}).get('version') != entry['version']:
                raise ValidationError('registry and IDL version/address disagree')

    def resolve(self, slot: int) -> dict:
        hits = [e for e in self.entries if e['start_slot'] <= slot < e['end_slot']]
        if len(hits) != 1:
            raise Unavailable(f'no unique historical program binding for slot {slot}')
        entry = hits[0]
        if entry.get('model') != 'MM_V012':
            raise Unsupported(f'direct model not supported: {entry.get("model")}')
        return entry

    def check_event(self, event: Event) -> dict:
        entry = self.resolve(event.order.slot)
        for ev_key, reg_key in (('program_version','version'), ('binary_sha256','binary_sha256'), ('idl_hash','idl_sha256')):
            value = getattr(event, ev_key)
            if value is None:
                raise Unavailable(f'event lacks explicit {ev_key}; current version is not a fallback')
            if value != entry.get(reg_key):
                raise ValidationError(f'event program binding mismatch: {ev_key}')
        return entry


def ordered_events(events: Iterable[Event]) -> Iterator[Event]:
    """Streaming verifier; does not silently sort an incorrect/missing chain order."""
    previous = None
    previous_ms = None
    for event in events:
        if previous is not None and event.order <= previous:
            raise ValidationError('non-increasing Solana order (slot, tx-index, ix-index, inner-index)')
        if previous_ms is not None and event.block_time_ms < previous_ms:
            raise Unsupported('block time regresses; reference alignment requires explicit clock policy')
        previous, previous_ms = event.order, event.block_time_ms
        yield event


def economic_projection(state: PoolState) -> dict:
    """Scope-specific ACCOUNT_PARITY. Not full byte-for-byte program parity."""
    return {'active_id': state.active_id, 'bin_step': state.bin_step,
            'parameters': asdict(state.parameters), 'variable': asdict(state.variable),
            'bins': {str(i): {k: getattr(b,k) for k in ('amount_x','amount_y','price','liquidity_supply',
                                                      'open_order_amount','processed_order_remaining_amount')}
                     for i,b in sorted(state.bins.items())}}


def compare_states(actual: PoolState, expected: PoolState) -> dict:
    if actual.pool != expected.pool:
        raise ValidationError('checkpoint pool mismatch')
    a, b = economic_projection(actual), economic_projection(expected)
    if digest(a) != digest(b):
        fields = [k for k in a if a[k] != b[k]]
        raise ValidationError('account checkpoint mismatch: ' + ','.join(fields))
    return {'scope': MM_ACCOUNT_PARITY_SCOPE, 'equal': True,
            'excluded': list(MM_ACCOUNT_PARITY_EXCLUDED)}


class Replay:
    def __init__(self, initial: PoolState, registry: VersionRegistry, *, boundary: str = 'END_SLOT',
                 allow_partial_clock: bool = False):
        initial.validate()
        self.initial, self.state, self.registry = deepcopy(initial), deepcopy(initial), registry
        if boundary != 'END_SLOT':
            raise Unsupported('only explicitly end-slot starting snapshots are currently accepted')
        self.allow_partial_clock = allow_partial_clock
        self.scope_level = Level.PARTIAL_RECONSTRUCTION
        self.checkpoints = 0
        self.event_count = 0
        self.partial_clock_events = 0
        self.max_token_residual = 0

    def _bind(self, event: Event):
        if event.pool != self.state.pool or event.program_id != self.state.program_id:
            raise ValidationError('event pool/program differs from replay state')
        if event.order.slot <= self.initial.slot:
            raise ValidationError('event is at/before end-slot starting snapshot')
        if event.data_kind != self.initial.data_kind:
            raise ValidationError('synthetic/current/historical data kind mixing')
        self.registry.check_event(event)
        if event.pre_state_hash is not None and event.pre_state_hash != self.state.economic_hash():
            raise ValidationError('event pre-state continuity hash mismatch')

    def _clock(self, event: Event) -> int:
        if event.clock_unix_timestamp is not None and event.clock_source in (
                'CAPTURED_SYSVAR', 'ARCHIVAL_SYSVAR', 'SVM_CAPTURE', 'SYNTHETIC_FIXTURE'):
            return event.clock_unix_timestamp
        if self.allow_partial_clock:
            self.partial_clock_events += 1
            return event.block_time_ms // 1000
        raise Unavailable('historical Clock sysvar unavailable; blockTime is not silently exact Clock')

    def step(self, event: Event) -> dict:
        self._bind(event)
        before = self.state
        if event.kind != 'SWAP':
            self.state = deepcopy(self.state)
        fees, fill_rows = [], []
        quote_summary = None
        if event.kind == 'SWAP':
            payload = event.payload
            if (payload.get('exact_out') or
                    payload.get('instruction_name') in ('swap_exact_out', 'swap_exact_out2')):
                raise Unsupported('exact-out factual replay requires SDK/SVM adapter')
            if payload.get('instruction_name') in ('swap_with_price_impact', 'swap_with_price_impact2'):
                raise Unsupported('price-impact swap bounds require an instruction-semantics adapter')
            canonical = payload.get('canonical_pair_verified') is True
            if event.data_kind == 'HISTORICAL':
                if not canonical or payload.get('normalization_mode') != 'CANONICAL_PAIRED':
                    raise Unsupported('historical swap requires a verified canonical instruction/event pair')
                for b in self.state.bins.values():
                    if any(getattr(b, field) is None for field in
                           ('open_order_amount', 'processed_order_remaining_amount',
                            'total_processing_order_amount')):
                        raise Unavailable('historical MM replay cannot treat unknown LO liquidity as zero')
            if canonical and payload.get('host_fee_present') is not False:
                raise Unsupported('canonical host account present/unknown requires verified referral split adapter')
            timestamp = self._clock(event)
            q = quote_exact_in(self.state, int(event.payload['amount_in']),
                               event.payload['swap_for_y'], timestamp, slot=event.order.slot)
            expected = event.observed
            for field, actual in (('amount_in',q.amount_in), ('amount_out',q.amount_out), ('start_bin',q.start_bin), ('end_bin',q.end_bin),
                                  ('fee',q.fee), ('amount_left',q.amount_left)):
                if field in expected and expected[field] != actual:
                    raise ValidationError(f'swap observed {field}={expected[field]} vs replay {actual}')
            # Host is a sub-allocation of protocol, never an additional LP haircut.
            if expected.get('host_fee',0):
                raise Unsupported('nonzero host fee requires event-version verified protocol split adapter')
            if 'protocol_fee' in expected and expected['protocol_fee'] != q.protocol_fee:
                raise ValidationError('protocol fee parity failed')
            if expected.get('limit_order_fee',0):
                raise Unsupported('event contains limit-order fee; MM-only replay prohibited')
            if 'mm_fee' in expected and expected['mm_fee'] != q.fee-q.protocol_fee:
                raise ValidationError('Swap2 MM fee parity failed')
            on_input = self.state.parameters.collect_fee_mode == 0 or not event.payload['swap_for_y']
            if 'fees_on_input' in expected and expected['fees_on_input'] != on_input:
                raise ValidationError('fee mode event parity failed')
            if 'fees_on_token_x' in expected and expected['fees_on_token_x'] != (q.fills[0].fee_token=='x'):
                raise ValidationError('fee currency parity failed')
            self.state = q.state
            # Explicit, checkpoint-tested reconstruction rule. Official quote is
            # stateless; an unknown deployed timestamp rule will FAIL checkpoints.
            self.state.variable.last_update_timestamp = timestamp
            for f in q.fills:
                fill_rows.append(asdict(f))
                fees.append({'bin_id': f.bin_id, 'token': f.fee_token, 'lp_base': f.lp_base,
                             'lp_variable': f.lp_variable, 'protocol': f.protocol_fee,
                             'trader_total': f.trader_fee})
                self.max_token_residual = max(self.max_token_residual,
                                              abs(f.token_conservation_x),abs(f.token_conservation_y))
            quote_summary = q.summary()
        elif event.kind == 'FEE_PARAMETERS':
            if not event.payload:
                raise ValidationError('empty fee parameter update')
            for key, val in event.payload.items():
                if key not in self.state.parameters.__dataclass_fields__:
                    raise Unsupported(f'unknown parameter update {key}')
                setattr(self.state.parameters, key, val)
            self.state.parameters.__post_init__()
        elif event.kind == 'OBSERVED_LIQUIDITY':
            # Explicitly observed per-bin deltas, not a guessed split of aggregate
            # add/remove event amounts. Raw account evidence is checked by bundle.
            payload = event.payload
            if not payload.get('pre_accounts_evidence') or not payload.get('post_accounts_evidence'):
                raise Unavailable('liquidity transition lacks independent pre/post account evidence')
            dx = dy = 0
            for item in payload.get('bins_after', []):
                b = Bin(**item)
                b.validate()
                old = self.state.bins.get(b.bin_id)
                dx += b.amount_x - (old.amount_x if old else 0)
                dy += b.amount_y - (old.amount_y if old else 0)
                self.state.bins[b.bin_id] = b
            if not payload.get('bins_after'):
                raise ValidationError('empty observed liquidity transition')
            fees = payload.get('fees', [])
            for fee in fees:
                if fee.get('token') not in ('x','y'):
                    raise ValidationError('unknown liquidity fee token')
                if any(type(fee.get(k,0)) is not int or fee.get(k,0)<0 for k in
                       ('lp_base','lp_variable','lp_composition','protocol','trader_total')):
                    raise ValidationError('invalid liquidity fee amounts')
                if fee['trader_total']!=sum(fee.get(k,0) for k in ('lp_base','lp_variable','lp_composition','protocol')):
                    raise ValidationError('liquidity fee allocation does not conserve tokens')
            fee_x = sum(f['trader_total'] for f in fees if f['token']=='x')
            fee_y = sum(f['trader_total'] for f in fees if f['token']=='y')
            # external_flow is into principal+fee vaults, positive for addition.
            for k, actual in (('x',dx+fee_x),('y',dy+fee_y)):
                if f'external_flow_{k}' not in payload:
                    raise Unavailable('liquidity transition has no observed token flow')
                if actual != payload[f'external_flow_{k}']:
                    raise ValidationError('liquidity token conservation failed')
            if payload.get('active_id_after', self.state.active_id) != self.state.active_id:
                raise Unsupported('liquidity event also changes active bin; requires richer SVM transition')
        elif event.kind == 'COMPOSITION_FEE':
            # Usually part of the same add. Must be joined to the observed
            # liquidity transition to avoid double-counting.
            raise Unsupported('standalone composition event must join its observed liquidity transition')
        else:
            raise Unsupported(f'no factual state transition for {event.kind}')
        self.state.slot = event.order.slot
        self.state.validate()
        if event.post_state_hash is not None and event.post_state_hash != self.state.economic_hash():
            raise ValidationError('event post-state continuity hash mismatch')
        self.event_count += 1
        return {'event_id': event.id, 'event': asdict(event), 'pre': before.to_dict(),
                'post': self.state.to_dict(), 'fills': fill_rows, 'fees': fees,
                'quote': quote_summary, 'token_conservation_residual': 0,
                'state_continuity': 'PASS'}

    def run(self, events: Iterable[Event], checkpoints: dict[str,PoolState] | None = None) -> Iterator[dict]:
        checkpoints = checkpoints or {}
        for event in ordered_events(events):
            row = self.step(event)
            if event.id in checkpoints:
                row['checkpoint'] = compare_states(self.state, checkpoints[event.id])
                self.checkpoints += 1
                if not self.partial_clock_events:
                    self.scope_level = Level.ACCOUNT_PARITY
            yield row
        if not self.event_count:
            raise Unavailable('zero factual events is not a validation pass')

    def report(self) -> dict:
        return {'events': self.event_count, 'checkpoints': self.checkpoints,
                'validation_level': str(self.scope_level), 'partial_clock_events': self.partial_clock_events,
                'max_token_conservation_residual': self.max_token_residual,
                'account_parity_scope': MM_ACCOUNT_PARITY_SCOPE,
                'account_parity_excluded': list(MM_ACCOUNT_PARITY_EXCLUDED),
                'mechanics_scope': 'MM_ONLY_EXACT_IN',
                'unsupported_semantics': list(MM_UNSUPPORTED_SEMANTICS),
                'economic_hash_schema': ECONOMIC_STATE_HASH_SCHEMA,
                'full_program_byte_parity': 'UNAVAILABLE',
                'data_kind': self.initial.data_kind}


def compact_trace(row:dict)->dict:
    """Keep only changed state fields/bins; avoid O(events * total_bins) storage."""
    pre,post=row['pre'],row['post']
    header={k:v for k,v in post.items() if k!='bins' and pre.get(k)!=v}
    bins={str(k):v for k,v in post['bins'].items() if pre['bins'].get(k,pre['bins'].get(str(k)))!=v}
    # Input to this function may use int or JSON-normalized str keys.
    deleted=[str(k) for k in pre['bins'] if k not in post['bins'] and str(k) not in post['bins']]
    out={k:v for k,v in row.items() if k not in ('pre','post')}
    out.update(trace_format='COMPACT_STATE_DELTAS_V2',
               economic_hash_schema=ECONOMIC_STATE_HASH_SCHEMA,
               state_patch={'header':header,'bins':bins,'deleted':deleted},
               pre_economic_hash=economic_dict_hash(pre),
               post_economic_hash=economic_dict_hash(post))
    return out


def economic_dict_hash(d:dict)->str:
    return economic_state_hash(d)


def expand_traces(rows:Iterable[dict],initial:PoolState,*,verify_hashes:bool=True)->Iterator[dict]:
    state=initial.to_dict()
    state['bins']={str(k):v for k,v in state['bins'].items()}
    for row in rows:
        if (row.get('trace_format') != 'COMPACT_STATE_DELTAS_V2' or
                row.get('economic_hash_schema') != ECONOMIC_STATE_HASH_SCHEMA):
            raise Unsupported('compact trace/hash schema is not V2; frozen legacy traces are not automatically rehashed')
        if verify_hashes and economic_dict_hash(state)!=row['pre_economic_hash']:
            raise ValidationError('compact trace pre-state continuity mismatch')
        pre=state
        patch=row['state_patch']
        state={**state,**patch['header'],'bins':state['bins'].copy()}
        for k in patch.get('deleted',[]):
            state['bins'].pop(str(k),None)
        state['bins'].update({str(k):v for k,v in patch['bins'].items()})
        if verify_hashes and economic_dict_hash(state)!=row['post_economic_hash']:
            raise ValidationError('compact trace post-state continuity mismatch')
        yield {**{k:v for k,v in row.items() if k != 'state_patch'},'pre':pre,'post':state}
