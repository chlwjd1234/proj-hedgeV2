"""Four-window D1 pilot, small prespecified strategy family, guarded conclusions."""
from __future__ import annotations

import csv
import statistics
from pathlib import Path

from .attribution import AttributionEngine
from .capacity import perturbation
from .dataset import Dataset
from .errors import Unavailable,ValidationError,ResearchError
from .model import PoolState
from .reference import Reference
from .replay import expand_traces
from .regimes import explicit_windows,select_windows,describe,rv30_width
from .util import D,dec,read_jsonl,write_jsonl,write_json,digest,hash_file,verify_run,read_json,open_text,canonical


def require_d0(root:Path,dataset:Dataset,source_fingerprint:str)->tuple[Path,dict]:
    pointer=root/'runs/d0_latest.json'
    if not pointer.exists():
        raise Unavailable('no D0 manifest exists for this dataset')
    p=read_json(pointer);path=root/'runs'/p['run_id']
    if hash_file(path/'manifest.json')!=p['manifest_sha256']:
        raise ValidationError('D0 pointer manifest hash mismatch')
    manifest=verify_run(path)
    result=read_json(path/'result.json')
    gate=result.get('replay_gate') if dataset.kind=='SYNTHETIC' and result.get('data_kind')=='SYNTHETIC' else result.get('historical_gate')
    if gate!='PASS':
        raise Unavailable('D0 historical gate did not PASS; no D1 economic conclusion allowed')
    if result.get('dataset_sha256')!=hash_file(dataset.path):
        raise ValidationError('D0 belongs to another dataset')
    if manifest['source_fingerprint']!=source_fingerprint:
        raise ValidationError('source changed since D0 validation')
    # Rebind every transitive evidence file, not only a top-level manifest name.
    for name,h in result['input_hashes'].items():
        if not Path(name).is_file() or hash_file(Path(name))!=h:
            raise ValidationError('D0 underlying evidence changed')
    return path,result


