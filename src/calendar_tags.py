"""Derive calendar scenes from configured dates; never rewrite raw records."""
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))

def calendar_tags(dt, cfg):
    if dt.tzinfo is not None:
        dt = dt.astimezone(TZ)
    date = dt.date()
    hol = cfg.get('holiday', {})
    days = sorted({datetime.fromisoformat(s).date() for s in hol.get('holiday_days', [])})
    blocks = []
    for d in days:
        if not blocks or d != blocks[-1][-1] + timedelta(days=1):
            blocks.append([d])
        else:
            blocks[-1].append(d)
    kind = 'normal_weekend' if date.weekday() >= 5 else 'normal_weekday'
    phase, holiday = 'none', None
    label = '普通周末' if kind == 'normal_weekend' else '普通工作日'
    if date.isoformat() in hol.get('makeup_workdays', []):
        kind, label = 'makeup_workday', '调休工作日'
    else:
        for block in blocks:
            name = hol.get('names', {}).get(block[0].isoformat(), '假期 ' + block[0].isoformat())
            if date in block:
                kind, holiday = 'holiday', name
                phase = 'day_1' if date == block[0] else ('return' if len(block) >= 3 and date >= block[-1] - timedelta(days=1) else 'mid')
                label = name + ' · ' + {'day_1':'首日','return':'后段','mid':'中段'}[phase]
                break
            if date == block[0] - timedelta(days=1):
                holiday, phase, label = name, 'eve', name + ' · 节前一天'
                break
        if phase == 'none' and any(a[-1] < date < b[0] and (b[0]-a[-1]).days <= 7 for a,b in zip(blocks,blocks[1:])):
            phase, label = 'between', '节间日期 · ' + label
    return {'calendar_type':kind, 'holiday_id':holiday, 'holiday_phase':phase,
            'calendar_version':hol.get('calendar_version',1), 'scene':label}
