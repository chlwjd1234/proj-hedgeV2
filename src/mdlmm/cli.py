"""One-command, read-only stage-gated research workflow."""
from __future__ import annotations
import argparse,importlib.metadata,json,os,platform,subprocess,sys,tomllib
from pathlib import Path

from .errors import Unavailable,ValidationError,ResearchError
from .util import Run,read_json,write_json,write_jsonl,hash_file,source_fingerprint,verify_run,read_jsonl,iso_ms
from .dataset import Dataset,seal_descriptor
from .model import PROGRAM_ID,Event
from .validation import validate_dataset,live_sample
from .pilot import pilot,require_d0
from .perp import BookStore,TermsSchedule,FundingSeries,HyperliquidSOL,DynamicHedge,static_hedge_oracle
from .network import Http,RawStore,SolanaRPC,sync_sources,capture_pool,collect_blocks,discover_rpc
from .normalize import normalize_block
from .codec import IdlCodec

STAGES=('preflight','source-sync','demo-generate','d0','d1','d2','d3-smoke','all','extract',
        'discover','discover-historical','snapshot','blocks','pool-history','normalize','seal-bundle','prepare-historical','archive-download','funding-rates','funding-join','svm')


def find_root()->Path:
    return Path(__file__).resolve().parents[2]


def load_config(root:Path,args)->dict:
    path=root/(args.config or ('configs/synthetic.toml' if args.demo else 'configs/research.toml'))
    with path.open('rb') as f:cfg=tomllib.load(f)
    if args.dataset:cfg['data']['dataset']=args.dataset
    if args.capital_ladder:cfg['pilot']['capital_ladder']=[int(x) for x in args.capital_ladder.split(',')]
    if args.market_bundle:cfg['perp']['market_bundle']=args.market_bundle
    from .config import validate_config
    validate_config(cfg)
    return cfg


def bind_dataset(run:Run,d:Dataset):
    run.metadata['program_ids']=[PROGRAM_ID]
    run.metadata['dataset_kind']=d.kind
    run.metadata['pools']=[x['pool'] for x in d.meta['pools']]
    run.metadata['time_ranges']=[[s['start_ms'],s['end_ms']] for s in d.segments]
    run.metadata['reference_price_input']={'path':str(d.reference_path),'sha256':hash_file(d.reference_path)}
    run.metadata['program_registry']=read_json(d.registry_path)
    run.metadata['inputs'].update({name:{'path':name,'sha256':h} for name,h in d.files.items()})


def print_summary(path:Path):
    verify_run(path)
    r=read_json(path/'result.json')
    print('RUN='+str(path))
    for k in ('status','scope','data_kind','historical_gate','events_validated','cases_expected','cases_pass',
              'edge_assessment','d3_eligible','economic_evidence','max_nav_residual','strategy_success',
              'reason','workflow_completed','canonical_mode','canonical_swaps','strict_canonical_pass'):
        if k in r:print(k.upper()+'='+json.dumps(r[k],ensure_ascii=False,separators=(',',':')))
    checks=r.get('checks',[])
    if checks:
        counts={s:sum(c['status']==s for c in checks) for s in ('PASS','FAIL','UNAVAILABLE')}
        print('CHECK_COUNTS='+json.dumps(counts))
        for c in checks:
            if c['status']!='PASS':print('CHECK='+json.dumps(c,ensure_ascii=False,separators=(',',':')))
    if 'inventory_trace' in r:print('STATIC_TRACE='+json.dumps(r['inventory_trace'],separators=(',',':')))
    if 'diagnostics_by_prespecified_width' in r:
        print('WIDTH_DIAGNOSTICS='+json.dumps(r['diagnostics_by_prespecified_width'],separators=(',',':')))
    if r.get('scope')=='ANALYTICAL_SYNTHETIC_ORACLE':
        for row in r['rows']:print('STATIC='+json.dumps(row,separators=(',',':')))


def get_latest(root:Path,stage:str)->Path:
    pointer=root/'runs'/f'{stage}_latest.json'
    if not pointer.exists():raise Unavailable('no run for stage '+stage)
    p=read_json(pointer);path=root/'runs'/p['run_id']
    if hash_file(path/'manifest.json')!=p['manifest_sha256']:
        raise ValidationError('latest manifest pointer hash mismatch')
    return path


