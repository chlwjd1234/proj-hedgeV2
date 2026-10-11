"""Bounded binary decoding. No guessed offsets for an unrecognized account layout.

The compact layouts below are an audited transcription of the pinned 0.12.0 IDL,
not a copy of a full historical IDL. `IdlCodec` also loads an entire supplied IDL.
Integer token amounts never pass through float.
"""
from __future__ import annotations

import base64
import hashlib
import struct
from pathlib import Path
from typing import Any

from .errors import Unsupported, ValidationError
from .model import Bin, FeeParameters, PoolState, VariableState, PROGRAM_ID, SOL_MINT
from .util import read_json, sha256

ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
B58 = {c: i for i, c in enumerate(ALPHABET)}


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, 'big')
    out = ''
    while n:
        n, r = divmod(n, 58)
        out = ALPHABET[r] + out
    return '1' * (len(data) - len(data.lstrip(b'\0'))) + out


def b58decode(text: str) -> bytes:
    if not isinstance(text, str) or len(text) > 32768:
        raise ValidationError('invalid/oversized base58')
    n = 0
    try:
        for c in text:
            n = n * 58 + B58[c]
    except KeyError as exc:
        raise ValidationError('invalid base58 character') from exc
    payload = n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return b'\0' * (len(text) - len(text.lstrip('1'))) + payload


def discriminator(namespace: str, name: str) -> bytes:
    return hashlib.sha256(f'{namespace}:{name}'.encode()).digest()[:8]


