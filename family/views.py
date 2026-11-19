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
from .models import FamilyAccess, FamilyMessage

# 入場の印の有効期間（これを過ぎたら、LINE のリンクから入り直す）
SESSION_DAYS = 30

# 保護者のホームのアイコン（3 列）。作っていく順に、準備中から本物の画面へ差し替える
FAMILY_ICONS = [
    {'key': 'from_facility', 'label': '連絡帳\n{facility}より', 'icon': 'bi-journal-arrow-down'},
    {'key': 'to_facility', 'label': '連絡帳\n保護者より', 'icon': 'bi-journal-arrow-up'},
    {'key': 'absence', 'label': '欠席連絡', 'icon': '', 'text': '欠', 'url': 'family:absence'},
    {'key': 'message', 'label': 'その他の連絡', 'icon': 'bi-telephone', 'url': 'family:message'},
    {'key': 'jisseki', 'label': '実績記録票', 'icon': 'bi-clipboard-check'},
    {'key': 'request', 'label': '利用日申請', 'icon': 'bi-calendar3'},
    {'key': 'oshirase', 'label': 'お知らせ', 'icon': 'bi-envelope'},
    {'key': 'plan', 'label': '個別支援計画', 'icon': 'bi-file-earmark-text'},
]


def _no_store(response):
    """
    保護者ページは端末・中継に残さない。ほかのサイトへは送り元（アドレス）を渡さない。
    no-referrer にすると、ブラウザが同じサイト内の送信（POST）にも Origin: null を付け、Django の CSRF の確認で
    403 になる（2026-11-20 実機で確認）。same-origin なら、同じサイト内だけに送り元を付け、外へは何も渡さない
    """
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'same-origin'
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
    from django.urls import reverse
    out = []
    for it in FAMILY_ICONS:
        url = it.get('url')
        out.append({**it, 'label': it['label'].format(facility=facility.name), 'soon': not url,
                    'href': reverse(url) if url else reverse('family:page', args=[it['key']])})
    return out


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
        if not item['soon']:
            return redirect(item['href'])
        return render(request, 'family/soon.html', {**self.base_context(), 'item': item})


class ContactFormView(FamilyMixin, View):
    """欠席連絡・その他の連絡：日にちを選ぶ（きょう／カレンダーから複数）→ 内容 → 送信。下に、これまでに送った連絡と確認の状態"""
    kind = FamilyMessage.KIND_OTHER
    template_name = 'family/contact_form.html'

    def context(self, **extra):
        first, last = services.date_range()
        sent = (FamilyMessage.objects.filter(facility=self.facility, beneficiary=self.child, kind=self.kind,
                                             line_user_id=self.access.line_user_id, via=FamilyMessage.VIA_PAGE,
                                             target_date__gte=first - datetime.timedelta(days=14))
                .order_by('-target_date', '-created_at')[:30])
        return {**self.base_context(), 'kind': self.kind, 'is_absence': self.kind == FamilyMessage.KIND_ABSENCE,
                'today': first, 'last_day': last, 'dates_max': services.DATES_MAX, 'body_max': FamilyMessage.BODY_MAX,
                'reasons': FamilyMessage.REASON_CHOICES, 'sent': sent, **extra}

    def get(self, request):
        flash = request.session.pop('family_flash', None) if request.GET.get('done') == '1' else None
        if flash:
            flash = {k: [datetime.date.fromisoformat(v) for v in vs] for k, vs in flash.items()}
        return render(request, self.template_name, self.context(flash=flash))

    def post(self, request):
        p = request.POST
        values = p.getlist('dates')
        if p.get('use_today') == '1':
            values.append(timezone.localdate().isoformat())
        dates = services.clean_dates(values)
        reason = p.get('reason', '')
        body = (p.get('body') or '').strip()
        errors = []
        if not dates:
            errors.append('日にちを選んでください。')
        if self.kind == FamilyMessage.KIND_ABSENCE and reason not in dict(FamilyMessage.REASON_CHOICES):
            errors.append('欠席の理由（病欠・都合欠）を選んでください。')
        if self.kind == FamilyMessage.KIND_OTHER and not body:
            errors.append('連絡の内容を書いてください。')
        if errors:
            return render(request, self.template_name, self.context(
                errors=errors, form={'dates': [d.isoformat() for d in dates], 'reason': reason, 'body': body}), status=400)
        guardian = next(g for g in self.guardians if g.beneficiary_id == self.child.pk)
        made, skipped = services.create_messages(self.facility, self.child, guardian, self.kind, dates, reason=reason, body=body)
        request.session['family_flash'] = {'made': [d.isoformat() for d in (m.target_date for m in made)],
                                           'skipped': [d.isoformat() for d in skipped]}
        return redirect(request.path + '?done=1')


