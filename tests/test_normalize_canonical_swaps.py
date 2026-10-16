"""Canonical grouping/argument tests using raw Anchor instruction/event bytes.

The six instruction declarations and event layouts are taken from dlmm-sdk
576919e3e4368e542c402f000b4264724f7f23ec.  These tests do not assert swap math.
"""
import base64
from copy import deepcopy
import struct

import pytest

from mdlmm.codec import IdlCodec, b58decode, b58encode, discriminator
from mdlmm.errors import ValidationError
from mdlmm.model import PROGRAM_ID
from mdlmm.normalize import EVENT_CPI_TAG, normalize_block


def key(n):
    return b58encode(bytes([n]) * 32)


POOL, USER, HOST, ROUTER, FOREIGN = [key(n) for n in (2, 3, 4, 5, 6)]
NAMES = ('swap', 'swap2', 'swap_exact_out', 'swap_exact_out2',
         'swap_with_price_impact', 'swap_with_price_impact2')
ACCOUNT_NAMES = ['lb_pair', 'bin_array_bitmap_extension', 'reserve_x', 'reserve_y',
                 'user_token_in', 'user_token_out', 'token_x_mint', 'token_y_mint',
                 'oracle', 'host_fee_in', 'user', 'token_x_program', 'token_y_program']
COMMON = [('lb_pair', 'pubkey'), ('from', 'pubkey'), ('start_bin_id', 'i32'), ('end_bin_id', 'i32')]
EVENT_DECLARATIONS = {
    'Swap': COMMON + [('amount_in', 'u64'), ('amount_out', 'u64'), ('swap_for_y', 'bool'),
                      ('fee', 'u64'), ('protocol_fee', 'u64'), ('fee_bps', 'u128'), ('host_fee', 'u64')],
    'Swap2Evt': COMMON + [('swap_for_y', 'bool'), ('fee_bps', 'u128'), ('amount_in', 'u64'),
                          ('amount_left', 'u64'), ('amount_out', 'u64'), ('mm_fee', 'u64'),
                          ('protocol_fee', 'u64'), ('limit_order_fee', 'u64'), ('host_fee', 'u64'),
                          ('fees_on_input', 'bool'), ('fees_on_token_x', 'bool')],
}


@pytest.fixture
def pinned_idl():
    instructions = []
    for name in NAMES:
        if name.startswith('swap_exact_out'):
            args = [('max_in_amount', 'u64'), ('out_amount', 'u64')]
        elif name.startswith('swap_with_price_impact'):
            args = [('amount_in', 'u64'), ('active_id', {'option': 'i32'}), ('max_price_impact_bps', 'u16')]
        else:
            args = [('amount_in', 'u64'), ('min_amount_out', 'u64')]
        if name.endswith('2'):
            args.append(('remaining_accounts_info', {'defined': {'name': 'RemainingAccountsInfo'}}))
        accounts = ACCOUNT_NAMES + (['memo_program'] if name.endswith('2') else []) + ['event_authority', 'program']
        instructions.append({'name': name, 'discriminator': list(discriminator('global', name)),
                             'args': [{'name': n, 'type': t} for n, t in args],
                             'accounts': [{'name': n} for n in accounts]})
    types = [{'name': name, 'type': {'kind': 'struct', 'fields': [{'name': n, 'type': t} for n, t in fields]}}
             for name, fields in EVENT_DECLARATIONS.items()]
    types.extend([
        {'name': 'RemainingAccountsInfo', 'type': {'kind': 'struct', 'fields': [
            {'name': 'slices', 'type': {'vec': {'defined': {'name': 'RemainingAccountsSlice'}}}}]}},
        {'name': 'RemainingAccountsSlice', 'type': {'kind': 'struct', 'fields': [
            {'name': 'accounts_type', 'type': {'defined': {'name': 'AccountsType'}}},
            {'name': 'length', 'type': 'u8'}]}},
        {'name': 'AccountsType', 'type': {'kind': 'enum', 'variants': [
            {'name': 'TransferHookX'}, {'name': 'TransferHookY'}, {'name': 'TransferHookReward'},
            {'name': 'TransferHookMultiReward', 'fields': ['u8']}, {'name': 'TransferHookReferral'}]}},
    ])
    return IdlCodec({'address': PROGRAM_ID, 'instructions': instructions, 'types': types,
                     'events': [{'name': n, 'discriminator': list(discriminator('event', n))}
                                for n in EVENT_DECLARATIONS]})


