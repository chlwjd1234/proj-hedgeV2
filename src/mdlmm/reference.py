"""Indexed reference prices. Causal decisions and ex-post markouts are different APIs."""
from __future__ import annotations
import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from .errors import Unavailable, ValidationError
from .util import dec, D, hash_file, read_jsonl


@dataclass(frozen=True)
class PricePoint:
    ts_ms: int
    available_ms: int
    price: D
    source: str


class Reference:
    def __init__(self, database: Path | str = ':memory:'):
        self.db = sqlite3.connect(str(database))
        self.db.execute('CREATE TABLE IF NOT EXISTS prices (ts INTEGER PRIMARY KEY, available INTEGER NOT NULL, price TEXT NOT NULL, source TEXT NOT NULL)')
        self.db.execute('CREATE INDEX IF NOT EXISTS available_time ON prices(available,ts)')
        self.db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)')

    def close(self):
        self.db.close()

    def load(self, path: Path, *, allowed_symbol='SOL/USDC') -> dict:
        h = hash_file(path)
        old = self.db.execute('SELECT value FROM metadata WHERE key="input_hash"').fetchone()
        if old:
            if old[0] != h:
                raise ValidationError('reference database belongs to different input bytes')
            return {'sha256':h,'rows':self.count()}
        if self.count():
            raise ValidationError('unbound reference DB is not empty')
        if path.suffix == '.csv':
            with path.open(encoding='utf8',newline='') as stream:
                self.insert(csv.DictReader(stream), allowed_symbol=allowed_symbol)
        else:
            self.insert(read_jsonl(path), allowed_symbol=allowed_symbol)
        if not self.count():
            raise Unavailable('reference source has zero observations')
        self.db.execute('INSERT INTO metadata VALUES (?,?)',('input_hash',h))
        self.db.commit()
        return {'sha256':h,'rows':self.count()}

    def insert(self, rows, *, allowed_symbol='SOL/USDC'):
        with self.db:
            for row in rows:
                if row.get('symbol') != allowed_symbol:
                    raise ValidationError('reference symbol/numeraire must explicitly be SOL/USDC')
                t,a = int(row['ts_ms']),int(row['available_ms'])
                p = dec(row['price'])
                source = row.get('source')
                if p <= 0 or a < t or not source:
                    raise ValidationError('invalid reference price/time/provenance')
                try:
                    self.db.execute('INSERT INTO prices VALUES (?,?,?,?)',(t,a,str(p),source))
                except sqlite3.IntegrityError as exc:
                    raise ValidationError('duplicate reference timestamp; reconcile sources before import') from exc

    def count(self):
        return self.db.execute('SELECT COUNT(*) FROM prices').fetchone()[0]

    @staticmethod
    def point(row):
        return PricePoint(int(row[0]),int(row[1]),dec(row[2]),row[3])

    def asof(self, decision_ms: int, *, max_age_ms: int) -> PricePoint:
        row = self.db.execute('SELECT * FROM prices WHERE ts<=? AND available<=? ORDER BY ts DESC LIMIT 1',
                              (decision_ms,decision_ms)).fetchone()
        if row is None or decision_ms-row[0] > max_age_ms:
            raise Unavailable(f'causal reference missing/stale at {decision_ms}')
        return self.point(row)

    def markout_at(self, event_ms: int, horizon_ms: int, *, max_delay_ms: int) -> PricePoint:
        """First market observation AT/AFTER future horizon, ex-post diagnostic.

        This method cannot be used by a decision policy. Actual delay/horizon is
        emitted, and absence stays UNAVAILABLE instead of carrying last price.
        """
        target = event_ms+horizon_ms
        row = self.db.execute('SELECT * FROM prices WHERE ts>=? ORDER BY ts LIMIT 1',(target,)).fetchone()
        if row is None or row[0]-target > max_delay_ms:
            raise Unavailable(f'markout reference missing at {target}')
        return self.point(row)

    def rows(self, start_ms: int | None = None, end_ms: int | None = None):
        clauses, args = [],[]
        if start_ms is not None:
            clauses.append('ts>=?');args.append(start_ms)
        if end_ms is not None:
            clauses.append('ts<?');args.append(end_ms)
        query='SELECT * FROM prices'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY ts'
        for r in self.db.execute(query,args):
            yield self.point(r)


def markouts(ref: Reference, ts_ms: int, principal_sol_delta, principal_usdc_delta,
             fee_usdc, *, horizons_ms=(0,1000,10000,60000,300000),
             immediate_max_age_ms=1000, max_delay_ms=1000) -> list[dict]:
    dx,dy,fee=map(dec,(principal_sol_delta,principal_usdc_delta,fee_usdc))
    if dx == 0:
        return []
    trade_price=-dy/dx
    output=[]
    for horizon in horizons_ms:
        row={'horizon_ms':horizon,'event_ms':ts_ms,'lp_sol_delta':dx,
             'trade_price':trade_price,'lp_fee_usdc_at_event':fee}
        try:
            p=ref.asof(ts_ms,max_age_ms=immediate_max_age_ms) if horizon==0 else ref.markout_at(ts_ms,horizon,max_delay_ms=max_delay_ms)
            # Positive adverse selection means LP buys above / sells below future fair value.
            adverse=dx*(trade_price-p.price)
            row.update(status='PASS',reference_price=p.price,reference_ms=p.ts_ms,
                       actual_horizon_ms=p.ts_ms-ts_ms,reference_source=p.source,
                       directional_adverse_markout=adverse,fee_minus_markout=fee-adverse,
                       ex_post_diagnostic=horizon>0)
        except Unavailable as exc:
            row.update(status='UNAVAILABLE',reason=str(exc))
        output.append(row)
    return output
