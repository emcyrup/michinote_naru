"""
保護者アプリのログインの印（セッション）。

セッションに入れるのは、アカウントの番号・いま見ている子の番号・パスワードの印だけ。
画面のたびに family.accounts.session_context() を呼び直すので、職員がアカウントを止める・子どもを外すと、次の操作から入れなくなる。
パスワードが変わると印が合わなくなり、ほかの端末のログインも切れる。
"""
from django.utils.crypto import constant_time_compare, salted_hmac

from family import accounts
from family.models import FamilyAccount

SESSION_ACCOUNT = 'family_account'
SESSION_CHILD = 'family_child'
SESSION_STAMP = 'family_stamp'
SESSION_DAYS = 30          # ログインが続く日数（過ぎたら入り直す）


def no_store(response):
    """保護者の画面は端末・中継に残さない。ほかのサイトへは送り元（アドレス）を渡さない（family/views.py の _no_store と同じ）"""
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'same-origin'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


def _stamp(account):
    return salted_hmac('familyapp.session', account.password).hexdigest()


def login(request, account):
    # 入る前からあった印（ほかの人が仕込んだもの）を引き継がないよう、セッションの番号を付け替える
    request.session.cycle_key()
    request.session[SESSION_ACCOUNT] = account.pk
    request.session[SESSION_STAMP] = _stamp(account)
    request.session.pop(SESSION_CHILD, None)
    request.session.set_expiry(SESSION_DAYS * 24 * 60 * 60)


def logout(request):
    request.session.flush()


def current(request):
    """
    いまのログインで使うひとそろい（accounts.session_context の返り値）。入っていない・使えなくなったときは None。
    子どもは必ずこの返り値の中から選ぶ（URL やフォームで受けた番号は、そのまま使わない）。
    """
    pk = request.session.get(SESSION_ACCOUNT)
    if not pk:
        return None
    account = FamilyAccount.objects.select_related('facility').filter(pk=pk).first()
    if account is None or not constant_time_compare(request.session.get(SESSION_STAMP) or '', _stamp(account)):
        return None
    return accounts.session_context(account, child_id=request.session.get(SESSION_CHILD))
