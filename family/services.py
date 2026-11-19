"""保護者ページの部品：だれの子か・リンクを作る・LINE で案内する"""
import datetime

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from beneficiaries.models import Guardian
from schedules.models import ScheduledVisit

from .models import FamilyAccess, FamilyMessage

# 公式 LINE でこの言葉を送ると、保護者ページのリンクを返す（画面下のメニューのボタンにも同じ言葉を割り当てる）
MENU_WORDS = ('メニュー', 'めにゅー', 'ホーム', 'ほーむ', '保護者ページ', 'マイページ', 'アプリ', 'MENU', 'HOME')
SESSION_KEY = 'family_access'


def is_menu_word(text):
    return ''.join((text or '').split()).upper() in {w.upper() for w in MENU_WORDS}


def guardians_of(facility, line_user_id):
    """この LINE の人が保護者になっている、この事業所の子（在籍中）。きょうだいは複数"""
    if not line_user_id:
        return Guardian.objects.none()
    return (Guardian.objects.filter(beneficiary__facility=facility, line_user_id=line_user_id, line_linked=True)
            .filter(beneficiary__status='active')
            .select_related('beneficiary').order_by('beneficiary__last_name_kana', 'beneficiary__first_name_kana', 'pk'))


def access_for(facility, line_user_id):
    """この LINE の人のリンク（無ければ作る）。保護者として登録されていなければ None"""
    if not facility.use_family_page or not guardians_of(facility, line_user_id).exists():
        return None
    access, _ = FamilyAccess.objects.get_or_create(facility=facility, line_user_id=line_user_id)
    return access


def enter_url(access, base):
    """LINE に貼るアドレス。base は https://ドメイン/（request.build_absolute_uri('/')）"""
    return base.rstrip('/') + reverse('family:enter', args=[access.token])


def menu_reply(facility, line_user_id, base):
    """公式 LINE への返事の文（保護者ページを使わない事業所・保護者でない人には None）"""
    access = access_for(facility, line_user_id)
    if access is None:
        return None
    return (f'{facility.name} の保護者ページです。下のリンクから開いてください。\n{enter_url(access, base)}\n\n'
            'このリンクはご家庭専用です。ほかの方には送らないでください。')


# ---------------------------------------------------------------- 保護者からの連絡
# 保護者が選べる日：きょうから DATE_AHEAD_DAYS 日先まで。1 回に DATES_MAX 日まで
DATE_AHEAD_DAYS = 92
DATES_MAX = 31


def date_range():
    today = timezone.localdate()
    return today, today + datetime.timedelta(days=DATE_AHEAD_DAYS)


def clean_dates(values):
    """画面から届いた日付（YYYY-MM-DD の並び）を、選べる範囲の日付だけにして古い順に返す"""
    first, last = date_range()
    out = set()
    for v in values:
        try:
            d = datetime.date.fromisoformat(str(v).strip())
        except ValueError:
            continue
        if first <= d <= last:
            out.add(d)
    return sorted(out)[:DATES_MAX]


def sender_label(guardian):
    return f'{guardian.full_name}（{guardian.get_relation_display()}）' if guardian.relation else guardian.full_name


def create_messages(facility, child, guardian, kind, dates, reason='', body=''):
    """
    保護者ページからの連絡を、日ごとに 1 件ずつ作る。
    同じ子・同じ日の欠席連絡がすでにあれば作らない（二重に送っても 1 件）。
    返り値：(作った連絡の並び, すでにあって作らなかった日付の並び)
    """
    body = (body or '').strip()[:FamilyMessage.BODY_MAX]
    today = timezone.localdate()
    made, skipped = [], []
    with transaction.atomic():
        for d in dates:
            if kind == FamilyMessage.KIND_ABSENCE and FamilyMessage.objects.filter(
                    facility=facility, beneficiary=child, kind=kind, target_date=d).exists():
                skipped.append(d)
                continue
            made.append(FamilyMessage.objects.create(
                facility=facility, beneficiary=child, line_user_id=guardian.line_user_id, sender_name=sender_label(guardian),
                kind=kind, via=FamilyMessage.VIA_PAGE, target_date=d, sent_on=today,
                reason=reason if kind == FamilyMessage.KIND_ABSENCE else '', body=body))
    return made, skipped


