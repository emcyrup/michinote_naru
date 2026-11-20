"""
お知らせ（2026-11 事業所「成」の要望）。

職員：過去のお知らせの一覧 → 新規作成（タイトル・配信先＝全員／グループ／児童個別・本文）→ 下書き保存／送信。
保護者：保護者ページ「お知らせ」に、新しいものが上・古いものが下にたまっていく。
送信すると、宛先の子（NoticeRecipient）を控え、LINE を登録している保護者へ「お知らせが届きました」とリンクだけを知らせる（本文は流さない）。
"""
import logging

from django.db import transaction
from django.utils import timezone

from beneficiaries.models import Beneficiary, Guardian

from .models import Notice, NoticeGroup, NoticeRecipient

logger = logging.getLogger(__name__)


def active_children(facility):
    return Beneficiary.objects.filter(facility=facility, status=Beneficiary.STATUS_ACTIVE) \
        .order_by('last_name_kana', 'first_name_kana', 'pk')


def target_children(notice):
    """いまの設定での宛先の子（在籍中の子だけ）"""
    base = active_children(notice.facility)
    if notice.target_kind == Notice.TARGET_GROUP:
        return base.filter(notice_groups=notice.group) if notice.group_id else base.none()
    if notice.target_kind == Notice.TARGET_CHILDREN:
        return base.filter(pk__in=notice.children.values('pk'))
    return base


def target_label(notice, children=None):
    if notice.target_kind == Notice.TARGET_ALL:
        return '全員'
    if notice.target_kind == Notice.TARGET_GROUP:
        return f'グループ：{notice.group.name}' if notice.group_id else 'グループ（未選択）'
    children = list(children if children is not None else target_children(notice))
    if not children:
        return '児童個別（未選択）'
    first = children[0].full_name
    return f'{first} さん' if len(children) == 1 else f'{first} さん ほか {len(children) - 1} 人'


def clean(facility, post):
    """画面から届いた値を整える → (値, 問題の文の並び)。問題は送信のときだけ見る"""
    title = (post.get('title') or '').strip()[:Notice.TITLE_MAX]
    body = (post.get('body') or '').strip()[:Notice.BODY_MAX]
    kind = post.get('target_kind')
    if kind not in dict(Notice.TARGET_CHOICES):
        kind = Notice.TARGET_ALL
    group = None
    if kind == Notice.TARGET_GROUP:
        try:
            group = NoticeGroup.objects.filter(facility=facility, pk=int(post.get('group') or 0)).first()
        except ValueError:
            group = None
    ids = []
    if kind == Notice.TARGET_CHILDREN:
        ids = [int(v) for v in post.getlist('children') if str(v).isdigit()]
    children = list(active_children(facility).filter(pk__in=ids)) if ids else []
    problems = []
    if not title:
        problems.append('タイトルを書いてください。')
    if not body:
        problems.append('本文を書いてください。')
    if kind == Notice.TARGET_GROUP and group is None:
        problems.append('配信先のグループを選んでください。')
    if kind == Notice.TARGET_CHILDREN and not children:
        problems.append('配信先の子を選んでください。')
    return {'title': title, 'body': body, 'target_kind': kind, 'group': group, 'children': children}, problems


@transaction.atomic
def save_draft(facility, staff, values, notice=None):
    """下書きとして保存する（送信済みのものは書き換えない）"""
    if notice is None:
        notice = Notice(facility=facility, created_by=staff)
    elif notice.is_sent:
        return notice
    notice.title, notice.body = values['title'], values['body']
    notice.target_kind, notice.group = values['target_kind'], values['group']
    notice.save()
    notice.children.set(values['children'] if values['target_kind'] == Notice.TARGET_CHILDREN else [])
    return notice


@transaction.atomic
def send(notice, staff):
    """送信する：宛先の子を控えて、保護者ページに出す。宛先の人数を返す（0 人なら送らない）"""
    if notice.is_sent:
        return notice.recipients.count()
    children = list(target_children(notice))
    if not children:
        return 0
    NoticeRecipient.objects.bulk_create([NoticeRecipient(notice=notice, beneficiary=c) for c in children])
    notice.target_label = target_label(notice, children)[:120]
    notice.status, notice.sent_at, notice.sent_by = Notice.STATUS_SENT, timezone.now(), staff
    notice.save(update_fields=['target_label', 'status', 'sent_at', 'sent_by', 'updated_at'])
    return len(children)


def notify(notice, base):
    """宛先の保護者の LINE へ「お知らせが届きました」とリンクだけを送る（同じ LINE には 1 通）。(送れた人数, 送れなかった理由の並び)"""
    from line_integration import sending

    from . import services
    facility = notice.facility
    if not (facility.use_line and facility.use_family_page):
        return 0, []
    child_ids = list(notice.recipients.values_list('beneficiary_id', flat=True))
    seen, sent, errors = set(), 0, []
    for g in Guardian.objects.filter(beneficiary_id__in=child_ids, line_linked=True).exclude(line_user_id='') \
            .order_by('beneficiary__last_name_kana', 'beneficiary__first_name_kana', 'pk'):
        if g.line_user_id in seen:
            continue
        seen.add(g.line_user_id)
        access = services.access_for(facility, g.line_user_id)
        url = f'{services.enter_url(access, base)}?to=notice&c={g.beneficiary_id}'
        ok, why = sending.push_text(facility, g.line_user_id,
                                    f'{facility.name}からお知らせが届きました。\n「{notice.title}」\n\n下のリンクから読めます。\n{url}')
        if ok:
            sent += 1
        elif why not in errors:
            errors.append(why)
    if sent:
        Notice.objects.filter(pk=notice.pk).update(line_count=sent)
    return sent, errors


def feed(facility, child):
    """保護者ページに出すお知らせ（新しいものが上）"""
    return NoticeRecipient.objects.filter(beneficiary=child, notice__facility=facility, notice__status=Notice.STATUS_SENT) \
        .select_related('notice').order_by('-notice__sent_at', '-notice__pk')


def unread_count(facility, child):
    return feed(facility, child).filter(read_at__isnull=True).count()


def mark_read(facility, child, guardian):
    from . import services
    return feed(facility, child).filter(read_at__isnull=True).update(
        read_at=timezone.now(), read_by=services.sender_label(guardian)[:120])


def staff_list(facility):
    """職員の一覧（送信済みは新しい順。下書きは上に）。読んだ人数を付ける"""
    from django.db.models import Count, Q
    rows = list(Notice.objects.filter(facility=facility).select_related('group')
                .annotate(n_to=Count('recipients'), n_read=Count('recipients', filter=Q(recipients__read_at__isnull=False))))
    drafts = sorted((n for n in rows if not n.is_sent), key=lambda n: n.updated_at, reverse=True)
    sent = sorted((n for n in rows if n.is_sent), key=lambda n: n.sent_at, reverse=True)
    for n in drafts:
        n.label = target_label(n)
    return drafts, sent