class Reader:
    def __init__(self, data: bytes, offset: int = 0):
        self.data, self.offset = data, offset

    def take(self, size: int) -> bytes:
        if size < 0 or self.offset + size > len(self.data):
            raise ValidationError(f'truncated binary at {self.offset}: need {size}')
        out = self.data[self.offset:self.offset + size]
        self.offset += size
        return out

    def integer(self, bits: int, signed: bool = False) -> int:
        return int.from_bytes(self.take(bits // 8), 'little', signed=signed)

    def key(self) -> str:
        return b58encode(self.take(32))


def account_bytes(account: dict) -> bytes:
    d = account.get('account', account)
    if d.get('owner') != PROGRAM_ID:
        raise ValidationError('wrong account owner')
    data = d.get('data')
    if not isinstance(data, list) or len(data) != 2 or data[1] != 'base64':
        raise Unsupported('account data must be uncompressed base64')
    try:
        raw = base64.b64decode(data[0], validate=True)
    except ValueError as exc:
        raise ValidationError('bad account base64') from exc
    if d.get('space', len(raw)) != len(raw):
        raise ValidationError('account space/byte length mismatch')
    return raw


def _disc(raw: bytes, name: str, size: int | None = None):
    if raw[:8] != discriminator('account', name):
        raise Unsupported(f'not a recognized {name} account')
    if size is not None and len(raw) != size:
        raise Unsupported(f'{name} length {len(raw)} != pinned layout {size}')


def decode_lb_pair(raw: bytes) -> dict:
    _disc(raw, 'LbPair', 904)
    r = Reader(raw, 8)
    p = {k: r.integer(16) for k in ('base_factor', 'filter_period', 'decay_period', 'reduction_factor')}
    p.update({k: r.integer(32) for k in ('variable_fee_control', 'max_volatility_accumulator')})
    p.update({k: r.integer(32, True) for k in ('min_bin_id', 'max_bin_id')})
    p['protocol_share'] = r.integer(16)
    p.update({k: r.integer(8) for k in ('base_fee_power_factor', 'function_type', 'collect_fee_mode')})
    r.take(3)
    v = {k: r.integer(32) for k in ('volatility_accumulator', 'volatility_reference')}
    v['index_reference'] = r.integer(32, True)
    r.take(4)
    v['last_update_timestamp'] = r.integer(64, True)
    r.take(8)
    d = {'parameters': p, 'variable': v, 'bump_seed': r.integer(8), 'bin_step_seed': list(r.take(2)),
         'pair_type': r.integer(8), 'active_id': r.integer(32, True), 'bin_step': r.integer(16),
         'status': r.integer(8), 'require_base_factor_seed': r.integer(8)}
    r.take(2)
    d['activation_type'], d['creator_pool_on_off_control'] = r.integer(8), r.integer(8)
    for k in ('token_x_mint', 'token_y_mint', 'reserve_x', 'reserve_y'):
        d[k] = r.key()
    d['protocol_fee_x'], d['protocol_fee_y'] = r.integer(64), r.integer(64)
    r.take(32)
    rewards = []
    for _ in range(2):
        rewards.append({'mint': r.key(), 'vault': r.key(), 'funder': r.key(),
                        'duration': r.integer(64), 'duration_end': r.integer(64),
                        'rate': r.integer(128), 'last_update': r.integer(64),
                        'cumulative_seconds_with_empty_liquidity': r.integer(64)})
    d['rewards'] = rewards
    d['oracle'] = r.key()
    d['bin_array_bitmap'] = [r.integer(64) for _ in range(16)]
    d['last_updated_at'] = r.integer(64, True)
    r.take(32)
    d['pre_activation_swap_address'], d['base_key'] = r.key(), r.key()
    d['activation_point'], d['pre_activation_duration'] = r.integer(64), r.integer(64)
    r.take(16)
    d['creator'] = r.key()
    d['token_x_program_flag'], d['token_y_program_flag'], d['version'] = [r.integer(8) for _ in range(3)]
    r.take(21)
    if r.offset != len(raw):
        raise ValidationError('LbPair parser did not consume exact layout')
    d['account_sha256'] = sha256(raw)
    return d


def decode_bin_array(raw: bytes) -> dict:
    _disc(raw, 'BinArray', 10136)
    r = Reader(raw, 8)
    index, version = r.integer(64, True), r.integer(8)
    r.take(7)
    pool = r.key()
    bins = []
    for j in range(70):
        b = {'bin_id': index * 70 + j, 'amount_x': r.integer(64), 'amount_y': r.integer(64),
             'price': r.integer(128), 'liquidity_supply': r.integer(128)}
        b.update({k: r.integer(64) for k in ('fulfilled_order_amount_x', 'fulfilled_order_amount_y',
                                           'limit_order_fee_ask_side', 'limit_order_fee_bid_side')})
        b['fee_x_stored'], b['fee_y_stored'] = r.integer(128), r.integer(128)
        b.update({k: r.integer(64) for k in ('open_order_amount', 'total_processing_order_amount',
                                           'processed_order_remaining_amount')})
        b['order_age'], b['limit_order_ask_side'] = r.integer(32), r.integer(8)
        r.take(3)
        bins.append(b)
    return {'index': index, 'version': version, 'pool': pool, 'bins': bins, 'account_sha256': sha256(raw)}


def decode_position_prefix(raw: bytes) -> dict:
    _disc(raw, 'PositionV2')
    if len(raw) < 7936:
        raise ValidationError('truncated PositionV2 prefix')
    r = Reader(raw, 8)
    pool, owner = r.key(), r.key()
    shares = [r.integer(128) for _ in range(70)]
    r.take(70 * 48)  # UserRewardInfo
    fee_info = []
    for _ in range(70):
        fee_info.append({'x_complete': r.integer(128), 'y_complete': r.integer(128),
                         'x_pending': r.integer(64), 'y_pending': r.integer(64)})
    lower, upper = r.integer(32, True), r.integer(32, True)
    if upper < lower or upper - lower + 1 > 70:
        raise Unsupported('extended PositionV2 requires full IDL/extension decoder')
    return {'pool': pool, 'owner': owner, 'lower_bin_id': lower, 'upper_bin_id': upper,
            'shares': dict(zip(range(lower, upper + 1), shares[:upper - lower + 1])),
            'fees': fee_info[:upper - lower + 1], 'decoded_scope': 'FIXED_70_BIN_PREFIX',
            'trailing_bytes': len(raw) - r.offset}


def decode_clock(raw: bytes) -> dict:
    if len(raw) != 40:
        raise ValidationError('Clock sysvar must have 40 bytes')
    values = struct.unpack('<QqQQq', raw)
    return dict(zip(('slot', 'epoch_start_timestamp', 'epoch', 'leader_schedule_epoch', 'unix_timestamp'), values))


def build_pool_state(pool: str, pair: dict, arrays: list[dict], *, slot: int,
                     complete_bin_map: bool = False, data_kind: str = 'CURRENT') -> PoolState:
    bins = {}
    for a in arrays:
        if a['pool'] != pool:
            raise ValidationError('BinArray belongs to another pool')
        for b in a['bins']:
            if b['bin_id'] in bins:
                raise ValidationError('duplicate bin array')
            bins[b['bin_id']] = Bin(**{k: v for k, v in b.items() if k in Bin.__dataclass_fields__})
    scalar = {k: pair[k] for k in ('active_id', 'bin_step', 'token_x_mint', 'token_y_mint',
                                  'status', 'pair_type', 'activation_type', 'activation_point',
                                  'token_x_program_flag', 'token_y_program_flag')}
    dx, dy = (9, 6) if pair['token_x_mint'] == SOL_MINT else (6, 9)
    state = PoolState(pool, parameters=FeeParameters(**pair['parameters']),
                      variable=VariableState(**pair['variable']), bins=bins,
                      decimals_x=dx, decimals_y=dy, slot=slot, **scalar,
                      known_bin_arrays=sorted(a['index'] for a in arrays),
                      complete_bin_map=complete_bin_map, data_kind=data_kind,
                      reward_mints=(tuple(r['mint'] for r in pair['rewards'])
                                    if pair.get('rewards') is not None else None),
                      protocol_fee_x=pair.get('protocol_fee_x'),
                      protocol_fee_y=pair.get('protocol_fee_y'),
                      bin_array_bitmap=pair.get('bin_array_bitmap'),
                      last_updated_at=pair.get('last_updated_at'))
    state.validate()
    return state


# Borsh event fields: generated from audited field declarations; no alignment.
EVENT_FIELDS = {
    'Swap': [('lb_pair','pubkey'),('from','pubkey'),('start_bin_id','i32'),('end_bin_id','i32'),
             ('amount_in','u64'),('amount_out','u64'),('swap_for_y','bool'),('fee','u64'),
             ('protocol_fee','u64'),('fee_bps','u128'),('host_fee','u64')],
    'Swap2Evt': [('lb_pair','pubkey'),('from','pubkey'),('start_bin_id','i32'),('end_bin_id','i32'),
                 ('swap_for_y','bool'),('fee_bps','u128'),('amount_in','u64'),('amount_left','u64'),
                 ('amount_out','u64'),('mm_fee','u64'),('protocol_fee','u64'),('limit_order_fee','u64'),
                 ('host_fee','u64'),('fees_on_input','bool'),('fees_on_token_x','bool')],
    'CompositionFee': [('from','pubkey'),('bin_id','i16'),('token_x_fee_amount','u64'),
                       ('token_y_fee_amount','u64'),('protocol_token_x_fee_amount','u64'),
                       ('protocol_token_y_fee_amount','u64')],
    'LbPairCreate': [('lb_pair','pubkey'),('bin_step','u16'),('token_x','pubkey'),('token_y','pubkey')],
    'FeeParameterUpdate': [('lb_pair','pubkey'),('protocol_share','u16'),('base_factor','u16'),('base_fee_power_factor','u8')],
    'DynamicFeeParameterUpdate': [('lb_pair','pubkey'),('filter_period','u16'),('decay_period','u16'),
                                  ('reduction_factor','u16'),('variable_fee_control','u32'),('max_volatility_accumulator','u32')],
}


def scalar_decode(r: Reader, kind: str):
    if kind == 'pubkey':
        return r.key()
    if kind == 'bool':
        b = r.integer(8)
        if b not in (0, 1):
            raise ValidationError('invalid Borsh bool')
        return bool(b)
    if kind[0] in 'ui' and kind[1:] in ('8','16','32','64','128','256'):
        return r.integer(int(kind[1:]), kind[0] == 'i')
    raise Unsupported(f'unsupported scalar {kind}')


def decode_event(raw: bytes, idl: 'IdlCodec | None' = None) -> dict:
    if idl:
        return idl.decode_named('events', raw)
    for name, fields in EVENT_FIELDS.items():
        if raw[:8] == discriminator('event', name):
            r = Reader(raw, 8)
            out = {k: scalar_decode(r, t) for k, t in fields}
            if r.offset != len(raw):
                raise Unsupported(f'{name}: layout extension or extra bytes')
            return {'name': name, 'data': out}
    raise Unsupported(f'unknown event discriminator {raw[:8].hex()}')


class IdlCodec:
    """Bounded Anchor IDL decoder. Explicit padding in IDL is honored.

    Solana bytemuck account layouts in the pinned IDL include their padding.
    This is NOT a host ctypes structure (host u128 alignment is different).
    """
    def __init__(self, idl: dict):
        if idl.get('address') != PROGRAM_ID:
            raise ValidationError('IDL program address mismatch')
        self.idl = idl
        self.types = {t['name']: t['type'] for t in idl.get('types', [])}

    @classmethod
    def load(cls, path: Path):
        return cls(read_json(path))

    def value(self, r: Reader, typ: Any, depth: int = 0):
        if depth > 32:
            raise ValidationError('IDL nesting bound')
        if isinstance(typ, str):
            if typ in ('bytes', 'string'):
                n = r.integer(32)
                if n > 8_000_000:
                    raise ValidationError('Borsh bytes allocation bound')
                b = r.take(n)
                return b.hex() if typ == 'bytes' else b.decode('utf8')
            return scalar_decode(r, typ)
        if 'defined' in typ:
            name = typ['defined'] if isinstance(typ['defined'], str) else typ['defined']['name']
            if name not in self.types:
                raise Unsupported(f'undefined IDL type {name}')
            return self.structure(r, self.types[name], depth + 1)
        if 'array' in typ or 'vec' in typ:
            if 'array' in typ:
                child, n = typ['array']
            else:
                child, n = typ['vec'], r.integer(32)
            if not isinstance(n, int) or not 0 <= n <= 100_000:
                raise ValidationError('Borsh array bound')
            return [self.value(r, child, depth + 1) for _ in range(n)]
        if 'option' in typ:
            tag = r.integer(8)
            if tag not in (0,1):
                raise ValidationError('invalid option tag')
            return self.value(r, typ['option'], depth + 1) if tag else None
        raise Unsupported(f'IDL type not implemented: {typ}')

    def structure(self, r: Reader, typ: dict, depth: int = 0):
        if typ['kind'] == 'struct':
            return {f['name']: self.value(r, f['type'], depth + 1) for f in typ['fields']}
        if typ['kind'] == 'enum':
            i = r.integer(8)
            if i >= len(typ['variants']):
                raise ValidationError('invalid enum index')
            variant = typ['variants'][i]
            fields = variant.get('fields', [])
            if fields and isinstance(fields[0], dict) and 'name' in fields[0]:
                val = {f['name']: self.value(r, f['type'], depth + 1) for f in fields}
            else:
                val = [self.value(r, f, depth + 1) for f in fields]
            return {'variant': variant['name'], 'fields': val}
        raise Unsupported('IDL kind not implemented')

    def decode_named(self, category: str, raw: bytes) -> dict:
        for item in self.idl.get(category, []):
            namespace = {'accounts': 'account', 'events': 'event', 'instructions': 'global'}[category]
            disc = bytes(item.get('discriminator', discriminator(namespace, item['name'])))
            if raw[:8] != disc:
                continue
            r = Reader(raw, 8)
            if category == 'instructions':
                out = {f['name']: self.value(r, f['type']) for f in item['args']}
            else:
                if item['name'] not in self.types:
                    raise Unsupported('IDL account/event type missing')
                out = self.structure(r, self.types[item['name']])
            if r.offset != len(raw):
                raise Unsupported(f'{item["name"]} trailing bytes: {len(raw)-r.offset}')
            return {'name': item['name'], 'data': out}
        raise Unsupported(f'unknown {category} discriminator {raw[:8].hex()}')


def _edwards_decompressible(raw: bytes) -> bool:
    """Solana PDA rejection criterion: an Edwards25519 point decompresses.

    This is not a public-key/subgroup validity check. Small-order points must
    also be rejected as PDAs. No signing operation is implemented here.
    """
    if len(raw) != 32:
        raise ValidationError('compressed Edwards point must contain 32 bytes')
    p = 2**255 - 19
    y = (int.from_bytes(raw, 'little') & (2**255 - 1)) % p
    d = (-121665 * pow(121666, p-2, p)) % p
    numerator = (y*y - 1) % p
    denominator = (d*y*y + 1) % p
    if denominator == 0:
        return False
    x2 = numerator * pow(denominator, p-2, p) % p
    return x2 == 0 or pow(x2, (p-1)//2, p) == 1


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    """Derive a PDA with the canonical descending bump; checked vs web3 in SDK oracle."""
    import hashlib
    if len(seeds) > 15 or any(len(s) > 32 for s in seeds):
        raise ValidationError('PDA seed bounds exceeded')
    program = b58decode(program_id)
    if len(program) != 32:
        raise ValidationError('program public key must contain 32 bytes')
    for bump in range(255, -1, -1):
        raw = hashlib.sha256(b''.join(seeds) + bytes([bump]) + program + b'ProgramDerivedAddress').digest()
        if not _edwards_decompressible(raw):
            return b58encode(raw), bump
    raise ValidationError('no PDA bump found')
