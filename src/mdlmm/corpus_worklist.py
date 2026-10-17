"""Read-only D1 canonical index audit and block-aware acquisition worklist.

This is a CANONICAL-INDEX COVERAGE result, never a full block or historical
account completeness certificate. The old full-gTFA pool-touch corpus and the
selected D1 periods/pools remain immutable and are not reconstructed.

Incremental JSON entry parsing avoids materializing a ~160 MiB JSON index into
multiple gigabytes of Python objects. The output is bounded, deduplicated by
(block slot, pool), and contains no RPC keys or account bytes.
"""
from __future__ import annotations

import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile

from .errors import Unavailable, ValidationError
from .model import PROGRAM_ID
from .normalize import (
    _invocations, _typed_instruction_pool, SWAP_DISCRIMINATORS,
    EVENT_CPI_TAG, account_keys,
)
from .codec import IdlCodec

FROZEN_INDEX_SHA = 'a9a1f4387eeb6d1da8558732194bd7f64d7747303d9702716551efbae22b221b'
FROZEN_SUMMARY_SHA = 'be37c67446548ba71085f3d6f1857d3adc8f4b0e7402eaa8f88b276d3cc72d7b'
FROZEN_ACQUISITION_SHA = '10f1dbc1d2610721325afdcc693bcb467dd8f3e9c092d7cf0a2424f432b7e5bf'
FROZEN_PLAN_SHA = 'b86fdd66d7bd75cb1c957151aa728ba35ac2b89ed1e6641344f5f6005a55f33a'
POOLS = frozenset((
    'BVRbyLjjfSBcoyiYFuxbgKYnWuiFaF9CSXEa5vdSZ9Hh',
    '2L47gbUzGsnJZqRzfny4b3VWvKBnMRTMeNH3uzFrj6ko',
))
LABELS = frozenset(('downtrend_stress', 'low_vol_sideways', 'uptrend', 'high_vol_roundtrip'))
VARIANTS = frozenset(('swap', 'swap2', 'swap_exact_out', 'swap_exact_out2'))


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def check_sha(path: Path, expected: str):
    if not path.is_file() or sha_file(path) != expected:
        raise ValidationError(f'frozen D1 file unavailable or SHA256 drift: {path}')


def iter_json_array(path: Path, key: str = 'entries'):
    """Bounded-memory parser for one named JSON array of objects."""
    decoder = json.JSONDecoder()
    match_re = re.compile(r'"' + re.escape(key) + r'"\s*:\s*\[')
    with path.open('r', encoding='utf-8') as f:
        buffer = ''
        while True:
            chunk = f.read(65536)
            if not chunk:
                raise ValidationError(f'JSON index has no {key!r} array')
            buffer += chunk
            match = match_re.search(buffer)
            if match:
                buffer = buffer[match.end():]
                break
            if len(buffer) > 4 * 1024 * 1024:
                raise ValidationError('canonical JSON prefix exceeds bounded parser limit')
        while True:
            buffer = buffer.lstrip()
            if not buffer:
                chunk = f.read(65536)
                if not chunk:
                    raise ValidationError('truncated JSON canonical entry list')
                buffer += chunk
                continue
            if buffer.startswith(']'):
                return
            if buffer.startswith(','):
                buffer = buffer[1:]
                continue
            try:
                row, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError as exc:
                chunk = f.read(65536)
                if not chunk:
                    raise ValidationError('truncated or invalid canonical JSON entry') from exc
                buffer += chunk
                if len(buffer) > 8 * 1024 * 1024:
                    raise ValidationError('single canonical entry exceeds 8 MiB bound')
                continue
            if not isinstance(row, dict):
                raise ValidationError('non-object canonical swap entry')
            yield row
            buffer = buffer[end:]


