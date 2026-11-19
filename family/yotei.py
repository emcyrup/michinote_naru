"""
予定のカレンダーと利用日申請（2026-11 事業所「成」の要望）。

- カレンダーは日曜始まり。日付の下に、休業日・活動の予定・お出かけ先（DayEvent.title）。日付を押すと詳しい内容（持ち物・参加費など）
- 職員の画面と保護者ページで同じものを出す（保護者は見るだけ）
- 利用日申請：保護者がカレンダーで日にちを選んで送る。職員が代わりに入れることもできる
  申請した日は、そのまま来所予定（ScheduledVisit）になる → 連絡帳・日誌・実績記録票の日ごとの一覧に出る
"""
import calendar
import datetime

from django.utils import timezone

from config.jp_holidays import holiday_name
from schedules.daylist import WEEK_JP
from schedules.models import ScheduledVisit

from .models import DayEvent, UseRequest

# 保護者が申請できる範囲（あしたから。きょうの分は電話などで）
REQUEST_AHEAD_DAYS = 92
# 職員が代わりに入れられる範囲
STAFF_BACK_DAYS = 62
STAFF_AHEAD_DAYS = 185
DATES_MAX = 62


def parse_ym(value):
    """'2026-12' を (2026, 12) に。読めなければ今月"""
    try:
        y, m = str(value or '').split('-')[:2]
        y, m = int(y), int(m)
        if 1 <= m <= 12 and 2000 <= y <= 2100:
            return y, m
    except ValueError:
        pass
    today = timezone.localdate()
    return today.year, today.month


def month_span(year, month):
    return datetime.date(year, month, 1), datetime.date(year, month, calendar.monthrange(year, month)[1])


def parent_window():
    today = timezone.localdate()
    return today + datetime.timedelta(days=1), today + datetime.timedelta(days=REQUEST_AHEAD_DAYS)


def staff_window():
    today = timezone.localdate()
    return today - datetime.timedelta(days=STAFF_BACK_DAYS), today + datetime.timedelta(days=STAFF_AHEAD_DAYS)


def events_between(facility, first, last):
    return {e.date: e for e in DayEvent.objects.filter(facility=facility, date__range=(first, last))}


def month_view(facility, year, month, child=None, window=None):
    """
    カレンダー 1 か月ぶん（日曜始まり）。
    child を渡すと、その子の来所予定（利用・欠席）を付ける。渡さなければ、その日に来る人数を付ける（職員の画面）。
    window=(first, last) を渡すと、申請できる日に can_pick を付ける（休業日・すでに予定のある日は選べない）。
    """
    first, last = month_span(year, month)
    weeks_raw = calendar.Calendar(firstweekday=6).monthdatescalendar(year, month)
    events = events_between(facility, first, last)
    visits = ScheduledVisit.objects.filter(facility=facility, date__range=(first, last))
    mine, counts = {}, {}
    if child is not None:
        mine = {v.date: v for v in visits.filter(beneficiary=child)}
    else:
        for d in visits.exclude(status=ScheduledVisit.STATUS_ABSENT).values_list('date', flat=True):
            counts[d] = counts.get(d, 0) + 1
    today = timezone.localdate()
    weeks, agenda = [], []
    for week in weeks_raw:
        row = []
        for d in week:
            if d.month != month:
                row.append({'date': d, 'in_month': False})
                continue
            ev = events.get(d)
            v = mine.get(d)
            hol = holiday_name(d) or ''
            cell = {
                'date': d, 'in_month': True, 'iso': d.isoformat(), 'is_today': d == today,
                'weekday': WEEK_JP[d.weekday()], 'is_sun': d.weekday() == 6 or bool(hol), 'is_sat': d.weekday() == 5 and not hol,
                'holiday': hol, 'event': ev, 'label': ev.label if ev else '', 'is_closed': bool(ev and ev.is_closed),
                'has_detail': bool(ev and ev.detail.strip()), 'count': counts.get(d, 0),
                'use': bool(v and v.status != ScheduledVisit.STATUS_ABSENT),
                'absent': bool(v and v.status == ScheduledVisit.STATUS_ABSENT),
            }
            cell['can_pick'] = bool(window and window[0] <= d <= window[1] and not cell['is_closed'] and v is None)
            row.append(cell)
            if ev and (ev.label or cell['has_detail']):
                agenda.append(cell)
        weeks.append(row)
    prev_m = first - datetime.timedelta(days=1)
    next_m = last + datetime.timedelta(days=1)
    return {
        'weeks': weeks, 'agenda': agenda, 'year': year, 'month': month, 'ym': f'{year}-{month:02d}',
        'prev_ym': f'{prev_m.year}-{prev_m.month:02d}', 'next_ym': f'{next_m.year}-{next_m.month:02d}',
        'this_ym': f'{today.year}-{today.month:02d}', 'is_this_month': (today.year, today.month) == (year, month),
        'week_head': ['日', '月', '火', '水', '木', '金', '土'],
    }