def swap_ix(name='swap', *, height=None, host=None, amount_in=1000, max_in=2000,
            out_amount=777, active_id=-12, pool=POOL):
    raw = discriminator('global', name)
    if name.startswith('swap_exact_out'):
        raw += struct.pack('<QQ', max_in, out_amount)
    elif name.startswith('swap_with_price_impact'):
        raw += struct.pack('<Q', amount_in)
        raw += b'\0' if active_id is None else b'\1' + struct.pack('<i', active_id)
        raw += struct.pack('<H', 123)
    else:
        raw += struct.pack('<QQ', amount_in, 700)
    if name.endswith('2'):
        raw += struct.pack('<I', 0)
    accounts = [pool, PROGRAM_ID, *[key(n) for n in range(12, 19)], host or PROGRAM_ID,
                USER, key(20), key(21)]
    if name.endswith('2'):
        accounts.append(key(22))
    accounts.extend([key(23), PROGRAM_ID])
    ix = {'programId': PROGRAM_ID, 'accounts': accounts, 'data': b58encode(raw)}
    if height is not None:
        ix['stackHeight'] = height
    return ix


def event_fields(**changes):
    fields = {'lb_pair': POOL, 'from': USER, 'start_bin_id': -2, 'end_bin_id': -3,
              'amount_in': 997, 'amount_out': 777, 'swap_for_y': True, 'fee': 987,
              'protocol_fee': 5, 'fee_bps': 10000000, 'host_fee': 0, 'amount_left': 3,
              'mm_fee': 7, 'limit_order_fee': 11, 'fees_on_input': True, 'fees_on_token_x': True}
    fields.update(changes)
    return fields


def event_ix(name, fields=None, *, height=2):
    f = fields or event_fields()
    prefix = b58decode(f['lb_pair']) + b58decode(f['from']) + struct.pack('<ii', f['start_bin_id'], f['end_bin_id'])
    if name == 'Swap':
        body = prefix + struct.pack('<QQ?QQ', f['amount_in'], f['amount_out'], f['swap_for_y'], f['fee'], f['protocol_fee'])
        body += f['fee_bps'].to_bytes(16, 'little') + struct.pack('<Q', f['host_fee'])
    else:
        body = prefix + struct.pack('<?', f['swap_for_y']) + f['fee_bps'].to_bytes(16, 'little')
        body += struct.pack('<QQQQQQQ??', f['amount_in'], f['amount_left'], f['amount_out'],
                            f['mm_fee'], f['protocol_fee'], f['limit_order_fee'], f['host_fee'],
                            f['fees_on_input'], f['fees_on_token_x'])
    ix = {'programId': PROGRAM_ID, 'accounts': [],
          'data': b58encode(EVENT_CPI_TAG + discriminator('event', name) + body)}
    if height is not None:
        ix['stackHeight'] = height
    return ix


def pair(fields=None, *, height=2, reverse=False):
    rows = [event_ix('Swap2Evt', fields, height=height), event_ix('Swap', fields, height=height)]
    return rows[::-1] if reverse else rows


def foreign_ix(program=ROUTER, height=None):
    ix = {'programId': program, 'accounts': [POOL], 'data': ''}
    if height is not None:
        ix['stackHeight'] = height
    return ix


def block(top=None, inner=None, logs=None):
    return {'blockTime': 1000, 'transactions': [{
        'transaction': {'signatures': ['fixture_sig'], 'message': {
            'accountKeys': [PROGRAM_ID, POOL, USER, ROUTER],
            'instructions': [top if top is not None else swap_ix()]}},
        'meta': {'err': None, 'fee': 5000, 'innerInstructions': [
            {'index': 0, 'instructions': inner if inner is not None else pair()}],
            'logMessages': logs or []}}]}


