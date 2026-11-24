"""
保護者アプリ（別アプリ。メールアドレスとパスワードで入る）のアカウントまわり。

職員の側（利用者の画面の保護者の欄）と、保護者アプリの側の両方から、ここの関数を使う。
保護者アプリの作り方は docs/FAMILY_APP.md。

  職員の側
    link_guardian(guardian, staff)   その保護者のメールアドレスでアカウントを作る（同じアドレスのアカウントがあれば、そこへ結びつける）
    unlink_guardian(guardian)        結びつきを外す（どの子にも結びつかなくなったアカウントは止める）
    setup_url(account)               パスワードを決める画面のリンク（7 日・1 回かぎり）。保護者へ渡す
    send_setup_mail(account)         そのリンクをメールで送る（メールの設定がある環境だけ）
    set_active(account, on)          止める／また使えるようにする

  保護者アプリの側
    authenticate(email, password, facility=None)   入る。まちがいが続くと 15 分止まる
    check_setup_token(token)                       設定用のリンクの合い言葉を確かめる → アカウント
    set_password(account, raw)                     パスワードを決める（決まりに合わなければ ValidationError）
    guardians_of(account)                          結びついた保護者の記録（子どもごとに 1 件。在籍中の子だけ）
    session_context(account, child_id=None)        画面で使うひとそろい（事業所・子どもの一覧・いま見ている子・その保護者の記録）
"""
import datetime

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from beneficiaries.models import Guardian

from .models import FamilyAccount, FamilyAccountLink

SETUP_SALT = 'family.account.setup'
SETUP_DAYS = 7                 # 設定用のリンクが使える日数
MAX_FAILURES = 5               # 続けてこの回数まちがえると
LOCK_MINUTES = 15              # この分数だけ入れない
SETUP_PATH = '/account/setup/{token}/'   # 保護者アプリの側で用意する画面（docs/FAMILY_APP.md）


class AccountError(Exception):
    """職員の画面にそのまま出せる文つきの失敗"""


def normalize_email(value):
    return (value or '').strip().lower()


# ---------------------------------------------------------------- 職員の側
class NeedsConfirm(AccountError):
    """同じメールアドレスのアカウントがもうある。結びつける前に、職員に確かめてもらう（children＝いま見えている子の名前）"""

    def __init__(self, account, children):
        self.account, self.children = account, children
        super().__init__(f'このメールアドレス（{account.email}）のアカウントは、もうあります（{"・".join(children) or "子どもなし"} の保護者）。'
                         '同じ保護者（きょうだい）なら、もう一度「アカウントを作る」を押すと、同じアカウントに結びつきます。'
                         '別の家庭なら、メールアドレスの入れまちがいです。保護者の「編集」で直してください。')


def link_guardian(guardian, staff=None, confirm=False):
    """
    その保護者のメールアドレスで、保護者アプリのアカウントを用意して結びつける。
    同じ事業所に同じメールアドレスのアカウントがあれば、そこへ結びつける（きょうだい）。
    ほかの子にもう結びついているアカウントへは、confirm=True のときだけ結びつける（そうでなければ NeedsConfirm）：
    メールアドレスの入れまちがいで、別の家庭に連絡帳などが見えてしまうのを防ぐ。
    返り値：(アカウント, 新しく作ったか)
    """
    email = normalize_email(guardian.email)
    if not email:
        raise AccountError('メールアドレスが入っていません。保護者の「編集」でメールアドレスを入れてください。')
    try:
        validate_email(email)
    except ValidationError:
        raise AccountError('メールアドレスの形が正しくありません。保護者の「編集」で直してください。') from None
    facility = guardian.beneficiary.facility
    with transaction.atomic():
        link = FamilyAccountLink.objects.select_related('account').filter(guardian=guardian).first()
        if link is not None:
            if link.account.email == email:
                return link.account, False
            raise AccountError(f'この保護者は、すでに別のメールアドレス（{link.account.email}）のアカウントに結びついています。'
                               '変えるときは、いったん「アカウントを外す」を押してください。')
        account = FamilyAccount.objects.filter(facility=facility, email=email).first()
        created = account is None
        if created:
            account = FamilyAccount.objects.create(facility=facility, email=email, password=make_password(None), created_by=staff)
        elif not confirm:
            others = [link.guardian.beneficiary.full_name for link in
                      account.links.select_related('guardian__beneficiary').exclude(guardian__beneficiary=guardian.beneficiary)]
            if others:
                raise NeedsConfirm(account, others)
        FamilyAccountLink.objects.create(account=account, guardian=guardian)
    return account, created