class AbsenceView(ContactFormView):
    kind = FamilyMessage.KIND_ABSENCE


class MessageView(ContactFormView):
    kind = FamilyMessage.KIND_OTHER


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


class StaffInboxView(LoginRequiredMixin, View):
    """保護者からの連絡（日ごと）。この日が対象の連絡は赤い字、この日に届いた先の日付の連絡は黒い字で、確認すると対象日へ移る"""

    def get(self, request):
        from schedules.daylist import day_nav, parse_day
        facility = request.user.facility
        if facility is None or not facility.use_family_page:
            raise Http404
        day = parse_day(request.GET.get('date'))
        today_rows, future_rows = services.inbox_for_day(facility, day)
        # ほかの日の未確認（見落とさないよう、日付ごとの件数を上に出す）
        from django.db.models import Count
        pending = list(FamilyMessage.objects.filter(facility=facility, confirmed_at__isnull=True)
                       .values('sent_on').annotate(n=Count('pk')).order_by('sent_on'))
        shown = {m.pk for m in today_rows if not m.is_confirmed} | {m.pk for m in future_rows}
        other_days = [{'day': r['sent_on'], 'n': r['n']} for r in pending if r['sent_on'] != day]
        names = {}
        for m in today_rows + future_rows:
            if m.beneficiary_id is None and m.line_user_id not in names:
                names[m.line_user_id] = '・'.join(g.beneficiary.full_name for g in services.guardians_of(facility, m.line_user_id))
            m.children_label = names.get(m.line_user_id, '') if m.beneficiary_id is None else ''
        return render(request, 'family/staff_inbox.html', {
            **day_nav(day), 'today_rows': today_rows, 'future_rows': future_rows, 'other_days': other_days,
            'pending_here': len(shown), 'day_nav_note': f'未確認 {len(shown)} 件' if shown else '',
        })


class StaffConfirmView(LoginRequiredMixin, View):
    """確認する／確認を取り消す"""

    def post(self, request, pk):
        from schedules.daylist import parse_day
        facility = request.user.facility
        message = get_object_or_404(FamilyMessage, pk=pk, facility=facility)
        back = parse_day(request.POST.get('date'))
        if request.POST.get('undo') == '1':
            restored = services.unconfirm_message(message)
            messages.info(request, '確認を取り消しました。' + ('来所予定を元の状態に戻しました。' if restored else ''))
        else:
            was_future = message.target_date > back
            changed = services.confirm_message(message, request.user)
            text = '確認しました。保護者ページに「確認済み」と出ます。'
            if changed:
                text += f'{message.target_date.month}月{message.target_date.day}日の来所予定を「欠席」にしました。'
            elif message.is_absence and message.beneficiary_id:
                text += 'この日の来所予定は無い（または欠席になっている）ので、予定は変えていません。'
            if was_future:
                text += f'この連絡は {message.target_date.month}月{message.target_date.day}日 のページに移りました。'
            messages.success(request, text)
        from django.urls import reverse
        return redirect(f'{reverse("family:staff_inbox")}?date={back.isoformat()}')
