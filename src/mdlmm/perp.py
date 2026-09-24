"""Read-only Hyperliquid SOL research execution, never real order submission.

Time-scoped fees, depth-aware taker fills, conservative maker-first fallback,
integer resulting-position geometry, hourly funding and isolated margin ledger.
"""
from __future__ import annotations

import bisect
import json
import sqlite3
from dataclasses import dataclass, asdict
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN
from pathlib import Path
from typing import Protocol

from .errors import Unavailable, ValidationError
from .util import D, dec, read_jsonl, read_json, iso_ms, canonical

HOUR = 3_600_000


@dataclass(frozen=True)
class VenueTerms:
    start_ms: int
    end_ms: int
    maker_rate: D
    taker_rate: D
    lot_sol: D
    initial_margin_rate: D
    maintenance_margin_rate: D
    minimum_notional: D
    source: str

    @classmethod
    def from_dict(cls,d):
        d=dict(d)
        for k in ('maker_rate','taker_rate','lot_sol','initial_margin_rate','maintenance_margin_rate','minimum_notional'):
            d[k]=dec(d[k])
        out=cls(**d)
        if not (out.end_ms>out.start_ms and out.lot_sol>0 and out.taker_rate>=0
                and 0<out.maintenance_margin_rate<=out.initial_margin_rate<=1
                and out.minimum_notional>=0 and out.source):
            raise ValidationError('invalid/unproven venue terms')
        return out


class TermsSchedule:
    def __init__(self,rows:list[dict]):
        self.terms=sorted([VenueTerms.from_dict(r) for r in rows],key=lambda r:r.start_ms)
        for a,b in zip(self.terms,self.terms[1:]):
            if a.end_ms>b.start_ms:
                raise ValidationError('overlapping venue fee/margin intervals')

    def at(self,t:int)->VenueTerms:
        found=[r for r in self.terms if r.start_ms<=t<r.end_ms]
        if len(found)!=1:
            raise Unavailable(f'historical fee/lot/margin terms missing at {t}')
        return found[0]


@dataclass(frozen=True)
class Book:
    ts_ms: int
    available_ms: int
    bids: tuple[tuple[D,D],...]
    asks: tuple[tuple[D,D],...]
    source: str

    @classmethod
    def from_dict(cls,row:dict,*,observation_delay_ms:int|None=None):
        data=row.get('data',row.get('raw',row))
        if isinstance(data,str):
            data=json.loads(data)
        if data.get('coin','SOL')!='SOL':
            raise ValidationError('Hyperliquid adapter only accepts SOL perp')
        t=data.get('ts_ms',data.get('time'))
        if isinstance(t,str) and not t.isdigit():
            t=iso_ms(t)
        if t is None:
            raise ValidationError('book has no timestamp')
        t=int(t)
        available=data.get('available_ms',row.get('available_ms'))
        if available is None:
            if observation_delay_ms is None:
                raise Unavailable('archive has no observation time: explicit observation_delay_ms required')
            available=t+observation_delay_ms
        levels=data.get('levels')
        if levels is not None:
            bids=tuple((dec(r['px']),dec(r['sz'])) for r in levels[0])
            asks=tuple((dec(r['px']),dec(r['sz'])) for r in levels[1])
        else:
            bids=tuple((dec(p),dec(q)) for p,q in data['bids'])
            asks=tuple((dec(p),dec(q)) for p,q in data['asks'])
        if not bids or not asks or any(p<=0 or q<0 for p,q in bids+asks):
            raise ValidationError('empty/invalid book levels')
        if any(a[0]<=b[0] for a,b in zip(bids,bids[1:])) or any(a[0]>=b[0] for a,b in zip(asks,asks[1:])):
            raise ValidationError('book levels not strictly price sorted')
        if bids[0][0]>=asks[0][0] or int(available)<t:
            raise ValidationError('crossed book or noncausal availability')
        return cls(t,int(available),bids,asks,data.get('source',row.get('source','HYPERLIQUID_L2_ARCHIVE')))

    @property
    def mid(self):
        return (self.bids[0][0]+self.asks[0][0])/2