def scoped_dlmm_instructions(row: dict, pool: str, idl: IdlCodec):
    """Conservative mutation discovery, not proof of complete block effects."""
    tx, meta = row['transaction'], row['meta']
    if meta is None or meta.get('err') is not None:
        return []
    keys = account_keys(tx, meta)
    if pool not in keys:
        raise ValidationError('gTFA returned a row that does not touch the selected pool')
    inner = {entry['index']: entry['instructions'] for entry in meta.get('innerInstructions') or []}
    found = []
    for outer, top in enumerate(tx['message']['instructions']):
        nodes = _invocations(top, inner.get(outer, []), keys, outer)
        for node in nodes:
            if node.get('program') != PROGRAM_ID:
                continue
            if node.get('decode_error') or node.get('resolved') is None:
                found.append({'owner': 'UNKNOWN', 'name': 'DECODE_ERROR'})
                continue
            raw = node['resolved']['raw']
            if raw.startswith(EVENT_CPI_TAG):
                continue
            name = SWAP_DISCRIMINATORS.get(raw[:8])
            if name:
                # Swaps always begin with lb_pair in the pinned schema.
                accounts = node['resolved']['accounts']
                owner = accounts[0] if accounts else None
            else:
                try:
                    decoded = idl.decode_named('instructions', raw)
                    name = decoded['name']
                    node['decoded_mutation'] = decoded
                except Exception:
                    name = 'UNDECODED_' + raw[:8].hex()
                owner = _typed_instruction_pool(node, idl)
            if owner == pool:
                found.append({'owner': 'TARGET', 'name': name,
                              'outer': outer, 'inner': node['inner_index']})
            elif owner in POOLS or owner is not None:
                # A different pool may share a router transaction. If it also
                # explicitly lists the target account, its effects require
                # manual attribution and must not be presumed harmless.
                if pool in node['resolved']['accounts']:
                    found.append({'owner': 'AMBIGUOUS_SHARED_ACCOUNT', 'name': name})
            elif pool in node['resolved']['accounts']:
                found.append({'owner': 'UNKNOWN', 'name': name})
    return found


def scan_pages(root: Path, conn: sqlite3.Connection, *, idl: IdlCodec,
               max_pages_per_segment: int | None) -> dict:
    # Group the eight PnL-independent original gTFA segments. No new network
    # calls or replacement pool selection is performed.
    stats = {}
    scan_complete = True
    for label in sorted(LABELS):
        for pool in sorted(POOLS):
            key = label + ':' + pool
            folder = root / 'pages' / label / pool
            pages = sorted(folder.glob('page_*.json'))
            if not pages:
                raise Unavailable(f'persisted full-gTFA pages missing: {folder}')
            selected = pages if max_pages_per_segment is None else pages[:max_pages_per_segment]
            if len(selected) != len(pages):
                scan_complete = False
            counts = Counter()
            mutation_counts = Counter()
            first_slot = last_slot = None
            for i, path in enumerate(selected):
                if path.name != f'page_{i:06d}.json':
                    raise ValidationError(f'gap in gTFA pagination: {path}')
                page = json.loads(path.read_text())
                if page.get('pool') != pool or page.get('label') != label or not isinstance(page.get('rows'), list):
                    raise ValidationError(f'page schema/pool/label mismatch: {path}')
                counts['pages'] += 1
                counts['rows'] += len(page['rows'])
                for row in page['rows']:
                    slot = int(row['slot'])
                    first_slot = slot if first_slot is None else min(first_slot, slot)
                    last_slot = slot if last_slot is None else max(last_slot, slot)
                    tx = row['transaction']
                    signature = tx['signatures'][0]
                    conn.execute('''INSERT INTO scanned_transactions
                                    (label,pool,signature,slot) VALUES(?,?,?,?)''',
                                 (label, pool, signature, slot))
                    discovered = scoped_dlmm_instructions(row, pool, idl)
                    targets = [n for n in discovered if n['owner']=='TARGET']
                    ambiguous = [n for n in discovered if n['owner'] in ('UNKNOWN','AMBIGUOUS_SHARED_ACCOUNT')]
                    counts['target_instructions'] += len(targets)
                    counts['ambiguous_instruction_rows'] += bool(ambiguous)
                    counts['touch_without_identified_target'] += not bool(targets or ambiguous)
                    for item in targets:
                        mutation_counts[item['name']] += 1
                        found = conn.execute('''SELECT count(*) FROM canonical WHERE label=? AND pool=?
                                              AND signature=? AND outer_index=? AND inner_index=?''',
                                             (label,pool,signature,item['outer'],item['inner'])).fetchone()[0]
                        if item['name'] in VARIANTS:
                            if not found:
                                counts['swap_instruction_missing_from_frozen_index'] += 1
                        elif item['name'].startswith('UNDECODED_'):
                            counts['unclassified_mutations'] += 1
                        else:
                            counts['known_non_swap_mutations'] += 1
                    for item in ambiguous:
                        mutation_counts['UNKNOWN_SCOPE:' + item['name']] += 1
            stats[key] = {'pages_total': len(pages), 'pages_scanned': counts['pages'],
                          'rows_scanned': counts['rows'], 'first_slot': first_slot, 'last_slot': last_slot,
                          'metrics': dict(counts), 'target_instruction_names': dict(mutation_counts)}
            print(f'PAGE_SCAN {label} {pool[:8]} pages={counts["pages"]}/{len(pages)} '
                  f'rows={counts["rows"]} mutations={counts["known_non_swap_mutations"]} '
                  f'unknown_scope={counts["ambiguous_instruction_rows"]}', flush=True)
    return {'complete_page_scan': scan_complete, 'segments': stats,
            'full_block_mutation_coverage': 'NOT_PROVEN',
            'historical_prestate': 'UNAVAILABLE'}


