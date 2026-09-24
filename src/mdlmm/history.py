"""Pool-address history pagination with full-block canonical ordering.

This fetches raw transactions, NOT historical bin snapshots. Completeness is
conditional on the archive provider's finalized address index. A start-boundary
pagination proof is required, and exact Clock/account inputs remain separate.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path
from .errors import Unavailable, ValidationError, Unsupported
from .network import SolanaRPC
from .util import read_json, write_json, hash_file, safe_path


def collect_pool_history(rpc: SolanaRPC, pool: str, start_slot: int, end_slot: int,
                         output: Path, *, max_pages: int = 100, max_blocks: int = 10000) -> dict:
    if not 0 <= start_slot < end_slot or min(max_pages,max_blocks)<1:
        raise ValidationError('invalid historical collection bounds')
    output.mkdir(parents=True, exist_ok=True)
    db=sqlite3.connect(output/'signature_index.sqlite')
    db.execute('CREATE TABLE IF NOT EXISTS signatures(sig TEXT PRIMARY KEY, slot INTEGER NOT NULL)')
    if db.execute('SELECT COUNT(*) FROM signatures').fetchone()[0]:
        raise ValidationError('collector output already populated; use a new immutable run')
    before=None;pages=[];reached=False;last_slot=None;missing=[];first_slot=None
    try:
        for page_number in range(max_pages):
            rows=rpc.signatures(pool,before,1000)
            saved={'method':'getSignaturesForAddress','address':pool,'before':before,
                   'commitment':'finalized','result':rows,'raw_sha256':rpc.http.last_raw_hash}
            path=output/f'signatures_{page_number:05d}.json';write_json(path,saved,exclusive=True)
            pages.append({'path':path.name,'sha256':hash_file(path)})
            if not rows:
                # An exhausted provider index is NOT proof that older history
                # never existed; do not silently declare complete.
                break
            for item in rows:
                slot=int(item['slot']);sig=item['signature']
                if first_slot is None:first_slot=slot
                if last_slot is not None and slot>last_slot:
                    raise ValidationError('address-index slots not descending')
                last_slot=slot
                if start_slot<=slot<end_slot:
                    try:db.execute('INSERT INTO signatures VALUES(?,?)',(sig,slot))
                    except sqlite3.IntegrityError as exc:raise ValidationError('duplicate paginated signature') from exc
                if slot<start_slot:reached=True
            db.commit()
            before=rows[-1]['signature']
            if reached:break
        slots=[r[0] for r in db.execute('SELECT DISTINCT slot FROM signatures ORDER BY slot')]
        if len(slots)>max_blocks:
            raise Unsupported(f'{len(slots)} target blocks exceed --max-blocks; narrow explicit range')
        refs=[]
        for slot in slots:
            try:
                block=rpc.block(slot)
                expected={r[0] for r in db.execute('SELECT sig FROM signatures WHERE slot=?',(slot,))}
                actual={t['transaction']['signatures'][0] for t in block['transactions']}
                if not expected.issubset(actual):
                    raise ValidationError('address-index signature missing from finalized block')
                path=output/f'block_{slot}.json'
                write_json(path,{'slot':slot,'block':block,'raw_sha256':rpc.http.last_raw_hash},exclusive=True)
                refs.append({'path':path.name,'sha256':hash_file(path)})
            except Unavailable as exc:
                missing.append({'slot':slot,'reason':str(exc)})
        # Newest index item may precede end_slot for an inactive pool: that is
        # legitimate because pagination starts at the current finalized head.
        complete=reached and not missing and bool(slots)
        report={'schema_version':1,'source_kind':'POOL_ADDRESS_SIGNATURE_SCAN','pool':pool,
                'start_slot':start_slot,'end_slot':end_slot,'signature_pages':pages,
                'produced_slots':slots,'blocks':refs,'missing':missing,'complete':complete,
                'pagination_reached_before_start':reached,'pagination_started_at_current_head':True,
                'first_indexed_slot':first_slot,'signatures':db.execute('SELECT COUNT(*) FROM signatures').fetchone()[0],
                'source_assumption':'FINALIZED_PROVIDER_ADDRESS_INDEX_COMPLETE',
                'historical_state_obtained':False,'historical_clock_obtained':False}
        write_json(output/'coverage.json',report,exclusive=True)
        return report
    finally:db.close()


def verify_signature_coverage(coverage:dict, resolve_file) -> list[int]:
    """Re-read raw pagination, not just caller-supplied complete=True."""
    start,end=coverage['start_slot'],coverage['end_slot']
    if not 0<=start<end or not coverage.get('signature_pages'):
        raise ValidationError('invalid signature coverage descriptor')
    before=None;last_slot=None;reached=False;slots=set();all_signatures=set()
    # Bounded by the collector's explicit pagination budget. External exports
    # must split huge windows rather than ask this verifier to hold all history.
    for ref in coverage['signature_pages']:
        page=read_json(resolve_file(ref))
        if page.get('address')!=coverage['pool'] or page.get('before')!=before or page.get('commitment')!='finalized':
            raise ValidationError('signature pagination chain/address/commitment mismatch')
        rows=page['result']
        if not rows:raise Unavailable('address archive exhausted before start proof')
        for item in rows:
            slot=int(item['slot']);sig=item['signature']
            if last_slot is not None and slot>last_slot:raise ValidationError('signature slots not descending')
            if sig in all_signatures:raise ValidationError('duplicate signature in pagination proof')
            all_signatures.add(sig);last_slot=slot
            if start<=slot<end:slots.add(slot)
            if slot<start:reached=True
        before=rows[-1]['signature']
    if not reached:raise Unavailable('signature scan did not reach before starting snapshot')
    if sorted(slots)!=coverage['produced_slots']:
        raise ValidationError('target block inventory differs from raw address scan')
    return sorted(slots)


def discover_historical_creations(block_descriptors:list[dict],resolve_file)->dict:
    """Find canonical SOL/USDC create events, including now-closed pool addresses.

    A limited block export cannot prove completeness of the historical universe.
    """
    from .normalize import account_keys,resolve_ix,EVENT_CPI_TAG
    from .codec import decode_event
    from .model import PROGRAM_ID,SOL_MINT,USDC_MINT
    pools={};unknown=0;scanned=0
    for desc in block_descriptors:
        saved=read_json(resolve_file(desc));slot=saved['slot'];block=saved['block'];scanned+=1
        for ti,item in enumerate(block['transactions']):
            meta=item['meta'];tx=item['transaction']
            if meta is None or meta.get('err') is not None:continue
            keys=account_keys(tx,meta)
            if PROGRAM_ID not in keys:continue
            inner={r['index']:r['instructions'] for r in meta.get('innerInstructions') or []}
            for oi,top in enumerate(tx['message']['instructions']):
                for ii,ix in [(-1,top),*enumerate(inner.get(oi,[]))]:
                    try:r=resolve_ix(ix,keys)
                    except Unsupported:
                        unknown+=1;continue
                    if r['program']!=PROGRAM_ID or not r['raw'].startswith(EVENT_CPI_TAG):continue
                    try:event=decode_event(r['raw'][8:])
                    except (Unsupported,ValidationError):
                        unknown+=1;continue
                    fields=event['data']
                    if event['name']=='LbPairCreate' and {fields['token_x'],fields['token_y']}=={SOL_MINT,USDC_MINT}:
                        pool=fields['lb_pair']
                        if pool in pools:raise ValidationError('duplicate pool creation event')
                        pools[pool]={'pool':pool,'created_slot':slot,'created_time_ms':block['blockTime']*1000,
                           'transaction_index':ti,'instruction_index':oi,'inner_instruction_index':ii,
                           'signature':tx['signatures'][0],'bin_step':fields['bin_step'],
                           'token_x_mint':fields['token_x'],'token_y_mint':fields['token_y'],
                           'raw_block_sha256':desc['sha256'],'alive_today':'NOT_FILTERED'}
    return {'pools':list(pools.values()),'blocks_scanned':scanned,'unknown_event_decodes':unknown,
            'historical_universe_complete':False,'selection_basis':'RAW_CREATION_EVENTS_NOT_CURRENT_APR'}