class BookStore:
    def __init__(self,database:Path|str=':memory:'):
        self.db=sqlite3.connect(str(database))
        self.db.execute('CREATE TABLE IF NOT EXISTS books(ts INTEGER PRIMARY KEY, available INTEGER NOT NULL, payload TEXT NOT NULL)')

    def add(self,book:Book):
        try:
            self.db.execute('INSERT INTO books VALUES(?,?,?)',(book.ts_ms,book.available_ms,canonical(asdict(book)).decode()))
        except sqlite3.IntegrityError as exc:
            raise ValidationError('duplicate L2 timestamp; normalize/collapse identical snapshots explicitly') from exc

    def load(self,path:Path,*,observation_delay_ms:int|None=None):
        count=0
        with self.db:
            for row in read_jsonl(path):
                self.add(Book.from_dict(row,observation_delay_ms=observation_delay_ms));count+=1
        if not count:
            raise Unavailable('empty historical L2 file')
        return count

    def asof(self,t:int,max_age_ms:int)->Book:
        row=self.db.execute('SELECT ts,payload FROM books WHERE ts<=? AND available<=? ORDER BY ts DESC LIMIT 1',(t,t)).fetchone()
        if row is None or t-row[0]>max_age_ms:
            raise Unavailable(f'L2 missing/stale at modeled arrival {t}')
        return Book.from_dict(json.loads(row[1]))


@dataclass(frozen=True)
class Fill:
    time_ms: int
    quantity_sol: D
    vwap: D
    fee_usdc: D
    slippage_vs_mid_usdc: D
    unfilled_sol: D
    book_time_ms: int
    terms_source: str
    liquidity: str='TAKER'


class PerpVenue(Protocol):
    def mark_price_at(self,time_ms:int)->D: ...
    def quote_order_cost(self,quantity_sol:D,arrival_ms:int)->Fill: ...
    def fill(self,quantity_sol:D,arrival_ms:int)->Fill: ...
    def terms_at(self,time_ms:int)->VenueTerms: ...
    def funding_payment(self,position_sol:D,start_ms:int,end_ms:int)->D: ...


