"""Historical MM-share attribution with actual inventory changes, not volume*TVL."""
from __future__ import annotations
import math
from dataclasses import dataclass,field
from .capacity import to_economic
from .errors import Unavailable, ValidationError
from .ledger import Ledger
from .mechanics import human_price
from .model import PoolState
from .reference import Reference,markouts
from .util import D,dec


@dataclass
class ShadowPosition:
    shares: dict[int,int]
    lower_bin: int
    upper_bin: int
    initial_deployed: D
    desired_deployed: D
    floor_error_bound_usdc: D = D(0)

    @classmethod
    def spot(cls,state:PoolState,capital,price,width_fraction):
        cap,s,w=dec(capital),dec(price),dec(width_fraction)
        if not 0<w<1 or cap<=0:
            raise ValidationError('invalid attribution width/capital')
        # Geometric offsets around the observed active bin, not a future spot.
        low=state.active_id+math.floor(math.log1p(-float(w))/math.log1p(state.bin_step/10000))
        high=state.active_id+math.ceil(math.log1p(float(w))/math.log1p(state.bin_step/10000))
        per=cap/(high-low+1)
        shares={}
        bound=D(0)
        for i in range(low,high+1):
            b=state.bins.get(i)
            if b is None:
                if not state.complete_bin_map:
                    raise Unavailable('attribution range contains unknown bin state')
                # Empty historical bins cannot be acquired as existing shares.
                raise Unavailable('historically empty bin prevents full Spot attribution; not silently redeployed')
            sol,usdc=to_economic(state,b.amount_x,b.amount_y)
            value=sol*s+usdc
            if value<=0 or b.liquidity_supply<=0:
                raise Unavailable('Spot range contains empty historical MM liquidity')
            wanted=int(per/value*b.liquidity_supply)
            if wanted<=0:
                raise Unavailable('capital smaller than a representable liquidity share')
            if wanted>b.liquidity_supply:
                raise Unavailable('requested capital exceeds available historical ownership in a bin')
            shares[i]=wanted
            bound+=s/D(10**9)+D(1)/D(10**6)
        obj=cls(shares,low,high,D(0),cap,bound)
        sol,usdc=obj.inventory(state)
        obj.initial_deployed=sol*s+usdc
        return obj

    def fraction(self,state:PoolState,bin_id:int)->D:
        own=self.shares.get(bin_id,0)
        if not own:
            return D(0)
        b=state.bins.get(bin_id)
        if b is None or b.liquidity_supply<=0:
            raise Unavailable('owned historical liquidity disappeared')
        if own>b.liquidity_supply:
            raise Unavailable('historical burns exceed other ownership: fixed-share attribution invalid')
        return dec(own)/b.liquidity_supply

    def inventory(self,state:PoolState)->tuple[D,D]:
        x=y=D(0)
        for i in self.shares:
            fraction=self.fraction(state,i)
            b=state.bins[i]
            x+=fraction*b.amount_x;y+=fraction*b.amount_y
        return to_economic(state,x,y)

    def attribution(self,pre:PoolState,fees:list[dict])->list[dict]:
        out=[]
        for row in fees:
            f=self.fraction(pre,row['bin_id'])
            if not f:
                continue
            out.append({**row,'fraction':f,'lp_base':f*row.get('lp_base',0),
                        'lp_variable':f*row.get('lp_variable',0),'lp_composition':f*row.get('lp_composition',0),'protocol':f*row['protocol'],
                        'trader_total':f*row['trader_total']})
        return out


