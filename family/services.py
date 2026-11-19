"""保護者ページの部品：だれの子か・リンクを作る・LINE で案内する"""
from django.urls import reverse

from beneficiaries.models import Guardian

from .models import FamilyAccess

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
