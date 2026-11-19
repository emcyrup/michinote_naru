"""
保護者ページ（公式 LINE から開く）。

入り方：LINE に届いたリンク /family/enter/<合い言葉>/ を開く → この端末に入場の印（セッション）を付けて /family/ へ移す。
以後のアドレスには合い言葉を出さない（履歴・画面の写真・リンク先への送り元から漏れにくくする）。
職員のログインとは別の印なので、同じ端末で職員としてログインしていても混ざらない。
"""
import datetime

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from beneficiaries.models import Guardian
from line_integration.sending import push_text

from . import services
from .models import FamilyAccess

# 入場の印の有効期間（これを過ぎたら、LINE のリンクから入り直す）
SESSION_DAYS = 30

# 保護者のホームのアイコン（3 列）。作っていく順に、準備中から本物の画面へ差し替える
FAMILY_ICONS = [
    {'key': 'from_facility', 'label': '連絡帳\n{facility}より', 'icon': 'bi-journal-arrow-down'},
    {'key': 'to_facility', 'label': '連絡帳\n保護者より', 'icon': 'bi-journal-arrow-up'},
    {'key': 'absence', 'label': '欠席連絡', 'icon': '', 'text': '欠'},
    {'key': 'message', 'label': 'その他の連絡', 'icon': 'bi-telephone'},
    {'key': 'jisseki', 'label': '実績記録票', 'icon': 'bi-clipboard-check'},
    {'key': 'request', 'label': '利用日申請', 'icon': 'bi-calendar3'},
    {'key': 'oshirase', 'label': 'お知らせ', 'icon': 'bi-envelope'},
    {'key': 'plan', 'label': '個別支援計画', 'icon': 'bi-file-earmark-text'},
]


def _no_store(response):
    """保護者ページは端末・中継に残さない。リンク先へ送り元（アドレス）も渡さない"""
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


class EnterView(View):
    """LINE のリンクから入る"""

    def get(self, request, token):
        access = FamilyAccess.objects.select_related('facility').filter(token=token).first()
        if (access is None or not access.facility.use_family_page or not access.facility.use_line
                or not services.guardians_of(access.facility, access.line_user_id).exists()):
            return _no_store(render(request, 'family/closed.html', {'reason': 'link'}, status=404))
        # 入る前からあった印（ほかの人が仕込んだもの）を引き継がないよう、セッションの番号を付け替える
        request.session.cycle_key()
        request.session[services.SESSION_KEY] = {'id': access.pk, 'token': access.token,
                                                 'at': timezone.now().isoformat()}
        request.session.pop('family_child', None)
        FamilyAccess.objects.filter(pk=access.pk).update(last_used_at=timezone.now())
        return _no_store(redirect('family:home'))


class FamilyMixin:
    """入場の印を確かめて、保護者（access）・事業所・子ども（children／child）をそろえる"""

    def dispatch(self, request, *args, **kwargs):
        data = request.session.get(services.SESSION_KEY) or {}
        access = FamilyAccess.objects.select_related('facility').filter(pk=data.get('id')).first() if data.get('id') else None
        ok = access is not None and access.token == data.get('token') and access.facility.use_family_page and access.facility.use_line
        if ok:
            try:
                entered = datetime.datetime.fromisoformat(data.get('at', ''))
                ok = timezone.now() - entered < datetime.timedelta(days=SESSION_DAYS)
            except (TypeError, ValueError):
                ok = False
        guardians = list(services.guardians_of(access.facility, access.line_user_id)) if ok else []
        if not ok or not guardians:
            request.session.pop(services.SESSION_KEY, None)
            return _no_store(render(request, 'family/closed.html', {'reason': 'session'}, status=403))
        self.access = access
        self.facility = access.facility
        self.guardians = guardians
        self.children = [g.beneficiary for g in guardians]
        chosen = request.session.get('family_child')
        self.child = next((c for c in self.children if c.pk == chosen), self.children[0])
        return _no_store(super().dispatch(request, *args, **kwargs))

    def base_context(self):
        return {'facility': self.facility, 'children': self.children, 'child': self.child,
                'guardian': next(g for g in self.guardians if g.beneficiary_id == self.child.pk)}


def _icons(facility):
    return [{**it, 'label': it['label'].format(facility=facility.name)} for it in FAMILY_ICONS]


class HomeView(FamilyMixin, View):
    def get(self, request):
        return render(request, 'family/home.html', {**self.base_context(), 'icons': _icons(self.facility)})


class ChildSelectView(FamilyMixin, View):
    """きょうだいがいるとき：どの子の画面を見るかを切り替える"""

    def post(self, request):
        try:
            pk = int(request.POST.get('child', ''))
        except ValueError:
            pk = None
        if pk in {c.pk for c in self.children}:
            request.session['family_child'] = pk
        return redirect('family:home')


class PageView(FamilyMixin, View):
    """アイコンの先の画面。まだ作っていないものは「準備中」"""

    def get(self, request, key):
        item = next((it for it in _icons(self.facility) if it['key'] == key), None)
        if item is None:
            raise Http404
        return render(request, 'family/soon.html', {**self.base_context(), 'item': item})


class LeaveView(View):
    """この端末の入場の印を消す（共用の端末で開いたとき）"""

    def post(self, request):
        request.session.pop(services.SESSION_KEY, None)
        request.session.pop('family_child', None)
        return _no_store(render(request, 'family/closed.html', {'reason': 'left'}))


# ---------------------------------------------------------------- 職員の画面から
class StaffGuardianMixin(LoginRequiredMixin):
    def guardian(self, request, guardian_pk):
        g = get_object_or_404(Guardian.objects.select_related('beneficiary'), pk=guardian_pk,
                              beneficiary__facility=request.user.facility)
        return g

    def back(self, g):
        return redirect('beneficiaries:detail', pk=g.beneficiary_id)


class StaffSendLinkView(StaffGuardianMixin, View):
    """保護者ページのリンクを、その保護者の LINE へ送る"""

    def post(self, request, guardian_pk):
        g = self.guardian(request, guardian_pk)
        facility = request.user.facility
        text = services.menu_reply(facility, g.line_user_id, request.build_absolute_uri('/')) if g.line_linked else None
        if text is None:
            messages.error(request, '保護者ページのリンクを送れません（施設設定の「保護者ページ」がオフか、この保護者が公式 LINE につながっていません）。')
            return self.back(g)
        ok, why = push_text(facility, g.line_user_id, text)
        if ok:
            messages.success(request, f'{g.full_name} さんの LINE に、保護者ページのリンクを送りました。')
        else:
            messages.error(request, f'LINE で送れませんでした：{why}')
        return self.back(g)


class StaffRotateLinkView(StaffGuardianMixin, View):
    """保護者ページのリンクを作り直す（前のリンクは開けなくなる。機種変更・リンクを人に送ってしまったとき）"""

    def post(self, request, guardian_pk):
        g = self.guardian(request, guardian_pk)
        access = services.access_for(request.user.facility, g.line_user_id) if g.line_linked else None
        if access is None:
            messages.error(request, '作り直すリンクがありません（保護者ページがオフか、公式 LINE につながっていません）。')
            return self.back(g)
        access.rotate()
        messages.success(request, f'{g.full_name} さんの保護者ページのリンクを作り直しました。前のリンクは開けません。「リンクを LINE で送る」で新しいリンクを届けてください。')
        return self.back(g)
