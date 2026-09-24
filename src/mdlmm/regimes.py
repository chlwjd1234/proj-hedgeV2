"""External-price-only diagnostic strata; not an investable timing signal."""
from __future__ import annotations
import math
import statistics
from dataclasses import dataclass, asdict
from .errors import Unavailable, ValidationError
from .reference import Reference
from .util import iso_ms, utc_iso

DAY = 86_400_000


@dataclass(frozen=True)
class Window:
    label: str
    start_ms: int
    end_ms: int
    selection: str
    features: dict

    def __post_init__(self):
        if self.end_ms-self.start_ms != 7*DAY:
            raise ValidationError('pilot windows must be exactly 7 days, end exclusive')
        if self.start_ms % DAY or self.end_ms % DAY:
            raise ValidationError('pilot windows use UTC completed-day boundaries')


def completed_daily_closes(ref: Reference, *, max_age_ms: int = 120000) -> dict[int,float]:
    limits = ref.db.execute('SELECT MIN(ts),MAX(ts) FROM prices').fetchone()
    if limits[0] is None:
        raise Unavailable('no external SOL price observations')
    first_day, last_day = limits[0]//DAY, limits[1]//DAY
    result = {}
    for day in range(first_day,last_day+1):
        try:
            point = ref.asof((day+1)*DAY-1,max_age_ms=max_age_ms)
            result[day] = float(point.price)
        except Unavailable:
            continue
    return result


def rv30_width(ref: Reference, start_ms: int, multiplier: float, *, max_age_ms: int = 120000) -> float:
    if multiplier not in (1,2,1.0,2.0):
        raise ValidationError('pilot width rules are prespecified {1,2} daily-RV30 multiples')
    day = start_ms//DAY
    values = []
    for d in range(day-31,day):
        point = ref.asof((d+1)*DAY-1,max_age_ms=max_age_ms)
        # asof includes availability, so no future quote enters RV30.
        values.append(float(point.price))
    returns = [math.log(b/a) for a,b in zip(values,values[1:])]
    rms = math.sqrt(sum(x*x for x in returns)/30)
    width = math.expm1(multiplier*rms)
    if not 0 < width < 0.95:
        raise Unavailable('zero/extreme trailing RV width; no tuned fallback')
    return width


def explicit_windows(specs: list[dict]) -> list[Window]:
    windows = [Window(r['label'],iso_ms(r['start']),iso_ms(r['end']),'EXPLICIT_EXTERNAL_RESEARCH_OVERRIDE',{}) for r in specs]
    if len(windows) != 4 or len({w.label for w in windows}) != 4:
        raise ValidationError('exactly four uniquely labeled windows required')
    validate_nonoverlap(windows)
    return windows


def validate_nonoverlap(windows: list[Window]):
    ordered = sorted(windows,key=lambda w:w.start_ms)
    for a,b in zip(ordered,ordered[1:]):
        if a.end_ms > b.start_ms:
            raise ValidationError('pilot windows overlap')


def select_windows(ref: Reference, *, eligible_start_ms: int | None = None,
                   eligible_end_ms: int | None = None, max_daily_age_ms: int = 120000) -> list[Window]:
    close = completed_daily_closes(ref,max_age_ms=max_daily_age_ms)
    candidates = []
    for start in sorted(close):
        end = start+7
        if eligible_start_ms is not None and start*DAY < eligible_start_ms:
            continue
        if eligible_end_ms is not None and end*DAY > eligible_end_ms:
            continue
        days = range(start-1,end)
        if any(d not in close for d in days):
            continue
        prices=[close[d] for d in days]
        r=[math.log(b/a) for a,b in zip(prices,prices[1:])]
        total=sum(r)
        variation=math.sqrt(sum(x*x for x in r))
        path_variation=sum(abs(x) for x in r)
        features={'log_return':total,'realized_variation':variation,'path_variation':path_variation,
                  'roundtrip_variation':max(0,path_variation-abs(total)),
                  'price_high_low':max(prices)/min(prices)-1}
        candidates.append((start*DAY,end*DAY,features))
    scoring={
        'low_vol_sideways': lambda f: -f['realized_variation']-2*abs(f['log_return']),
        'uptrend':lambda f:f['log_return'],
        'downtrend_stress':lambda f:-f['log_return'],
        'high_vol_roundtrip':lambda f:f['roundtrip_variation']-abs(f['log_return']),
    }
    # Labels must describe the observed external regime, not merely its rank.
    # In a monotone history there may be no downtrend/roundtrip: do not invent one.
    qualifies={
        'low_vol_sideways':lambda f:True,
        'uptrend':lambda f:f['log_return']>0,
        'downtrend_stress':lambda f:f['log_return']<0,
        'high_vol_roundtrip':lambda f:f['path_variation']>0 and abs(f['log_return'])<=0.5*f['path_variation'],
    }
    selected=[]
    for label,score in scoring.items():
        eligible=[c for c in candidates if qualifies[label](c[2])
                  and all(c[1]<=w.start_ms or c[0]>=w.end_ms for w in selected)]
        if not eligible:
            raise Unavailable('not enough non-overlapping complete external-price weeks satisfying regime labels')
        a,b,f=max(eligible,key=lambda c:(score(c[2]),-c[0]))
        selected.append(Window(label,a,b,'EX_POST_EXTERNAL_ONLY_DIAGNOSTIC_STRATIFICATION',f))
    validate_nonoverlap(selected)
    return selected


def describe(windows):
    return [{**asdict(w),'start':utc_iso(w.start_ms),'end':utc_iso(w.end_ms),
             'not_untouched_oos':True,'lp_pnl_used_for_selection':False} for w in windows]
