"""
ログインの一時ロックを、どの入口でも効かせる認証バックエンド（2026-11-22 セキュリティ診断）。

これまでロックを確かめていたのは、職員のログイン画面（accounts/views.py）だけだった。
スマホアプリ用の API（/api/mobile/v1/auth/login/）と Django の管理画面（/admin/login/）は確かめていなかったので、
そこからは職員のパスワードを何回でも試せた。ここで確かめれば、authenticate() を通る入口のすべてに効く。

ロック中は、正しいパスワードでも通さない（accounts/security.py の locked_until。ユーザー名・接続元の IP ごと）。
"""
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import PermissionDenied

from . import security


class LockAwareBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        name = username if username is not None else kwargs.get('username')
        if name and security.locked_until(str(name), security.client_ip(request)) is not None:
            # ほかのバックエンドにも回さない。失敗の合図（user_login_failed）は Django が出し、
            # accounts/security.py が「ロック中に試行」として記録する
            raise PermissionDenied('ログインの失敗が続いたため、しばらく受け付けません。')
        return super().authenticate(request, username=username, password=password, **kwargs)
