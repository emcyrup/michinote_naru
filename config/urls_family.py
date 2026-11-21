"""
保護者アプリだけを動かすときの URL（config/settings_family.py）。

職員向けの画面（ログイン・利用者・記録・請求・管理画面など）は、この URL には載せない。
"""
from django.http import HttpResponse
from django.urls import include, path


def healthz(request):
    """ヘルスチェック用（認証なし・DB に触らない）"""
    return HttpResponse('ok', content_type='text/plain')


urlpatterns = [
    path('healthz/', healthz, name='healthz'),
    path('', include('familyapp.urls')),
]

handler404 = 'familyapp.views.not_found'