def normalize(b, *, idl=None, strict=True):
    return normalize_block(42, b, {POOL}, raw_hash='fixture_raw_hash', idl=idl,
                           require_canonical_swaps=strict)


@pytest.mark.parametrize('name', NAMES)
def test_all_six_variants_use_actual_instruction_args(name, pinned_idl):
    result = normalize(block(top=swap_ix(name, host=HOST)), idl=pinned_idl)
    assert result['strict_canonical_pass'] and not result['blockers']
    assert result['canonical_swap_count'] == 1 and result['legacy_swap_count'] == 0
    assert len(result['events']) == 1
    row = result['events'][0]
    p = row['payload']
    assert p['normalization_mode'] == 'CANONICAL_PAIRED' and p['canonical_pair_verified']
    assert p['host_fee_present'] is True and row['observed']['host_fee'] == 0
    assert p['host_fee_account'] == HOST and p['instruction_name'] == name
    assert p['instruction_location']['invocation_path'] == [0]
    assert p['compatibility_swap']['fee'] == 987  # Do not infer a relation to rich fee slices.
    assert 'fee' not in row['observed']
    assert row['observed']['amount_in'] == 997
    if 'exact_out' in name:
        assert p['exact_out'] and 'amount_in' not in p
        assert p['max_in_amount'] == 2000 and p['out_amount'] == 777
        assert p['instruction_args']['max_in_amount'] == 2000
    else:
        assert not p['exact_out'] and p['amount_in'] == 1000
        if 'price_impact' in name:
            assert p['instruction_args']['active_id'] == -12
            assert p['instruction_args']['max_price_impact_bps'] == 123


@pytest.mark.parametrize('name', ('swap', 'swap_exact_out', 'swap_with_price_impact'))
def test_bounded_legacy_instruction_decoder_can_make_full_canonical_pair(name):
    result = normalize(block(top=swap_ix(name, active_id=None)))
    assert result['strict_canonical_pass']
    p = result['events'][0]['payload']
    assert p['instruction_decode_basis'] == 'BOUNDED_LEGACY_LAYOUT'
    assert p['host_fee_present'] is False and p['host_fee_account'] is None


@pytest.mark.parametrize('name', ('swap2', 'swap_exact_out2', 'swap_with_price_impact2'))
def test_modern_tail_requires_idl(name):
    result = normalize(block(top=swap_ix(name)))
    assert not result['events'] and not result['strict_canonical_pass']
    assert any('supplied IDL' in b['reason'] for b in result['blockers'])


@pytest.mark.parametrize('reverse', [False, True])
def test_pair_event_order_and_mirrored_logs_are_not_extra_swaps(reverse):
    events = pair(reverse=reverse)
    logs = ['Program data: ' + base64.b64encode(b58decode(ix['data'])[8:]).decode() for ix in events]
    result = normalize(block(inner=events, logs=logs))
    assert result['strict_canonical_pass'] and len(result['events']) == 1
    row = result['events'][0]
    assert row['order']['inner_instruction_index'] == int(reverse)
    assert [s['inner_instruction_index'] for s in row['payload']['event_sources']] == [0, 1]


def test_compatibility_request_never_claims_strict_pass_even_with_complete_pairs():
    result = normalize(block(), strict=False)
    assert not result['strict_canonical_pass'] and result['canonical_swap_count'] == 1


def test_same_pool_two_swaps_under_one_router_keep_distinct_invocation_owners():
    inner = [swap_ix(height=2), *pair(height=3), foreign_ix(FOREIGN, 2),
             swap_ix(height=2, amount_in=2000), *pair(event_fields(amount_in=1990), height=3)]
    result = normalize(block(top=foreign_ix(), inner=inner))
    assert result['strict_canonical_pass'] and len(result['events']) == 2
    a, b = [r['payload'] for r in result['events']]
    assert a['instruction_location']['invocation_path'] == [0, 0]
    assert b['instruction_location']['invocation_path'] == [0, 4]
    assert [s['invocation_path'] for s in a['event_sources']] == [[0, 0, 1], [0, 0, 2]]
    assert [s['invocation_path'] for s in b['event_sources']] == [[0, 4, 5], [0, 4, 6]]
    assert (a['amount_in'], b['amount_in']) == (1000, 2000)