class FundingSeries:
    def __init__(self,rows:list[dict],coverage_start_ms:int,coverage_end_ms:int,source:str):
        self.start,self.end,self.source=coverage_start_ms,coverage_end_ms,source
        self.points={}
        if self.end<=self.start or not source:
            raise ValidationError('funding requires explicit coverage and source')
        for r in rows:
            t=int(r.get('time_ms',r.get('time',-1)))
            if t in self.points:
                raise ValidationError('duplicate funding interval')
            if t % HOUR:
                raise ValidationError('normalized funding time must identify UTC hour boundary')
            if 'oracle_price' not in r:
                raise Unavailable('funding mark conversion requires historical oracle_price')
            rate=dec(r.get('rate',r.get('fundingRate')))
            oracle=dec(r['oracle_price'])
            if oracle<=0:
                raise ValidationError('invalid funding oracle price')
            self.points[t]=(rate,oracle)

    def payment(self,position_sol,start_ms,end_ms):
        position=dec(position_sol)
        if end_ms<start_ms:
            raise ValidationError('funding interval regresses')
        if position==0:
            return D(0)
        if start_ms<self.start or end_ms>self.end:
            raise Unavailable('held position extends beyond funding coverage')
        first=(start_ms//HOUR+1)*HOUR
        total=D(0)
        for t in range(first,end_ms+1,HOUR):
            if t not in self.points:
                raise Unavailable(f'missing payable funding event {t}; not replaced with zero')
            rate,oracle=self.points[t]
            total-=position*oracle*rate
        return total


class HyperliquidSOL:
    def __init__(self,books:BookStore,schedule:TermsSchedule,funding:FundingSeries,*,max_book_age_ms:int):
        self.books,self.schedule,self.funding=books,schedule,funding
        self.max_age=max_book_age_ms
        self.used_depth:dict[tuple[int,str,D],D]={}

    def terms_at(self,time_ms):
        return self.schedule.at(time_ms)

    def mark_price_at(self,time_ms):
        return self.books.asof(time_ms,self.max_age).mid

    def _walk(self,quantity_sol,arrival_ms,*,consume:bool)->Fill:
        qty=dec(quantity_sol)
        terms=self.schedule.at(arrival_ms)
        if qty==0 or qty/terms.lot_sol!=(qty/terms.lot_sol).to_integral_value():
            raise ValidationError('order must be nonzero integer lot quantity')
        book=self.books.asof(arrival_ms,self.max_age)
        if abs(qty)*book.mid<terms.minimum_notional:
            raise Unavailable('order below venue minimum notional')
        side='asks' if qty>0 else 'bids'
        remaining=abs(qty);notional=D(0);filled=D(0);changes=[]
        for px,size in getattr(book,side):
            key=(book.ts_ms,side,px)
            capacity=max(D(0),size-self.used_depth.get(key,D(0)))
            capacity=(capacity/terms.lot_sol).to_integral_value(rounding=ROUND_FLOOR)*terms.lot_sol
            take=min(remaining,capacity)
            if take>0:
                filled+=take;notional+=take*px;remaining-=take;changes.append((key,take))
            if remaining==0:
                break
        if not filled:
            raise Unavailable('no usable historical L2 depth')
        if consume:
            for key,size in changes:
                self.used_depth[key]=self.used_depth.get(key,D(0))+size
        sign=D(1) if qty>0 else D(-1)
        vwap=notional/filled
        return Fill(arrival_ms,sign*filled,vwap,notional*terms.taker_rate,
                    sign*filled*(vwap-book.mid),sign*remaining,book.ts_ms,terms.source)

    def quote_order_cost(self,quantity_sol,arrival_ms):
        return self._walk(quantity_sol,arrival_ms,consume=False)

    def fill(self,quantity_sol,arrival_ms):
        return self._walk(quantity_sol,arrival_ms,consume=True)

    def funding_payment(self,position_sol,start_ms,end_ms):
        return self.funding.payment(position_sol,start_ms,end_ms)


@dataclass
class PerpAccount:
    collateral: D
    position_sol: D=D(0)
    mark_pnl: D=D(0)
    fees: D=D(0)
    slippage: D=D(0)
    funding: D=D(0)
    last_mark: D|None=None

    @property
    def equity(self):
        return self.collateral+self.mark_pnl+self.funding-self.fees-self.slippage

    def mark(self,price):
        price=dec(price)
        if price<=0:
            raise ValidationError('nonpositive perp mark')
        if self.last_mark is not None:
            self.mark_pnl+=self.position_sol*(price-self.last_mark)
        self.last_mark=price

    def check_margin(self,terms:VenueTerms,*,initial:bool=False,position=None,extra_cost=0):
        if self.last_mark is None:
            raise ValidationError('margin has no mark price')
        q=self.position_sol if position is None else dec(position)
        ratio=terms.initial_margin_rate if initial else terms.maintenance_margin_rate
        required=abs(q*self.last_mark)*ratio
        if self.equity-dec(extra_cost)<required:
            raise ValidationError('MARGIN_CONSTRAINT: no LP value or remote wallet counted as collateral')
        return self.equity-required

    def apply_fill(self,fill:Fill,terms:VenueTerms,mid):
        self.mark(mid)
        target=self.position_sol+fill.quantity_sol
        # Initial margin is required for an exposure increase; reducing orders
        # are allowed provided modeled maintenance solvency remains satisfied.
        increasing=abs(target)>abs(self.position_sol)
        self.check_margin(terms,initial=increasing,position=target,
                          extra_cost=fill.fee_usdc+fill.slippage_vs_mid_usdc)
        self.position_sol=target
        self.fees+=fill.fee_usdc;self.slippage+=fill.slippage_vs_mid_usdc
        self.check_margin(terms)


def target_position(lp_delta_sol,position_sol,nav_usdc,reference_price,terms:VenueTerms,
                    *,policy='boundary',band_bps=0)->D:
    lp,h,nav,s=map(dec,(lp_delta_sol,position_sol,nav_usdc,reference_price))
    if nav<=0 or s<=0 or dec(band_bps)<0:
        raise ValidationError('invalid hedge policy state')
    lot=terms.lot_sol
    if policy=='exact':
        return ((-lp/lot).to_integral_value(rounding=ROUND_HALF_EVEN))*lot
    width=nav*dec(band_bps)/10000/s
    lower,upper=-lp-width,-lp+width
    if lower<=h<=upper:
        return h
    if policy=='boundary':
        new=((lower/lot).to_integral_value(rounding=ROUND_CEILING)*lot if h<lower
             else (upper/lot).to_integral_value(rounding=ROUND_FLOOR)*lot)
    elif policy=='center':
        new=(-lp/lot).to_integral_value(rounding=ROUND_HALF_EVEN)*lot
    else:
        raise ValidationError('unknown hedge policy')
    if not lower<=new<=upper:
        raise Unavailable('no representable integer-lot position inside requested hedge band')
    return new


class DynamicHedge:
    """Single-flight frozen-target scheduler. Pending targets never use future LP.

    Maker-first without queue evidence credits NO maker fills, then takes after
    timeout. L2-only quote touches do not constitute maker-fill evidence.
    """
    def __init__(self,venue:PerpVenue,collateral,*,latency_ms:int,policy='boundary',band_bps=200,
                 execution='taker',maker_timeout_ms=1000):
        if min(latency_ms,maker_timeout_ms)<0 or execution not in ('taker','maker-first'):
            raise ValidationError('invalid execution policy')
        self.venue=venue;self.account=PerpAccount(dec(collateral))
        self.latency=latency_ms;self.policy=policy;self.band=band_bps
        self.execution=execution;self.maker_timeout=maker_timeout_ms
        self.pending=None;self.time=None;self.records=[]

    def _advance(self,t:int):
        if self.time is not None:
            self.account.funding+=self.venue.funding_payment(self.account.position_sol,self.time,t)
        self.account.mark(self.venue.mark_price_at(t))
        self.account.check_margin(self.venue.terms_at(t))
        self.time=t

    def _fill_due(self,t:int):
        if self.pending is None or self.pending['arrival_ms']>t:
            return
        order=self.pending;arrival=order['arrival_ms']
        self._advance(arrival)
        quantity=order['target_position']-self.account.position_sol
        preview=self.venue.quote_order_cost(quantity,arrival)
        terms=self.venue.terms_at(arrival)
        mid=self.venue.mark_price_at(arrival)
        # Check before consuming book depth to keep rejection transactional.
        target=self.account.position_sol+preview.quantity_sol
        self.account.check_margin(terms,initial=abs(target)>abs(self.account.position_sol),
                                  position=target,extra_cost=preview.fee_usdc+preview.slippage_vs_mid_usdc)
        fill=self.venue.fill(quantity,arrival)
        self.account.apply_fill(fill,terms,mid)
        self.records.append({'kind':'fill',**asdict(fill),'decision_ms':order['decision_ms'],
                             'frozen_lp_delta':order['lp_delta'], 'equity':self.account.equity,
                             'maker_policy':'NO_FILL_CREDIT_WITHOUT_QUEUE_EVIDENCE' if self.execution=='maker-first' else None})
        self.pending=None

    def decision(self,time_ms:int,lp_delta_sol,nav_usdc,reference_price):
        if self.time is not None and time_ms<self.time:
            raise ValidationError('hedge scheduler time regression')
        self._fill_due(time_ms)
        self._advance(time_ms)
        if self.pending is not None:
            self.records.append({'kind':'pending_skip','time_ms':time_ms})
            return
        target=target_position(lp_delta_sol,self.account.position_sol,nav_usdc,reference_price,
                               self.venue.terms_at(time_ms),policy=self.policy,band_bps=self.band)
        if target==self.account.position_sol:
            return
        extra=self.maker_timeout if self.execution=='maker-first' else 0
        self.pending={'decision_ms':time_ms,'arrival_ms':time_ms+self.latency+extra,
                      'target_position':target,'lp_delta':dec(lp_delta_sol)}
        self.records.append({'kind':'order_intent',**self.pending})
        self._fill_due(time_ms)

    def finish(self,end_ms:int):
        self._fill_due(end_ms)
        self._advance(end_ms)
        return {'status':'PASS','mode':'READ_ONLY_SIMULATED_EXECUTION',
                'position_sol':self.account.position_sol,'collateral':self.account.collateral,
                'equity':self.account.equity,'mark_pnl':self.account.mark_pnl,
                'execution_fees':self.account.fees,'slippage':self.account.slippage,
                'funding':self.account.funding,'pending_at_end':self.pending,
                'funding_tie_convention':'FUNDING_BEFORE_FILL_AT_EQUAL_TIMESTAMP',
                'margin_check_scope':'DECISION_AND_FILL_MARKS_NOT_CONTINUOUS_LIQUIDATION_REPLAY',
                'mark_model':'HISTORICAL_L2_MID_PROXY_NOT_EXCHANGE_MARK_ORACLE',
                'strategy_success':False,'records':self.records}


def static_hedge_oracle()->dict:
    """Two fixed bins, p=90/110, NAV=10k, LP=4k. Fees/funding explicitly absent.

    This is an analytical accounting fixture, NOT historical performance.
    """
    def quantities(s):
        # 2,000 quote in lower bin; 20 SOL in upper bin at entry S=100.
        x=(D(2000)/90 if s<90 else D(0))+(D(20) if s<110 else D(0))
        y=(D(0) if s<90 else D(2000))+(D(0) if s<110 else D(2200))
        return x,y
    rows=[]
    for price in (D(80),D(100),D(120)):
        x,y=quantities(price)
        short=D(-20)
        account=PerpAccount(D(6000),short,last_mark=D(100))
        account.mark(price)
        nav=x*price+y+account.equity
        expected= D(88000)/9 if price==80 else D(9800) if price==120 else D(10000)
        residual=nav-expected
        if abs(residual)>D('1e-40'):
            raise ValidationError('static hedge analytical parity failed')
        rows.append({'price':price,'lp_sol':x,'lp_usdc':y,'initial_hedge_sol':short,
                     'residual_delta_sol':x+short,'hedge_collateral':account.collateral,
                     'hedge_mark_pnl':account.mark_pnl,'nav':nav,'analytical_nav':expected,
                     'accounting_residual':residual})
    return {'status':'PASS','scope':'ANALYTICAL_SYNTHETIC_ORACLE','rows':rows,
            'fees':'EXPLICITLY_ZERO_ANALYTICAL_ASSUMPTION',
            'funding':'EXPLICITLY_ZERO_ANALYTICAL_ASSUMPTION','historical_strategy_evidence':False}
