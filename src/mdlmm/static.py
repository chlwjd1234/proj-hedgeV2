"""D2 ideal static hedge on the actual inventory ledger produced by D1.

This is explicitly a zero-friction accounting oracle, not a historical net
strategy estimate. No additional capital is introduced: idle cash is internally
reserved as collateral and never counted twice. D3 supplies real friction.
"""
from __future__ import annotations
from pathlib import Path
from .util import D,dec,read_jsonl,write_jsonl
from .errors import ValidationError,Unavailable


def static_trace(d1_path:Path, output:Path, *, collateral_fraction='1') -> dict:
    fraction=dec(collateral_fraction)
    if not 0<=fraction<=1:raise ValidationError('invalid static collateral fraction')
    cases={r['case_id']:r for r in read_jsonl(d1_path/'case_results.jsonl') if r['status']=='PASS'}
    if not cases:raise Unavailable('no D1 cases available for static inventory oracle')
    latest={};count=0;max_error=D(0)
    def rows():
        nonlocal count,max_error
        for item in read_jsonl(d1_path/'ledgers.jsonl.gz'):
            case=cases.get(item['case_id'])
            if case is None:continue
            for key in ('initial_sol_delta','initial_reference_price','initial_idle_cash','initial_nav'):
                if key not in case:raise Unavailable('D1 result lacks static-oracle entry inventory; rerun D1')
            hedge=-dec(case['initial_sol_delta']);s=dec(item['reference_price']);s0=dec(case['initial_reference_price'])
            collateral=dec(case['initial_idle_cash'])*fraction
            mark=hedge*(s-s0)
            unhedged=dec(item['ledger']['nav'])
            # Removing C from idle and adding C+mark in the hedge account leaves
            # total NAV exactly unhedged NAV + mark, not NAV + C + mark.
            nav=(unhedged-collateral)+(collateral+mark)
            residual=nav-unhedged-hedge*(s-s0)
            if abs(residual)>D('1e-35'):raise ValidationError('D2 static NAV identity failed')
            max_error=max(max_error,abs(residual));count+=1
            row={'case_id':item['case_id'],'event_id':item['event_id'],'time_ms':item['ts_ms'],
                 'reference_price':s,'static_hedge_sol':hedge,'lp_plus_fee_sol':item['total_sol_delta_including_fees'],
                 'residual_delta_sol':dec(item['total_sol_delta_including_fees'])+hedge,
                 'hedge_collateral':collateral,'hedge_equity':collateral+mark,'hedge_mark_pnl':mark,
                 'unhedged_nav':unhedged,'static_hedged_nav':nav,'nav_accounting_residual':residual,
                 'margin_warning':collateral+mark<0,'data_kind':case['data_kind']}
            latest[item['case_id']]=row
            yield row
    write_jsonl(output/'static_inventory_trace.jsonl.gz',rows())
    if not count:raise Unavailable('no D1 inventory events')
    # Include terminal reference marking and exit costs from the D1 final ledger.
    terminal=[]
    for key,case in cases.items():
        hedge=-dec(case['initial_sol_delta']);s=dec(case['terminal_reference_price'])
        mark=hedge*(s-dec(case['initial_reference_price']))
        terminal.append({'case_id':key,'static_hedge_sol':hedge,'hedge_mark_pnl':mark,
                         'nav':dec(case['nav'])+mark,'residual_delta_sol':
                         dec(case['principal_sol'])+dec(case['fee_sol_total'])+hedge})
    write_jsonl(output/'static_terminal.jsonl',terminal)
    return {'status':'PASS','scope':'D1_INVENTORY_STATIC_HEDGE_ACCOUNTING_ORACLE','cases':len(terminal),
            'rows':count,'max_nav_residual':max_error,
            'fees':'EXPLICIT_IDEAL_ORACLE_ZERO_NOT_HISTORICAL_TERMS',
            'funding':'EXPLICIT_IDEAL_ORACLE_ZERO_NOT_HISTORICAL_FUNDING',
            'collateral':'INTERNAL_RESERVATION_FROM_INITIAL_IDLE_CASH',
            'liquidation_model':'NOT_IMPLEMENTED_IN_D2_USE_D3_MARGIN_CHECKS',
            'economic_strategy_evidence':False}
