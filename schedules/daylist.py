"""
日ごとの一覧の共通部品（2026-11 事業所「成」の要望）。
連絡帳・日誌・実績記録票などの画面で「その日に来る予定の子だけを並べ、←→ で前の日・次の日へ送る」ために使う。
欠席になった子は一覧から消さず、下に分けて出す（背景をグレーに）。
"""
import datetime

from config.jp_holidays import holiday_name

from .models import ScheduledVisit

WEEK_JP = ['月', '火', '水', '木', '金', '土', '日']


def parse_day(value, default=None):
    """?date=2026-12-01 を日付に。読めなければきょう"""
    try:
        return datetime.date.fromisoformat(str(value or ''))
    except ValueError:
        return default or datetime.date.today()


def day_nav(day):
    """←→ と日付の見出しに使う値"""
    today = datetime.date.today()
    return {
        'day': day, 'today': today, 'is_today': day == today,
        'prev_day': day - datetime.timedelta(days=1), 'next_day': day + datetime.timedelta(days=1),
        'weekday': WEEK_JP[day.weekday()], 'holiday': holiday_name(day) or '',
    }


def day_visits(facility, day):
    """その日の来所予定を（来る子, 欠席の子）に分けて返す。どちらも名前（かな）順"""
    visits = list(ScheduledVisit.objects.filter(facility=facility, date=day)
                  .select_related('beneficiary')
                  .order_by('beneficiary__last_name_kana', 'beneficiary__first_name_kana', 'beneficiary_id'))
    coming = [v for v in visits if v.status != ScheduledVisit.STATUS_ABSENT]
    absent = [v for v in visits if v.status == ScheduledVisit.STATUS_ABSENT]
    return coming, absent