def clean_days(values, window):
    """画面から届いた日付（YYYY-MM-DD）を、範囲の中の日付だけにして古い順に"""
    out = set()
    for v in values:
        try:
            d = datetime.date.fromisoformat(str(v).strip())
        except ValueError:
            continue
        if window[0] <= d <= window[1]:
            out.add(d)
    return sorted(out)[:DATES_MAX]


def apply_days(facility, child, days, *, via, line_user_id='', sender_name='', staff=None):
    """
    利用日を申請する：その日の来所予定を作り、控え（UseRequest）を残す。
    休業日・すでに予定（欠席を含む）のある日は入れない。(入れた日, 入れなかった日) を返す。
    """
    if not days:
        return [], []
    closed = set(DayEvent.objects.filter(facility=facility, date__in=days, is_closed=True).values_list('date', flat=True))
    made, skipped = [], []
    for d in days:
        if d in closed:
            skipped.append(d)
            continue
        _, created = ScheduledVisit.objects.get_or_create(
            beneficiary=child, date=d, defaults={'facility': facility, 'status': ScheduledVisit.STATUS_SCHEDULED})
        if not created:
            skipped.append(d)
            continue
        UseRequest.objects.create(
            facility=facility, beneficiary=child, date=d, via=via, line_user_id=line_user_id, sender_name=sender_name,
            staff=staff, seen_at=timezone.now() if via == UseRequest.VIA_STAFF else None,
            seen_by=staff if via == UseRequest.VIA_STAFF else None)
        made.append(d)
    return made, skipped


def unseen_requests(facility):
    """保護者から届いた申請のうち、職員がまだ確認していないもの"""
    return UseRequest.objects.filter(facility=facility, via=UseRequest.VIA_PARENT, seen_at__isnull=True)


def unseen_groups(facility):
    """まだ確認していない申請を、子どもごとにまとめる [{'beneficiary', 'days': [...], 'sender', 'at'}]"""
    groups = {}
    for r in unseen_requests(facility).select_related('beneficiary').order_by('date'):
        g = groups.setdefault(r.beneficiary_id, {'beneficiary': r.beneficiary, 'days': [], 'sender': r.sender_name, 'at': r.created_at})
        g['days'].append(r.date)
        g['at'] = max(g['at'], r.created_at)
    return sorted(groups.values(), key=lambda g: (g['beneficiary'].last_name_kana, g['beneficiary'].first_name_kana))


def unseen_count(facility):
    """確認していない申請のある子どもの人数（ホームのアイコンの数字）"""
    return unseen_requests(facility).values('beneficiary_id').distinct().count()


def mark_seen(facility, staff):
    return unseen_requests(facility).update(seen_at=timezone.now(), seen_by=staff)


def save_event(facility, day, title, is_closed, detail, staff, keep_detail=False):
    """1 日ぶんの予定を保存する。何も無くなったら消す。変わったら True"""
    title = (title or '').strip()[:DayEvent.TITLE_MAX]
    ev = DayEvent.objects.filter(facility=facility, date=day).first()
    detail = (ev.detail if ev else '') if keep_detail else (detail or '').strip()[:DayEvent.DETAIL_MAX]
    if not title and not is_closed and not detail:
        if ev:
            ev.delete()
            return True
        return False
    if ev is None:
        DayEvent.objects.create(facility=facility, date=day, title=title, is_closed=is_closed, detail=detail, updated_by=staff)
        return True
    if (ev.title, ev.is_closed, ev.detail) == (title, is_closed, detail):
        return False
    ev.title, ev.is_closed, ev.detail, ev.updated_by = title, is_closed, detail, staff
    ev.save()
    return True


def day_info(facility, day, child=None):
    """日付を押したときの画面に出すもの"""
    ev = DayEvent.objects.filter(facility=facility, date=day).first()
    info = {'day': day, 'weekday': WEEK_JP[day.weekday()], 'holiday': holiday_name(day) or '', 'event': ev,
            'is_today': day == timezone.localdate()}
    if child is not None:
        v = ScheduledVisit.objects.filter(facility=facility, beneficiary=child, date=day).first()
        info['use'] = bool(v and v.status != ScheduledVisit.STATUS_ABSENT)
        info['absent'] = bool(v and v.status == ScheduledVisit.STATUS_ABSENT)
    return info