def perp_smoke(root:Path,output:Path,cfg:dict,*,synthetic:bool=False)->dict:
    market=root/cfg['perp']['market_bundle']
    if not market.exists():raise Unavailable('Hyperliquid market bundle missing; no zero funding/fee fallback')
    meta=read_json(market);parent=market.parent
    if meta.get('data_kind')!='HISTORICAL':raise ValidationError('D3 market bundle is not historical')
    files={}
    for key in ('books','terms','funding','decisions'):
        desc=meta[key];p=(parent/desc['path']).resolve()
        if not p.is_relative_to(parent.resolve()) or not p.exists() or hash_file(p)!=desc['sha256']:
            raise ValidationError('Hyperliquid market input hash mismatch: '+key)
        files[key]=p
    books=BookStore(output/'books.sqlite')
    books.load(files['books'],observation_delay_ms=meta.get('observation_delay_ms'))
    schedule=TermsSchedule(read_json(files['terms'])['intervals'])
    funding_data=read_json(files['funding'])
    funding=FundingSeries(funding_data['events'],funding_data['coverage_start_ms'],funding_data['coverage_end_ms'],funding_data['source'])
    opts=cfg['perp'];venue=HyperliquidSOL(books,schedule,funding,max_book_age_ms=opts['max_book_age_ms'])
    engine=DynamicHedge(venue,opts['collateral'],latency_ms=opts['latency_ms'],policy=opts['policy'],
                        band_bps=opts['band_bps'],execution=opts['execution'],maker_timeout_ms=opts['maker_timeout_ms'])
    count=0
    for d in read_jsonl(files['decisions']):
        engine.decision(d['time_ms'],d['lp_delta_sol'],d['nav_usdc'],d['reference_price']);count+=1
    if not count:raise Unavailable('D3 decision trace empty')
    result=engine.finish(meta['end_ms'])
    write_jsonl(output/'fills_orders.jsonl',result.pop('records'))
    result.update(scope='D3_EXECUTION_FOUNDATION_SMOKE',decisions=count,data_kind='HISTORICAL',
                  input_hashes={str(p):hash_file(p) for p in [market,*files.values()]})
    books.db.close()
    return result


