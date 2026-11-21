"""
保護者アプリ（別アプリ。メールアドレスとパスワードで入る）として動かすときの設定。

職員向けと同じコード・同じ DB のまま、外から見える画面を保護者用だけに絞る（URL を config/urls_family.py に替える）。
  手元：python manage.py runserver 127.0.0.1:8001 --settings config.settings_family
  配備：環境変数 DJANGO_SETTINGS_MODULE=config.settings_family で Gunicorn を起動する
"""
from .settings import *  # noqa: F401,F403

FAMILY_APP = True
ROOT_URLCONF = 'config.urls_family'

# 同じ端末・同じホスト名（手元の localhost など）で職員向けと並べて開いても、ログインの印が混ざらないように名前を分ける
SESSION_COOKIE_NAME = 'family_sessionid'
CSRF_COOKIE_NAME = 'family_csrftoken'