class AttributionEngine:
    def __init__(self,state:PoolState,reference:Reference,*,start_ms:int,nav,lp_fraction,width_fraction,
                 max_reference_age_ms:int,markout_max_delay_ms:int,costs:dict,
                 end_ms:int,horizons_ms=(0,1000,10000,60000,300000)):
        price=reference.asof(start_ms,max_age_ms=max_reference_age_ms).price
        self.position=ShadowPosition.spot(state,dec(nav)*dec(lp_fraction),price,width_fraction)
        sol,usdc=self.position.inventory(state)
        self.ledger=Ledger.start(nav,sol,usdc,price)
        self.initial_sol,self.initial_price=sol,price
        self.initial_nav=dec(nav)
        self.initial_idle=dec(nav)-sol*price-usdc
        self.ref,self.start_ms,self.end_ms=reference,start_ms,end_ms
        self.max_age,self.max_delay=max_reference_age_ms,markout_max_delay_ms
        self.horizons=horizons_ms
        self.count=0
        self.last_ms=start_ms
        self.in_range_ms=0
        self.last_active=state.active_id
        self.bin_crossings=0
        self.fee_rounding_bound=D(0)
        self.costs=costs
        # Cost values MUST be supplied, including explicitly zero assumptions.
        for k in ('lp_execution_cost','composition_cost','priority_cost','base_transaction_cost'):
            if k not in costs:
                raise Unavailable(f'entry cost model missing {k}; not silently zero')
            self.ledger.charge(k,costs[k])
        self.initial_idle=self.ledger.cash_usdc

    def step(self,row:dict)->dict:
        t=row['event']['block_time_ms']
        if not self.start_ms<=t<self.end_ms:
            raise ValidationError('trace event outside requested attribution window')
        pre,post=PoolState.from_dict(row['pre']),PoolState.from_dict(row['post'])
        if self.position.lower_bin<=self.last_active<=self.position.upper_bin:
            self.in_range_ms+=t-self.last_ms
        self.bin_crossings+=abs(post.active_id-pre.active_id)
        self.last_ms,self.last_active=t,post.active_id
        p=self.ref.asof(t,max_age_ms=self.max_age)
        old_sol,old_usdc=self.position.inventory(pre)
        new_sol,new_usdc=self.position.inventory(post)
        dx,dy=new_sol-old_sol,new_usdc-old_usdc
        # Actual swap basis; non-swap ownership effects are reference transfers.
        basis_price=-dy/dx if dx and dy and dx*dy<0 else p.price
        self.ledger.principal_transition(new_sol,new_usdc,p.price,basis_price=basis_price)
        fee_total=D(0)
        attributed=self.position.attribution(pre,row['fees'])
        for fee in attributed:
            for category in ('base','variable','composition'):
                x,y=(fee['lp_'+category],D(0)) if fee['token']=='x' else (D(0),fee['lp_'+category])
                sol,usdc=to_economic(pre,x,y)
                self.ledger.earn_fee(category,sol,usdc)
                fee_total+=sol*p.price+usdc
            x,y=(fee['protocol'],D(0)) if fee['token']=='x' else (D(0),fee['protocol'])
            sol,usdc=to_economic(pre,x,y)
            self.ledger.protocol_information+=sol*p.price+usdc
            # Continuous-share fee allocation vs on-chain claim rounding.
            self.fee_rounding_bound+=p.price/D(10**9)+D(1)/D(10**6)
        marks=markouts(self.ref,t,dx,dy,fee_total,horizons_ms=self.horizons,
                       immediate_max_age_ms=self.max_age,max_delay_ms=self.max_delay) if row['event']['kind']=='SWAP' else []
        for m in marks:
            m['event_id']=row['event_id']
            m['variable_fee_states']=[f['volatility_accumulator'] for f in row['fills']]
        self.count+=1
        r=self.ledger.report()
        return {'event_id':row['event_id'],'ts_ms':t,'sol_inventory':new_sol,'usdc_inventory':new_usdc,
                'reference_price':p.price,'reference_ms':p.ts_ms,'attributed_fees':attributed,
                'markouts':marks,'ledger':r,'total_sol_delta_including_fees':new_sol+sum(self.ledger.fee_sol.values(),D(0))}

    def finish(self)->dict:
        if self.count==0:
            raise Unavailable('zero event attribution cannot prove an LP edge')
        if self.position.lower_bin<=self.last_active<=self.position.upper_bin:
            self.in_range_ms+=self.end_ms-self.last_ms
        end=self.ref.asof(self.end_ms,max_age_ms=self.max_age)
        self.ledger.mark(end.price)
        for k in ('exit_lp_execution_cost','exit_composition_cost','exit_priority_cost','exit_base_transaction_cost'):
            if k not in self.costs:
                raise Unavailable(f'exit cost model missing {k}')
            self.ledger.charge(k.removeprefix('exit_'),self.costs[k])
        result=self.ledger.report()
        result.update({'initial_sol_delta':self.initial_sol,'initial_reference_price':self.initial_price,
                       'initial_idle_cash':self.initial_idle,'initial_nav':self.initial_nav,
                       'terminal_reference_price':end.price,'events':self.count,'time_in_range_fraction':dec(self.in_range_ms)/(self.end_ms-self.start_ms),
                       'bin_crossings':self.bin_crossings,'lower_bin':self.position.lower_bin,
                       'upper_bin':self.position.upper_bin,'initial_deployed':self.position.initial_deployed,
                       'fee_per_initial_deployed':(result['lp_base_fee_value']+result['lp_variable_fee_value']+result['lp_composition_fee_value'])/self.position.initial_deployed,
                       'claim_rounding_bound_usdc':self.fee_rounding_bound+self.position.floor_error_bound_usdc,
                       'attribution_mode':'FIXED_EXISTING_MM_SHARES_NOT_ADDITIONAL_LIQUIDITY',
                       'shape':'SPOT_EQUAL_ENTRY_REFERENCE_VALUE',
                       'ownership_fraction_rounding':'INTEGER_SHARES_CONTINUOUS_RESERVE_ATTRIBUTION',
                       'rerange_policy':'PASSIVE_7_DAY_ENTRY_EXIT_CONTROL',
                       'additional_liquidity_return_claim':False})
        return result
