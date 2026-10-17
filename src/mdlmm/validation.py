"""D0 receipt generation. Unit tests and real historical validation never merge."""
from __future__ import annotations
from pathlib import Path
from dataclasses import asdict
from .dataset import Dataset
from .errors import Unavailable,ValidationError,Unsupported
from .model import Check,Status,checked,aggregate,PoolState,PROGRAM_ID
from .replay import Replay,compact_trace
from .util import write_json,write_jsonl,read_jsonl,hash_file
from .network import RawStore,Http,SolanaRPC,discover_rpc,capture_pool,program_fingerprint,rank_current_api_pools
from .normalize import normalize_block,account_keys,resolve_ix
from .sdk import oracle
from .codec import IdlCodec


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
            # The old D0 checkpoint bundle omits transaction-scoped SPL CPI,
            # Oracle byte and full-block non-swap mutation evidence. LO model
            # agreement may not be promoted through that weaker D0 gate.
            if dataset.kind == 'HISTORICAL' and engine.lo_swap_count:
                raise Unavailable('LO model replay needs a separate raw-account/CPI/Oracle/full-block promotion gate')
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



def _transaction_dlmm_invocations(result:dict,pool:str)->int:
    """Count real DLMM instructions for *pool* in one finalized transaction.

    getSignaturesForAddress only proves that an address participates in a
    transaction. It does NOT prove that the owning DLMM program was invoked.
    This helper requires both the Meteora DLMM program id and the target pool
    in the resolved instruction account list.
    """
    tx=result.get('transaction');meta=result.get('meta')
    if not isinstance(tx,dict) or not isinstance(meta,dict) or meta.get('err') is not None:
        return 0
    keys=account_keys(tx,meta)
    if pool not in keys:
        return 0
    inner={x['index']:x.get('instructions',[]) for x in meta.get('innerInstructions') or []}
    count=0
    for outer_index,top in enumerate(tx['message'].get('instructions') or []):
        for ix in [top,*inner.get(outer_index,[])]:
            try:
                resolved=resolve_ix(ix,keys)
            except (Unavailable,Unsupported,ValidationError):
                continue
            if resolved['program']==PROGRAM_ID and pool in resolved['accounts']:
                count+=1
    return count


def _activity_probe(rpc:SolanaRPC,pool:str,limit:int)->dict:
    rows=rpc.signatures(pool,limit=limit)
    successful=[x for x in rows if x.get('err') is None]
    failed=len(rows)-len(successful)
    latest=max((int(x['slot']) for x in successful),default=-1)
    return {'pool':pool,'signature_rows':len(rows),'successful_signatures':len(successful),
            'failed_signatures':failed,'latest_success_slot':latest}


def _scan_pool_for_dlmm(rpc:SolanaRPC,pool:str,cfg:dict)->dict:
    """Find finalized transactions that actually invoke Meteora DLMM for pool.

    getTransaction is only a candidate filter. The caller re-fetches selected
    slots with getBlock before normalization so transaction ordering comes from
    the canonical finalized block list.
    """
    target=max(1,int(cfg.get('sample_blocks_per_pool',10)))
    minimum=max(1,min(target,int(cfg.get('minimum_sample_blocks_per_pool',1))))
    page_size=max(1,min(1000,int(cfg.get('sample_signature_page_size',25))))
    max_pages=max(1,int(cfg.get('sample_signature_max_pages',4)))
    before=None;seen=set();slots=[];slot_set=set();selected=[]
    stats={'pool':pool,'signature_rows':0,'successful_signatures':0,'failed_signatures':0,
           'transactions_fetched':0,'transaction_unavailable':0,'resolve_errors':0,
           'dlmm_invoking_transactions':0,'dlmm_instruction_count':0,'pages_scanned':0}
    for _ in range(max_pages):
        rows=rpc.signatures(pool,before=before,limit=page_size)
        stats['pages_scanned']+=1;stats['signature_rows']+=len(rows)
        if not rows:
            break
        for row in rows:
            signature=row.get('signature')
            if not signature or signature in seen:
                continue
            seen.add(signature)
            if row.get('err') is not None:
                stats['failed_signatures']+=1
                continue
            stats['successful_signatures']+=1
            try:
                result=rpc.transaction(signature);stats['transactions_fetched']+=1
                count=_transaction_dlmm_invocations(result,pool)
            except Unavailable:
                stats['transaction_unavailable']+=1
                continue
            except (Unsupported,ValidationError,KeyError,TypeError,ValueError):
                stats['resolve_errors']+=1
                continue
            if not count:
                continue
            stats['dlmm_invoking_transactions']+=1;stats['dlmm_instruction_count']+=count
            slot=int(result.get('slot',row['slot']))
            selected.append({'slot':slot,'signature':signature,'dlmm_instruction_count':count})
            if slot not in slot_set:
                slot_set.add(slot);slots.append(slot)
            if len(slots)>=target:
                break
        if len(slots)>=target or len(rows)<page_size:
            break
        before=rows[-1].get('signature')
        if not before:
            break
    stats['selected_unique_blocks']=len(slots)
    stats['target_blocks']=target
    stats['minimum_blocks']=minimum
    stats['minimum_reached']=len(slots)>=minimum
    stats['target_reached']=len(slots)>=target
    stats['selected_slots']=slots[:target]
    stats['selected_transactions']=selected
    return stats


