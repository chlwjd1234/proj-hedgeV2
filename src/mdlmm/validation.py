"""D0 receipt generation. Unit tests and real historical validation never merge."""
from __future__ import annotations
from pathlib import Path
from dataclasses import asdict
from .dataset import Dataset
from .errors import Unavailable,ValidationError
from .model import Check,Status,checked,aggregate,PoolState
from .replay import Replay,compact_trace
from .util import write_json,write_jsonl,read_jsonl,hash_file
from .network import RawStore,Http,SolanaRPC,discover_rpc,capture_pool,program_fingerprint
from .normalize import normalize_block
from .sdk import oracle


def validate_dataset(dataset:Dataset,output:Path)->dict:
    checks=[];segments={};counts=0
    for segment in dataset.segments:
        sid=segment['id']
        if any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in sid):
            raise ValidationError('unsafe segment id')
        trace_rel=f'traces/{sid}.jsonl.gz'
        local=[]
        try:
            initial=dataset.snapshot(segment['snapshot'])
            if initial.pool!=segment['pool']:
                raise ValidationError('segment/snapshot pool mismatch')
            capture=__import__('json').loads(dataset.file(segment['snapshot']).read_text())
            if capture['timestamp_ms']>segment['start_ms']:
                raise ValidationError('starting snapshot is from the future')
            if initial.data_kind=='CURRENT':
                raise ValidationError('current snapshot cannot seed past historical replay')
            checkpoints=dataset.checkpoints(segment)
            coverage=dataset.verify_coverage(segment,dataset.events(segment))
            if coverage['last_event_id'] not in checkpoints:
                raise Unavailable('last event needs an independent ending account checkpoint')
            engine=Replay(initial,dataset.registry)
            n=write_jsonl(output/trace_rel,(compact_trace(r) for r in engine.run(dataset.events(segment),checkpoints)))
            report=engine.report();counts+=n
            if engine.scope_level!='ACCOUNT_PARITY':
                raise Unavailable('factual replay did not achieve independent account parity')
            if engine.checkpoints!=len(checkpoints):
                raise ValidationError('one or more checkpoint IDs were never reached')
            if not coverage['swaps']:
                raise Unavailable('no real swap observations to validate quote/fee parity')
            for name in ('starting_account_decode','active_bin_parity','bin_reserve_liquidity_parity',
                         'fee_parameter_reference_parity','quote_fee_observation_parity','transaction_order_determinism',
                         'financial_token_conservation','replay_state_continuity','historical_coverage'):
                local.append(Check(f'{sid}/{name}',Status.PASS,metrics={'events':n,'scope':report['account_parity_scope']}))
            segments[sid]={'status':'PASS','trace':trace_rel,'trace_sha256':hash_file(output/trace_rel),
                           **report,'coverage':coverage}
        except Unavailable as exc:
            local.append(Check(f'{sid}/replay',Status.UNAVAILABLE,str(exc)))
            segments[sid]={'status':'UNAVAILABLE','reason':str(exc)}
        except (ValidationError,KeyError,ValueError,TypeError) as exc:
            local.append(Check(f'{sid}/replay',Status.FAIL,f'{type(exc).__name__}: {exc}'))
            segments[sid]={'status':'FAIL','reason':str(exc)}
        checks+=local
    dataset.recheck()
    status=aggregate(checks)
    write_json(output/'checks.json',[asdict(c) for c in checks])
    return {'status':str(status),'replay_gate':str(status),'historical_gate':str(status) if dataset.kind=='HISTORICAL' else 'UNAVAILABLE','data_kind':dataset.kind,
            'historical_evidence':dataset.kind=='HISTORICAL' and status==Status.PASS,
            'dataset_sha256':hash_file(dataset.path),'input_hashes':dataset.files,
            'segments':segments,'events_validated':counts,
            'checks':[asdict(c) for c in checks],
            'full_program_exactness':'UNAVAILABLE','level_scope':'MM_ECONOMIC_STATE_NOT_FULL_ACCOUNT_BYTES',
            'not_claimed':['Full program EXACT parity','Limit-order state mutations','Transfer-hook/Token-2022 replay']}


