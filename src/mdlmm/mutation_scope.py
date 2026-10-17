"""Conservative, independent IDL instruction ownership/argument-status recovery.

A matching discriminator and named lb_pair role can identify the *candidate*
owner when Borsh args are malformed/unparseable. This never certifies execution,
mutation, historical prestate, or a replayable economic transition.
"""
from __future__ import annotations

from .codec import IdlCodec
from .errors import Unsupported, ValidationError
from .normalize import _flatten_account_names


def identify_mutation_instruction(raw: bytes, accounts: list[str], pool: str,
                                  idl: IdlCodec) -> dict:
    if not isinstance(raw, bytes) or len(raw) < 8:
        return {'name': 'INVALID_INSTRUCTION_DATA', 'scope': 'UNRESOLVED',
                'args_status': 'NOT_DECODED', 'reason': 'missing 8-byte discriminator',
                'lb_pair': None, 'discriminator': raw[:8].hex() if isinstance(raw,bytes) else None}
    disc = raw[:8]
    if not isinstance(accounts, list) or any(not isinstance(x, str) for x in accounts):
        return {'name': 'INVALID_ACCOUNT_VECTOR', 'scope': 'UNRESOLVED',
                'args_status': 'NOT_DECODED', 'reason': 'invalid account vector',
                'lb_pair': None, 'discriminator': disc.hex()}
    matches = [x for x in idl.idl.get('instructions', [])
               if bytes(x.get('discriminator', [])) == disc]
    if len(matches) != 1:
        return {'name': 'UNDECODED_' + disc.hex(), 'scope': 'UNRESOLVED',
                'args_status': 'NOT_IN_PINNED_IDL',
                'reason': 'zero or multiple pinned IDL declarations for discriminator',
                'lb_pair': None, 'discriminator': disc.hex()}

    declaration = matches[0]
    name = declaration['name']
    names = _flatten_account_names(declaration.get('accounts', []))
    if len(accounts) < len(names):
        return {'name': name, 'scope': 'UNRESOLVED',
                'args_status': 'NOT_DECODED',
                'reason': f'account vector truncated: {len(accounts)} < {len(names)}',
                'lb_pair': None, 'discriminator': disc.hex()}

    try:
        decoded = idl.decode_named('instructions', raw)
        if decoded.get('name') != name:
            raise ValidationError('decoded name disagrees with IDL discriminator')
        args_status, args_error = 'FULL_IDL_ARGS_PASS', None
    except (Unsupported, ValidationError, KeyError, ValueError, TypeError) as exc:
        args_status = 'IDL_ARGS_UNPARSEABLE'
        args_error = f'{type(exc).__name__}: {str(exc)[:160]}'

    candidates = [i for i, role in enumerate(names) if role == 'lb_pair']
    if len(candidates) != 1:
        scope, lb_pair = 'UNRESOLVED_NO_UNIQUE_LB_PAIR_ROLE', None
    else:
        lb_pair = accounts[candidates[0]]
        if lb_pair == pool:
            scope = 'TARGET'
        elif pool in accounts:
            scope = 'AMBIGUOUS_SHARED_ACCOUNT'
        else:
            scope = 'FOREIGN_WITH_PROVEN_LB_PAIR_ROLE'
    return {'name': name, 'scope': scope, 'args_status': args_status,
            'args_error': args_error, 'lb_pair': lb_pair,
            'discriminator': disc.hex(), 'pinned_role_basis': 'NAMED_IDL_LB_PAIR_ACCOUNT',
            'execution_semantics': 'UNVERIFIED' if args_status != 'FULL_IDL_ARGS_PASS'
                                   else 'NOT_VALIDATED_BY_SCOPE_CLASSIFIER'}
