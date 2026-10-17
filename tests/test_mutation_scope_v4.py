"""Fail-closed IDL discriminator/owner recovery regressions."""
import pytest
from mdlmm.codec import IdlCodec, discriminator
from mdlmm.model import PROGRAM_ID
from mdlmm.mutation_scope import identify_mutation_instruction as classify
from mdlmm.corpus_worklist import scoped_dlmm_instructions

POOL='So11111111111111111111111111111111111111112'
OTHER='11111111111111111111111111111111'
POS='Vote111111111111111111111111111111111111111'
NAME='add_liquidity_by_strategy2'


def idl():
    return IdlCodec({'address':PROGRAM_ID, 'instructions':[
        {'name':NAME,'discriminator':list(discriminator('global', NAME)),
         'accounts':[{'name':'position'}, {'name':'lb_pair'}, {'name':'sender'}],
         'args':[{'name':'some_unknown_struct','type':{'defined':{'name':'MissingNestedType'}}}]},
        {'name':'close_position2',
         'discriminator':list(discriminator('global','close_position2')),
         'accounts':[{'name':'position'}, {'name':'sender'}], 'args':[]},
    ],'events':[],'types':[]})


def call(raw, accounts, *, pool=POOL):
    return classify(raw, accounts, pool, idl())


def test_recognized_discriminator_recovers_second_account_owner_without_faking_args():
    row=call(discriminator('global',NAME)+b'garbage', [POS, POOL, OTHER])
    assert row['name']==NAME and row['scope']=='TARGET'
    assert row['args_status']=='IDL_ARGS_UNPARSEABLE'
    assert row['execution_semantics']=='UNVERIFIED'


def test_known_other_pool_does_not_claim_target():
    row=call(discriminator('global',NAME)+b'garbage', [POS, OTHER, POS])
    assert row['scope']=='FOREIGN_WITH_PROVEN_LB_PAIR_ROLE'
    assert row['lb_pair']==OTHER


def test_shared_account_is_ambiguous_even_with_named_foreign_pool():
    row=call(discriminator('global',NAME)+b'garbage', [POOL, OTHER, POS])
    assert row['scope']=='AMBIGUOUS_SHARED_ACCOUNT'


def test_unknown_discriminator_never_becomes_target():
    row=call(bytes.fromhex('ebc23645410aec70'), [POS,POOL,OTHER])
    assert row['scope']=='UNRESOLVED'
    assert row['args_status']=='NOT_IN_PINNED_IDL'


def test_account_vector_truncation_blocks_owner_guess():
    row=call(discriminator('global',NAME),[POOL])
    assert row['scope']=='UNRESOLVED'
    assert 'truncated' in row['reason']


def test_close_position_without_lb_pair_remains_unresolved():
    row=call(discriminator('global','close_position2'),[POS,OTHER])
    assert row['scope']=='UNRESOLVED_NO_UNIQUE_LB_PAIR_ROLE'


def test_gfta_scanner_preserves_unparseable_target_as_blocking_inventory():
    from mdlmm.codec import b58encode
    inner={'programId':PROGRAM_ID, 'accounts':[POS,POOL,OTHER],
           'data':b58encode(discriminator('global',NAME)+b'garbage'), 'stackHeight':2}
    top={'programId':OTHER,'accounts':[POOL], 'data':''}
    tx={'transaction':{'signatures':['sig'], 'message':{'accountKeys':[POOL,OTHER],
                         'instructions':[top]}},
        'meta':{'err':None,'innerInstructions':[{'index':0,'instructions':[inner]}]}}
    found=scoped_dlmm_instructions(tx,POOL,idl())
    assert len(found)==1
    assert found[0]['name']==NAME
    assert found[0]['owner']=='TARGET'
    assert found[0]['args_status']=='IDL_ARGS_UNPARSEABLE'