def test_nested_foreign_invocation_and_nested_swap_are_distinguished():
    parsed_router = {'programId': FOREIGN, 'parsed': {'type': 'fixture'}, 'stackHeight': 2}
    inner = [parsed_router, swap_ix(height=3), *pair(height=4),
             swap_ix(height=2, amount_in=3000), *pair(event_fields(amount_in=2990), height=3)]
    result = normalize(block(top=foreign_ix(), inner=inner))
    assert result['strict_canonical_pass'] and len(result['events']) == 2
    assert [r['payload']['instruction_location']['invocation_path'] for r in result['events']] == [[0, 0, 1], [0, 4]]


def test_nested_dlmm_swap_returns_before_parent_pair():
    inner = [swap_ix(height=2, amount_in=2000), *pair(event_fields(amount_in=1990), height=3), *pair(height=2)]
    result = normalize(block(inner=inner))
    assert result['strict_canonical_pass'] and len(result['events']) == 2
    assert [r['payload']['instruction_location']['inner_instruction_index'] for r in result['events']] == [0, -1]


@pytest.mark.parametrize('height', [None, 1, 4, True, '2'])
def test_missing_or_invalid_cpi_ancestry_cannot_make_canonical_pair(height):
    result = normalize(block(inner=pair(height=height)))
    assert not result['events'] and result['blockers'] and not result['strict_canonical_pass']


def test_missing_second_swap_event_does_not_borrow_first_swaps_pair():
    inner = [swap_ix(height=2), *pair(height=3), swap_ix(height=2)]
    result = normalize(block(top=foreign_ix(), inner=inner))
    assert not result['strict_canonical_pass'] and result['blockers']
    assert all(r['payload']['instruction_location']['inner_instruction_index'] != 3 for r in result['events'])


@pytest.mark.parametrize('changed', [
    {'lb_pair': key(8)}, {'from': key(9)}, {'start_bin_id': 99}, {'end_bin_id': 99},
    {'swap_for_y': False}, {'fee_bps': 55}, {'amount_in': 55}, {'amount_out': 55},
    {'protocol_fee': 55}, {'host_fee': 55},
])
def test_pair_common_field_disagreement_suppresses_swap(changed):
    result = normalize(block(inner=[event_ix('Swap2Evt'), event_ix('Swap', event_fields(**changed))]))
    assert not result['events'] and result['blockers']
    assert any('common fields differ' in b['reason'] for b in result['blockers'])


def test_pair_agreement_does_not_override_actual_instruction_user():
    result = normalize(block(inner=pair(event_fields(**{'from': key(9)}))))
    assert not result['events'] and any('actual instruction user' in b['reason'] for b in result['blockers'])


@pytest.mark.parametrize('events', [[], [event_ix('Swap')], [event_ix('Swap2Evt')],
                                   [*pair(), event_ix('Swap')], [*pair(), event_ix('Swap2Evt')]])
def test_missing_duplicate_and_extra_events_fail_closed(events):
    result = normalize(block(inner=events))
    assert not result['events'] and result['blockers']
    assert all(b['resolution_policy'] == 'RENORMALIZE_OR_REJECT' for b in result['blockers'])


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('malformation', ['truncated', 'trailing'])
def test_malformed_arguments_do_not_degrade_to_legacy(name, malformation, pinned_idl):
    top = swap_ix(name)
    raw = b58decode(top['data'])
    top['data'] = b58encode(raw[:-1] if malformation == 'truncated' else raw + b'\0')
    result = normalize(block(top=top), idl=pinned_idl, strict=False)
    assert not result['events'] and result['blockers']


