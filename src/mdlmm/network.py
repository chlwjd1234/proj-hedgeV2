"""Read-only acquisition, explicit rate/error handling and immutable raw evidence."""
from __future__ import annotations

import base64
import gzip
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .codec import (account_bytes, b58encode, decode_lb_pair, decode_bin_array,
                    decode_clock, discriminator, build_pool_state, find_program_address, b58decode)
from .errors import Unavailable, ValidationError, Unsupported
from .model import PROGRAM_ID, SOL_MINT, USDC_MINT, CLOCK_ID
from .util import canonical, sha256, write_json, read_json, hash_file, git_blob_hash, digest

READ_METHODS = {'getVersion', 'getSlot', 'getBlock', 'getBlockTime', 'getBlocks',
                'getAccountInfo', 'getMultipleAccounts', 'getProgramAccounts',
                'getSignaturesForAddress', 'getTransaction', 'getGenesisHash'}
DATA_API = 'https://dlmm.datapi.meteora.ag'


class RawStore:
    def __init__(self, root: Path):
        self.root = root

    def put(self, payload: bytes, metadata: dict) -> str:
        h = sha256(payload)
        path = self.root / 'objects' / h[:2] / (h + '.json.gz')
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open('xb') as out:
                out.write(gzip.compress(payload, mtime=0))
        except FileExistsError:
            if sha256(gzip.decompress(path.read_bytes())) != h:
                raise ValidationError('raw object collision/corruption')
        receipt = self.root / 'receipts' / (str(time.time_ns()) + '_' + uuid.uuid4().hex[:8] + '.json')
        write_json(receipt, {**metadata, 'raw_sha256': h, 'received_ms': time.time_ns() // 1_000_000}, exclusive=True)
        return h

    def get(self, h: str) -> Any:
        if len(h) != 64 or any(c not in '0123456789abcdef' for c in h):
            raise ValidationError('bad raw hash')
        path = self.root / 'objects' / h[:2] / (h + '.json.gz')
        if not path.exists():
            raise Unavailable(f'raw evidence object missing: {h}')
        raw = gzip.decompress(path.read_bytes())
        if sha256(raw) != h:
            raise ValidationError('raw evidence hash mismatch')
        return json.loads(raw)


class Http:
    def __init__(self, raw: RawStore | None = None, *, timeout: float = 20,
                 retries: int = 3, requests_per_second: float = 4, max_bytes: int = 128_000_000):
        self.raw = raw
        self.timeout, self.retries = timeout, retries
        self.interval = 1 / requests_per_second
        self.last = 0.0
        self.max_bytes = max_bytes
        self.last_raw_hash: str | None = None

    def request(self, url: str, payload: dict | None = None, *, metadata: dict | None = None) -> Any:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ('https', 'http'):
            raise ValidationError('only HTTP(S) sources supported')
        data = canonical(payload) if payload is not None else None
        request = urllib.request.Request(url, data=data, headers={
            'Content-Type': 'application/json', 'Accept': 'application/json',
            'User-Agent': 'meteora-dlmm-hedged-research/0.1.0'})
        last_error = 'unknown'
        for attempt in range(self.retries + 1):
            time.sleep(max(0, self.last + self.interval - time.monotonic()))
            self.last = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise Unsupported('HTTP body exceeds configured memory bound')
                # Never store RPC credential query/path/userinfo in manifests.
                md = {'source_host': parsed.hostname, 'request': payload, **(metadata or {})}
                self.last_raw_hash = self.raw.put(raw, md) if self.raw else sha256(raw)
                return json.loads(raw)
            except urllib.error.HTTPError as exc:
                last_error = f'HTTP {exc.code} from {parsed.hostname}'
                if exc.code not in (408, 429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
                last_error = f'{type(exc).__name__} from {parsed.hostname}'
            except json.JSONDecodeError as exc:
                raise Unavailable(f'non-JSON response from {parsed.hostname}') from exc
            if attempt < self.retries:
                time.sleep(min(8, 0.5 * 2**attempt))
        raise Unavailable(last_error)


class SolanaRPC:
    def __init__(self, http: Http, url: str | None = None):
        self.http = http
        self.url = url or os.environ.get('SOLANA_RPC_URL')
        self.n = 0

    def call(self, method: str, params: list | None = None):
        if method not in READ_METHODS:
            raise ValidationError(f'RPC write/unapproved method prohibited: {method}')
        if not self.url:
            raise Unavailable('SOLANA_RPC_URL is required; public Solana RPC fallback is disabled')
        self.n += 1
        response = self.http.request(self.url, {'jsonrpc': '2.0', 'id': self.n, 'method': method, 'params': params or []},
                                     metadata={'source': 'solana_rpc', 'commitment': 'finalized'})
        if 'error' in response:
            error = response['error']
            raise Unavailable(f'RPC {method} error {error.get("code")}: {str(error.get("message"))[:160]}')
        if 'result' not in response or response['result'] is None:
            raise Unavailable(f'RPC {method} result unavailable')
        return response['result']

    def block(self, slot: int) -> dict:
        return self.call('getBlock', [slot, {'commitment': 'finalized', 'encoding': 'json',
                         'transactionDetails': 'full', 'rewards': False, 'maxSupportedTransactionVersion': 1}])


    def transaction(self, signature: str) -> dict:
        return self.call('getTransaction', [signature, {'commitment': 'finalized', 'encoding': 'json',
                         'maxSupportedTransactionVersion': 1}])

    def signatures(self, address: str, before: str | None = None, limit: int = 1000) -> list:
        opts = {'commitment': 'finalized', 'limit': min(limit, 1000)}
        if before:
            opts['before'] = before
        return self.call('getSignaturesForAddress', [address, opts])

    def account(self, address: str) -> dict:
        return self.call('getAccountInfo', [address, {'commitment': 'finalized', 'encoding': 'base64'}])

    def multiple_accounts(self, addresses: list[str], *, min_context_slot: int | None = None) -> dict:
        if len(addresses) > 100:
            raise Unsupported('getMultipleAccounts accepts at most 100 pubkeys')
        opts = {'commitment': 'finalized', 'encoding': 'base64'}
        if min_context_slot is not None:
            opts['minContextSlot'] = int(min_context_slot)
        return self.call('getMultipleAccounts', [addresses, opts])

    def program_accounts(self, filters: list, *, min_context_slot: int | None = None) -> dict:
        opts = {'commitment': 'finalized', 'encoding': 'base64',
                'withContext': True, 'filters': filters}
        if min_context_slot is not None:
            opts['minContextSlot'] = int(min_context_slot)
        return self.call('getProgramAccounts', [PROGRAM_ID, opts])


def discover_rpc(rpc: SolanaRPC) -> dict:
    rows, slots = {}, []
    for x, y in ((SOL_MINT, USDC_MINT), (USDC_MINT, SOL_MINT)):
        result = rpc.program_accounts([
            {'memcmp': {'offset': 0, 'bytes': b58encode(discriminator('account', 'LbPair'))}},
            {'memcmp': {'offset': 88, 'bytes': x}}, {'memcmp': {'offset': 120, 'bytes': y}}])
        slots.append(result['context']['slot'])
        for item in result['value']:
            decoded = decode_lb_pair(account_bytes(item))
            if decoded['token_x_mint'] != x or decoded['token_y_mint'] != y:
                raise ValidationError('RPC pool filter/decoder disagreement')
            rows[item['pubkey']] = {'pool': item['pubkey'], 'observed_slot': result['context']['slot'],
                                    'discovery_basis': 'CURRENT_ALIVE_ACCOUNTS_NOT_HISTORICAL_UNIVERSE',
                                    **decoded}
    if not rows:
        raise Unavailable('no canonical SOL/USDC pool accounts returned')
    return {'pools': sorted(rows.values(), key=lambda r: r['pool']), 'context_slots': slots,
            'historical_universe_complete': False}


def discover_api(http: Http, max_pages: int = 20) -> dict:
    pools = {}
    pages = 0
    for page in range(1, max_pages + 1):
        query = urllib.parse.urlencode({'page': page, 'page_size': 1000, 'query': 'SOL-USDC'})
        response = http.request(DATA_API + '/pools?' + query, metadata={'source': 'meteora_data_api', 'page': page})
        if isinstance(response, list):
            values = response
        elif isinstance(response, dict):
            data = response.get('data', response)
            values = data if isinstance(data, list) else data.get('pools', data.get('data', []))
        else:
            raise Unsupported('unrecognized Meteora pool response schema')
        if not isinstance(values, list):
            raise Unsupported('Meteora pools payload is not a list')
        pages += 1
        for r in values:
            # Discovery hints only. RPC mint verification is mandatory.
            address = r.get('address') or r.get('pool_address')
            if address:
                pools[address] = r
        if len(values) < 1000:
            break
    if not pools:
        raise Unavailable('no API discovery hints; use RPC discovery')
    return {'candidates': list(pools.values()), 'pages': pages, 'truncated': pages == max_pages,
            'mint_verified': False, 'historical_universe_complete': False}


def rank_current_api_pools(http: Http, discovery: dict, *, limit: int = 20) -> dict:
    """Rank CURRENT exact SOL/USDC pools by official Meteora 24h volume.

    The Data API is only a candidate-ranking hint. Every returned address is
    intersected with the exact-mint, enabled on-chain accounts already decoded
    by discover_rpc; it is never accepted as chain-state truth by itself.
    """
    enabled = {p['pool']: p for p in discovery['pools'] if p.get('status') == 0}
    query = urllib.parse.urlencode({'page': 1, 'page_size': 100, 'query': 'SOL-USDC',
                                    'sort_by': 'volume_24h:desc', 'filter_by': 'is_blacklisted=false'})
    response = http.request(DATA_API + '/pools?' + query,
                            metadata={'source': 'meteora_data_api', 'purpose': 'CURRENT_D0_POOL_RANKING'})
    if isinstance(response, list):
        values = response
        total = len(values)
    elif isinstance(response, dict):
        values = response.get('data', response.get('pools', []))
        if isinstance(values, dict):
            values = values.get('data', values.get('pools', []))
        total = response.get('total')
    else:
        raise Unsupported('unrecognized Meteora ranked-pool response schema')
    if not isinstance(values, list):
        raise Unsupported('Meteora ranked-pool payload is not a list')

    ranked=[]
    for row in values:
        if not isinstance(row,dict):
            continue
        address=row.get('address') or row.get('pool_address')
        tx=row.get('token_x') or {}; ty=row.get('token_y') or {}
        mx=tx.get('address') or row.get('token_x_mint')
        my=ty.get('address') or row.get('token_y_mint')
        if address not in enabled or {mx,my}!={SOL_MINT,USDC_MINT}:
            continue
        volume=row.get('volume') or {}; fees=row.get('fees') or {}; pc=row.get('pool_config') or {}
        def number(x):
            try:return float(x or 0)
            except (TypeError,ValueError):return 0.0
        ranked.append({'pool':address,'name':row.get('name'),
                       'volume_24h':number(volume.get('24h')),'tvl':number(row.get('tvl')),
                       'fees_24h':number(fees.get('24h')),'bin_step':pc.get('bin_step'),
                       'base_fee_pct':pc.get('base_fee_pct'),'max_fee_pct':pc.get('max_fee_pct')})
    ranked.sort(key=lambda r:(r['volume_24h'],r['tvl']),reverse=True)
    if not ranked:
        raise Unavailable('official Data API returned no exact on-chain-enabled SOL/USDC ranking candidates')
    return {'basis':'OFFICIAL_DATA_API_24H_VOLUME_HINT_X_ONCHAIN_EXACT_MINT',
            'api_total':total,'api_rows_returned':len(values),'exact_onchain_matches':len(ranked),
            'ranking':ranked[:max(1,int(limit))]}


def capture_pool(rpc: SolanaRPC, pool: str, *, attempts: int = 3) -> dict:
    """Capture a complete current DLMM mechanics validation vector.

    BinArray accounts are obtained from one withContext getProgramAccounts call;
    core pool/Clock/mint/reserve/bitmap/reward accounts are obtained from one
    getMultipleAccounts call constrained with minContextSlot >= the BinArray
    context. If both contexts are equal the capture is an atomic current
    snapshot. If they differ, the exact frozen bytes are still valid for
    decoder/official-SDK mechanics parity, but are explicitly labelled a
    NON_ATOMIC_CURRENT_VALIDATION_VECTOR and must never seed historical replay.

    This avoids the documented 100-pubkey getMultipleAccounts limit without
    silently stitching chunks and pretending they are one historical state.
    """
    filters = [{'memcmp': {'offset': 0, 'bytes': b58encode(discriminator('account', 'BinArray'))}},
               {'memcmp': {'offset': 24, 'bytes': pool}}]
    zero='11111111111111111111111111111111'
    last_reason='unknown'
    for _ in range(attempts):
        pair_initial = decode_lb_pair(account_bytes(rpc.account(pool)['value']))

        arrays = rpc.program_accounts(filters)
        arrays_hash = rpc.http.last_raw_hash
        array_slot = int(arrays['context']['slot'])
        array_items = arrays['value']
        array_keys = sorted(x['pubkey'] for x in array_items)
        array_raw = {x['pubkey']: x['account'] for x in array_items}

        bitmap, _ = find_program_address([b'bitmap', b58decode(pool)], PROGRAM_ID)
        reward_keys = [reward[k] for reward in pair_initial['rewards'] for k in ('mint','vault')
                       if reward[k] != zero]
        core_keys = list(dict.fromkeys([
            pool, CLOCK_ID, pair_initial['token_x_mint'], pair_initial['token_y_mint'],
            pair_initial['reserve_x'], pair_initial['reserve_y'], bitmap, *reward_keys
        ]))
        if len(core_keys) > 100:
            raise Unsupported(f'core account set unexpectedly exceeds RPC bound: {len(core_keys)}')

        if hasattr(rpc,'multiple_accounts'):
            core = rpc.multiple_accounts(core_keys,min_context_slot=array_slot)
        else:  # offline synthetic test doubles
            core = rpc.call('getMultipleAccounts', [core_keys, {'commitment':'finalized','encoding':'base64',
                                                                 'minContextSlot':array_slot}])
        core_hash = rpc.http.last_raw_hash
        core_slot = int(core['context']['slot'])
        core_values = core['value']
        if any(v is None and k != bitmap for k,v in zip(core_keys,core_values)):
            last_reason='required core account absent'
            continue

        raw_map = dict(zip(core_keys,core_values))
        raw_map.update(array_raw)
        pair = decode_lb_pair(account_bytes(raw_map[pool]))
        if any(pair[k] != pair_initial[k] for k in ('token_x_mint','token_y_mint','reserve_x','reserve_y','rewards')):
            last_reason='pool core-address set changed during capture'
            continue

        bin_arrays = [decode_bin_array(account_bytes(array_raw[k])) for k in array_keys]
        clock = decode_clock(base64.b64decode(raw_map[CLOCK_ID]['data'][0]))
        if clock['slot'] != core_slot:
            raise ValidationError('Clock/core context slot mismatch')

        atomic = array_slot == core_slot
        context_span = core_slot-array_slot
        if context_span < 0:
            raise ValidationError('core context predates minContextSlot-constrained BinArray context')

        # The BinArray set is complete at array_slot. A mixed-slot vector is
        # explicitly NOT a factual state claim at core_slot.
        state = build_pool_state(pool,pair,bin_arrays,slot=core_slot,complete_bin_map=True)
        combined_hash=digest({'bin_arrays_raw_sha256':arrays_hash,'core_raw_sha256':core_hash,
                              'bin_array_context_slot':array_slot,'core_context_slot':core_slot})
        source_kind='CURRENT_RPC_ATOMIC' if atomic else 'CURRENT_RPC_NONATOMIC_VALIDATION_VECTOR'
        note=('Atomic current snapshot; still not a historical pre-transaction state' if atomic else
              'Non-atomic current validation vector. Exact frozen bytes may be used only for decoder/SDK mechanics parity; '
              'must not seed factual or historical replay.')
        return {'schema_version':2,'pool':pool,'slot':core_slot,'boundary':'CURRENT_VALIDATION_VECTOR',
                'clock':clock,'raw_accounts':raw_map,'raw_sha256':combined_hash,
                'timestamp_ms':clock['unix_timestamp']*1000,'raw_accounts_sha256':digest(raw_map),
                'source_kind':source_kind,'atomic_current_snapshot':atomic,
                'factual_current_snapshot':atomic,'historical_seed_allowed':False,
                'context_slots':{'bin_arrays':array_slot,'core':core_slot},
                'context_slot_span':context_span,
                'source_receipt':{'methods':['getProgramAccounts','getMultipleAccounts'],
                    'commitment':'finalized','bin_arrays_raw_sha256':arrays_hash,'core_raw_sha256':core_hash,
                    'bin_array_context_slot':array_slot,'core_context_slot':core_slot,
                    'get_multiple_accounts_count':len(core_keys),'bin_array_account_count':len(array_keys)},
                'bitmap_extension_address':bitmap,
                'absent_accounts':[k for k,v in raw_map.items() if v is None],
                'state':state.to_dict(),'decoded_pair':pair,'note':note}
    raise Unavailable('could not construct current mechanics validation vector: '+last_reason)


def program_fingerprint(rpc: SolanaRPC, program: str = PROGRAM_ID) -> dict:
    a = rpc.account(program)
    raw = base64.b64decode(a['value']['data'][0])
    if int.from_bytes(raw[:4], 'little') != 2 or len(raw) != 36:
        raise Unsupported('not upgradeable-loader Program layout')
    from .codec import b58encode
    address = b58encode(raw[4:36])
    b = rpc.account(address)
    programdata = base64.b64decode(b['value']['data'][0])
    if int.from_bytes(programdata[:4], 'little') != 3 or len(programdata) <= 45:
        raise Unsupported('not upgradeable-loader ProgramData layout')
    deployment_slot = int.from_bytes(programdata[4:12], 'little')
    binary = programdata[45:]
    return {'program_id': program, 'programdata_address': address,
            'observed_slot': b['context']['slot'], 'latest_deployment_slot': deployment_slot,
            'binary_sha256': sha256(binary), 'binary_base64': base64.b64encode(binary).decode(),
            'historical_prior_binaries_known': False, 'semantic_version': 'UNKNOWN',
            'raw_sha256': rpc.http.last_raw_hash}


def collect_blocks(rpc: SolanaRPC, start_slot: int, end_slot: int, output: Path,
                   *, max_blocks: int = 10000) -> dict:
    if start_slot < 0 or end_slot <= start_slot:
        raise ValidationError('invalid half-open slot range')
    slots = rpc.call('getBlocks', [start_slot, end_slot-1, {'commitment': 'finalized'}])
    slot_list_raw_hash = rpc.http.last_raw_hash
    if len(slots) > max_blocks:
        raise Unsupported('block collection exceeds explicit max_blocks; split the request')
    output.mkdir(parents=True, exist_ok=True)
    receipt={'method':'getBlocks','range':[start_slot,end_slot],'result':slots,
             'raw_sha256':slot_list_raw_hash,'commitment':'finalized'}
    write_json(output/'produced_slots_receipt.json',receipt,exclusive=True)
    rows, failed = [], []
    for slot in slots:
        path = output / f'{slot}.json'
        if path.exists():
            saved = read_json(path)
            if saved.get('slot') != slot:
                raise ValidationError('cached block slot mismatch')
        else:
            try:
                block = rpc.block(slot)
                saved = {'slot': slot, 'block': block, 'raw_sha256': rpc.http.last_raw_hash}
                write_json(path, saved, exclusive=True)
            except Unavailable as exc:
                failed.append({'slot': slot, 'reason': str(exc)})
                continue
        rows.append({'slot': slot, 'path': path.name, 'sha256': hash_file(path)})
    result = {'schema_version': 1, 'range': [start_slot, end_slot], 'produced_slots': slots,
              'blocks': rows, 'missing': failed, 'complete': not failed,
              'slot_list_receipt':{'path':'produced_slots_receipt.json','sha256':hash_file(output/'produced_slots_receipt.json')},
              'evidence_basis': 'RPC_GETBLOCKS_PLUS_FULL_FINALIZED_BLOCKS'}
    write_json(output / ('coverage_' + str(start_slot) + '_' + str(end_slot) + '.json'), result, exclusive=True)
    return result


def sync_sources(root: Path, *, timeout: float = 30) -> dict:
    """Pin first, download later. A source drift is FAIL, not a fallback to main."""
    lock = read_json(root / 'reference/source_lock.json')
    commit = lock['sdk_commit'] if 'sdk_commit' in lock else lock['commit']
    files = lock['files']
    destination = root / 'external/dlmm-sdk'
    result = []
    for rel, expected in files.items():
        expected = expected.get('git_blob_sha1', expected.get('sha')) if isinstance(expected, dict) else expected
        path = destination / rel
        if not path.exists():
            url = f'https://raw.githubusercontent.com/MeteoraAg/dlmm-sdk/{commit}/{rel}'
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'mdlmm-research/0.1'})
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    raw = response.read(16_000_001)
            except (OSError, urllib.error.URLError) as exc:
                raise Unavailable(f'pinned source download unavailable: {rel}') from exc
            if len(raw) > 16_000_000:
                raise Unsupported('source artifact exceeds bound')
            if git_blob_hash(raw) != expected:
                raise ValidationError(f'official Git blob mismatch: {rel}')
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as out:
                out.write(raw)
        raw = path.read_bytes()
        if git_blob_hash(raw) != expected:
            raise ValidationError(f'cached source drift: {rel}')
        result.append({'path': str(path.relative_to(root)), 'git_blob_sha1': expected, 'sha256': sha256(raw)})
    receipt = {'commit': commit, 'files': result, 'historical_binary_binding': 'UNKNOWN'}
    write_json(destination / 'source_receipt.json', receipt)
    return receipt
