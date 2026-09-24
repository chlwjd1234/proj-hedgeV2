from decimal import Decimal as D
import pytest
from mdlmm.ledger import Ledger,InventoryBenchmark,CostBasis
from mdlmm.reference import Reference,markouts
from mdlmm.errors import *


def test_self_financing_benchmark_cash_and_pnl():
    b=InventoryBenchmark.start(2,1000,100)
    b.mark_and_rebalance(110,3)
    assert b.pnl==20 and b.cash==690
    b.mark_and_rebalance(90,1)
    assert b.pnl==-40 and b.quantity*90+b.cash==960


def test_fee_claim_not_new_income_and_protocol_no_double_cut():
    l=Ledger.start(10000,20,2000,100)
    l.earn_fee('base',usdc=9,protocol_usdc=1)
    assert l.report()['nav_pnl']==9
    assert l.report()['protocol_cut_informational_only']==1


def test_fee_sol_price_pnl_removed_from_mm_surplus():
    l=Ledger.start(10000,20,2000,100)
    l.earn_fee('variable',sol=1)
    l.mark(120)
    r=l.report()
    assert r['raw_lp_principal_pnl']==400
    assert r['fee_asset_price_pnl']==20
    assert r['inventory_matched_benchmark_pnl']==420
    assert r['inventory_matched_surplus']==100
    assert r['nav_accounting_residual']==0


def test_external_flow_and_collateral_never_profit():
    l=Ledger.start(10000,20,2000,100)
    l.external_cash_flow(500)
    l.transfer_collateral(1000)
    assert l.nav()==10500 and l.report()['nav_pnl']==0
    l.transfer_collateral(-1000)
    l.external_cash_flow(-500)
    assert l.report()['nav']==10000
    with pytest.raises(ValidationError):l.transfer_collateral(100000)


def test_reallocation_not_profit():
    l=Ledger.start(10000,20,2000,100)
    l.reallocate(10,1000,acquisition_cost=2)
    assert l.report()['nav_pnl']==-2
    l.mark(120)
    assert l.report()['nav_pnl']==198
    assert l.report()['inventory_matched_surplus']==-2


def test_costs_require_spendable_cash():
    l=Ledger.start(4000,20,2000,100)
    with pytest.raises(ValidationError):l.charge('lp_execution_cost',1)
    with pytest.raises(ValidationError):l.charge('unknown',0)


def test_wac_realized_unrealized():
    b=CostBasis();b.trade(D(2),D(100));b.trade(D(1),D(160))
    b.trade(D(-1),D(150))
    assert b.realized==30 and b.unrealized(D(100))==-40
    with pytest.raises(ValidationError):b.trade(D(-3),D(100))


def ref_fixture():
    r=Reference();r.insert([
        {'ts_ms':0,'available_ms':0,'price':'100','source':'TEST','symbol':'SOL/USDC'},
        {'ts_ms':1000,'available_ms':2000,'price':'110','source':'TEST','symbol':'SOL/USDC'},
        {'ts_ms':3000,'available_ms':3000,'price':'90','source':'TEST','symbol':'SOL/USDC'}])
    return r


def test_reference_causality_and_staleness():
    r=ref_fixture()
    assert r.asof(1500,max_age_ms=2000).price==100
    assert r.asof(2000,max_age_ms=2000).price==110
    with pytest.raises(Unavailable):r.asof(5000,max_age_ms=1000)
    r.close()


def test_markout_not_decision_price():
    r=ref_fixture()
    result=markouts(r,0,1,-101,2,horizons_ms=(0,1000,2000,60000),max_delay_ms=1000)
    assert result[0]['directional_adverse_markout']==1
    assert result[0]['fee_minus_markout']==1
    assert result[1]['ex_post_diagnostic'] and result[1]['reference_price']==110
    assert result[2]['actual_horizon_ms']==3000
    assert result[3]['status']=='UNAVAILABLE'
    r.close()


def test_reference_ambiguous_numeraire_and_duplicate_rejected():
    r=Reference()
    with pytest.raises(ValidationError):r.insert([{'ts_ms':0,'available_ms':0,'price':100,'source':'x','symbol':'SOL/USD'}])
    row={'ts_ms':0,'available_ms':0,'price':100,'source':'x','symbol':'SOL/USDC'}
    r.insert([row])
    with pytest.raises(ValidationError):r.insert([row])
    r.close()
