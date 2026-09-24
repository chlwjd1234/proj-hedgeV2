"""Explicit read-only Hyperliquid archive acquisition and funding normalization.

Requester-pays downloads are opt-in and size bounded. Public fundingHistory
returns rates, not the historical oracle price needed for settlement notional;
normalization refuses missing oracle evidence instead of inserting zero.
"""
from __future__ import annotations
import os
from pathlib import Path
from .errors import Unavailable,ValidationError
from .network import Http
from .util import hash_file,write_json,read_jsonl,dec,D
from .perp import HOUR


def download_s3_object(bucket:str,key:str,destination:Path,*,max_bytes:int,
                       requester_pays:bool=False)->dict:
    if not bucket or not key or max_bytes<=0:
        raise ValidationError('S3 bucket/key/positive max_bytes required')
    if destination.exists():raise ValidationError('archive destination already exists')
    try:
        import boto3
        from botocore.exceptions import BotoCoreError,ClientError
    except ImportError as exc:raise Unavailable('archive extra not installed') from exc
    client=boto3.client('s3')
    args={'Bucket':bucket,'Key':key}
    if requester_pays:args['RequestPayer']='requester'
    try:
        header=client.head_object(**args)
        length=header['ContentLength']
        if length>max_bytes:raise Unavailable('S3 object exceeds explicit --max-bytes cost/storage bound')
        response=client.get_object(**args)
        # Reject source replacement between HEAD and GET, rather than trusting
        # a multipart ETag as though it were an MD5 content hash.
        if response.get('ETag')!=header.get('ETag'):
            response['Body'].close();raise ValidationError('S3 object changed between HEAD and GET')
        destination.parent.mkdir(parents=True,exist_ok=True)
        n=0
        try:
            with destination.open('xb') as out:
                for chunk in response['Body'].iter_chunks(1024*1024):
                    n+=len(chunk)
                    if n>max_bytes:raise ValidationError('S3 size bound exceeded while streaming')
                    out.write(chunk)
            if n!=length:raise ValidationError('incomplete S3 object body')
        except BaseException:
            destination.unlink(missing_ok=True);raise
        finally:response['Body'].close()
        report={'source':'S3','bucket':bucket,'key':key,'bytes':n,'sha256':hash_file(destination),
                'etag':header.get('ETag'),'version_id':response.get('VersionId'),
                'requester_pays_explicit':requester_pays,'fee_or_funding_assumptions':None}
        write_json(destination.with_name(destination.name+'.receipt.json'),report,exclusive=True)
        return report
    except (BotoCoreError,ClientError) as exc:
        raise Unavailable('S3 archive access failed: '+type(exc).__name__) from exc


def collect_funding_rates(http:Http,start_ms:int,end_ms:int,*,max_pages:int=100)->dict:
    if not 0<=start_ms<end_ms:raise ValidationError('invalid funding range')
    cursor=start_ms;rows={};receipts=[];complete=False
    for _ in range(max_pages):
        batch=http.request('https://api.hyperliquid.xyz/info',
            {'type':'fundingHistory','coin':'SOL','startTime':cursor,'endTime':end_ms},
            metadata={'source':'HYPERLIQUID_FUNDING_HISTORY_RATES_ONLY'})
        receipts.append(http.last_raw_hash)
        if not isinstance(batch,list):raise Unavailable('unrecognized funding API response')
        if not batch:break
        last=cursor-1
        for r in batch:
            t=int(r['time'])
            if r.get('coin','SOL')!='SOL' or t<cursor or t> end_ms:
                raise ValidationError('funding response coin/time invalid')
            if t in rows and rows[t]!=r:raise ValidationError('conflicting funding records')
            rows[t]=r;last=max(last,t)
        if last<cursor:raise ValidationError('funding pagination did not advance')
        cursor=last+1
        if cursor>end_ms:complete=True;break
        if len(batch)<500:break
    due=set(range((start_ms//HOUR+1)*HOUR,end_ms+1,HOUR))
    missing=sorted(due-set(rows))
    return {'status':'PASS' if not missing else 'UNAVAILABLE','source':'HYPERLIQUID_FUNDING_HISTORY',
            'coverage_start_ms':start_ms,'coverage_end_ms':end_ms,'events':[rows[k] for k in sorted(rows)],
            'raw_source_hashes':receipts,'missing_payable_hours':missing,
            'oracle_prices_available':False,'usable_for_funding_settlement':False}


def join_funding_oracles(rates:dict,oracle_rows)->dict:
    """Exact payable-hour oracle join, not nearest spot or book-mid substitution."""
    oracles={}
    for r in oracle_rows:
        if r.get('symbol')!='SOL' or r.get('kind')!='HYPERLIQUID_ORACLE' or not r.get('source'):
            raise ValidationError('funding requires sourced SOL exchange-oracle observations')
        t=int(r['time_ms'])
        if t in oracles:raise ValidationError('duplicate funding oracle hour')
        p=dec(r['oracle_price'])
        if p<=0:raise ValidationError('invalid funding oracle price')
        oracles[t]=p
    events=[]
    start,end=rates['coverage_start_ms'],rates['coverage_end_ms']
    by_time={int(r['time']):r for r in rates['events']}
    for t in range((start//HOUR+1)*HOUR,end+1,HOUR):
        if t not in by_time or t not in oracles:raise Unavailable(f'funding rate/oracle missing at {t}')
        events.append({'time_ms':t,'rate':str(dec(by_time[t]['fundingRate'])),'oracle_price':str(oracles[t])})
    return {'coverage_start_ms':start,'coverage_end_ms':end,'source':'EXACT_HYPERLIQUID_FUNDING_ORACLE_JOIN',
            'events':events,'zero_funding_imputed':False}
