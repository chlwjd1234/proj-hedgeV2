from copy import deepcopy
import pytest
from mdlmm.perp import *
from mdlmm.errors import *


@pytest.fixture(autouse=True)
def close_test_owned_book_stores(monkeypatch):
    """Each test owns its in-memory stores, including exception-test branches."""
    opened=[]
    original=BookStore.__init__
    def tracked_init(self,*args,**kwargs):
        original(self,*args,**kwargs)
        opened.append(self)
    monkeypatch.setattr(BookStore,'__init__',tracked_init)
    yield
    for store in opened:
        store.db.close()


def terms():
    return {'start_ms':0,'end_ms':10*HOUR,'maker_rate':'0.00015','taker_rate':'0.00045',
            'lot_sol':'0.1','initial_margin_rate':'0.2','maintenance_margin_rate':'0.1',
            'minimum_notional':'0','source':'EXPLICIT_SYNTHETIC_TERMS'}


def venue():
    store=BookStore()
    for t in (0,100,1000,1100,2000,2100,3000,4000,5000):
        store.add(Book.from_dict({'ts_ms':t,'available_ms':t,'bids':[['99','1'],['98','2']],
                   'asks':[['101','1'],['102','2']],'source':'SYNTHETIC_BOOK'}))
    funding=FundingSeries([],0,HOUR,'SYNTHETIC_NO_PAYABLE_HOUR')
    return HyperliquidSOL(store,TermsSchedule([terms()]),funding,max_book_age_ms=1000)


@pytest.mark.parametrize('q',['0.1','1','2','3','4','-0.1','-1','-2','-3','-4'])
def test_depth_walk_and_conservation(q):
    v=venue();q=dec(q)
    first=v.quote_order_cost(q,0); second=v.quote_order_cost(q,0)
    assert first==second
    filled=v.fill(q,0)
    assert filled==first
    assert filled.quantity_sol+filled.unfilled_sol==q
    assert abs(filled.quantity_sol)<=3
    assert filled.fee_usdc==abs(filled.quantity_sol)*filled.vwap*dec('0.00045')
    assert filled.slippage_vs_mid_usdc>=0
    v.books.db.close()


def test_depth_not_reused_without_refresh():
    v=venue();v.fill('3',0)
    with pytest.raises(Unavailable):v.fill('.1',1)
    assert v.fill('3',100).quantity_sol==3


def test_unrepresentable_lot_rejected():
    with pytest.raises(ValidationError):venue().fill('.01',0)


def test_stale_and_future_book_rejected():
    v=venue()
    with pytest.raises(Unavailable):v.books.asof(-1,1000)
    with pytest.raises(Unavailable):v.books.asof(7000,1000)
    b=BookStore();b.add(Book.from_dict({'ts_ms':0,'available_ms':1000,'bids':[['99',1]],'asks':[['101',1]]}))
    with pytest.raises(Unavailable):b.asof(999,2000)


@pytest.mark.parametrize('lp',[D('-10.33'),D('10.33'),D('0.03')])
@pytest.mark.parametrize('h',[D('-15'),D('0'),D('15')])
def test_boundary_targets_resulting_position(lp,h):
    t=VenueTerms.from_dict(terms())
    result=target_position(lp,h,10000,100,t,policy='boundary',band_bps=20)
    assert abs(lp+result)<=D('.2')
    assert result/t.lot_sol==(result/t.lot_sol).to_integral_value()
    if abs(lp+h)<=D('.2'):assert result==h


def test_no_lot_inside_tiny_band():
    with pytest.raises(Unavailable):
        target_position('.03',0,10000,100,VenueTerms.from_dict(terms()),policy='boundary',band_bps=0)


def test_schedule_gap_and_overlap():
    t=terms();t['start_ms']=100
    schedule=TermsSchedule([t])
    with pytest.raises(Unavailable):schedule.at(99)
    with pytest.raises(Unavailable):schedule.at(10*HOUR)
    with pytest.raises(ValidationError):TermsSchedule([t,t])


def test_funding_signed_hourly_and_missing_not_zero():
    f=FundingSeries([{'time_ms':HOUR,'rate':'.001','oracle_price':100},
                     {'time_ms':2*HOUR,'rate':'-.002','oracle_price':110}],0,3*HOUR,'observed')
    assert f.payment(-10,0,HOUR)==1
    assert f.payment(-10,HOUR,2*HOUR)==D('-2.2')
    assert f.payment(10,0,2*HOUR)==D('1.2')
    with pytest.raises(Unavailable):f.payment(1,2*HOUR,3*HOUR)
    assert f.payment(0,2*HOUR,3*HOUR)==0
    with pytest.raises(Unavailable):FundingSeries([{'time_ms':HOUR,'rate':0}],0,HOUR,'observed')


def test_frozen_target_not_recomputed_from_future_inventory():
    engine=DynamicHedge(venue(),1000,latency_ms=1000,policy='exact')
    engine.decision(0,1,10000,100)
    engine.decision(1000,2,10000,100)
    fill=[r for r in engine.records if r['kind']=='fill'][0]
    assert fill['quantity_sol']==-1 and fill['frozen_lp_delta']==1
    assert engine.pending['target_position']==-2


def test_maker_first_credits_no_magic_fills():
    engine=DynamicHedge(venue(),1000,latency_ms=100,policy='exact',execution='maker-first',maker_timeout_ms=2000)
    engine.decision(0,1,10000,100)
    engine.decision(1000,1,10000,100)
    assert engine.account.position_sol==0
    engine.decision(2100,1,10000,100)
    assert engine.account.position_sol==-1
    fills=[r for r in engine.records if r['kind']=='fill']
    assert len(fills)==1 and fills[0]['liquidity']=='TAKER'


def test_insufficient_collateral_no_remote_lp_credit():
    v=venue();engine=DynamicHedge(v,1,latency_ms=0,policy='exact')
    with pytest.raises(ValidationError,match='MARGIN_CONSTRAINT'):
        engine.decision(0,1,1000000,100)
    assert engine.account.position_sol==0 and not v.used_depth


def test_perp_account_identity_roundtrip():
    a=PerpAccount(D(1000));t=VenueTerms.from_dict(terms())
    a.apply_fill(Fill(0,D(-2),D(99),D('.1'),D(2),D(0),0,'x'),t,D(100))
    a.mark(90)
    a.apply_fill(Fill(1000,D(2),D(91),D('.1'),D(2),D(0),1000,'x'),t,D(90))
    assert a.position_sol==0
    # Sale 2*99 minus buy 2*91 minus fees .2 = 15.8.
    assert a.equity==D('1015.8')
    a.mark(200);assert a.equity==D('1015.8')


def test_static_oracle():
    report=static_hedge_oracle()
    assert report['status']=='PASS'
    assert all(abs(r['accounting_residual'])<D('1e-40') for r in report['rows'])
