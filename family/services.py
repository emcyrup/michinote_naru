"""保護者ページの部品：だれの子か・リンクを作る・LINE で案内する"""
import datetime

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from beneficiaries.models import Guardian
from schedules.models import ScheduledVisit

from .models import ContactBook, FamilyAccess, FamilyMessage, ParentNote

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


# ---------------------------------------------------------------- 連絡帳
# 保護者が「連絡帳（保護者より）」を書ける日：NOTE_BACK_DAYS 日前から NOTE_AHEAD_DAYS 日先まで（それより外は読むだけ）
NOTE_BACK_DAYS = 7
NOTE_AHEAD_DAYS = 14


def note_writable(day):
    today = timezone.localdate()
    return today - datetime.timedelta(days=NOTE_BACK_DAYS) <= day <= today + datetime.timedelta(days=NOTE_AHEAD_DAYS)


def book_url(access, base, child, day):
    """LINE の知らせに貼るアドレス：入ってそのまま、その子・その日の「連絡帳（事業所より）」を開く"""
    return f'{enter_url(access, base)}?to=book&c={child.pk}&date={day.isoformat()}'


def save_book(facility, child, day, body, staff, send=False):
    """
    連絡帳（事業所より）を保存する。send=True で保護者に見えるようにする（送り直すと、保護者の確認は付け直しになる）。
    返り値：(連絡帳, 今回の操作で新しく保護者に届いたか)
    """
    body = (body or '').strip()[:ContactBook.BODY_MAX]
    with transaction.atomic():
        book, _ = ContactBook.objects.select_for_update().get_or_create(
            facility=facility, beneficiary=child, date=day)
        changed = book.body != body
        book.body = body
        book.updated_by = staff
        delivered = False
        if send:
            if not book.is_sent or changed:
                book.status = ContactBook.STATUS_SENT
                book.sent_at = timezone.now()
                book.sent_by = staff
                book.read_at = None
                book.read_by = ''
                book.line_notified = False
                delivered = True
        book.save()
    return book, delivered


def notify_book(book, base):
    """
    連絡帳が届いたことを、その子の保護者の LINE に知らせる（文そのものは送らず、保護者ページのリンクだけ）。
    返り値：(知らせた人数, 送れなかった理由の並び)
    """
    from line_integration.sending import push_text
    facility = book.facility
    sent, errors = 0, []
    if not (facility.use_line and facility.use_family_page):
        return sent, errors
    line_ids = []
    for g in Guardian.objects.filter(beneficiary=book.beneficiary, line_linked=True).exclude(line_user_id=''):
        if g.line_user_id not in line_ids:
            line_ids.append(g.line_user_id)
    for line_id in line_ids:
        access = access_for(facility, line_id)
        if access is None:
            continue
        text = (f'{book.beneficiary.full_name}さんの連絡帳（{book.date.month}月{book.date.day}日）が届きました。\n'
                f'{book_url(access, base, book.beneficiary, book.date)}')
        ok, why = push_text(facility, line_id, text)
        if ok:
            sent += 1
        else:
            errors.append(why)
    if sent:
        ContactBook.objects.filter(pk=book.pk).update(line_notified=True)
    return sent, errors


def mark_book_read(book, guardian):
    """保護者がその日の連絡帳を開いた（最初の 1 回だけ記録する）"""
    if book.is_sent and not book.is_read:
        ContactBook.objects.filter(pk=book.pk, read_at__isnull=True).update(read_at=timezone.now(), read_by=sender_label(guardian))
        return True
    return False


def unread_books(facility, child):
    """その子の、送信ずみでまだ保護者が開いていない連絡帳（新しい順）"""
    return ContactBook.objects.filter(facility=facility, beneficiary=child, status=ContactBook.STATUS_SENT,
                                      read_at__isnull=True).order_by('-date')


def add_parent_note(facility, child, guardian, day, body):
    body = (body or '').strip()[:ParentNote.BODY_MAX]
    if not body:
        return None
    return ParentNote.objects.create(facility=facility, beneficiary=child, date=day, body=body,
                                     line_user_id=guardian.line_user_id, sender_name=sender_label(guardian))


def book_rows(facility, day):
    """
    職員の「連絡帳」の 1 日ぶん：その日に来る子（coming）・欠席の子（absent）・予定に無いが連絡帳や保護者の文がある子（extra）。
    各行に、連絡帳（book）と、保護者がその日に書いた文の数（notes）を付ける。
    """
    from schedules.daylist import day_visits
    coming, absent = day_visits(facility, day)
    books = {b.beneficiary_id: b for b in ContactBook.objects.filter(facility=facility, date=day).select_related('beneficiary')}
    notes = {}
    for n in ParentNote.objects.filter(facility=facility, date=day).select_related('beneficiary'):
        notes.setdefault(n.beneficiary_id, []).append(n)

    def row(child, visit):
        return {'beneficiary': child, 'visit': visit, 'book': books.get(child.pk), 'notes': len(notes.get(child.pk, []))}

    listed = {v.beneficiary_id for v in coming} | {v.beneficiary_id for v in absent}
    extra = {}
    for b_id, b in books.items():
        if b_id not in listed and (b.is_sent or b.body):
            extra[b_id] = b.beneficiary
    for b_id, ns in notes.items():
        if b_id not in listed:
            extra[b_id] = ns[0].beneficiary
    extra_rows = [row(c, None) for c in sorted(extra.values(), key=lambda c: (c.last_name_kana, c.first_name_kana, c.pk))]
    return [row(v.beneficiary, v) for v in coming], [row(v.beneficiary, v) for v in absent], extra_rows
