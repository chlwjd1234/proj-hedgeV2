from copy import deepcopy
from dataclasses import replace
from decimal import Decimal,localcontext
import pytest
from mdlmm.mechanics import *
from mdlmm.errors import *
from mdlmm.model import *

@pytest.mark.parametrize('step',[1,10,40,100,1000])
@pytest.mark.parametrize('i',[-100,-3,-1,0,1,3,100])
def test_integer_price_vs_high_precision(step,i):
    result=dec(price_from_id(i,step))/Q64
    with localcontext() as c:
        c.prec=80
        expected=(D(1)+D(step)/10000)**i
        assert abs(result/expected-1)<D('1e-11')

@pytest.mark.parametrize('gross',[1,2,7,10000,10**9,2**64-1])
@pytest.mark.parametrize('rate',[0,1,100000,MAX_FEE_RATE])
def test_fee_integer_rounding(gross,rate):
    fee=fee_from_gross(gross,rate)
    assert fee*FEE_PRECISION>=gross*rate
    assert fee==0 or (fee-1)*FEE_PRECISION<gross*rate
    assert 0<=fee<=gross

@pytest.mark.parametrize('total',[0,1,2,9,99999999])
@pytest.mark.parametrize('split',[0,1000,2000,10000])
def test_decomposition_conserves(total,split):
    p=fee_components(total,123,456,split)
    assert p['lp_base']+p['lp_variable']+p['protocol_fee']==total
    assert p['trader_base']+p['trader_variable']==total
    assert min(p['lp_base'],p['lp_variable'])>=0

@pytest.mark.parametrize('direction',[True,False])
@pytest.mark.parametrize('mode',[0,1])
@pytest.mark.parametrize('amount',[2,1000000,1100000000])
def test_quote_conservation_purity_and_currency(state,direction,mode,amount):
    state.parameters.collect_fee_mode=mode
    before=deepcopy(state)
    q=quote_exact_in(state,amount,direction,1001)
    assert state.to_dict()==before.to_dict()
    assert q.amount_in==amount and q.amount_left==0
    assert sum(f.gross_input for f in q.fills)==amount
    assert all(f.token_conservation_x==0 and f.token_conservation_y==0 for f in q.fills)
    assert q.fee==sum(f.lp_base+f.lp_variable+f.protocol_fee for f in q.fills)
    expected='x' if direction and mode==0 else 'y'
    assert {f.fee_token for f in q.fills}=={expected}
    q.state.bins[3].amount_x=0
    assert state.bins[3].amount_x==before.bins[3].amount_x

@pytest.mark.parametrize('elapsed,reference,vr',[(29,99,80000),(30,0,50000),(599,0,50000),(600,0,0)])
def test_variable_reference_thresholds(state,elapsed,reference,vr):
    state.variable=VariableState(100000,80000,99,1000)
    update_references(state,1000+elapsed)
    assert state.variable.index_reference==reference
    assert state.variable.volatility_reference==vr

def test_negative_time_no_fallback(state):
    with pytest.raises(ValidationError):quote_exact_in(state,100,True,999)

def test_missing_array_not_empty(state):
    state.complete_bin_map=False;state.bins.pop(-1)
    with pytest.raises(Unsupported):quote_exact_in(state,2_000_000_000,True,1001)

def test_known_array_incomplete_is_invalid(state):
    state.complete_bin_map=False;state.known_bin_arrays=[-1];state.bins.pop(-1)
    with pytest.raises(ValidationError):quote_exact_in(state,2_000_000_000,True,1001)

def test_limit_orders_not_silently_ignored(state):
    state.bins[0].open_order_amount=1
    with pytest.raises(Unsupported):quote_exact_in(state,100,True,1001)

@pytest.mark.parametrize('what',['collect_fee_mode','token_flag','overflow','zero'])
def test_explicit_unsupported(state,what):
    if what=='collect_fee_mode':state.parameters.collect_fee_mode=9
    if what=='token_flag':state.token_y_program_flag=1
    with pytest.raises((Unsupported,ValidationError)):
        quote_exact_in(state,2**64 if what=='overflow' else 0 if what=='zero' else 100,True,1001)

def test_exact_drain_does_not_advance(state):
    state.parameters.base_factor=0;state.parameters.variable_fee_control=0
    q=quote_exact_in(state,state.bins[0].amount_y,True,1001)
    assert q.end_bin==0 and q.state.bins[0].amount_y==0

def test_zero_total_fee_with_zero_rates():
    assert fee_components(0,0,0,1000)['lp_base']==0
    with pytest.raises(ValidationError):fee_components(1,0,0,1000)

def test_partial_quote_explicit(state):
    with pytest.raises(InsufficientLiquidity):quote_exact_in(state,10_000_000_000,True,1001)
    q=quote_exact_in(state,10_000_000_000,True,1001,allow_partial=True)
    assert q.amount_left>0 and q.amount_in+q.amount_left==10_000_000_000


@pytest.mark.parametrize('delta',[0,1,1000,100000000000])
@pytest.mark.parametrize('rate',[0,1,1000000,100000000])
def test_composition_fee_integer_formula(delta,rate):
    from mdlmm.mechanics import composition_fee
    r=composition_fee(delta,rate,1000)
    expected=delta*rate*(10**9+rate)//10**18
    assert r['trader_total']==expected
    assert r['lp_composition']+r['protocol']==expected
