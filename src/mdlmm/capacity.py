"""Fixed-intent quote perturbations, NOT an endogenous market counterfactual."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from .errors import Unavailable, ValidationError
from .mechanics import Q64, human_price, price_from_id, quote_exact_in
from .model import Bin, PoolState, SOL_MINT
from .util import D, dec


def to_economic(state: PoolState, x, y) -> tuple[D,D]:
    x,y=dec(x)/(10**state.decimals_x),dec(y)/(10**state.decimals_y)
    return (x,y) if state.token_x_mint==SOL_MINT else (y,x)


def add_hypothetical_spot(state: PoolState, capital, lower: int, upper: int, reference_price) -> tuple[PoolState,list[dict]]:
    """Equal reference-marked value per bin. Pro-rata where a bin is populated.

    This is a capacity shock, not a prediction of SDK add-liquidity instruction
    rounding/composition fees. Empty new bins are explicit new local liquidity.
    """
    cap,s=dec(capital),dec(reference_price)
    if cap<=0 or s<=0 or upper<lower or upper-lower>10000:
        raise ValidationError('invalid capacity shock')
    out=deepcopy(state)
    per=cap/(upper-lower+1)
    report=[]
    for i in range(lower,upper+1):
        old=out.bins.get(i)
        if old is None:
            if not state.complete_bin_map:
                raise Unavailable('unknown bin in proposed liquidity range')
            old=Bin(i,0,0,price_from_id(i,state.bin_step),0)
            out.bins[i]=old
        old_sol,old_usdc=to_economic(out,old.amount_x,old.amount_y)
        old_value=old_sol*s+old_usdc
        if old_value>0:
            scale=per/old_value
            dx=int(dec(old.amount_x)*scale)
            dy=int(dec(old.amount_y)*scale)
            new_supply=int(dec(old.liquidity_supply)*scale)
        else:
            # DLMM X liquidity above active, Y liquidity below active. At active
            # an empty bin gets a 50/50 *value* shock, explicitly not historical.
            x_fraction=D(1) if i>state.active_id else D(0) if i<state.active_id else D('0.5')
            px=s if out.token_x_mint==SOL_MINT else D(1)
            py=D(1) if out.token_x_mint==SOL_MINT else s
            dx=int(per*x_fraction/px*(10**out.decimals_x))
            dy=int(per*(1-x_fraction)/py*(10**out.decimals_y))
            new_supply=(old.price or price_from_id(i,out.bin_step))*dx+Q64*dy
        old_x,old_y,old_supply=old.amount_x,old.amount_y,old.liquidity_supply
        old.amount_x+=dx;old.amount_y+=dy;old.liquidity_supply+=new_supply
        old.validate()
        report.append({'bin_id':i,'added_x':dx,'added_y':dy,
                       'share_x':dec(dx)/old.amount_x if old.amount_x else D(0),
                       'share_y':dec(dy)/old.amount_y if old.amount_y else D(0),
                       'liquidity_share':dec(new_supply)/old.liquidity_supply if old.liquidity_supply else D(0),
                       'was_empty':old_supply==0,'historical_x':old_x,'historical_y':old_y})
    return out,report


def principal_trade_price(state: PoolState, quote):
    dx=dy=0
    for f in quote.fills:
        dx+=f.post_x-f.pre_x;dy+=f.post_y-f.pre_y
    sol,usdc=to_economic(state,dx,dy)
    if sol==0:
        raise Unavailable('zero SOL principal transfer in quote')
    return -usdc/sol


def perturbation(state: PoolState, amount_in: int, swap_for_y: bool, timestamp: int,
                 reference_price, lower: int, upper: int, capital, *,
                 small_share='0.01', large_share='0.25', small_quote_bps='0.1', large_quote_bps='10') -> dict:
    base=quote_exact_in(state,amount_in,swap_for_y,timestamp)
    changed,shares=add_hypothetical_spot(state,capital,lower,upper,reference_price)
    altered=quote_exact_in(changed,amount_in,swap_for_y,timestamp)
    if not base.amount_out:
        raise Unavailable('cannot assess relative quote perturbation for zero output')
    qbps=dec(altered.amount_out-base.amount_out)/base.amount_out*10000
    share=max((r['liquidity_share'] for r in shares),default=D(0))
    output_share=max((r['share_y' if swap_for_y else 'share_x'] for r in shares),default=D(0))
    active=next((r['liquidity_share'] for r in shares if r['bin_id']==state.active_id),D(0))
    path=[f.bin_id for f in base.fills];path_new=[f.bin_id for f in altered.fills]
    changed_path=path!=path_new or base.end_bin!=altered.end_bin
    if max(share,output_share)>=dec(large_share) or abs(qbps)>=dec(large_quote_bps):
        kind='LARGE_COUNTERFACTUAL'
    elif max(share,output_share)<=dec(small_share) and abs(qbps)<=dec(small_quote_bps) and not changed_path:
        kind='SMALL_PRICE_TAKER'
    else:
        kind='MODERATE_PERTURBATION'
    p0,p1=principal_trade_price(state,base),principal_trade_price(changed,altered)
    s=dec(reference_price)
    # Positive means worse price for the trader, excluding fee.
    selling_sol=swap_for_y if state.token_x_mint==SOL_MINT else not swap_for_y
    sign=-1 if selling_sol else 1
    return {'status':'PASS','capital_deployed_usdc':dec(capital),'classification':kind,
            'active_bin_lp_share':active,'max_per_bin_liquidity_share':share,'max_output_side_share':output_share,
            'historical_quote_out':base.amount_out,'perturbed_quote_out':altered.amount_out,
            'quote_perturbation_bps':qbps,'historical_bin_path':path,'perturbed_bin_path':path_new,
            'historical_end_bin':base.end_bin,'perturbed_end_bin':altered.end_bin,
            'bin_path_perturbation':changed_path,
            'historical_principal_price_impact_bps':sign*(p0/s-1)*10000,
            'perturbed_principal_price_impact_bps':sign*(p1/s-1)*10000,
            'price_impact_change_bps':sign*(p1-p0)/s*10000,
            'per_bin':shares,'flow_assumption':'FIXED_HISTORICAL_ORDER_INTENT_LOCAL_SHOCK_ONLY',
            'endogenous_routing_arbitrage_validated':False,'thresholds_are_heuristics':True,
            'composition_fee_in_shock':'NOT_MODELED_CAPACITY_ONLY_NOT_STRATEGY_PNL'}
