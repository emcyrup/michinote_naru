"""勤務表（シフト）の処理：月の表・曜日の基本から入れる・保存"""
import calendar
import datetime

from config.jp_holidays import holiday_name

from .models import WEEK_JP, ShiftDay, ShiftMember, ShiftPattern, ShiftWeekly

# はじめに入れられる「よく使う勤務」：(名前, 印, 始まり, 終わり, 休憩, 休みか, 色)
DEFAULT_PATTERNS = [
    ('早番', '早', '08:30', '17:30', 60, False, '#dbe9f6'),
    ('日勤', '日', '09:30', '18:30', 60, False, '#dff0d8'),
    ('遅番', '遅', '10:30', '19:30', 60, False, '#fde9c9'),
    ('午前', '前', '09:00', '13:00', 0, False, '#e6e0f3'),
    ('午後', '後', '13:00', '18:00', 0, False, '#f3ddea'),
    ('休み', '休', '', '', 0, True, '#ececec'),
    ('有休', '有', '', '', 0, True, '#fff7c2'),
]


def parse_ym(value):
    try:
        y, m = str(value or '').split('-')[:2]
        y, m = int(y), int(m)
        if 1 <= m <= 12 and 2000 <= y <= 2100:
            return y, m
    except ValueError:
        pass
    today = datetime.date.today()
    return today.year, today.month


def parse_hm(value):
    value = (value or '').strip()
    if not value:
        return None
    try:
        return datetime.time.fromisoformat(value[:5])
    except ValueError:
        return None


def fmt_minutes(m):
    return f'{m // 60}:{m % 60:02d}' if m else ''


def month_days(year, month):
    today = datetime.date.today()
    out = []
    for d in range(1, calendar.monthrange(year, month)[1] + 1):
        day = datetime.date(year, month, d)
        hol = holiday_name(day) or ''
        out.append({'date': day, 'day': d, 'iso': day.isoformat(), 'weekday': WEEK_JP[day.weekday()],
                    'is_sun': day.weekday() == 6 or bool(hol), 'is_sat': day.weekday() == 5 and not hol,
                    'holiday': hol, 'is_today': day == today, 'closed': False})
    return out


def closed_days(facility, first, last):
    """予定のカレンダーで「休業日」にした日（あれば）"""
    try:
        from family.models import DayEvent
        return set(DayEvent.objects.filter(facility=facility, date__range=(first, last), is_closed=True).values_list('date', flat=True))
    except Exception:  # noqa: BLE001
        return set()


def add_defaults(facility):
    """勤務の種類が 1 つも無いとき、よく使うものを入れる。入れた数を返す"""
    if ShiftPattern.objects.filter(facility=facility).exists():
        return 0
    for i, (name, mark, s, e, brk, off, color) in enumerate(DEFAULT_PATTERNS):
        ShiftPattern.objects.create(facility=facility, name=name, mark=mark, start_time=parse_hm(s), end_time=parse_hm(e),
                                    break_minutes=brk, is_off=off, color=color, order=i)
    return len(DEFAULT_PATTERNS)


def import_staff(facility):
    """ログインする職員のうち、まだ勤務表に載せていない人を足す。足した数を返す"""
    from accounts.models import StaffAccount
    have = set(ShiftMember.objects.filter(facility=facility, staff__isnull=False).values_list('staff_id', flat=True))
    last = ShiftMember.objects.filter(facility=facility).count()
    n = 0
    for s in StaffAccount.objects.filter(facility=facility, is_active=True).order_by('pk'):
        if s.pk in have or getattr(s, 'is_developer', False) or s.is_superuser:
            continue
        ShiftMember.objects.create(facility=facility, name=str(s)[:ShiftMember.NAME_MAX], staff=s, order=last + n)
        n += 1
    return n


def month_table(facility, year, month):
    """月の勤務表：人 × 日。行の合計（出勤の日数・時間）と、日ごとの出勤の人数"""
    days = month_days(year, month)
    first, last = days[0]['date'], days[-1]['date']
    closed = closed_days(facility, first, last)
    for d in days:
        d['closed'] = d['date'] in closed
    patterns = list(ShiftPattern.objects.filter(facility=facility))
    by_pk = {p.pk: p for p in patterns}
    entries = {(e.member_id, e.date): e for e in ShiftDay.objects.filter(facility=facility, date__range=(first, last))}
    used = {m for m, _ in entries}
    members = [m for m in ShiftMember.objects.filter(facility=facility) if m.is_active or m.pk in used]
    counts = {d['date']: 0 for d in days}
    rows = []
    for m in members:
        cells, work_days, minutes = [], 0, 0
        for d in days:
            e = entries.get((m.pk, d['date']))
            p = by_pk.get(e.pattern_id) if e else None
            if p and not p.is_off:
                work_days += 1
                minutes += p.minutes
                counts[d['date']] += 1
            cells.append({'day': d, 'pattern': p, 'name': f'c_{m.pk}_{d["day"]}'})
        rows.append({'member': m, 'cells': cells, 'work_days': work_days, 'hours': fmt_minutes(minutes)})
    prev_m = first - datetime.timedelta(days=1)
    next_m = last + datetime.timedelta(days=1)
    today = datetime.date.today()
    return {
        'year': year, 'month': month, 'ym': f'{year}-{month:02d}', 'days': days, 'rows': rows,
        'patterns': [p for p in patterns if p.is_active], 'all_patterns': patterns,
        'counts': [counts[d['date']] for d in days], 'has_entries': bool(entries),
        'prev_ym': f'{prev_m.year}-{prev_m.month:02d}', 'next_ym': f'{next_m.year}-{next_m.month:02d}',
        'this_ym': f'{today.year}-{today.month:02d}', 'is_this_month': (today.year, today.month) == (year, month),
    }