def test_invalid_option_tag_and_short_account_vector_are_rejected():
    top = swap_ix('swap_with_price_impact')
    raw = bytearray(b58decode(top['data']))
    raw[16] = 2
    top['data'] = b58encode(bytes(raw))
    assert not normalize(block(top=top))['events']
    top = swap_ix()
    top['accounts'] = top['accounts'][:10]
    result = normalize(block(top=top))
    assert not result['events'] and result['blockers']


def test_legacy_single_swap_is_explicitly_weaker_and_strict_mode_rejects_it():
    fixture = block(inner=[event_ix('Swap')])
    compat = normalize(fixture, strict=False)
    assert len(compat['events']) == 1 and not compat['blockers']
    p = compat['events'][0]['payload']
    assert p['normalization_mode'] == 'LEGACY_SINGLE_SWAP'
    assert not p['canonical_pair_verified'] and p['amount_in'] == 1000
    assert not compat['strict_canonical_pass']
    assert not normalize(fixture)['events']


def test_old_discriminator_only_fixture_never_infers_instruction_or_host_evidence():
    top = {'programId': PROGRAM_ID, 'accounts': [POOL], 'data': b58encode(discriminator('global', 'swap'))}
    fixture = block(top=top, inner=[event_ix('Swap', event_fields(host_fee=12), height=None)])
    compat = normalize(fixture, strict=False)
    assert len(compat['events']) == 1 and not compat['blockers']
    p = compat['events'][0]['payload']
    assert p['normalization_mode'] == 'LEGACY_EVENT_ONLY'
    assert p['instruction_args'] is None and p['host_fee_present'] is None
    assert p['input_evidence'] == 'LEGACY_EVENT_ONLY'
    strict = normalize(fixture)
    assert not strict['events'] and strict['blockers']


def test_event_decode_failure_cannot_be_downgraded_to_single_swap():
    bad = event_ix('Swap2Evt')
    bad['data'] = b58encode(b58decode(bad['data'])[:-1])
    result = normalize(block(inner=[bad, event_ix('Swap')]), strict=False)
    assert not result['events'] and result['blockers']


def test_unknown_mutation_is_not_cleared_by_some_other_swaps_event():
    unknown = {'programId': PROGRAM_ID, 'accounts': [POOL], 'stackHeight': 2,
               'data': b58encode(discriminator('global', 'set_something_unknown'))}
    result = normalize(block(inner=[*pair(), unknown]))
    assert result['blockers'] and not result['strict_canonical_pass']
    assert any(b['blocker_kind'] == 'STATE_MUTATION' and b['inner_instruction'] == 2 for b in result['blockers'])


def test_event_from_unknown_instruction_cannot_cover_earlier_swap():
    unknown = {'programId': PROGRAM_ID, 'accounts': [POOL], 'stackHeight': 2,
               'data': b58encode(discriminator('global', 'set_something_unknown'))}
    result = normalize(block(top=foreign_ix(), inner=[swap_ix(height=2), unknown, *pair(height=3)]))
    assert not result['events'] and result['blockers']


def test_extra_log_event_still_blocks_when_other_cpi_events_are_present():
    extra = event_ix('Swap2Evt', event_fields(amount_in=9999))
    log = 'Program data: ' + base64.b64encode(b58decode(extra['data'])[8:]).decode()
    result = normalize(block(logs=[log]))
    assert result['blockers'] and not result['strict_canonical_pass'] and not result['events']
    assert any('extra DLMM event' in b['reason'] for b in result['blockers'])
    assert result['suppressed_swap_rows'] == 1


def fee_parameter_event(*, height=3):
    body = b58decode(POOL) + struct.pack('<HHB', 1000, 1000, 0)
    raw = EVENT_CPI_TAG + discriminator('event', 'FeeParameterUpdate') + body
    return {'programId': PROGRAM_ID, 'accounts': [], 'data': b58encode(raw), 'stackHeight': height}