def unlink_guardian(guardian):
    """結びつきを外す。どの保護者の記録にも結びつかなくなったアカウントは止める（消さない：送った記録に名前が残るため）"""
    link = FamilyAccountLink.objects.select_related('account').filter(guardian=guardian).first()
    if link is None:
        return None
    account = link.account
    with transaction.atomic():
        link.delete()
        if not account.links.exists() and account.is_active:
            account.is_active = False
            account.save(update_fields=['is_active'])
    return account


def set_active(account, on):
    account.is_active = bool(on)
    account.failed_count = 0
    account.locked_until = None
    account.save(update_fields=['is_active', 'failed_count', 'locked_until'])
    revoke_links(account)      # 止めたときも戻したときも、前に出したリンクは使えなくする
    return account


def _stamp(account):
    # パスワードを決める（変える）と変わる値と、リンクの版。合い言葉に混ぜて、1 回使ったリンク・出し直す前のリンクを使えなくする。
    # パスワードのハッシュそのものは入れない（合い言葉は署名つきだが、中身は読めるため）
    import hashlib
    return hashlib.sha256(f'{account.pk}:{account.link_version}:{account.password}'.encode()).hexdigest()[:24]


def revoke_links(account):
    """これまでに出したパスワード設定のリンクを、すべて使えなくする"""
    FamilyAccount.objects.filter(pk=account.pk).update(link_version=F('link_version') + 1)
    account.refresh_from_db(fields=['link_version'])
    return account


def setup_token(account):
    return signing.dumps({'a': account.pk, 's': _stamp(account)}, salt=SETUP_SALT)


def check_setup_token(token):
    """設定用のリンクの合い言葉を確かめる。使えるアカウントなら返す。期限切れ・使用ずみ・止めたアカウントは None"""
    try:
        data = signing.loads(token or '', salt=SETUP_SALT, max_age=datetime.timedelta(days=SETUP_DAYS))
    except signing.BadSignature:
        return None
    account = FamilyAccount.objects.select_related('facility').filter(pk=data.get('a'), is_active=True).first()
    if account is None or not signing.constant_time_compare(data.get('s') or '', _stamp(account)):
        return None
    return account


def app_base_url():
    """保護者アプリのアドレス（.env の FAMILY_APP_URL。末尾の / は除く）。未設定なら空"""
    return (getattr(settings, 'FAMILY_APP_URL', '') or '').rstrip('/')


def setup_url(account, base=None):
    """パスワードを決める画面のリンク。保護者アプリのアドレスが分からなければ空"""
    base = (base or app_base_url()).rstrip('/')
    if not base:
        return ''
    # 出し直すと、前のリンクは使えなくなる（使えるのは、最後に出した 1 本だけ）
    revoke_links(account)
    url = base + SETUP_PATH.format(token=setup_token(account))
    FamilyAccount.objects.filter(pk=account.pk).update(invited_at=timezone.now())
    return url


def send_setup_mail(account, base=None):
    """
    設定用のリンクをメールで送る。(送れたか, 送れなかった理由) を返す。
    メールの設定（.env の EMAIL_HOST）が無い環境では送らない（職員がリンクを写して渡す）。
    """
    if not getattr(settings, 'EMAIL_HOST', ''):
        return False, 'メールを送る設定がこの環境にありません'
    url = setup_url(account, base)
    if not url:
        return False, '保護者アプリのアドレスが設定されていません'
    name = account.facility.name
    body = (f'{name} の保護者アプリのご案内です。\n\n'
            f'下のリンクを開いて、パスワードを決めてください（{SETUP_DAYS} 日以内・1 回かぎり）。\n{url}\n\n'
            f'ログインに使うメールアドレス：{account.email}\n\n'
            'このメールに心当たりがないときは、そのまま破棄してください。\n')
    try:
        send_mail(f'【{name}】保護者アプリのパスワードの設定', body, None, [account.email], fail_silently=False)
    except Exception as e:  # noqa: BLE001  送れない理由を職員に見せる（アドレスまちがい・サーバーの不調）
        return False, f'メールを送れませんでした（{type(e).__name__}）'
    return True, ''