def pilot(dataset:Dataset,d0_path:Path,d0_result:dict,output:Path,config:dict)->dict:
    ref=Reference(output/'reference_index.sqlite')
    ref.load(dataset.reference_path)
    try:
        wspec=config.get('windows') or dataset.meta.get('windows')
        cfg=config['pilot']
        if wspec:
            windows=explicit_windows(wspec)
        else:
            start=min(s['start_ms'] for s in dataset.segments)
            end=max(s['end_ms'] for s in dataset.segments)
            windows=select_windows(ref,eligible_start_ms=start,eligible_end_ms=end,
                                   max_daily_age_ms=cfg['daily_reference_max_age_ms'])
        write_json(output/'windows.json',describe(windows))
        pools=dataset.meta.get('pools',[])
        pool_ids=[p['pool'] for p in pools]
        if len(pool_ids)!=2 or len(set(pool_ids))!=2:
            raise Unavailable('D1 pilot requires two explicit canonical SOL/USDC pools')
        if dataset.kind=='HISTORICAL':
            earliest=min(w.start_ms for w in windows)
            for p in pools:
                if p.get('selection_time_ms',earliest+1)>earliest or not p.get('metadata_source'):
                    raise Unavailable('point-in-time pool selection/provenance missing')
                if p.get('selection_rule') in ('CURRENT_APR','EX_POST_PNL'):
                    raise ValidationError('survivor/performance-selected pool universe prohibited')
        multipliers=cfg.get('width_multipliers',[1,2])
        if multipliers!=[1,2]:
            raise ValidationError('prespecified D1 width family is exactly [1,2]')
        results=[];capacity_rows=[];markout_count=0;markout_unavailable=0;groups={}
        ledger_stream=open_text(output/'ledgers.jsonl.gz','wt')
        markout_stream=open_text(output/'markouts.jsonl.gz','wt')
        for window in windows:
            for pool in pool_ids:
                candidates=[s for s in dataset.segments if s['pool']==pool and s['start_ms']<=window.start_ms and s['end_ms']>=window.end_ms]
                if len(candidates)!=1:
                    raise Unavailable(f'no unique validated segment for {pool}/{window.label}')
                segment=candidates[0]
                trace_entry=d0_result['segments'][segment['id']]
                trace_path=d0_path/trace_entry['trace']
                initial=dataset.snapshot(segment['snapshot'])
                for multiple in multipliers:
                    case_id=f'{window.label}_{pool[:10]}_rv{multiple}'
                    try:
                        width=rv30_width(ref,window.start_ms,multiple,max_age_ms=cfg['daily_reference_max_age_ms'])
                        engine=None;cap_count=0;stream_count=0
                        for row in expand_traces(read_jsonl(trace_path),dataset.snapshot(segment['snapshot']),verify_hashes=False):
                            t=row['event']['block_time_ms']
                            if t<window.start_ms:
                                initial=PoolState.from_dict(row['post']);continue
                            if t>=window.end_ms:
                                break
                            if engine is None:
                                initial=PoolState.from_dict(row['pre'])
                                engine=AttributionEngine(initial,ref,start_ms=window.start_ms,end_ms=window.end_ms,
                                     nav=cfg['initial_nav'],lp_fraction=cfg['lp_fraction'],width_fraction=width,
                                     max_reference_age_ms=cfg['reference_max_age_ms'],
                                     markout_max_delay_ms=cfg['markout_max_delay_ms'],costs=config['costs'],
                                     horizons_ms=cfg['markout_horizons_ms'])
                            out=engine.step(row);stream_count+=1
                            ledger_stream.write(canonical({'case_id':case_id,**{k:v for k,v in out.items() if k not in ('markouts','attributed_fees')}}).decode()+'\n')
                            for m in out['markouts']:
                                markout_stream.write(canonical({'case_id':case_id,**m}).decode()+'\n')
                                markout_count+=1
                                if m['status']!='PASS':
                                    markout_unavailable+=1
                                    continue
                                state_label='VARIABLE_ACTIVE' if any(m['variable_fee_states']) else 'VARIABLE_ZERO'
                                key=(m['horizon_ms'],state_label)
                                group=groups.setdefault(key,{'n':0,'fee':D(0),'adverse':D(0),'fee_minus_markout':D(0)})
                                group['n']+=1;group['fee']+=dec(m['lp_fee_usdc_at_event'])
                                group['adverse']+=dec(m['directional_adverse_markout'])
                                group['fee_minus_markout']+=dec(m['fee_minus_markout'])
                            if row['event']['kind']=='SWAP' and cap_count<cfg['capacity_samples_per_case'] and (stream_count-1)%cfg['capacity_stride']==0:
                                for capital in cfg['capital_ladder']:
                                    try:
                                        pre=PoolState.from_dict(row['pre'])
                                        capacity=perturbation(pre,row['event']['payload']['amount_in'],
                                            row['event']['payload']['swap_for_y'],row['event']['clock_unix_timestamp'],
                                            out['reference_price'],engine.position.lower_bin,engine.position.upper_bin,capital)
                                    except Unavailable as exc:
                                        capacity={'status':'UNAVAILABLE','capital_deployed_usdc':capital,'reason':str(exc)}
                                    capacity_rows.append({'case_id':case_id,'event_id':row['event_id'],**capacity})
                                cap_count+=1
                        if engine is None:
                            raise Unavailable('no events in selected window')
                        summary=engine.finish()
                        summary.update(status='PASS',case_id=case_id,pool=pool,window=window.label,
                                       start_ms=window.start_ms,end_ms=window.end_ms,width_multiplier=multiple,
                                       width_fraction=width,data_kind=dataset.kind)
                    except Unavailable as exc:
                        summary={'status':'UNAVAILABLE','case_id':case_id,'pool':pool,'window':window.label,'reason':str(exc)}
                    results.append(summary)
        write_jsonl(output/'case_results.jsonl',results)
        ledger_stream.close();markout_stream.close()
        write_jsonl(output/'capacity.jsonl.gz',capacity_rows)
        # Per-state fee-minus-markout summaries. Future diagnostic labels never
        # feed back into entry/range policy during this run.
        write_json(output/'fee_toxicity_summary.json',[{'horizon_ms':h,'fee_state':s,**v} for (h,s),v in sorted(groups.items())])
        good=[r for r in results if r['status']=='PASS']
        complete=len(good)==16
        diagnostics={}
        for multiple in multipliers:
            rows=[r for r in good if r['width_multiplier']==multiple]
            vals=[dec(r['inventory_matched_surplus']) for r in rows]
            if vals:
                diagnostics[str(multiple)]={'n':len(vals),'sum_surplus':sum(vals,D(0)),
                     'mean_surplus':sum(vals,D(0))/len(vals),'minimum_surplus':min(vals),
                     'positive_windows':sum(v>0 for v in vals),
                     'max_claim_rounding_bound':max(dec(r['claim_rounding_bound_usdc']) for r in rows)}
        uncertainty=dec(cfg['model_cost_uncertainty_usdc_per_window'])
        edge='INCONCLUSIVE'
        if complete and all(dec(r['inventory_matched_surplus'])+uncertainty<0 for r in good):
            edge='ABSENT_IN_TESTED_FAMILY'
        if (complete and any(len([r for r in good if r['width_multiplier']==m and
                    dec(r['inventory_matched_surplus'])>uncertainty+dec(r['claim_rounding_bound_usdc'])])>=6 for m in multipliers)
                and config['research']['cost_evidence_mode']=='CONSERVATIVE_DOCUMENTED_BOUND'):
            edge='CANDIDATE_REQUIRES_D3_AND_OOS'
        economic=dataset.kind=='HISTORICAL' and complete
        if dataset.kind=='SYNTHETIC':
            edge='SYNTHETIC_NOT_ECONOMIC_EVIDENCE'
        return {'status':'PASS' if complete else 'UNAVAILABLE','scope':'D1_RESEARCH_PILOT',
                'cases_expected':16,'cases_pass':len(good),'cases_unavailable':len(results)-len(good),
                'data_kind':dataset.kind,'economic_evidence':economic,'edge_assessment':edge,
                'd3_eligible':economic and edge=='CANDIDATE_REQUIRES_D3_AND_OOS',
                'parameter_mining':False,'annualization_used':False,'untouched_oos':False,
                'diagnostics_by_prespecified_width':diagnostics,
                'capacity_samples':len(capacity_rows),'markouts':markout_count,
                'markouts_unavailable':markout_unavailable,
                'cost_evidence_mode':config['research']['cost_evidence_mode'],
                'max_nav_residual':max((abs(dec(r['nav_accounting_residual'])) for r in good),default=None),
                'limitations':['Fixed existing shares are not a new-liquidity strategy return',
                               'Four diagnostic weeks do not establish expected annual return',
                               'Capacity is a local fixed-intent perturbation, not router/arbitrage response',
                               'Continuous-share fee attribution has a reported claim-rounding bound']}
    finally:
        for stream in (locals().get('ledger_stream'),locals().get('markout_stream')):
            if stream is not None: stream.close()
        ref.close()
