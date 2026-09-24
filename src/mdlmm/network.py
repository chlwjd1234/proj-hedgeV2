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
        self.url = url or os.environ.get('SOLANA_RPC_URL') or 'https://api.mainnet-beta.solana.com'
        self.n = 0

    def call(self, method: str, params: list | None = None):
        if method not in READ_METHODS:
            raise ValidationError(f'RPC write/unapproved method prohibited: {method}')
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
                         'transactionDetails': 'full', 'rewards': False, 'maxSupportedTransactionVersion': 0}])

    def signatures(self, address: str, before: str | None = None, limit: int = 1000) -> list:
        opts = {'commitment': 'finalized', 'limit': min(limit, 1000)}
        if before:
            opts['before'] = before
        return self.call('getSignaturesForAddress', [address, opts])

    def account(self, address: str) -> dict:
        return self.call('getAccountInfo', [address, {'commitment': 'finalized', 'encoding': 'base64'}])

    def program_accounts(self, filters: list) -> dict:
        return self.call('getProgramAccounts', [PROGRAM_ID, {'commitment': 'finalized', 'encoding': 'base64',
                                                            'withContext': True, 'filters': filters}])


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


def capture_pool(rpc: SolanaRPC, pool: str, *, attempts: int = 3) -> dict:
    """All pool, bin, reserve, mint and Clock accounts in ONE coherent RPC response.

    Discovering >100 necessary accounts fails rather than stitching different
    slots together. A provider's archival snapshot export can lift that bound.
    """
    filters = [{'memcmp': {'offset': 0, 'bytes': b58encode(discriminator('account', 'BinArray'))}},
               {'memcmp': {'offset': 24, 'bytes': pool}}]
    for _ in range(attempts):
        pair_initial = decode_lb_pair(account_bytes(rpc.account(pool)['value']))
        arrays = rpc.program_accounts(filters)
        array_keys = sorted(x['pubkey'] for x in arrays['value'])
        bitmap, _ = find_program_address([b'bitmap', b58decode(pool)], PROGRAM_ID)
        reward_keys = [reward[k] for reward in pair_initial['rewards'] for k in ('mint','vault')
                       if reward[k] != '11111111111111111111111111111111']
        keys = list(dict.fromkeys([pool, CLOCK_ID, pair_initial['token_x_mint'], pair_initial['token_y_mint'],
                pair_initial['reserve_x'], pair_initial['reserve_y'], bitmap, *reward_keys, *array_keys]))
        if len(keys) > 100:
            raise Unsupported(f'{len(keys)} accounts require an atomic archival/provider snapshot, not mixed RPC slots')
        result = rpc.call('getMultipleAccounts', [keys, {'commitment': 'finalized', 'encoding': 'base64'}])
        capture_hash = rpc.http.last_raw_hash
        slot = result['context']['slot']
        values = result['value']
        if any(v is None and k != bitmap for k,v in zip(keys,values)):
            continue
        # Recheck list: no silent omission of concurrently initialized arrays.
        after = rpc.program_accounts(filters)
        if sorted(x['pubkey'] for x in after['value']) != array_keys:
            continue
        raw_map = dict(zip(keys, values))
        pair = decode_lb_pair(account_bytes(raw_map[pool]))
        if any(pair[k] != pair_initial[k] for k in ('token_x_mint','token_y_mint','reserve_x','reserve_y','rewards')):
            continue
        bin_arrays = [decode_bin_array(account_bytes(raw_map[k])) for k in array_keys]
        clock = decode_clock(base64.b64decode(raw_map[CLOCK_ID]['data'][0]))
        if clock['slot'] != slot:
            raise ValidationError('Clock/context slot mismatch')
        # This captures all LIVE arrays at the snapshot, not all historical arrays.
        state = build_pool_state(pool, pair, bin_arrays, slot=slot, complete_bin_map=True)
        return {'schema_version': 1, 'pool': pool, 'slot': slot, 'boundary': 'END_SLOT',
                'clock': clock, 'raw_accounts': raw_map, 'raw_sha256': capture_hash,
                'timestamp_ms': clock['unix_timestamp']*1000, 'raw_accounts_sha256':digest(raw_map),
                'source_kind':'CURRENT_RPC','source_receipt':{'method':'getMultipleAccounts','commitment':'finalized',
                    'raw_sha256':capture_hash,'context_slot':slot},
                'bitmap_extension_address': bitmap,
                'absent_accounts':[k for k,v in raw_map.items() if v is None],
                'state': state.to_dict(), 'decoded_pair': pair,
                'note': 'Current snapshot cannot seed transactions at or before this slot'}
    raise Unavailable('could not capture coherent pool account set')


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
