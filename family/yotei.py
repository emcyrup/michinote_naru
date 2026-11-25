"""
予定のカレンダーと利用日申請（2026-11 事業所「成」の要望）。

- カレンダーは日曜始まり。日付の下に、休業日・活動の予定・お出かけ先（DayEvent.title）。日付を押すと詳しい内容（持ち物・参加費など）
- 予定ごとに見せる相手を選べる（全員／お知らせと同じグループ）。同じ日に、グループごとに違う予定を入れられる。
  保護者（child を渡したとき）には、全員向けと、その子が入っているグループ向けの予定だけを出す
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

from .models import DayEvent, FamilyMessage, UseRequest

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


class DaySummary:
    """
    1 日ぶんの予定をまとめたもの（画面に出す形）。以前は 1 日 1 件だったので、同じ名前の欄（title・label・detail・is_closed）を持つ。
    events が、その日の予定（見せてよいものだけ）。show_audience（職員の画面）なら、全員向けでない予定に見せる相手を添える
    """

    def __init__(self, events, show_audience=False):
        self.events = list(events)
        self.is_closed = any(e.is_closed for e in self.events)
        named = [e for e in self.events if e.title]
        self.title = '／'.join(self._name(e, show_audience) for e in named)
        self.label = self.title or ('休業日' if self.is_closed else '')
        detailed = [e for e in self.events if e.detail.strip()]
        if len(detailed) == 1 and len(named) <= 1:
            self.detail = detailed[0].detail
        else:
            self.detail = '\n\n'.join(f'【{self._name(e, show_audience) or "予定"}】\n{e.detail.strip()}' for e in detailed)

    @staticmethod
    def _name(e, show_audience):
        return f'{e.title}（{e.audience}）' if show_audience and e.title and not e.for_everyone else e.title

    def __bool__(self):
        # 休業日の印だけの日（見せる予定が無い保護者にも、休業日は出す）も「予定あり」
        return bool(self.events) or self.is_closed


def child_group_ids(child):
    """その子が入っているグループ（お知らせと同じ NoticeGroup）"""
    return set(child.notice_groups.values_list('pk', flat=True)) if child is not None else set()


def _visible(events, child, group_ids):
    """child を渡したとき（保護者）：全員向けと、その子のグループ向けだけ。渡さなければ（職員）全部"""
    if child is None:
        return list(events)
    return [e for e in events if e.for_everyone or group_ids & {g.pk for g in e.group_list}]


def events_between(facility, first, last, child=None):
    """日付 → その日の予定のまとめ（DaySummary）。予定の無い日は入らない"""
    by_day = {}
    for e in DayEvent.objects.filter(facility=facility, date__range=(first, last)).prefetch_related('groups'):
        by_day.setdefault(e.date, []).append(e)
    gids = child_group_ids(child)
    out = {}
    for d, events in by_day.items():
        seen = _visible(events, child, gids)
        # 休業日の印は、どのグループ向けの予定に付いていても、全員に効く
        closed = any(e.is_closed for e in events)
        if seen or closed:
            summary = DaySummary(seen, show_audience=child is None)
            if closed and not summary.is_closed:
                summary.is_closed = True
                summary.label = summary.title or '休業日'
            out[d] = summary
    return out


def month_view(facility, year, month, child=None, window=None):
    """
    カレンダー 1 か月ぶん（日曜始まり）。
    child を渡すと、その子の来所予定（利用・欠席）を付ける。渡さなければ、その日に来る人数を付ける（職員の画面）。
    window=(first, last) を渡すと、申請できる日に can_pick を付ける（休業日・すでに予定のある日は選べない）。
    """
    first, last = month_span(year, month)
    weeks_raw = calendar.Calendar(firstweekday=6).monthdatescalendar(year, month)
    events = events_between(facility, first, last, child)
    visits = ScheduledVisit.objects.filter(facility=facility, date__range=(first, last))
    mine, counts = {}, {}
    off = {}
    if child is not None:
        mine = {v.date: v for v in visits.filter(beneficiary=child)}
        # 予定は無いが、欠席の連絡がある日（利用日申請で「都合欠」を選んだ日など）
        for d, reason in FamilyMessage.objects.filter(facility=facility, beneficiary=child, kind=FamilyMessage.KIND_ABSENCE,
                                                      target_date__range=(first, last)).values_list('target_date', 'reason'):
            off[d] = '都合欠' if reason == FamilyMessage.REASON_PERSONAL else '欠席'
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
                'has_detail': bool(ev and ev.detail.strip()), 'count': counts.get(d, 0), 'events': ev.events if ev else [],
                'use': bool(v and v.status != ScheduledVisit.STATUS_ABSENT),
                'absent': bool(v and v.status == ScheduledVisit.STATUS_ABSENT),
                'off': off.get(d, '') if v is None else '',
            }
            cell['can_pick'] = bool(window and window[0] <= d <= window[1] and not cell['is_closed'] and v is None
                                    and not cell['off'])
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


def apply_days(facility, child, days, *, via, line_user_id='', sender_name='', staff=None, account=None):
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
            account=account, staff=staff, seen_at=timezone.now() if via == UseRequest.VIA_STAFF else None,
            seen_by=staff if via == UseRequest.VIA_STAFF else None)
        made.append(d)
    return made, skipped


# ---------------------------------------------------------------- 曜日で決めた利用日の選び漏れ（2026-11-25 成の要望）
# 児童台帳の「利用曜日」（Beneficiary.weekday_mon〜weekday_sat）がある子は、申請するときに、その曜日の日を
# 選んでいなければ「利用」か「都合欠」かを必ず選んでもらう（選び漏れと休みの希望を分ける）。曜日が無い子には出さない。
WEEKDAY_FIELDS = ['weekday_mon', 'weekday_tue', 'weekday_wed', 'weekday_thu', 'weekday_fri', 'weekday_sat']
PERSONAL_OFF_BODY = '利用日申請で「都合欠」を選びました'


def regular_weekdays(child):
    """利用曜日（月＝0 … 土＝5）。決めていなければ空"""
    return {i for i, f in enumerate(WEEKDAY_FIELDS) if getattr(child, f, False)}


def missing_regular_days(facility, child, first, last, picked=()):
    """
    first〜last のうち、利用曜日の日で、まだ何も決まっていない日（古い順）。
    休業日・もう予定のある日・もう欠席の連絡のある日・picked（いま選んだ日）は除く
    """
    weekdays = regular_weekdays(child)
    if not weekdays or first > last:
        return []
    days = [first + datetime.timedelta(days=n) for n in range((last - first).days + 1)]
    days = [d for d in days if d.weekday() in weekdays and d not in set(picked)]
    if not days:
        return []
    closed = set(DayEvent.objects.filter(facility=facility, date__in=days, is_closed=True).values_list('date', flat=True))
    have = set(ScheduledVisit.objects.filter(beneficiary=child, date__in=days).values_list('date', flat=True))
    have |= set(FamilyMessage.objects.filter(facility=facility, beneficiary=child, kind=FamilyMessage.KIND_ABSENCE,
                                             target_date__in=days).values_list('target_date', flat=True))
    return [d for d in days if d not in closed and d not in have]


def apply_personal_off(facility, child, guardian, days):
    """
    利用日申請で「都合欠」を選んだ日：欠席連絡（都合欠）として残す。職員の「保護者からの連絡」のその日に出て、
    保護者のカレンダーには「都合欠」の印が出る（来所予定は作らない）。(作った連絡, もうあって作らなかった日) を返す
    """
    from .services import create_messages
    return create_messages(facility, child, guardian, FamilyMessage.KIND_ABSENCE, days,
                           reason=FamilyMessage.REASON_PERSONAL, body=PERSONAL_OFF_BODY)


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


def facility_groups(facility):
    """予定の見せる相手に選べるグループ（お知らせのグループ）"""
    from .models import NoticeGroup
    return list(NoticeGroup.objects.filter(facility=facility).order_by('name', 'pk'))


def save_day(facility, day, is_closed, entries, staff):
    """
    1 日ぶんの予定をまとめて保存する。変わったら True。
    entries：[{'title': 文, 'detail': 文, 'group_ids': [グループの番号…]}, …]（並びの順。空の予定は捨てる。MAX_PER_DAY 件まで）
    group_ids が空なら全員向け。この事業所のグループでない番号は捨てる。
    休業日の印は、1 件目の予定に付ける（予定が無ければ、印だけの 1 件を作る）。何も無くなったら、その日の予定を消す。
    """
    valid = {g.pk: g for g in facility_groups(facility)}
    want = []
    for e in entries:
        title = (e.get('title') or '').strip()[:DayEvent.TITLE_MAX]
        detail = (e.get('detail') or '').strip()[:DayEvent.DETAIL_MAX]
        if not title and not detail:
            continue
        gids = sorted({int(g) for g in (e.get('group_ids') or []) if str(g).isdigit() and int(g) in valid})
        want.append((title, detail, gids))
    want = want[:DayEvent.MAX_PER_DAY]
    if is_closed and not want:
        want = [('', '', [])]
    have = list(DayEvent.objects.filter(facility=facility, date=day).prefetch_related('groups'))
    before = [(e.title, e.detail, sorted(g.pk for g in e.group_list), e.is_closed) for e in have]
    after = [(t, d, g, bool(is_closed) and i == 0) for i, (t, d, g) in enumerate(want)]
    if before == after:
        return False
    from django.db import transaction
    with transaction.atomic():
        for i, (title, detail, gids, closed) in enumerate(after):
            ev = have[i] if i < len(have) else DayEvent(facility=facility, date=day)
            ev.title, ev.detail, ev.is_closed, ev.order, ev.updated_by = title, detail, closed, i, staff
            ev.save()
            ev.groups.set([valid[g] for g in gids])
        for ev in have[len(after):]:
            ev.delete()
    return True


def save_event(facility, day, title, is_closed, detail, staff, keep_detail=False):
    """全員向けの予定を 1 件だけ持つ日として保存する（以前の入口。その日のほかの予定は消える）。変わったら True"""
    if keep_detail:
        ev = DayEvent.objects.filter(facility=facility, date=day).first()
        detail = ev.detail if ev else ''
    return save_day(facility, day, is_closed, [{'title': title, 'detail': detail, 'group_ids': []}], staff)


def day_info(facility, day, child=None):
    """日付を押したときの画面に出すもの"""
    ev = events_between(facility, day, day, child).get(day)
    info = {'day': day, 'weekday': WEEK_JP[day.weekday()], 'holiday': holiday_name(day) or '', 'event': ev,
            'events': ev.events if ev else [], 'is_today': day == timezone.localdate()}
    if child is not None:
        v = ScheduledVisit.objects.filter(facility=facility, beneficiary=child, date=day).first()
        info['use'] = bool(v and v.status != ScheduledVisit.STATUS_ABSENT)
        info['absent'] = bool(v and v.status == ScheduledVisit.STATUS_ABSENT)
        # 予定は無いが欠席の連絡がある日（利用日申請で「都合欠」を選んだ日など）：'都合欠' か '欠席'
        reason = None if v else FamilyMessage.objects.filter(
            facility=facility, beneficiary=child, kind=FamilyMessage.KIND_ABSENCE, target_date=day).values_list('reason', flat=True).first()
        info['off'] = '' if reason is None else ('都合欠' if reason == FamilyMessage.REASON_PERSONAL else '欠席')
    return info
