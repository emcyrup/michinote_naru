"""
実績記録票（2026-11 事業所「成」の要望）。
来所予定（ScheduledVisit）に、来た時刻・帰った時刻を持たせる。日ごとの一覧でまとめて入れ、子どもごとに 1 か月ぶんを見る。

- 時間は来所予定の actual_start / actual_end。入っていなければ、日誌の入室・退室時間を出す（二度入れなくてよいように）
- 時間を入れた日（きょうまで）は、状態が「予定」なら「来所」にする
- その日の日誌があれば、入室・退室時間も同じ値にそろえる（延長支援加算の計算に使うため）
"""
import calendar
import datetime

from .daylist import WEEK_JP, day_visits
from .models import ScheduledVisit


def parse_hm(value):
    """'16:00' を時刻に。空・読めないものは None"""
    value = (value or '').strip()
    if not value:
        return None
    try:
        return datetime.time.fromisoformat(value[:5])
    except ValueError:
        return None


def minutes_between(start, end):
    if not start or not end:
        return None
    m = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    return m if m > 0 else None


def fmt_minutes(m):
    """135 → '2:15'"""
    return '' if m is None else f'{m // 60}:{m % 60:02d}'


def _records(facility, child_ids, days):
    """日誌の入室・退室時間 {(利用者, 日): 日誌}"""
    from records.models import DailyRecord
    rows = DailyRecord.objects.filter(facility=facility, beneficiary_id__in=child_ids, date__in=days) \
        .only('id', 'beneficiary_id', 'date', 'entry_time', 'exit_time')
    return {(r.beneficiary_id, r.date): r for r in rows}


def _row(visit, record):
    start, end, from_journal = visit.actual_start, visit.actual_end, False
    if not start and not end and record is not None and (record.entry_time or record.exit_time):
        start, end, from_journal = record.entry_time, record.exit_time, True
    m = minutes_between(start, end)
    return {
        'visit': visit, 'beneficiary': visit.beneficiary, 'start': start, 'end': end,
        'from_journal': from_journal, 'minutes': m, 'length': fmt_minutes(m),
        'done': bool(start and end), 'partial': bool(start or end) and not (start and end),
        'absent': visit.status == ScheduledVisit.STATUS_ABSENT, 'warn': False,
    }


def day_rows(facility, day):
    """日ごとの一覧：（来る子, 欠席の子）。連絡帳を送ったのに時間が入っていない子には warn"""
    coming, absent = day_visits(facility, day)
    ids = [v.beneficiary_id for v in coming]
    recs = _records(facility, ids, [day])
    sent = set()
    if facility.use_family_page:
        from family.models import ContactBook
        sent = set(ContactBook.objects.filter(facility=facility, date=day, beneficiary_id__in=ids,
                                              status=ContactBook.STATUS_SENT).values_list('beneficiary_id', flat=True))
    rows = []
    for v in coming:
        r = _row(v, recs.get((v.beneficiary_id, day)))
        r['book_sent'] = v.beneficiary_id in sent
        r['warn'] = r['book_sent'] and not r['done']
        rows.append(r)
    return rows, [_row(v, None) for v in absent]


def set_times(facility, visits, start, end):
    """まとめて時間を入れる（start・end とも None なら消す）。入れた件数を返す"""
    from records.models import DailyRecord
    today = datetime.date.today()
    n = 0
    for v in visits:
        if v.facility_id != facility.pk or v.status == ScheduledVisit.STATUS_ABSENT:
            continue
        v.actual_start, v.actual_end = start, end
        fields = ['actual_start', 'actual_end', 'updated_at']
        if (start or end) and v.status == ScheduledVisit.STATUS_SCHEDULED and v.date <= today:
            v.status = ScheduledVisit.STATUS_ATTENDED
            fields.append('status')
        v.save(update_fields=fields)
        DailyRecord.objects.filter(facility=facility, beneficiary_id=v.beneficiary_id, date=v.date) \
            .update(entry_time=start, exit_time=end)
        n += 1
    return n


def check_times(start, end, raw_start='', raw_end=''):
    """入力の確かめ。問題があれば理由の文、無ければ ''"""
    if (raw_start or '').strip() and start is None:
        return '始まりの時間が読めません。'
    if (raw_end or '').strip() and end is None:
        return '終わりの時間が読めません。'
    if end and not start:
        return '始まりの時間を入れてください。'
    if start and end and end <= start:
        return '終わりの時間は、始まりより後にしてください。'
    return ''


def month_sheet(facility, child, year, month):
    """子ども 1 人の 1 か月ぶん：来所予定のある日を並べる"""
    first = datetime.date(year, month, 1)
    last = datetime.date(year, month, calendar.monthrange(year, month)[1])
    visits = list(ScheduledVisit.objects.filter(facility=facility, beneficiary=child, date__range=(first, last))
                  .select_related('beneficiary').order_by('date'))
    recs = _records(facility, [child.pk], [v.date for v in visits])
    today = datetime.date.today()
    rows = []
    for v in visits:
        r = _row(v, recs.get((child.pk, v.date)))
        r['weekday'] = WEEK_JP[v.date.weekday()]
        r['missing'] = not r['absent'] and not r['done'] and v.date <= today
        rows.append(r)
    used = [r for r in rows if not r['absent'] and (r['done'] or r['partial'])]
    total = sum(r['minutes'] or 0 for r in rows if not r['absent'])
    prev_m = first - datetime.timedelta(days=1)
    next_m = last + datetime.timedelta(days=1)
    return {
        'rows': rows, 'first': first, 'last': last,
        'days_used': len(used), 'days_absent': sum(1 for r in rows if r['absent']),
        'days_missing': sum(1 for r in rows if r['missing']), 'total': fmt_minutes(total) if total else '',
        'prev_month': prev_m, 'next_month': next_m,
    }
