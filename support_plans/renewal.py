"""
個別支援計画の更新の一覧（2026-11 事業所「成」の要望）。
在籍中の子を全員並べ、計画期間の終わりが今月の子は赤、来月の子はオレンジ、ほかは黒で出す。
名前を押すと、その子の計画（作成の流れ＝アセスメント → 原案 → 担当者会議 → 説明・同意・交付 → モニタリング）へ。
"""
import datetime

from beneficiaries.models import Beneficiary

from .models import SupportPlan


def _next_month(day):
    return (day.year + (day.month == 12), day.month % 12 + 1)


def renewal_rows(facility, today=None):
    today = today or datetime.date.today()
    this_m, next_m = (today.year, today.month), _next_month(today)
    plans = {}
    for p in (SupportPlan.objects.filter(facility=facility).exclude(status=SupportPlan.STATUS_CLOSED)
              .select_related('draft').order_by('created_at', 'pk')):
        d = plans.setdefault(p.beneficiary_id, {})
        d['active' if p.status == SupportPlan.STATUS_ACTIVE else 'working'] = p      # 同じ状態が複数あれば新しいほう
    rows = []
    for b in Beneficiary.objects.filter(facility=facility, status=Beneficiary.STATUS_ACTIVE) \
            .order_by('last_name_kana', 'first_name_kana', 'pk'):
        mine = plans.get(b.pk, {})
        active, working = mine.get('active'), mine.get('working')
        draft = getattr(active, 'draft', None) if active else None
        end = draft.period_end if draft else None
        color, label = 'black', ''
        if active is None and working is None:
            label = '計画がありません'
        elif active is None:
            label = '作成中'
        elif end is None:
            label = '計画期間が未入力'
        elif end < today:
            color, label = 'red', '期限が過ぎています'
        elif (end.year, end.month) == this_m:
            color, label = 'red', '今月更新'
        elif (end.year, end.month) == next_m:
            color, label = 'orange', '来月更新'
        rows.append({
            'beneficiary': b, 'color': color, 'label': label, 'period_end': end,
            'plan': working or active, 'active': active, 'working': working,
            'working_step': working.get_current_step_display() if working else '',
        })
    return rows


def renewal_counts(rows):
    return {'red': sum(1 for r in rows if r['color'] == 'red'), 'orange': sum(1 for r in rows if r['color'] == 'orange'),
            'none': sum(1 for r in rows if r['plan'] is None)}