def apply_weekly(facility, year, month, staff, overwrite=False):
    """曜日の基本を、その月の日に入れる。入っている日はそのまま（overwrite なら入れ直す）。休業日には入れない。入れた数を返す"""
    days = month_days(year, month)
    first, last = days[0]['date'], days[-1]['date']
    closed = closed_days(facility, first, last)
    weekly = {}
    for w in ShiftWeekly.objects.filter(member__facility=facility, member__is_active=True, pattern__is_active=True):
        weekly[(w.member_id, w.weekday)] = w.pattern_id
    have = {(e.member_id, e.date): e for e in ShiftDay.objects.filter(facility=facility, date__range=(first, last))}
    n = 0
    for (member_id, weekday), pattern_id in weekly.items():
        for d in days:
            day = d['date']
            if day.weekday() != weekday or day in closed:
                continue
            e = have.get((member_id, day))
            if e is None:
                ShiftDay.objects.create(facility=facility, member_id=member_id, date=day, pattern_id=pattern_id, updated_by=staff)
                n += 1
            elif overwrite and e.pattern_id != pattern_id:
                e.pattern_id, e.updated_by = pattern_id, staff
                e.save(update_fields=['pattern', 'updated_by', 'updated_at'])
                n += 1
    return n


def save_month(facility, year, month, post, staff):
    """月の勤務表のマスを保存する（c_<人>_<日> = 勤務の種類。空なら消す）。変わった数を返す"""
    days = month_days(year, month)
    first, last = days[0]['date'], days[-1]['date']
    patterns = set(ShiftPattern.objects.filter(facility=facility).values_list('pk', flat=True))
    have = {(e.member_id, e.date): e for e in ShiftDay.objects.filter(facility=facility, date__range=(first, last))}
    changed = 0
    for m in ShiftMember.objects.filter(facility=facility):
        for d in days:
            key = f'c_{m.pk}_{d["day"]}'
            if key not in post:
                continue
            raw = post.get(key, '')
            pk = int(raw) if str(raw).isdigit() and int(raw) in patterns else None
            e = have.get((m.pk, d['date']))
            if pk is None:
                if e is not None:
                    e.delete()
                    changed += 1
            elif e is None:
                ShiftDay.objects.create(facility=facility, member=m, date=d['date'], pattern_id=pk, updated_by=staff)
                changed += 1
            elif e.pattern_id != pk:
                e.pattern_id, e.updated_by = pk, staff
                e.save(update_fields=['pattern', 'updated_by', 'updated_at'])
                changed += 1
    return changed


def clear_month(facility, year, month):
    days = month_days(year, month)
    n, _ = ShiftDay.objects.filter(facility=facility, date__range=(days[0]['date'], days[-1]['date'])).delete()
    return n


def weekly_table(facility):
    """曜日の基本の表：人 × 曜日"""
    weekly = {(w.member_id, w.weekday): w.pattern_id for w in ShiftWeekly.objects.filter(member__facility=facility)}
    rows = []
    for m in ShiftMember.objects.filter(facility=facility, is_active=True):
        rows.append({'member': m, 'cells': [{'weekday': i, 'label': WEEK_JP[i], 'pattern_id': weekly.get((m.pk, i)),
                                             'name': f'w_{m.pk}_{i}'} for i in range(7)]})
    return rows


def save_weekly(facility, post):
    patterns = set(ShiftPattern.objects.filter(facility=facility).values_list('pk', flat=True))
    changed = 0
    for m in ShiftMember.objects.filter(facility=facility, is_active=True):
        for i in range(7):
            key = f'w_{m.pk}_{i}'
            if key not in post:
                continue
            raw = post.get(key, '')
            pk = int(raw) if str(raw).isdigit() and int(raw) in patterns else None
            cur = ShiftWeekly.objects.filter(member=m, weekday=i).first()
            if pk is None:
                if cur:
                    cur.delete()
                    changed += 1
            elif cur is None:
                ShiftWeekly.objects.create(member=m, weekday=i, pattern_id=pk)
                changed += 1
            elif cur.pattern_id != pk:
                cur.pattern_id = pk
                cur.save(update_fields=['pattern'])
                changed += 1
    return changed