def _select_live_pools(rpc:SolanaRPC,discovery:dict,cfg:dict,requested:list[str])->dict:
    enabled=[p['pool'] for p in discovery['pools'] if p.get('status')==0]
    enabled_set=set(enabled)
    target_pool_count=max(1,int(cfg.get('sample_pool_count',2)))
    activity_limit=max(1,min(1000,int(cfg.get('pool_activity_signature_limit',20))))
    candidate_limit=max(target_pool_count,int(cfg.get('candidate_pool_scan_limit',8)))

    api_report=None;activity=[]
    try:
        api_report=rank_current_api_pools(rpc.http,discovery,limit=candidate_limit)
        api_candidates=[x['pool'] for x in api_report['ranking']]
    except Unavailable as exc:
        api_candidates=[]
        api_report={'status':'UNAVAILABLE','reason':str(exc),'ranking':[]}

    requested_valid=[p for p in dict.fromkeys(requested) if p in enabled_set]
    requested_invalid=[p for p in dict.fromkeys(requested) if p not in enabled_set]
    candidates=list(requested_valid)
    candidates.extend(p for p in api_candidates if p not in candidates)

    # Address activity is only a last-resort candidate hint, never evidence of
    # DLMM invocation.
    if len(candidates)<candidate_limit:
        for pool in enabled:
            if pool in candidates:
                continue
            try:
                activity.append(_activity_probe(rpc,pool,activity_limit))
            except Unavailable as exc:
                activity.append({'pool':pool,'signature_rows':0,'successful_signatures':0,
                                 'failed_signatures':0,'latest_success_slot':-1,'unavailable':str(exc)})
        activity.sort(key=lambda x:(x.get('successful_signatures',0),x.get('latest_success_slot',-1)),reverse=True)
        candidates.extend(x['pool'] for x in activity if x['pool'] not in candidates)

    candidates=candidates[:candidate_limit]
    if requested_valid:
        basis='EXPLICIT_CURRENT_POOLS_THEN_OFFICIAL_API_VOLUME_FALLBACK_THEN_DLMM_INVOCATION'
    elif api_candidates:
        basis='OFFICIAL_API_VOLUME_THEN_ACTUAL_DLMM_INVOCATION'
    else:
        basis='RECENT_SUCCESS_ACTIVITY_FALLBACK_THEN_ACTUAL_DLMM_INVOCATION'

    scans={}
    for pool in candidates:
        scan=_scan_pool_for_dlmm(rpc,pool,cfg);scans[pool]=scan
        if sum(x['minimum_reached'] for x in scans.values())>=target_pool_count:
            break
    selected=[p for p in candidates if p in scans and scans[p]['minimum_reached']][:target_pool_count]
    return {'basis':basis,'enabled_pool_count':len(enabled),'activity':activity,
            'api_ranking':api_report,'requested_pools':requested_valid,'requested_invalid':requested_invalid,
            'candidate_pools':candidates,'scans':scans,'selected_pools':selected,
            'target_pool_count':target_pool_count,
            'target_blocks_per_pool':max(1,int(cfg.get('sample_blocks_per_pool',10))),
            'minimum_blocks_per_pool':max(1,int(cfg.get('minimum_sample_blocks_per_pool',1))),
            'sample_target_complete':len(selected)==target_pool_count and all(scans[p]['target_reached'] for p in selected),
            'selection_complete':len(selected)==target_pool_count}