def live_sample(root:Path,output:Path,config:dict)->dict:
    """Current-account and actual finalized transaction SAMPLE integration checks.

    This does not bootstrap a 2025 historical state and cannot open the D1 gate.
    """
    raw=RawStore(root/'data/raw')
    cfg=config.get('network',{})
    rpc=SolanaRPC(Http(raw,timeout=cfg.get('timeout_seconds',15),retries=cfg.get('retries',1),
                       requests_per_second=cfg.get('requests_per_second',3)))
    checks=[];captures=[];events=[]
    try:
        discovery=discover_rpc(rpc);write_json(output/'pool_discovery.json',discovery)
        checks.append(Check('current_pool_discovery',Status.PASS if discovery['pools'] else Status.UNAVAILABLE,
                            'No target pools is not a completed sample' if not discovery['pools'] else '',
                            metrics={'pools':len(discovery['pools'])}))
    except Unavailable as exc:
        return {'status':'UNAVAILABLE','scope':'CURRENT_RPC_SAMPLE_ONLY','historical_gate':'UNAVAILABLE',
                'checks':[asdict(Check('current_pool_discovery',Status.UNAVAILABLE,str(exc)))],
                'reason':'RPC unavailable. No on-chain validation claim.'}
    requested=config.get('data',{}).get('live_pool_ids',[])
    chosen=requested or [p['pool'] for p in discovery['pools'] if p['status']==0][:2]
    for pool in chosen:
        folder=output/pool;folder.mkdir()
        try:
            capture=capture_pool(rpc,pool);path=folder/'capture.json';write_json(path,capture)
            captures.append(str(path.relative_to(output)))
            checks.append(Check(f'{pool}/account_decode',Status.PASS,metrics={'slot':capture['slot'],'bins':len(capture['state']['bins'])}))
            checks.append(checked(f'{pool}/official_sdk_parity',lambda:oracle(root,path,folder/'sdk',
                [{'amount_in':'1000000','swap_for_y':True},{'amount_in':'1000000','swap_for_y':False}])))
            sigs=rpc.signatures(pool,limit=10)
            slots=sorted({s['slot'] for s in sigs if s.get('err') is None})[-cfg.get('sample_blocks_per_pool',2):]
            n=0
            for slot in slots:
                block=rpc.block(slot);h=rpc.http.last_raw_hash
                write_json(folder/f'block_{slot}.json',{'slot':slot,'block':block,'raw_sha256':h})
                normalized=normalize_block(slot,block,{pool},raw_hash=h)
                events+=normalized['events'];n+=len(normalized['events'])
                write_json(folder/f'normalized_{slot}.json',normalized)
            checks.append(Check(f'{pool}/actual_transaction_sample_decode',Status.PASS if n else Status.UNAVAILABLE,
                                metrics={'decoded_events':n,'program_version':'UNKNOWN_FOR_HISTORICAL_TX',
                                         'factual_state_replay':'UNAVAILABLE_NO_PRE_TX_SNAPSHOT'}))
        except Unavailable as exc:
            checks.append(Check(f'{pool}/sample',Status.UNAVAILABLE,str(exc)))
        except (ValidationError,KeyError,ValueError) as exc:
            checks.append(Check(f'{pool}/sample',Status.FAIL,str(exc)))
    try:
        fingerprint=program_fingerprint(rpc)
        write_json(output/'program_observation.json',{k:v for k,v in fingerprint.items() if k!='binary_base64'})
        import base64
        (output/'current_observed_program.so').write_bytes(base64.b64decode(fingerprint['binary_base64']))
        checks.append(Check('current_program_binary_hash',Status.PASS,metrics={'sha256':fingerprint['binary_sha256']}))
    except Unavailable as exc:
        checks.append(Check('current_program_binary_hash',Status.UNAVAILABLE,str(exc)))
    write_jsonl(output/'normalized_transaction_samples.jsonl',events)
    write_json(output/'checks.json',[asdict(c) for c in checks])
    return {'status':str(aggregate(checks)),'scope':'CURRENT_RPC_SAMPLE_ONLY','historical_gate':'UNAVAILABLE',
            'checks':[asdict(c) for c in checks],'captures':captures,'transaction_events':len(events),
            'reason':'Current state + transaction decode does not establish historical state replay'}