def message_from_line(facility, line_user_id, text):
    """
    公式 LINE のトークに直接書かれた文も「保護者からの連絡」に出す（保護者ページを使う事業所）。
    きょうだいがいる家庭はどの子のことか分からないので、子どもが 1 人のときだけ子どもに結びつける。
    """
    text = (text or '').strip()
    guardians = list(guardians_of(facility, line_user_id))
    if not text or not guardians:
        return None
    today = timezone.localdate()
    return FamilyMessage.objects.create(
        facility=facility, beneficiary=guardians[0].beneficiary if len(guardians) == 1 else None,
        line_user_id=line_user_id, sender_name=sender_label(guardians[0]),
        kind=FamilyMessage.KIND_OTHER, via=FamilyMessage.VIA_LINE, target_date=today, sent_on=today,
        body=text[:FamilyMessage.BODY_MAX])


def confirm_message(message, staff):
    """
    職員が「確認」を押した。欠席連絡なら、その日の来所予定を「欠席」にする（予定がある日だけ。変える前の状態を覚えておく）。
    返り値：来所予定を欠席に変えたら True
    """
    if message.is_confirmed:
        return False
    changed = False
    with transaction.atomic():
        message.confirmed_at = timezone.now()
        message.confirmed_by = staff
        message.visit_prev_status = ''
        if message.is_absence and message.beneficiary_id:
            visit = (ScheduledVisit.objects.select_for_update()
                     .filter(facility=message.facility, beneficiary=message.beneficiary, date=message.target_date).first())
            if visit is not None and visit.status != ScheduledVisit.STATUS_ABSENT:
                message.visit_prev_status = visit.status
                visit.status = ScheduledVisit.STATUS_ABSENT
                note = f'保護者から欠席連絡（{message.get_reason_display() or "理由なし"}）'
                if note not in (visit.notes or ''):
                    visit.notes = f'{visit.notes}\n{note}'.strip() if visit.notes else note
                visit.save(update_fields=['status', 'notes', 'updated_at'])
                changed = True
        message.save(update_fields=['confirmed_at', 'confirmed_by', 'visit_prev_status'])
    return changed


def unconfirm_message(message):
    """確認を取り消す（押しまちがい）。確認のときに来所予定を欠席に変えていたら、元の状態に戻す"""
    if not message.is_confirmed:
        return False
    restored = False
    with transaction.atomic():
        if message.visit_prev_status and message.beneficiary_id:
            visit = (ScheduledVisit.objects.select_for_update()
                     .filter(facility=message.facility, beneficiary=message.beneficiary, date=message.target_date).first())
            if visit is not None and visit.status == ScheduledVisit.STATUS_ABSENT:
                visit.status = message.visit_prev_status
                visit.save(update_fields=['status', 'updated_at'])
                restored = True
        message.confirmed_at = None
        message.confirmed_by = None
        message.visit_prev_status = ''
        message.save(update_fields=['confirmed_at', 'confirmed_by', 'visit_prev_status'])
    return restored


def unconfirmed_count(facility):
    return FamilyMessage.objects.filter(facility=facility, confirmed_at__isnull=True).count()


def inbox_for_day(facility, day):
    """
    職員の「保護者からの連絡」の 1 日ぶん。
      today_rows ：この日が対象の連絡（赤い字）。先の日付として届いてまだ確認していないものには未確認の印
      future_rows：この日に届いた、先の日付の連絡のうち、まだ確認していないもの（黒い字。確認すると対象日のページへ移る）
    """
    base = FamilyMessage.objects.filter(facility=facility).select_related('beneficiary', 'confirmed_by')
    today_rows = list(base.filter(target_date=day).order_by('created_at', 'pk'))
    future_rows = list(base.filter(sent_on=day, target_date__gt=day, confirmed_at__isnull=True).order_by('target_date', 'created_at', 'pk'))
    return today_rows, future_rows