@pytest.mark.parametrize('use_selected_idl', [False, True])
def test_unlocated_non_swap_mutation_log_cannot_hide_behind_a_canonical_swap(use_selected_idl, pinned_idl):
    raw = b58decode(fee_parameter_event()['data'])[8:]
    result = normalize(block(logs=['Program data: ' + base64.b64encode(raw).decode()]),
                       idl=pinned_idl if use_selected_idl else None)
    assert result['blockers'] and not result['events'] and not result['strict_canonical_pass']
    assert result['suppressed_swap_rows'] == 1


def test_located_non_swap_write_and_its_mutation_blocker_are_both_preserved():
    mutation = {'programId': PROGRAM_ID, 'accounts': [POOL], 'stackHeight': 2,
                'data': b58encode(discriminator('global', 'set_something_unknown'))}
    result = normalize(block(inner=[*pair(), mutation, fee_parameter_event()]))
    assert [r['kind'] for r in result['events']] == ['SWAP', 'FEE_PARAMETERS']
    assert not result['strict_canonical_pass']
    assert any(b['blocker_kind'] == 'STATE_MUTATION' for b in result['blockers'])


def test_ambiguous_swap_transaction_preserves_non_swap_write_diagnostics():
    mutation = {'programId': PROGRAM_ID, 'accounts': [POOL], 'stackHeight': 2,
                'data': b58encode(discriminator('global', 'set_something_unknown'))}
    result = normalize(block(inner=[*pair(), event_ix('Swap'), mutation, fee_parameter_event()]))
    assert [r['kind'] for r in result['events']] == ['FEE_PARAMETERS']
    assert any(b['blocker_kind'] == 'SWAP_CANONICALIZATION' for b in result['blockers'])
    assert any(b['blocker_kind'] == 'STATE_MUTATION' for b in result['blockers'])


def test_boolean_outer_height_does_not_count_as_stack_height_one():
    top = swap_ix()
    top['stackHeight'] = True
    result = normalize(block(top=top))
    assert not result['events'] and result['blockers']


def test_foreign_program_event_with_common_name_is_not_misattributed_to_dlmm():
    raw = b58decode(event_ix('Swap2Evt', event_fields(amount_in=1234))['data'])[8:]
    logs = [f'Program {FOREIGN} invoke [1]',
            'Program data: ' + base64.b64encode(raw).decode(), f'Program {FOREIGN} success']
    result = normalize(block(logs=logs))
    assert result['strict_canonical_pass'] and not result['blockers']


@pytest.mark.parametrize('include_program_context', [False, True])
def test_unknown_log_event_cannot_be_assumed_harmless_in_strict_mode(include_program_context):
    logs = ['Program data: ' + base64.b64encode(b'unknown_event_payload').decode()]
    if include_program_context:
        logs = [f'Program {PROGRAM_ID} invoke [1]', *logs, f'Program {PROGRAM_ID} success']
    result = normalize(block(logs=logs))
    assert not result['events'] and result['blockers'] and not result['strict_canonical_pass']


def test_duplicate_inner_groups_are_not_silently_overwritten():
    fixture = block()
    groups = fixture['transactions'][0]['meta']['innerInstructions']
    groups.append(deepcopy(groups[0]))
    with pytest.raises(ValidationError, match='duplicate inner instruction group'):
        normalize(fixture)


def test_no_swaps_cannot_produce_a_nonempty_strict_pass():
    result = normalize(block(top=foreign_ix(), inner=[]))
    assert not result['strict_canonical_pass'] and result['canonical_swap_count'] == 0


# ---------------------------------------------------------------------------
# Multi-pool router attribution — canonical target swaps remain fail-closed.
# The synthetic ClaimFee event body is deliberately minimal; on-chain evidence
# is separately checked by the pinned full-block historical pilot.
# ---------------------------------------------------------------------------