# ---------------------------------------------------------------- 保護者アプリの側
def set_password(account, raw):
    """パスワードを決める。短すぎる・ありふれている・メールアドレスに似ている などは ValidationError"""
    validate_password(raw, user=account)     # メールアドレスに似ていないかも見る
    account.password = make_password(raw)
    account.password_set_at = timezone.now()
    account.failed_count = 0
    account.locked_until = None
    account.save(update_fields=['password', 'password_set_at', 'failed_count', 'locked_until'])
    return account


# アカウントが無いときも同じだけ時間をかける（あるかないかを、応答の速さから読み取られないように）
_DUMMY_HASH = None


def _burn(password):
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = make_password('family-account-dummy')
    check_password(password or '', _DUMMY_HASH)


def authenticate(email, password, facility=None):
    """
    メールアドレスとパスワードで入る。合えばアカウント、合わなければ None。
    - 止めたアカウント・パスワードを決めていないアカウント・保護者の記録が 1 つも結びついていないアカウントは入れない
    - 続けて MAX_FAILURES 回まちがえると LOCK_MINUTES 分止まる（その間は、正しいパスワードでも入れない）
    - facility を渡さないとき、同じメールアドレスが複数の事業所にあれば、パスワードが合う 1 つだけを通す
    """
    email = normalize_email(email)
    qs = FamilyAccount.objects.select_related('facility').filter(email=email)
    if facility is not None:
        qs = qs.filter(facility=facility)
    candidates = list(qs)
    if not candidates or not password:
        _burn(password)
        return None
    matched = []
    now = timezone.now()
    for account in candidates:
        if account.is_locked:
            _burn(password)
            continue
        if not account.has_password:
            _burn(password)        # パスワード未設定のアカウントも、同じだけ時間をかける（招待ずみのアドレスかどうかを、応答の速さから読み取られないように）
        elif check_password(password, account.password):
            if account.is_active and account.links.exists():
                matched.append(account)
            continue
        # まちがいの回数は DB の中で足す（同時に送られても、数えもれが出ないように）
        FamilyAccount.objects.filter(pk=account.pk).update(failed_count=F('failed_count') + 1)
        account.refresh_from_db(fields=['failed_count'])
        if account.failed_count >= MAX_FAILURES:
            account.locked_until = now + datetime.timedelta(minutes=LOCK_MINUTES)
            account.failed_count = 0
            account.save(update_fields=['failed_count', 'locked_until'])
    if len(matched) != 1:
        return None
    account = matched[0]
    account.failed_count = 0
    account.locked_until = None
    account.last_login_at = now
    account.save(update_fields=['failed_count', 'locked_until', 'last_login_at'])
    return account


def guardians_of(account):
    """
    そのアカウントに結びついた保護者の記録（子どもごとに 1 件・在籍中の子だけ・子どもの名前の順）。
    同じ子の記録が 2 件結びついていても（利用者の重複をまとめたあとなど）、子ども 1 人につき 1 件だけ返す。
    """
    rows = (Guardian.objects.filter(family_link__account=account, beneficiary__facility=account.facility,
                                    beneficiary__status='active')
            .select_related('beneficiary').order_by('beneficiary__last_name_kana', 'beneficiary__first_name_kana', 'beneficiary_id', 'pk'))
    seen, out = set(), []
    for g in rows:
        if g.beneficiary_id not in seen:
            seen.add(g.beneficiary_id)
            out.append(g)
    return out


def session_context(account, child_id=None):
    """
    保護者アプリの画面で使うひとそろい。使えないアカウント（止めた・子どもがいない・保護者ページがオフ）は None。
    返り値の guardian を、family/services.py・family/yotei.py・family/notices.py の関数にそのまま渡せる。
    """
    if account is None or not account.is_active or not account.facility.use_family_page:
        return None
    guardians = guardians_of(account)
    if not guardians:
        return None
    guardian = next((g for g in guardians if g.beneficiary_id == child_id), guardians[0])
    return {'account': account, 'facility': account.facility, 'guardians': guardians,
            'children': [g.beneficiary for g in guardians], 'child': guardian.beneficiary, 'guardian': guardian}


def status_label(account):
    """職員の画面に出す、いまの状態"""
    if not account.is_active:
        return '停止中'
    if not account.has_password:
        return 'パスワード未設定'
    return '利用中'
