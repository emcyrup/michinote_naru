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
from .models import ContactBook, DayEvent, FamilyAccess, FamilyMessage, ParentNote, UseRequest

# 入場の印の有効期間（これを過ぎたら、LINE のリンクから入り直す）
SESSION_DAYS = 30

# 保護者のホームのアイコン（3 列）。作っていく順に、準備中から本物の画面へ差し替える
FAMILY_ICONS = [
    {'key': 'from_facility', 'label': '連絡帳\n{facility}より', 'icon': 'bi-journal-arrow-down', 'url': 'family:book_from'},
    {'key': 'to_facility', 'label': '連絡帳\n保護者より', 'icon': 'bi-journal-arrow-up', 'url': 'family:book_to'},
    {'key': 'absence', 'label': '欠席連絡', 'icon': '', 'text': '欠', 'url': 'family:absence'},
    {'key': 'message', 'label': 'その他の連絡', 'icon': 'bi-telephone', 'url': 'family:message'},
    {'key': 'jisseki', 'label': '実績記録票', 'icon': 'bi-clipboard-check'},
    {'key': 'request', 'label': '利用日申請', 'icon': 'bi-calendar3', 'url': 'family:calendar'},
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
        # LINE の知らせから来たとき：その子・その日の連絡帳を開く（決まった行き先だけ。外のアドレスへは送らない）
        if request.GET.get('to') == 'book':
            from django.urls import reverse
            try:
                child_pk = int(request.GET.get('c', ''))
            except ValueError:
                child_pk = None
            if child_pk in {g.beneficiary_id for g in services.guardians_of(access.facility, access.line_user_id)}:
                request.session['family_child'] = child_pk
            try:
                day = datetime.date.fromisoformat(request.GET.get('date', '')).isoformat()
            except ValueError:
                day = ''
            return _no_store(redirect(reverse('family:book_from') + (f'?date={day}' if day else '')))
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
        icons = _icons(self.facility)
        unread = services.unread_books(self.facility, self.child).count()
        for it in icons:
            it['badge'] = unread if it['key'] == 'from_facility' else 0
        return render(request, 'family/home.html', {**self.base_context(), 'icons': icons})


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


class BookFromView(FamilyMixin, View):
    """連絡帳（事業所より）：日ごとに読む。←→ で前の日・次の日。開くと、職員の画面に「確認済み」と出る"""

    def get(self, request):
        from schedules.daylist import day_nav, parse_day
        day = parse_day(request.GET.get('date'))
        book = ContactBook.objects.filter(facility=self.facility, beneficiary=self.child, date=day,
                                          status=ContactBook.STATUS_SENT).first()
        recent = list(ContactBook.objects.filter(facility=self.facility, beneficiary=self.child, status=ContactBook.STATUS_SENT)
                      .order_by('-date')[:14])
        return render(request, 'family/book_from.html', {**self.base_context(), **day_nav(day), 'book': book, 'recent': recent})

    def post(self, request):
        """開いたことの記録（画面が出たあとに画面のスクリプトが送る。リンクの下見（プレビュー）では付かない）"""
        from django.http import JsonResponse
        try:
            pk = int(request.POST.get('book', ''))
        except ValueError:
            pk = None
        book = ContactBook.objects.filter(pk=pk, facility=self.facility, beneficiary=self.child,
                                          status=ContactBook.STATUS_SENT).first()
        if book is None:
            return JsonResponse({'ok': False}, status=404)
        guardian = next(g for g in self.guardians if g.beneficiary_id == self.child.pk)
        services.mark_book_read(book, guardian)
        book.refresh_from_db()
        if request.headers.get('X-Requested-With') == 'fetch':
            return JsonResponse({'ok': True, 'read_at': timezone.localtime(book.read_at).strftime('%-m/%-d %H:%M')})
        from django.urls import reverse
        return redirect(f'{reverse("family:book_from")}?date={book.date.isoformat()}')


class BookToView(FamilyMixin, View):
    """連絡帳（保護者より）：その日の子どもの様子・伝えたいことを書いて送る。←→ で前の日・次の日"""

    def context(self, day, **extra):
        from schedules.daylist import day_nav
        notes = ParentNote.objects.filter(facility=self.facility, beneficiary=self.child, date=day)
        return {**self.base_context(), **day_nav(day), 'notes': notes, 'writable': services.note_writable(day),
                'body_max': ParentNote.BODY_MAX, **extra}

    def get(self, request):
        from schedules.daylist import parse_day
        day = parse_day(request.GET.get('date'))
        return render(request, 'family/book_to.html', self.context(day, sent=request.GET.get('sent') == '1'))

    def post(self, request):
        from django.urls import reverse
        from schedules.daylist import parse_day
        day = parse_day(request.POST.get('date'))
        body = (request.POST.get('body') or '').strip()
        if not services.note_writable(day):
            return render(request, 'family/book_to.html', self.context(day, error='この日の連絡帳は、もう書けません。'), status=400)
        if not body:
            return render(request, 'family/book_to.html', self.context(day, error='内容を書いてください。'), status=400)
        guardian = next(g for g in self.guardians if g.beneficiary_id == self.child.pk)
        services.add_parent_note(self.facility, self.child, guardian, day, body)
        return redirect(f'{reverse("family:book_to")}?date={day.isoformat()}&sent=1')


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


class StaffBookListView(LoginRequiredMixin, View):
    """連絡帳（日ごと）：その日に来る子だけ。送信済み・保護者の確認・保護者からの文の有無が一目で分かる"""

    def get(self, request):
        from schedules.daylist import day_nav, parse_day
        facility = request.user.facility
        if facility is None or not facility.use_family_page:
            raise Http404
        day = parse_day(request.GET.get('date'))
        rows, absent_rows, extra_rows = services.book_rows(facility, day)
        sent = sum(1 for r in rows if r['book'] and r['book'].is_sent)
        return render(request, 'family/staff_book_list.html', {
            **day_nav(day), 'rows': rows, 'absent_rows': absent_rows, 'extra_rows': extra_rows,
            'day_nav_note': f'送信済み {sent} / {len(rows)} 人',
        })


class StaffBookEditView(LoginRequiredMixin, View):
    """連絡帳を書く（1 人・1 日）：上に、保護者がその日に書いた文。下書き保存／送信"""

    def setup_objects(self, request, beneficiary_pk):
        from beneficiaries.models import Beneficiary
        from schedules.daylist import parse_day
        facility = request.user.facility
        if facility is None or not facility.use_family_page:
            raise Http404
        self.facility = facility
        self.child = get_object_or_404(Beneficiary, pk=beneficiary_pk, facility=facility)
        self.day = parse_day(request.POST.get('date') or request.GET.get('date'))

    def context(self, **extra):
        from schedules.daylist import day_nav
        from schedules.models import ScheduledVisit
        book = ContactBook.objects.filter(facility=self.facility, beneficiary=self.child, date=self.day).first()
        journal_text = ''
        try:
            from records.models import DailyRecord
            rec = DailyRecord.objects.filter(facility=self.facility, beneficiary=self.child, date=self.day).first()
            journal_text = (rec.parent_message_draft or '').strip() if rec else ''
        except Exception:  # noqa: BLE001
            journal_text = ''
        return {**day_nav(self.day), 'b': self.child, 'book': book,
                'notes': ParentNote.objects.filter(facility=self.facility, beneficiary=self.child, date=self.day),
                'visit': ScheduledVisit.objects.filter(facility=self.facility, beneficiary=self.child, date=self.day).first(),
                'line_linked': self.child.guardians.filter(line_linked=True).exists(),
                'journal_text': journal_text, 'body_max': ContactBook.BODY_MAX,
                'day_nav_query': '', **extra}

    def get(self, request, beneficiary_pk):
        self.setup_objects(request, beneficiary_pk)
        return render(request, 'family/staff_book_edit.html', self.context())

    def post(self, request, beneficiary_pk):
        from django.urls import reverse
        self.setup_objects(request, beneficiary_pk)
        body = (request.POST.get('body') or '').strip()
        send = request.POST.get('action') == 'send'
        if send and not body:
            return render(request, 'family/staff_book_edit.html', self.context(error='内容を書いてから送信してください。', draft=body), status=400)
        if not send:
            # 送信済みの連絡帳は、保存だけでは書き換えない（保護者に見えている文が、知らせなしに変わらないように）
            if ContactBook.objects.filter(facility=self.facility, beneficiary=self.child, date=self.day,
                                          status=ContactBook.STATUS_SENT).exists():
                messages.info(request, '送信済みの連絡帳は「送り直す」で差し替えてください（下書きとしては保存していません）。')
            else:
                services.save_book(self.facility, self.child, self.day, body, request.user, send=False)
                messages.success(request, '下書きを保存しました（保護者にはまだ見えません）。')
            return redirect(f'{reverse("family:staff_book_edit", args=[self.child.pk])}?date={self.day.isoformat()}')
        book, delivered = services.save_book(self.facility, self.child, self.day, body, request.user, send=True)
        if delivered:
            n, errors = services.notify_book(book, request.build_absolute_uri('/'))
            text = f'{self.child.full_name} さんの連絡帳を送信しました。'
            if n:
                text += f'保護者の LINE に知らせました（{n} 人）。'
            elif errors:
                text += f'LINE での知らせは送れませんでした（{errors[0]}）。保護者ページには出ています。'
            else:
                text += '公式 LINE につながっている保護者がいないので、LINE での知らせは送っていません。'
            messages.success(request, text)
        else:
            messages.info(request, '内容が変わっていないので、送り直していません。')
        return redirect(f'{reverse("family:staff_book")}?date={self.day.isoformat()}')


# =============================================
# 予定のカレンダーと利用日申請（family/yotei.py）
# =============================================
def _day_or_404(value):
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        raise Http404


class CalendarView(FamilyMixin, View):
    """保護者ページ「利用日申請」：職員の『予定』と同じカレンダー（見るだけ）。「利用日を申請する」で日にちを選んで送る"""

    def get(self, request):
        from django.urls import reverse
        from . import yotei
        year, month = yotei.parse_ym(request.GET.get('ym'))
        applying = request.GET.get('apply') == '1'
        window = yotei.parent_window()
        view = yotei.month_view(self.facility, year, month, child=self.child, window=window if applying else None)
        flash = request.session.pop('family_flash_request', None) if request.GET.get('done') == '1' else None
        if flash:
            flash = {k: [datetime.date.fromisoformat(v) for v in vs] for k, vs in flash.items()}
        return render(request, 'family/calendar.html', {
            **self.base_context(), **view, 'cal_apply': applying, 'cal_day_base': reverse('family:calendar') + 'day/',
            'cal_query': '&apply=1' if applying else '', 'window': window, 'flash': flash,
            'error': '日にちを選んでから送信してください。' if request.GET.get('err') == '1' else '',
            'can_pick_any': any(c.get('can_pick') for w in view['weeks'] for c in w),
        })

    def post(self, request):
        from django.urls import reverse
        from . import yotei
        year, month = yotei.parse_ym(request.POST.get('ym'))
        ym = f'{year}-{month:02d}'
        days = yotei.clean_days(request.POST.getlist('dates'), yotei.parent_window())
        if not days:
            return redirect(f'{reverse("family:calendar")}?ym={ym}&apply=1&err=1')
        guardian = next(g for g in self.guardians if g.beneficiary_id == self.child.pk)
        made, skipped = yotei.apply_days(self.facility, self.child, days, via='parent',
                                         line_user_id=self.access.line_user_id, sender_name=services.sender_label(guardian))
        request.session['family_flash_request'] = {'made': [d.isoformat() for d in made], 'skipped': [d.isoformat() for d in skipped]}
        return redirect(f'{reverse("family:calendar")}?ym={ym}&done=1')


class CalendarDayView(FamilyMixin, View):
    """保護者ページ：日付を押したときの画面（その日の予定・持ち物や参加費などの詳しい内容）"""

    def get(self, request, day):
        from . import yotei
        day = _day_or_404(day)
        return render(request, 'family/calendar_day.html', {**self.base_context(), **yotei.day_info(self.facility, day, self.child),
                                                            'ym': f'{day.year}-{day.month:02d}'})


class StaffCalendarMixin(LoginRequiredMixin):
    """職員の『予定』（カレンダー）：アイコンのホームか保護者ページを使う事業所だけ"""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            facility = getattr(request.user, 'facility', None)
            if facility is None or not (facility.home_icons or facility.use_family_page):
                raise Http404
            self.facility = facility
        return super().dispatch(request, *args, **kwargs)


class StaffCalendarView(StaffCalendarMixin, View):
    """予定：日曜始まりのカレンダー。「編集」で、日付ごとに休業日・活動の予定・お出かけ先を入れる。保護者ページにも同じものが出る"""

    def get(self, request):
        from django.urls import reverse
        from . import yotei
        year, month = yotei.parse_ym(request.GET.get('ym'))
        editing = request.GET.get('edit') == '1'
        view = yotei.month_view(self.facility, year, month)
        return render(request, 'family/staff_calendar.html', {
            **view, 'editing': editing, 'cal_day_base': reverse('family:staff_calendar') + 'day/', 'cal_query': '&edit=1' if editing else '',
            'days': [c for w in view['weeks'] for c in w if c['in_month']], 'title_max': DayEvent.TITLE_MAX,
            'groups': yotei.unseen_groups(self.facility) if self.facility.use_family_page else [],
        })

    def post(self, request):
        from django.urls import reverse
        from . import yotei
        year, month = yotei.parse_ym(request.POST.get('ym'))
        back = f'{reverse("family:staff_calendar")}?ym={year}-{month:02d}'
        if request.POST.get('action') == 'seen':
            n = yotei.mark_seen(self.facility, request.user)
            if n:
                messages.success(request, '利用日申請を確認済みにしました。')
            return redirect(back)
        first, last = yotei.month_span(year, month)
        changed, d = 0, first
        while d <= last:
            iso = d.isoformat()
            if f't_{iso}' in request.POST:
                changed += yotei.save_event(self.facility, d, request.POST.get(f't_{iso}', ''), f'c_{iso}' in request.POST,
                                            '', request.user, keep_detail=True)
            d += datetime.timedelta(days=1)
        messages.success(request, f'{changed} 日ぶんの予定を保存しました。' if changed else '変わったところはありません。')
        return redirect(back)


class StaffCalendarDayView(StaffCalendarMixin, View):
    """予定の 1 日ぶん：休業日・予定・詳しい内容（持ち物・参加費など）を書く。その日に来る子も出す"""

    def context(self, day, **extra):
        from schedules.daylist import day_visits
        from . import yotei
        coming, absent = day_visits(self.facility, day)
        requested = set(UseRequest.objects.filter(facility=self.facility, date=day).values_list('beneficiary_id', flat=True))
        for v in coming + absent:
            v.requested = v.beneficiary_id in requested
        return {**yotei.day_info(self.facility, day), 'coming': coming, 'absent': absent, 'ym': f'{day.year}-{day.month:02d}',
                'title_max': DayEvent.TITLE_MAX, 'detail_max': DayEvent.DETAIL_MAX, **extra}

    def get(self, request, day):
        return render(request, 'family/staff_calendar_day.html', self.context(_day_or_404(day)))

    def post(self, request, day):
        from django.urls import reverse
        from . import yotei
        day = _day_or_404(day)
        p = request.POST
        changed = yotei.save_event(self.facility, day, p.get('title', ''), 'is_closed' in p, p.get('detail', ''), request.user)
        messages.success(request, f'{day.month}月{day.day}日の予定を保存しました。' if changed else '変わったところはありません。')
        return redirect(f'{reverse("family:staff_calendar")}?ym={day.year}-{day.month:02d}')


class StaffApplyView(StaffCalendarMixin, View):
    """職員が代わりに利用日を申請する（保護者から口頭・紙で受けたとき）。保護者ページと同じように、カレンダーで日にちを選ぶ"""

    def children(self):
        from beneficiaries.models import Beneficiary
        return Beneficiary.objects.filter(facility=self.facility, status='active') \
            .order_by('last_name_kana', 'first_name_kana', 'pk')

    def child_of(self, value):
        try:
            return self.children().filter(pk=int(value)).first()
        except (TypeError, ValueError):
            return None

    def get(self, request):
        from . import yotei
        year, month = yotei.parse_ym(request.GET.get('ym'))
        child = self.child_of(request.GET.get('child'))
        view = yotei.month_view(self.facility, year, month, child=child, window=yotei.staff_window()) if child else \
            yotei.month_view(self.facility, year, month)
        return render(request, 'family/staff_calendar_apply.html', {
            **view, 'children': self.children(), 'child': child, 'cal_apply': bool(child),
            'cal_query': f'&child={child.pk}' if child else '',
        })

    def post(self, request):
        from django.urls import reverse
        from . import yotei
        year, month = yotei.parse_ym(request.POST.get('ym'))
        child = self.child_of(request.POST.get('child'))
        if child is None:
            raise Http404
        back = f'{reverse("family:staff_apply")}?ym={year}-{month:02d}&child={child.pk}'
        days = yotei.clean_days(request.POST.getlist('dates'), yotei.staff_window())
        if not days:
            messages.error(request, '日にちを選んでから送信してください。')
            return redirect(back)
        made, skipped = yotei.apply_days(self.facility, child, days, via='staff', staff=request.user)
        label = '・'.join(f'{d.month}/{d.day}' for d in made)
        if made:
            messages.success(request, f'{child.full_name} さんの利用日を {len(made)} 日ぶん入れました（{label}）。')
        if skipped:
            messages.warning(request, f'{len(skipped)} 日ぶんは入れていません（休業日か、すでに予定のある日）。')
        return redirect(back)