def run_stage(root:Path,stage:str,cfg:dict,args)->tuple[dict,Path]:
    run=Run(root,stage,cfg,{'source_lock':root/'reference/source_lock.json'})
    for name in ('pytest','solders','boto3','lz4','jsonschema'):
        try:run.metadata['software'][name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:run.metadata['software'][name]=None
    try:
        from .config import cost_evidence
        evidence=cost_evidence(root,cfg)
        if evidence:run.metadata['inputs']['cost_evidence']=evidence
        if stage=='preflight':
            if sys.version_info<(3,11):raise ValidationError('Python 3.11+ required')
            result={'status':'PASS','scope':'LOCAL_ENVIRONMENT_NOT_CHAIN_VALIDATION',
                    'python':platform.python_version(),'dependencies':'CORE_STDLIB_ONLY',
                    'dataset_present':(root/cfg['data']['dataset']).is_file(),
                    'rpc_configured':bool(os.environ.get('SOLANA_RPC_URL')),
                    'sdk_installed':(root/'scripts/sdk/node_modules/@meteora-ag/dlmm').exists(),
                    'historical_gate':'UNAVAILABLE','private_key_required':False}
        elif stage=='source-sync':
            result={'status':'PASS','scope':'PINNED_OFFICIAL_SOURCE_DOWNLOAD',**sync_sources(root)}
        elif stage=='demo-generate':
            from .demo import generate
            p=generate(root/(args.output or 'data/synthetic'))
            result={'status':'PASS','data_kind':'SYNTHETIC','dataset':str(p),'economic_evidence':False}
        elif stage=='d0':
            path=root/cfg['data']['dataset']
            if args.demo and not path.exists():
                from .demo import generate
                generate(path.parent)
            if path.exists():
                d=Dataset(path);bind_dataset(run,d);result=validate_dataset(d,run.path);bind_dataset(run,d)
            else:
                if args.offline:
                    raise Unavailable('historical dataset missing; --offline prevents live sample collection')
                result=live_sample(root,run.path,cfg)
        elif stage=='d1':
            d=Dataset(root/cfg['data']['dataset']);bind_dataset(run,d)
            d0_path,d0_result=require_d0(root,d,source_fingerprint(root))
            run.metadata['parent_manifest']={'path':str(d0_path/'manifest.json'),'sha256':hash_file(d0_path/'manifest.json')}
            result=pilot(d,d0_path,d0_result,run.path,cfg);d.recheck();bind_dataset(run,d)
        elif stage=='d2':
            result=static_hedge_oracle()
            try:
                parent=get_latest(root,'d1');pm=verify_run(parent)
                if pm['source_fingerprint']!=source_fingerprint(root):
                    raise Unavailable('D1 source fingerprint changed; standalone analytical oracle only')
                from .static import static_trace
                result['inventory_trace']=static_trace(parent,run.path)
                run.metadata['parent_manifest']={'path':str(parent/'manifest.json'),'sha256':hash_file(parent/'manifest.json')}
            except Unavailable as exc:
                result['inventory_trace']={'status':'UNAVAILABLE','reason':str(exc)}
        elif stage=='d3-smoke':
            parent=get_latest(root,'d1');verify_run(parent);p=read_json(parent/'result.json')
            if not p.get('d3_eligible'):
                result={'status':'UNAVAILABLE','scope':'D3_RESEARCH_GATE','strategy_success':False,
                        'reason':'SKIPPED_RESEARCH_GATE: D1 absent/inconclusive/synthetic; D3 not interpreted as strategy success'}
            else:
                pm=read_json(parent/'manifest.json')
                if pm['source_fingerprint']!=source_fingerprint(root):raise ValidationError('source changed since D1')
                for desc in pm['inputs'].values():
                    if hash_file(Path(desc['path']))!=desc['sha256']:raise ValidationError('D1 input changed')
                run.metadata['parent_manifest']={'path':str(parent/'manifest.json'),'sha256':hash_file(parent/'manifest.json')}
                result=perp_smoke(root,run.path,cfg)
                run.metadata['inputs'].update({p:{'path':p,'sha256':h} for p,h in result['input_hashes'].items()})
        elif stage=='discover':
            rpc=SolanaRPC(Http(RawStore(root/'data/raw')));result={'status':'PASS',**discover_rpc(rpc)}
            write_json(run.path/'pools.json',result)
        elif stage=='discover-historical':
            if not args.input:raise ValidationError('--input hash-bound block inventory JSON required')
            from .history import discover_historical_creations
            from .util import safe_path
            inventory=(root/args.input).resolve();meta=read_json(inventory)
            def resolve(desc):
                p=safe_path(inventory.parent,desc['path'])
                if not p.exists() or hash_file(p)!=desc['sha256']:raise ValidationError('raw discovery input hash mismatch')
                run.metadata['inputs'][str(p)]={'path':str(p),'sha256':hash_file(p)}
                return p
            info=discover_historical_creations(meta['blocks'],resolve)
            write_json(run.path/'historical_candidates.json',info)
            result={'status':'PASS' if info['pools'] else 'UNAVAILABLE','scope':'HISTORICAL_CREATION_DISCOVERY',**info}
        elif stage=='snapshot':
            if not args.pool:raise ValidationError('--pool required')
            rpc=SolanaRPC(Http(RawStore(root/'data/raw')))
            capture=capture_pool(rpc,args.pool);write_json(run.path/'capture.json',capture)
            result={'status':'PASS','scope':'CURRENT_END_SLOT_SNAPSHOT','slot':capture['slot'],'pool':args.pool,
                    'historical_gate':'UNAVAILABLE'}
        elif stage=='pool-history':
            if not args.pool or args.start_slot is None or args.end_slot is None:
                raise ValidationError('--pool/--start-slot/--end-slot required')
            from .history import collect_pool_history
            rpc=SolanaRPC(Http(RawStore(root/'data/raw')))
            info=collect_pool_history(rpc,args.pool,args.start_slot,args.end_slot,run.path/'history',
                                      max_pages=args.max_pages,max_blocks=args.max_blocks)
            result={'status':'PASS' if info['complete'] else 'UNAVAILABLE','scope':'POOL_HISTORY_RAW_ACQUISITION',**info}
        elif stage=='blocks':
            if args.start_slot is None or args.end_slot is None:raise ValidationError('--start-slot/--end-slot required')
            rpc=SolanaRPC(Http(RawStore(root/'data/raw')))
            info=collect_blocks(rpc,args.start_slot,args.end_slot,run.path/'blocks',max_blocks=args.max_blocks)
            result={'status':'PASS' if info['complete'] else 'UNAVAILABLE','scope':'RAW_BLOCK_ACQUISITION',**info}
        elif stage=='normalize':
            if not args.input or not args.pool:raise ValidationError('--input blocks-directory and --pool required')
            path=(root/args.input).resolve()
            idl_path=root/'external/dlmm-sdk/ts-client/src/dlmm/idl/idl.json'
            idl=IdlCodec.load(idl_path) if idl_path.exists() else None
            all_rows=[];blockers=[]
            require_canonical=bool(getattr(args,'require_canonical_swaps',False))
            for file in sorted(path.glob('*.json'),key=lambda p:int(p.stem) if p.stem.isdigit() else -1):
                if not file.stem.isdigit():continue
                saved=read_json(file)
                rows=normalize_block(saved['slot'],saved['block'],{args.pool},raw_hash=saved.get('raw_sha256'),
                                     idl=idl,require_canonical_swaps=require_canonical)
                all_rows+=rows['events'];blockers+=rows['blockers']
            write_jsonl(run.path/'events.jsonl.gz',all_rows);write_json(run.path/'blockers.json',blockers)
            swap_rows=[row for row in all_rows if row['kind']=='SWAP']
            canonical_swaps=sum(row['payload'].get('canonical_pair_verified') is True for row in swap_rows)
            strict_pass=bool(require_canonical and swap_rows and not blockers and canonical_swaps==len(swap_rows))
            result={'status':'PASS' if all_rows and not blockers and (not require_canonical or strict_pass) else 'UNAVAILABLE',
                    'scope':'EVENT_DECODE_NOT_FACTUAL_REPLAY',
                    'events':len(all_rows),'unresolved_mutations':len(blockers),
                    'canonical_mode':'REQUIRED_PAIRS' if require_canonical else 'COMPATIBILITY_DIAGNOSTIC',
                    'canonical_swaps':canonical_swaps,'strict_canonical_pass':strict_pass,
                    'historical_gate':'UNAVAILABLE',
                    'program_binding':'UNKNOWN_UNTIL_HISTORICAL_REGISTRY_SUPPLIED','clock':'UNKNOWN_UNTIL_SYSVAR_EVIDENCE_SUPPLIED'}
            if not all_rows:result['reason']='no normalized event observations; inspect retained blockers'
        elif stage=='seal-bundle':
            if not args.input:raise ValidationError('--input descriptor.json required')
            path=(root/args.input).resolve();meta=read_json(path)
            out=seal_descriptor(path.parent,meta,args.output or 'dataset.json')
            result={'status':'PASS','scope':'HASH_SEAL_ONLY_NOT_VALIDATION','dataset':str(out)}
        elif stage=='prepare-historical':
            if not args.input:raise ValidationError('--input archival-plan.json required')
            from .prepare import prepare_historical
            plan_path=(root/args.input).resolve()
            run.metadata['inputs'][str(plan_path)]={'path':str(plan_path),'sha256':hash_file(plan_path)}
            result=prepare_historical(plan_path,args.output or 'dataset.json')
        elif stage=='archive-download':
            if not args.bucket or not args.key or not args.output or not args.max_bytes:
                raise ValidationError('--bucket/--key/--output/--max-bytes required; requester pays is opt-in')
            from .archive import download_s3_object
            result={'status':'PASS','scope':'RAW_HYPERLIQUID_ARCHIVE',**download_s3_object(
                args.bucket,args.key,(root/args.output).resolve(),max_bytes=args.max_bytes,requester_pays=args.requester_pays)}
        elif stage=='funding-rates':
            if not args.start or not args.end:raise ValidationError('--start/--end ISO timestamps required')
            from .archive import collect_funding_rates
            result=collect_funding_rates(Http(RawStore(root/'data/raw')),iso_ms(args.start),iso_ms(args.end),max_pages=args.max_pages)
            write_json(run.path/'funding_rates.json',result)
        elif stage=='funding-join':
            if not args.input or not args.oracle_file:raise ValidationError('--input rates.json/--oracle-file required')
            from .archive import join_funding_oracles
            rates_path=(root/args.input).resolve();oracle_path=(root/args.oracle_file).resolve()
            joined=join_funding_oracles(read_json(rates_path),read_jsonl(oracle_path))
            write_json(run.path/'funding.json',joined)
            run.metadata['inputs'].update({str(p):{'path':str(p),'sha256':hash_file(p)} for p in (rates_path,oracle_path)})
            result={'status':'PASS','scope':'SOURCED_FUNDING_ORACLE_JOIN','events':len(joined['events'])}
        elif stage=='svm':
            if not args.input:raise ValidationError('--input SVM-case.json required')
            from .svm import replay_case
            result=replay_case((root/args.input).resolve())
        else:
            raise ValidationError('unimplemented command '+stage)
    except Unavailable as exc:
        result={'status':'UNAVAILABLE','scope':stage,'reason':str(exc),'historical_gate':'UNAVAILABLE',
                'economic_evidence':False,'strategy_success':False}
    except (ResearchError,KeyError,ValueError,TypeError,OSError) as exc:
        result={'status':'FAIL','scope':stage,'reason':f'{type(exc).__name__}: {exc}',
                'historical_gate':'FAIL','economic_evidence':False,'strategy_success':False}
    except Exception as exc:
        import traceback
        (run.path/'internal_error.txt').write_text(traceback.format_exc())
        result={'status':'FAIL','scope':stage,'reason':f'INTERNAL_ERROR {type(exc).__name__}: {exc}',
                'historical_gate':'FAIL','economic_evidence':False,'strategy_success':False}
    path=run.finish(result);print_summary(path)
    return read_json(path/'result.json'),path


def main(argv=None)->int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=STAGES)
    parser.add_argument('--root',type=Path)
    parser.add_argument('--config');parser.add_argument('--dataset');parser.add_argument('--demo',action='store_true')
    parser.add_argument('--offline',action='store_true');parser.add_argument('--output');parser.add_argument('--input')
    parser.add_argument('--pool');parser.add_argument('--start-slot',type=int);parser.add_argument('--end-slot',type=int)
    parser.add_argument('--max-blocks',type=int,default=10000)
    parser.add_argument('--max-pages',type=int,default=100)
    parser.add_argument('--require-canonical-swaps',action='store_true',
                        help='normalize: require one verified Swap/Swap2Evt pair per decoded swap instruction')
    parser.add_argument('--capital-ladder');parser.add_argument('--market-bundle')
    parser.add_argument('--bucket');parser.add_argument('--key');parser.add_argument('--max-bytes',type=int)
    parser.add_argument('--requester-pays',action='store_true')
    parser.add_argument('--start');parser.add_argument('--end');parser.add_argument('--oracle-file')
    parser.add_argument('--extract-stage',default='d0')
    args=parser.parse_args(argv);root=(args.root or find_root()).resolve()
    try:
        if args.stage=='extract':
            path=(root/args.input).resolve() if args.input else get_latest(root,args.extract_stage)
            print_summary(path);return 0
        cfg=load_config(root,args)
        if args.stage=='all':
            receipts=[]
            for stage in ('preflight','d0','d1','d2','d3-smoke'):
                result,path=run_stage(root,stage,cfg,args)
                receipts.append({'stage':stage,'run':str(path),'status':result['status'],
                                 'manifest_sha256':hash_file(path/'manifest.json')})
            run=Run(root,'all',cfg)
            required=[r for r in receipts if r['stage']!='d3-smoke']
            status='FAIL' if any(r['status']=='FAIL' for r in required) else 'UNAVAILABLE' if any(r['status']=='UNAVAILABLE' for r in required) else 'PASS'
            result={'status':status,'scope':'STAGE_GATED_WORKFLOW','workflow_completed':True,'receipts':receipts,
                    'data_kind':'SYNTHETIC' if args.demo else 'HISTORICAL_REQUESTED',
                    'economic_evidence':False,'strategy_success':False,
                    'note':'D3 skipped by research gate is expected; stage manifests contain actual evidence scopes'}
            path=run.finish(result);print_summary(path)
        else:result,path=run_stage(root,args.stage,cfg,args)
        return {'PASS':0,'FAIL':2,'UNAVAILABLE':3}[result['status']]
    except (ResearchError,ValueError,KeyError,OSError) as exc:
        print(json.dumps({'status':'UNAVAILABLE' if isinstance(exc,Unavailable) else 'FAIL','reason':str(exc)}),file=sys.stderr)
        return 3 if isinstance(exc,Unavailable) else 2


if __name__=='__main__':
    raise SystemExit(main())