def _scope_test_idl(pinned_idl):
    data = deepcopy(pinned_idl.idl)
    data['instructions'].append({
        'name': 'claim_fee2',
        'discriminator': list(discriminator('global', 'claim_fee2')),
        'accounts': [{'name': 'lb_pair'}, {'name': 'position'}],
        'args': [],
    })
    for name in ('ClaimFee', 'ClaimFee2'):
        data['events'].append({'name': name,
                               'discriminator': list(discriminator('event', name))})
        data['types'].append({'name': name, 'type': {'kind': 'struct', 'fields': [
            {'name': 'lb_pair', 'type': 'pubkey'}
        ]}})
    return IdlCodec(data)


def _scope_claim_ix(pool, height=2):
    return {
        'programId': PROGRAM_ID, 'accounts': [pool, key(77)],
        'stackHeight': height,
        'data': b58encode(discriminator('global', 'claim_fee2')),
    }


def _scope_fee_event(name, pool, height=3):
    return {
        'programId': PROGRAM_ID, 'accounts': [],
        'stackHeight': height,
        'data': b58encode(EVENT_CPI_TAG + discriminator('event', name) + b58decode(pool)),
    }


def _scope_multiswap_block(claim_pool=FOREIGN, event_pool=FOREIGN):
    inner = [
        _scope_claim_ix(claim_pool),
        _scope_fee_event('ClaimFee', event_pool),
        _scope_fee_event('ClaimFee2', event_pool),
        swap_ix(height=2, pool=FOREIGN),
        *pair(event_fields(lb_pair=FOREIGN), height=3),
        swap_ix(height=2, pool=POOL),
        *pair(height=3),
        swap_ix(height=2, pool=POOL, amount_in=2000),
        *pair(event_fields(amount_in=1990), height=3),
    ]
    return block(top=foreign_ix(), inner=inner)


def test_proved_foreign_claims_do_not_suppress_two_target_swaps(pinned_idl):
    result = normalize(_scope_multiswap_block(), idl=_scope_test_idl(pinned_idl))
    assert result['strict_canonical_pass']
    assert result['canonical_swap_count'] == 2
    assert not result['blockers'] and result['suppressed_swap_rows'] == 0
    rows = result['events']
    assert len(rows) == 2 and all(r['kind'] == 'SWAP' for r in rows)
    assert [r['payload']['amount_in'] for r in rows] == [1000, 2000]
    assert all(r['pool'] == POOL for r in rows)


def test_target_claim_fee_mutation_still_blocks_even_with_two_swaps(pinned_idl):
    result = normalize(_scope_multiswap_block(POOL, POOL), idl=_scope_test_idl(pinned_idl))
    assert not result['strict_canonical_pass']
    assert any(b['blocker_kind'] == 'STATE_MUTATION' and
               b['mutation']['name'] == 'claim_fee2' for b in result['blockers'])


def test_claim_fee_event_owner_pool_disagreement_remains_integrity_blocker(pinned_idl):
    result = normalize(_scope_multiswap_block(POOL, FOREIGN), idl=_scope_test_idl(pinned_idl))
    assert not result['strict_canonical_pass']
    assert result['suppressed_swap_rows'] == 2
    assert any('disagreement' in b['reason'] for b in result['blockers'])


def test_foreign_mutation_referencing_target_account_still_blocks(pinned_idl):
    fixture = _scope_multiswap_block()
    mutation = fixture['transactions'][0]['meta']['innerInstructions'][0]['instructions'][0]
    mutation['accounts'][1] = POOL
    result = normalize(fixture, idl=_scope_test_idl(pinned_idl))
    assert not result['strict_canonical_pass']
    assert any('references the target pool' in b['reason'] for b in result['blockers'])


def test_unowned_foreign_fee_event_remains_fail_closed(pinned_idl):
    inner = [_scope_fee_event('ClaimFee', FOREIGN, height=2),
             swap_ix(height=2, pool=POOL), *pair(height=3)]
    result = normalize(block(top=foreign_ix(), inner=inner), idl=_scope_test_idl(pinned_idl))
    assert not result['strict_canonical_pass']
    assert result['suppressed_swap_rows'] == 1
    assert any('owning DLMM instruction' in b['reason'] for b in result['blockers'])
