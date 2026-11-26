"""
欠席時の対応の記録（2026-11-23 成の要望）。

職員の「連絡帳」で欠席（グレー）の子を開くと、保護者からの欠席連絡と、この記録を書く欄が出る。
並びと項目は、成がいま使っている紙の様式（Excel）に合わせる。

- 利用日：その日。対応した職員：事業所の既定（Facility.absence_staff_default）。連絡者：「母」。どれも書き換えられる
- 欠席理由：保護者の欠席連絡の理由（病欠 → 体調不良・都合欠 → 家族都合）に最初から印を付ける
- 次回利用予定日：その日より後で、来所予定があって、欠席でも欠席連絡のある日でもない最初の日
  （インフルエンザなどで 3 日続けて休む連絡なら、4 日目以降の予定の日になる）
- 欠席時対応加算：該当 → 実績記録票に【欠席】・該当しない → 【−】
"""
import datetime

from .models import AbsenceRecord, FamilyMessage

SUPPORTS = [
    ('health', '体調等に対する支援をした'),
    ('home', '家庭内における過ごし方を助言した'),
    ('consult', '自宅で過ごす中で事業所として家族支援が必要な状況があればいつでも電話相談することができる旨を助言した'),
    ('talk', '本人と電話で話をした'),
    ('next', '次回の利用を促す提案をした'),
]
METHODS = [('phone', '電話'), ('direct', '送迎時（直接）'), ('other', 'その他')]
REASONS = [('sick', '体調不良'), ('family', '家族都合'), ('refuse', '通所拒否'), ('hospital', '通院'), ('other', 'その他')]
# 保護者の欠席連絡の理由 → 欠席理由の印
REASON_FROM_MESSAGE = {FamilyMessage.REASON_SICK: 'sick', FamilyMessage.REASON_PERSONAL: 'family'}
DEFAULT_CONTACT_PERSON = '母'
NEXT_LOOKAHEAD_DAYS = 120


def absence_messages(facility, child, day):
    """その子のその日の欠席連絡（古い順）"""
    return list(FamilyMessage.objects.filter(facility=facility, beneficiary=child, target_date=day,
                                             kind=FamilyMessage.KIND_ABSENCE).select_related('confirmed_by'))


def next_visit_date(facility, child, day):
    """次回利用予定日：day より後で、来所予定があり、欠席でも欠席連絡のある日でもない最初の日。無ければ None"""
    from schedules.models import ScheduledVisit
    until = day + datetime.timedelta(days=NEXT_LOOKAHEAD_DAYS)
    off = set(FamilyMessage.objects.filter(facility=facility, beneficiary=child, kind=FamilyMessage.KIND_ABSENCE,
                                           target_date__gt=day, target_date__lte=until).values_list('target_date', flat=True))
    days = (ScheduledVisit.objects.filter(facility=facility, beneficiary=child, date__gt=day, date__lte=until)
            .exclude(status=ScheduledVisit.STATUS_ABSENT).order_by('date').values_list('date', flat=True))
    for d in days:
        if d not in off:
            return d
    return None


def initial(facility, child, day, msgs=None):
    """まだ記録が無いときに、最初から入れておく値"""
    if msgs is None:
        msgs = absence_messages(facility, child, day)
    reasons = []
    for m in msgs:
        key = REASON_FROM_MESSAGE.get(m.reason)
        if key and key not in reasons:
            reasons.append(key)
    return AbsenceRecord(facility=facility, beneficiary=child, date=day,
                         staff_name=facility.absence_staff_default, contact_person=DEFAULT_CONTACT_PERSON,
                         reasons=reasons, next_date=next_visit_date(facility, child, day))


def record_for(facility, child, day, msgs=None):
    """（記録, 保存ずみか）。まだ無ければ、最初の値を入れた保存前の記録"""
    rec = AbsenceRecord.objects.filter(facility=facility, beneficiary=child, date=day).first()
    if rec is not None:
        return rec, True
    return initial(facility, child, day, msgs), False


def _picked(values, choices):
    keys = [k for k, _ in choices]
    return [k for k in keys if k in values]


def save(facility, child, day, data, staff):
    """画面の入力（request.POST）を保存する。返り値は（記録, 問題の文）。問題があれば保存しない"""
    raw_next = (data.get('next_date') or '').strip()
    next_date = None
    if raw_next:
        try:
            next_date = datetime.date.fromisoformat(raw_next)
        except ValueError:
            return None, '次回利用予定日が読めません。'
    addon = data.get('addon') or ''
    if addon not in (AbsenceRecord.ADDON_YES, AbsenceRecord.ADDON_NO, ''):
        addon = ''
    rec = AbsenceRecord.objects.filter(facility=facility, beneficiary=child, date=day).first() \
        or AbsenceRecord(facility=facility, beneficiary=child, date=day)
    rec.staff_name = (data.get('staff_name') or '').strip()[:60]
    rec.contact_date = (data.get('contact_date') or '').strip()[:30]
    rec.contact_time = (data.get('contact_time') or '').strip()[:30]
    rec.contact_person = (data.get('contact_person') or '').strip()[:30]
    rec.supports = _picked(data.getlist('supports'), SUPPORTS)
    rec.methods = _picked(data.getlist('methods'), METHODS)
    rec.reasons = _picked(data.getlist('reasons'), REASONS)
    rec.next_date = next_date
    rec.note = (data.get('note') or '').strip()[:AbsenceRecord.NOTE_MAX]
    rec.addon = addon
    rec.updated_by = staff
    rec.save()
    # 来所予定の無い日（予定を入れる前に欠席の連絡が来た日など）は、欠席の予定として入れる。
    # 実績記録票（【欠席】／【−】）と請求は来所予定をもとに出すので、ここで作っておく
    from schedules.models import ScheduledVisit
    ScheduledVisit.objects.get_or_create(beneficiary=child, date=day,
                                         defaults={'facility': facility, 'status': ScheduledVisit.STATUS_ABSENT})
    if data.get('remember_staff') and rec.staff_name and rec.staff_name != facility.absence_staff_default:
        facility.absence_staff_default = rec.staff_name
        facility.save(update_fields=['absence_staff_default'])
    return rec, ''


def addon_marks(facility, child_ids, days):
    """実績記録票の表示に使う：{(利用者, 日): 'yes' / 'no'}（記録が無い・未選択なら入らない）"""
    rows = AbsenceRecord.objects.filter(facility=facility, beneficiary_id__in=child_ids, date__in=days) \
        .exclude(addon='').values_list('beneficiary_id', 'date', 'addon')
    return {(b, d): a for b, d, a in rows}


def addon_of(facility, child, day):
    """その日の欠席時対応加算の選択（'yes' / 'no' / ''）"""
    return AbsenceRecord.objects.filter(facility=facility, beneficiary=child, date=day).values_list('addon', flat=True).first() or ''
