"""Validate financial configuration rather than quietly ignoring typos."""
from pathlib import Path
from .util import dec,read_json,hash_file,safe_path
from .errors import ValidationError,Unavailable


def validate_config(cfg:dict):
    sections={'research','data','network','pilot','costs','perp','windows'}
    if set(cfg)-sections:raise ValidationError('unknown config sections: '+str(sorted(set(cfg)-sections)))
    for name in sections-{'windows'}:
        if not isinstance(cfg.get(name),dict):raise ValidationError('missing config section '+name)
    p=cfg['pilot']
    if dec(p['initial_nav'])<=0 or not 0<dec(p['lp_fraction'])<1:
        raise ValidationError('initial NAV/LP allocation invalid')
    if p['width_multipliers']!=[1,2]:raise ValidationError('width rules fixed to [1,2]')
    ladder=p['capital_ladder']
    if not ladder or len(ladder)>16 or len(set(ladder))!=len(ladder) or any(dec(x)<=0 for x in ladder):
        raise ValidationError('capacity ladder requires 1..16 distinct positive sizes')
    for name in ('reference_max_age_ms','daily_reference_max_age_ms','markout_max_delay_ms','capacity_samples_per_case','capacity_stride'):
        if not isinstance(p[name],int) or p[name]<=0:raise ValidationError('invalid positive integer setting '+name)
    if dec(p['model_cost_uncertainty_usdc_per_window'])<0:raise ValidationError('negative uncertainty budget')
    if p['markout_horizons_ms']!=sorted(set(p['markout_horizons_ms'])) or any(x<0 for x in p['markout_horizons_ms']):
        raise ValidationError('markout horizons must be sorted distinct nonnegative milliseconds')
    names={prefix+k for prefix in ('','exit_') for k in ('lp_execution_cost','composition_cost','priority_cost','base_transaction_cost')}
    if set(cfg['costs'])!=names or any(dec(x)<0 for x in cfg['costs'].values()):
        raise ValidationError('all eight explicit nonnegative entry/exit cost components are required')
    if cfg['research']['cost_evidence_mode'] not in ('SCENARIO_NOT_HISTORICAL','CONSERVATIVE_DOCUMENTED_BOUND'):
        raise ValidationError('unknown cost evidence mode')
    if cfg['perp']['policy'] not in ('exact','center','boundary') or cfg['perp']['execution'] not in ('taker','maker-first'):
        raise ValidationError('unknown perp policy/execution')


def cost_evidence(root:Path,cfg:dict)->dict|None:
    if cfg['research']['cost_evidence_mode']=='SCENARIO_NOT_HISTORICAL':return None
    desc=cfg['research'].get('cost_evidence')
    if not isinstance(desc,dict) or not desc.get('path') or not desc.get('sha256'):
        raise Unavailable('documented cost mode requires hash-bound cost_evidence path and sha256')
    path=safe_path(root,desc['path'])
    if not path.exists():raise Unavailable('cost evidence file absent')
    if hash_file(path)!=desc['sha256']:raise ValidationError('cost evidence hash mismatch')
    value=read_json(path)
    if value.get('basis')!='DOCUMENTED_CONSERVATIVE_BOUNDS' or not value.get('source_references'):
        raise Unavailable('conservative cost assertion lacks supporting sources')
    if value.get('costs')!=cfg['costs']:
        raise ValidationError('configured cost values differ from documented bounds')
    if dec(value.get('uncertainty_usdc_per_window',-1))!=dec(cfg['pilot']['model_cost_uncertainty_usdc_per_window']):
        raise ValidationError('configured uncertainty differs from documented bound')
    return {'path':str(path),'sha256':hash_file(path)}