def run(index: Path, summary: Path, acquisition: Path, plan: Path, output: Path,
        *, pages_root: Path | None = None, idl_path: Path | None = None,
        max_pages_per_segment: int | None = None) -> dict:
    for name, path, hash_ in (('index',index,FROZEN_INDEX_SHA),('summary',summary,FROZEN_SUMMARY_SHA),
                             ('acquisition',acquisition,FROZEN_ACQUISITION_SHA),('plan',plan,FROZEN_PLAN_SHA)):
        check_sha(path, hash_)
    summary_obj = json.loads(summary.read_text())
    if summary_obj.get('economic_swap_count') != 127743:
        raise ValidationError('frozen canonical economic swap total drift')
    if summary_obj.get('global_index', {}).get('sha256') != FROZEN_INDEX_SHA:
        raise ValidationError('canonical summary/index binding mismatch')
    counts = Counter()
    variants = Counter()
    segment = Counter()
    with tempfile.TemporaryDirectory(prefix='mdlmm_d1_worklist_') as temp:
        conn = sqlite3.connect(str(Path(temp) / 'canonical.sqlite'))
        conn.execute('''CREATE TABLE canonical(label TEXT,pool TEXT,signature TEXT,
                        outer_index INTEGER,inner_index INTEGER,slot INTEGER,txn_index INTEGER,
                        PRIMARY KEY(label,pool,signature,outer_index,inner_index))''')
        for row in iter_json_array(index):
            label, pool = row['label'], row['pool']
            if label not in LABELS or pool not in POOLS:
                raise ValidationError('canonical index contains an unexpected frozen window/pool')
            signature = row['signature']
            outer, inner = int(row['outer_index']), int(row['swap_instruction_inner_index'])
            if row['instruction_variant'] not in VARIANTS or outer < 0 or inner < -1:
                raise ValidationError('unrecognized canonical swap instruction variant/location')
            conn.execute('''INSERT INTO canonical VALUES(?,?,?,?,?,?,?)''',
                         (label,pool,signature,outer,inner,int(row['slot']),int(row['transaction_index'])))
            counts['economic_swaps'] += 1
            variants[row['instruction_variant']] += 1
            segment[label + ':' + pool] += 1
        if counts['economic_swaps'] != 127743:
            raise ValidationError('canonical index length differs from its immutable summary')
        if variants != Counter(summary_obj['instruction_variant_counts']):
            raise ValidationError('canonical instruction variants differ from frozen summary')
        unique_signatures = conn.execute('SELECT count(DISTINCT signature) FROM canonical').fetchone()[0]
        unique_slots = conn.execute('SELECT count(DISTINCT slot) FROM canonical').fetchone()[0]
        if unique_signatures != summary_obj['unique_transaction_signatures'] or \
                unique_slots != summary_obj['unique_slots']:
            raise ValidationError('canonical signature/slot counts differ from frozen summary')
        output.mkdir(parents=True,exist_ok=True)
        worklist = output / 'deduplicated_block_worklist.jsonl.gz'
        if worklist.exists():
            raise ValidationError('immutable acquisition worklist exists; use a new output directory')
        # One (pool,slot) line, sorted chronologically, with every swap in that
        # slot. The dedupe key is NOT the signature or the instruction.
        with worklist.open('xb') as binary:
            with gzip.GzipFile(fileobj=binary,mode='wb',mtime=0,filename='') as zipped:
                for slot,pool,n in conn.execute('''SELECT slot,pool, count(*) FROM canonical
                                                    GROUP BY slot,pool ORDER BY slot,pool'''):
                    zipped.write((json.dumps({'slot':slot,'pool':pool,'canonical_swaps':n},
                                             sort_keys=True,separators=(',',':'))+'\n').encode())
        worklist_sha = sha_file(worklist)
        detail = None
        if pages_root is not None:
            if idl_path is None or not idl_path.exists():
                raise Unavailable('full-gTFA scope scan requires the exact pinned IDL')
            idl = IdlCodec.load(idl_path)
            conn.execute('''CREATE TABLE scanned_transactions(label TEXT,pool TEXT,
                            signature TEXT,slot INTEGER,PRIMARY KEY(label,pool,signature))''')
            detail = scan_pages(pages_root,conn,idl=idl,
                                max_pages_per_segment=max_pages_per_segment)
            conn.commit()
            if detail['complete_page_scan']:
                total_rows = sum(s['rows_scanned'] for s in detail['segments'].values())
                expected_rows = json.loads(acquisition.read_text())['total_pool_touch_tx_rows']
                if total_rows != expected_rows:
                    raise ValidationError(f'full-gTFA row count mismatch: {total_rows} vs {expected_rows}')
                detail['all_pool_touch_rows'] = total_rows
                missing = conn.execute('''SELECT count(*) FROM canonical c WHERE NOT EXISTS
                    (SELECT 1 FROM scanned_transactions t WHERE c.label=t.label AND c.pool=t.pool
                      AND c.signature=t.signature AND c.slot=t.slot)''').fetchone()[0]
                detail['canonical_transactions_missing_from_pool_touch_pages'] = missing
                if missing:
                    raise ValidationError('frozen canonical swap signature absent from gTFA corpus')
        report = {'schema':'D1_CANONICAL_BLOCK_WORKLIST_V1',
                  'scope':'CANONICAL_INDEX_AND_POOL_TOUCH_PREPARATION_ONLY',
                  'historical_gate':'UNAVAILABLE',
                  'full_block_state_mutation_coverage':'NOT_PROVEN',
                  'historical_account_prestates':'NOT_ACQUIRED',
                  'frozen_index_sha256':FROZEN_INDEX_SHA,
                  'economic_swaps':counts['economic_swaps'],
                  'unique_swap_slots':unique_slots,
                  'unique_transaction_signatures':unique_signatures,
                  'instruction_variants':dict(variants),
                  'segments':dict(segment),
                  'worklist':str(worklist),'worklist_sha256':worklist_sha,
                  'gTFA_scan':detail}
        path = output / 'summary.json'
        if path.exists():
            raise ValidationError('immutable corpus summary exists; use new run directory')
        path.write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
        print('CANONICAL_INDEX_GATE=PASS')
        print('ECONOMIC_SWAPS=',report['economic_swaps'])
        print('UNIQUE_SWAP_SLOTS=',report['unique_swap_slots'])
        print('WORKLIST_SHA256=',worklist_sha)
        print('FULL_BLOCK_MUTATION_COVERAGE=NOT_PROVEN')
        print('D1_HISTORICAL_GATE=UNAVAILABLE')
        return report


def main(argv=None) -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('.'))
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--scan-pages',action='store_true')
    p.add_argument('--max-pages-per-segment',type=int)
    args=p.parse_args(argv)
    if args.max_pages_per_segment is not None and args.max_pages_per_segment < 1:
        p.error('--max-pages-per-segment must be >=1')
    root=args.root.resolve()
    base=root/'data/pilot/d1_full_gtfa_v1'
    run(base/'canonical_swap_index_v2.json',base/'canonical_swap_summary_v2.json',
        base/'acquisition_summary.json',root/'data/pilot/historical_plan_d1_selected.json',
        (root/args.output).resolve(),pages_root=base if args.scan_pages else None,
        idl_path=root/'external/dlmm-sdk/ts-client/src/dlmm/idl/idl.json',
        max_pages_per_segment=args.max_pages_per_segment)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