def live_sample(root:Path,output:Path,config:dict)->dict:
    """Current-account and actual finalized transaction SAMPLE integration checks.

    Pool-address activity is only a discovery hint. A transaction is accepted
    for the sample only after getTransaction proves that the Meteora DLMM
    program was actually invoked with the target pool. Selected slots are then
    fetched again with full getBlock so canonical transaction ordering is kept.

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
    selection=_select_live_pools(rpc,discovery,cfg,requested)
    write_json(output/'pool_selection.json',selection)
    # D0 current-mechanics validation needs at least one real active pool.
    # The two-pool requirement belongs to the later D1 pilot and is reported
    # separately rather than blocking current SDK/account/event validation.
    current_validation_complete=bool(selection['selected_pools'])
    d1_pool_target_complete=selection['selection_complete']
    selection_status=Status.PASS if current_validation_complete else Status.UNAVAILABLE
    selection_detail='' if d1_pool_target_complete else (
        'current D0 validation has an actual-DLMM pool, but the D1 two-pool target is not yet met'
        if current_validation_complete else
        'no pool reached the minimum actual-DLMM-invocation sample gate')
    checks.append(Check('current_active_pool_selection',selection_status,selection_detail,
                        metrics={'selected_pools':selection['selected_pools'],
                                 'validated_current_pool_count':len(selection['selected_pools']),
                                 'current_validation_minimum_pool_count':1,
                                 'current_validation_complete':current_validation_complete,
                                 'd1_target_pool_count':selection['target_pool_count'],
                                 'd1_pool_target_complete':d1_pool_target_complete,
                                 'candidate_pools_scanned':len(selection['scans'])}))
    idl_path=root/'external/dlmm-sdk/ts-client/src/dlmm/idl/idl.json'
    idl=IdlCodec.load(idl_path) if idl_path.exists() else None
    for pool in selection['selected_pools']:
        folder=output/pool;folder.mkdir()
        try:
            capture=capture_pool(rpc,pool);path=folder/'capture.json';write_json(path,capture)
            captures.append(str(path.relative_to(output)))
            checks.append(Check(f'{pool}/account_decode',Status.PASS,metrics={'slot':capture['slot'],'bins':len(capture['state']['bins']),
                'atomic_current_snapshot':capture.get('atomic_current_snapshot',False),
                'context_slot_span':capture.get('context_slot_span'),
                'source_kind':capture.get('source_kind'),
                'historical_seed_allowed':capture.get('historical_seed_allowed',False)}))
            checks.append(checked(f'{pool}/official_sdk_parity',lambda:oracle(root,path,folder/'sdk',
                [{'amount_in':'1000000','swap_for_y':True},{'amount_in':'1000000','swap_for_y':False}])))
            scan=selection['scans'][pool];n=0;swap_events=0;blockers=0
            for slot in sorted(scan['selected_slots']):
                block=rpc.block(slot);h=rpc.http.last_raw_hash
                write_json(folder/f'block_{slot}.json',{'slot':slot,'block':block,'raw_sha256':h})
                normalized=normalize_block(slot,block,{pool},raw_hash=h,idl=idl)
                events+=normalized['events'];n+=len(normalized['events'])
                swap_events+=sum(x['kind']=='SWAP' for x in normalized['events'])
                blockers+=len(normalized['blockers'])
                write_json(folder/f'normalized_{slot}.json',normalized)
            tx_status=Status.PASS if scan['minimum_reached'] and swap_events>0 else Status.UNAVAILABLE
            detail='' if tx_status==Status.PASS else 'no decoded swap event in the minimum actual-DLMM block sample'
            checks.append(Check(f'{pool}/actual_transaction_sample_decode',tx_status,detail,
                                metrics={'decoded_events':n,'decoded_swap_events':swap_events,
                                         'normalized_blockers':blockers,
                                         'sampled_unique_blocks':scan['selected_unique_blocks'],
                                         'minimum_blocks':scan['minimum_blocks'],
                                         'minimum_reached':scan['minimum_reached'],
                                         'target_blocks':scan['target_blocks'],
                                         'target_reached':scan['target_reached'],
                                         'sampled_dlmm_transactions':scan['dlmm_invoking_transactions'],
                                         'sampled_dlmm_instructions':scan['dlmm_instruction_count'],
                                         'program_version':'UNKNOWN_FOR_HISTORICAL_TX',
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
            'selected_pools':selection['selected_pools'],
            'current_validation_complete':bool(selection['selected_pools']),
            'd1_pool_target_complete':selection['selection_complete'],
            'selection_complete':selection['selection_complete'],
            'reason':'Current state + transaction decode does not establish historical state replay'}
