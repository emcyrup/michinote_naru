"""
保護者アプリ（メールアドレスとパスワードで入る。config/settings_family.py で動かす）の画面。

データと業務の決まりは職員向け（main）の側にある。ここでは表（モデル）に直接書き込まず、
family/accounts.py・services.py・yotei.py・notices.py の関数を通す（docs/FAMILY_APP.md）。
"""
import datetime

from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from facilities.models import Facility
from family import accounts, notices, services, yotei
from family.models import ContactBook, FamilyMessage, ParentNote
from schedules.daylist import day_nav, parse_day

from . import auth

# 連絡帳（成より）の「確認済み」を付ける時：True＝開いたら付く／False＝「確認しました」を押したら付く（成に確認中。いまは見本と同じ「開いたら」）
BOOK_READ_ON_OPEN = True

# ホームのアイコン（3 列。この順に左上から並ぶ。2026-11-24 成の要望）
ICONS = [
    {'key': 'to_facility', 'label': '連絡帳\n保護者より', 'icon': 'bi-journal-arrow-up', 'url': 'familyapp:book_to'},
    {'key': 'from_facility', 'label': '連絡帳\n{facility}より', 'icon': 'bi-journal-arrow-down', 'url': 'familyapp:book_from'},
    {'key': 'oshirase', 'label': 'お知らせ', 'icon': 'bi-envelope', 'url': 'familyapp:notice'},
    {'key': 'absence', 'label': '欠席連絡', 'icon': '', 'text': '欠', 'url': 'familyapp:absence'},
    {'key': 'message', 'label': 'その他の連絡', 'icon': 'bi-telephone', 'url': 'familyapp:message'},
    {'key': 'request', 'label': '利用日申請', 'icon': 'bi-calendar3', 'url': 'familyapp:calendar'},
    {'key': 'jisseki', 'label': '実績記録票', 'icon': 'bi-clipboard-check', 'url': 'familyapp:record', 'soon': True},
    {'key': 'plan', 'label': '個別支援計画', 'icon': 'bi-file-earmark-text', 'url': 'familyapp:plan'},
]


# ---------------------------------------------------------------- 利用日申請の締め切り（2026-11-24 成の要望）
# ある月の利用日は、その前の月の末日までに申請する。保護者から申請できるのは「来月の 1 日」から（いまの月の残りの日は締め切りずみ）。
# 職員の代理申請（family/views.py の StaffApplyView）には効かない。先の端は、これまでどおり yotei.parent_window() の終わり。
def _first_of_next_month(today):
    return (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)


def request_window():
    """保護者が申請できる日の範囲 (はじめ, おわり)"""
    today = timezone.localdate()
    first, last = yotei.parent_window()
    return max(first, _first_of_next_month(today)), last


# 「利用日申請をしてください」の印を出しはじめる日（毎月この日から月末まで。2026-11-25 成の要望）
REQUEST_NOTICE_FROM_DAY = 25


def request_due(facility, child):
    """
    来月分の利用日申請がまだなら、その月の 1 日（datetime.date）。済んでいれば None。
    「まだ」＝来月の利用の予定（欠席を含む）が 1 日も無い。職員が代理で入れたものも「済み」に数える。
    印を出すのは毎月 25 日から（それより前は、まだなら None）
    """
    from schedules.models import ScheduledVisit
    today = timezone.localdate()
    if today.day < REQUEST_NOTICE_FROM_DAY:
        return None
    first = _first_of_next_month(today)
    last = _first_of_next_month(first) - datetime.timedelta(days=1)
    if ScheduledVisit.objects.filter(facility=facility, beneficiary=child, date__range=(first, last)).exists():
        return None
    return first


def _safe_next(request, value):
    """ログインのあとに戻る先。このサイトの中のアドレスだけを通す"""
    if value and url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()}, require_https=request.is_secure()) \
            and value.startswith('/') and not value.startswith('//'):
        return value
    return ''


def not_found(request, exception=None):
    return auth.no_store(render(request, 'familyapp/message.html', {
        'title': 'ページが見つかりません', 'text': 'アドレスが変わったか、途中で切れています。', 'link': reverse('familyapp:home'),
        'link_label': 'ホームへ'}, status=404))


def manifest(request):
    """ホーム画面に追加したときの名前とアイコン。事業所が 1 つの環境（成）では、その名前を出す"""
    names = list(Facility.objects.filter(use_family_page=True).values_list('name', flat=True)[:2])
    name = names[0] if len(names) == 1 else '保護者ページ'
    from django.templatetags.static import static
    response = JsonResponse({
        'name': f'{name} 保護者ページ' if len(names) == 1 else name, 'short_name': name[:12], 'lang': 'ja',
        'start_url': '/', 'scope': '/', 'display': 'standalone', 'background_color': '#f6f4ef', 'theme_color': '#4e7ea8',
        'icons': [{'src': static(f'familyapp/icon-{n}.png'), 'sizes': f'{n}x{n}', 'type': 'image/png', 'purpose': 'any maskable'}
                  for n in (192, 512)],
    }, json_dumps_params={'ensure_ascii': False})
    response['Content-Type'] = 'application/manifest+json; charset=utf-8'
    return response


# ---------------------------------------------------------------- 入る・出る
class LoginView(View):
    template_name = 'familyapp/login.html'

    def get(self, request):
        if auth.current(request) is not None:
            return auth.no_store(redirect('familyapp:home'))
        return auth.no_store(render(request, self.template_name, {
            'next': _safe_next(request, request.GET.get('next')), 'done': request.GET.get('out') == '1'}))

    def post(self, request):
        email = (request.POST.get('email') or '').strip()
        nxt = _safe_next(request, request.POST.get('next'))
        account = accounts.authenticate(email, request.POST.get('password') or '')
        if account is None or accounts.session_context(account) is None:
            # どちらが違うかは出さない（アカウントがあるかどうかを知られないように）
            return auth.no_store(render(request, self.template_name, {
                'error': True, 'email': email, 'next': nxt, 'max_failures': accounts.MAX_FAILURES,
                'lock_minutes': accounts.LOCK_MINUTES}, status=400))
        auth.login(request, account)
        return auth.no_store(redirect(nxt or reverse('familyapp:home')))


class LogoutView(View):
    def post(self, request):
        auth.logout(request)
        return auth.no_store(redirect(reverse('familyapp:login') + '?out=1'))


class SetupView(View):
    """職員が出した「パスワード設定のリンク」から、パスワードを決める（7 日以内・1 回かぎり）"""
    template_name = 'familyapp/setup.html'

    def invalid(self, request):
        return auth.no_store(render(request, 'familyapp/message.html', {
            'title': 'このリンクは使えません',
            'text': '期限（7 日）が過ぎたか、すでにパスワードを決めたリンクです。\nパスワードを決めてあるときは、ログインしてください。分からないときは、事業所へご連絡ください。',
            'link': reverse('familyapp:login'), 'link_label': 'ログインの画面へ'}, status=404))

    def get(self, request, token):
        account = accounts.check_setup_token(token)
        if account is None:
            return self.invalid(request)
        return auth.no_store(render(request, self.template_name, {'account': account, 'facility': account.facility}))

    def post(self, request, token):
        account = accounts.check_setup_token(token)
        if account is None:
            return self.invalid(request)
        pw, pw2 = request.POST.get('password') or '', request.POST.get('password2') or ''
        errors = []
        if not pw:
            errors.append('パスワードを入れてください。')
        elif pw != pw2:
            errors.append('2 つのパスワードが同じではありません。もう一度入れてください。')
        else:
            try:
                accounts.set_password(account, pw)
            except ValidationError as e:
                errors = list(e.messages)
        if errors:
            return auth.no_store(render(request, self.template_name, {
                'account': account, 'facility': account.facility, 'errors': errors}, status=400))
        if accounts.session_context(account) is None:
            return auth.no_store(redirect('familyapp:login'))
        auth.login(request, account)
        return auth.no_store(redirect(reverse('familyapp:home') + '?welcome=1'))


# ---------------------------------------------------------------- ログインの要る画面
class FamilyView(View):
    """ログインを確かめて、事業所・子ども（children／child）・保護者の記録（guardian）をそろえる"""

    def dispatch(self, request, *args, **kwargs):
        ctx = auth.current(request)
        if ctx is None:
            had = bool(request.session.get(auth.SESSION_ACCOUNT))
            if had:
                auth.logout(request)
            if request.headers.get('X-Requested-With') == 'fetch':
                return auth.no_store(JsonResponse({'ok': False, 'login': True}, status=401))
            url = reverse('familyapp:login')
            if request.method == 'GET' and request.path != reverse('familyapp:home'):
                from urllib.parse import urlencode
                url += '?' + urlencode({'next': request.get_full_path()})
            return auth.no_store(redirect(url))
        self.ctx = ctx
        self.account, self.facility = ctx['account'], ctx['facility']
        self.children, self.child, self.guardian = ctx['children'], ctx['child'], ctx['guardian']
        return auth.no_store(super().dispatch(request, *args, **kwargs))

    def page(self, template, status=200, **extra):
        return render(self.request, template, {
            'account': self.account, 'facility': self.facility, 'children': self.children, 'child': self.child,
            'guardian': self.guardian, **extra}, status=status)


class HomeView(FamilyView):
    def get(self, request):
        unread = services.unread_books(self.facility, self.child).count()
        news = notices.unread_count(self.facility, self.child)
        due = request_due(self.facility, self.child)
        icons = [{**it, 'label': it['label'].format(facility=self.facility.name), 'href': reverse(it['url']),
                  'badge': unread if it['key'] == 'from_facility' else news if it['key'] == 'oshirase' else 0,
                  'flag': '未申請' if it['key'] == 'request' and due else ''} for it in ICONS]
        return self.page('familyapp/home.html', icons=icons, welcome=request.GET.get('welcome') == '1', request_due=due)


class ChildSelectView(FamilyView):
    """きょうだいがいるとき：どの子の画面を見るかを切り替える"""

    def post(self, request):
        try:
            pk = int(request.POST.get('child', ''))
        except ValueError:
            pk = None
        if pk in {c.pk for c in self.children}:
            request.session[auth.SESSION_CHILD] = pk
        return redirect('familyapp:home')


class RecordView(FamilyView):
    """実績記録票（保護者サイン）：方法が決まるまで「準備中」"""

    def get(self, request):
        item = next(it for it in ICONS if it['key'] == 'jisseki')
        return self.page('familyapp/soon.html', item=item)


# ---------------------------------------------------------------- 連絡帳
class BookFromView(FamilyView):
    """連絡帳（事業所より）：日ごとに読む。←→ で前の日・次の日。確認すると、職員の画面に「確認済み」と出る"""

    def get(self, request):
        day = parse_day(request.GET.get('date'))
        books = ContactBook.objects.filter(facility=self.facility, beneficiary=self.child, status=ContactBook.STATUS_SENT)
        return self.page('familyapp/book_from.html', **day_nav(day), book=books.filter(date=day).first(),
                         recent=list(books.order_by('-date')[:14]), read_on_open=BOOK_READ_ON_OPEN)

    def post(self, request):
        try:
            pk = int(request.POST.get('book', ''))
        except ValueError:
            pk = None
        book = ContactBook.objects.filter(pk=pk, facility=self.facility, beneficiary=self.child,
                                          status=ContactBook.STATUS_SENT).first()
        if book is None:
            return JsonResponse({'ok': False}, status=404)
        services.mark_book_read(book, self.guardian)
        book.refresh_from_db()
        if request.headers.get('X-Requested-With') == 'fetch':
            return JsonResponse({'ok': True, 'read_at': timezone.localtime(book.read_at).strftime('%-m/%-d %H:%M')})
        return redirect(f'{reverse("familyapp:book_from")}?date={book.date.isoformat()}')


class BookToView(FamilyView):
    """連絡帳（保護者より）：その日の子どもの様子・伝えたいことを書いて送る。←→ で前の日・次の日"""
    template_name = 'familyapp/book_to.html'

    def context(self, day, **extra):
        notes = ParentNote.objects.filter(facility=self.facility, beneficiary=self.child, date=day).order_by('created_at')
        return {**day_nav(day), 'notes': notes, 'writable': services.note_writable(day), 'body_max': ParentNote.BODY_MAX, **extra}

    def get(self, request):
        day = parse_day(request.GET.get('date'))
        return self.page(self.template_name, **self.context(day, sent=request.GET.get('sent') == '1'))

    def post(self, request):
        day = parse_day(request.POST.get('date'))
        body = (request.POST.get('body') or '').strip()
        if not services.note_writable(day):
            return self.page(self.template_name, status=400, **self.context(day, error='この日の連絡帳は、もう書けません。'))
        if not body:
            return self.page(self.template_name, status=400, **self.context(day, error='内容を書いてください。'))
        services.add_parent_note(self.facility, self.child, self.guardian, day, body)
        return redirect(f'{reverse("familyapp:book_to")}?date={day.isoformat()}&sent=1')


# ---------------------------------------------------------------- 欠席連絡・その他の連絡
class ContactFormView(FamilyView):
    """日にちを選ぶ（きょう／カレンダーから複数）→ 内容 → 送信。下に、これまでに送った連絡と職員の確認の状態"""
    kind = FamilyMessage.KIND_OTHER
    template_name = 'familyapp/contact_form.html'
    FLASH = 'family_flash'

    def context(self, **extra):
        first, last = services.date_range()
        # その子についての連絡（もう一方の保護者が送ったものも出す：二重に送らずに済むように）
        sent = (FamilyMessage.objects.filter(facility=self.facility, beneficiary=self.child, kind=self.kind,
                                             target_date__gte=first - datetime.timedelta(days=14))
                .order_by('-target_date', '-created_at')[:30])
        return {'kind': self.kind, 'is_absence': self.kind == FamilyMessage.KIND_ABSENCE, 'today': first, 'last_day': last,
                'dates_max': services.DATES_MAX, 'body_max': FamilyMessage.BODY_MAX, 'reasons': FamilyMessage.REASON_CHOICES,
                'sent': sent, **extra}

    def get(self, request):
        flash = request.session.pop(self.FLASH, None) if request.GET.get('done') == '1' else None
        if flash:
            flash = {k: [datetime.date.fromisoformat(v) for v in vs] for k, vs in flash.items()}
        return self.page(self.template_name, **self.context(flash=flash))

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
            return self.page(self.template_name, status=400, **self.context(
                errors=errors, form={'dates': [d.isoformat() for d in dates], 'reason': reason, 'body': body}))
        made, skipped = services.create_messages(self.facility, self.child, self.guardian, self.kind, dates, reason=reason, body=body)
        request.session[self.FLASH] = {'made': [m.target_date.isoformat() for m in made],
                                       'skipped': [d.isoformat() for d in skipped]}
        return redirect(request.path + '?done=1')


class AbsenceView(ContactFormView):
    kind = FamilyMessage.KIND_ABSENCE


class MessageView(ContactFormView):
    kind = FamilyMessage.KIND_OTHER


# ---------------------------------------------------------------- 予定のカレンダーと利用日申請
class CalendarView(FamilyView):
    """職員の『予定』と同じカレンダー（見るだけ）。「利用日を申請する」で日にちを選んで送る（承認なしで来所予定になる）"""
    FLASH = 'family_flash_request'

    def get(self, request):
        year, month = yotei.parse_ym(request.GET.get('ym'))
        applying = request.GET.get('apply') == '1'
        window = request_window()
        view = yotei.month_view(self.facility, year, month, child=self.child, window=window if applying else None)
        flash = request.session.pop(self.FLASH, None) if request.GET.get('done') == '1' else None
        if flash:
            flash = {k: [datetime.date.fromisoformat(v) for v in vs] for k, vs in flash.items()}
        return self.page('familyapp/calendar.html', **view, cal_apply=applying,
                         cal_day_base=reverse('familyapp:calendar') + 'day/', cal_query='&apply=1' if applying else '',
                         window=window, flash=flash,
                         error='日にちを選んでから送信してください。' if request.GET.get('err') == '1' else '',
                         can_pick_any=any(c.get('can_pick') for w in view['weeks'] for c in w),
                         request_due=request_due(self.facility, self.child),
                         # 「利用日を申請する」で開く月：締め切りを過ぎた月を見ているときは、申請できる最初の月へ
                         apply_ym=max(view['ym'], f'{window[0].year}-{window[0].month:02d}'))

    def post(self, request):
        year, month = yotei.parse_ym(request.POST.get('ym'))
        ym = f'{year}-{month:02d}'
        window = request_window()
        days = yotei.clean_days(request.POST.getlist('dates'), window)
        # 利用曜日（児童台帳）の日で選んでいない日は、「利用」か「都合欠」かを必ず選んでもらう（2026-11-25 成の要望）
        first, last = yotei.month_span(year, month)
        missing = yotei.missing_regular_days(self.facility, self.child, max(first, window[0]), min(last, window[1]), picked=days)
        if not days and not missing:
            return redirect(f'{reverse("familyapp:calendar")}?ym={ym}&apply=1&err=1')
        choices = {d: request.POST.get(f'choice_{d.isoformat()}') for d in missing}
        if any(c not in ('use', 'off') for c in choices.values()):
            asked = request.POST.get('step') == 'confirm'
            return self.page('familyapp/calendar_confirm.html', ym=ym, days=days, choices=choices,
                             missing=[{'date': d, 'choice': choices[d]} for d in missing], asked=asked,
                             status=400 if asked else 200)
        use = sorted(set(days) | {d for d, c in choices.items() if c == 'use'})
        off = [d for d, c in choices.items() if c == 'off']
        made, skipped = yotei.apply_days(self.facility, self.child, use, via='parent',
                                         sender_name=services.sender_label(self.guardian),
                                         account=services.account_of(self.guardian))
        offs, _ = yotei.apply_personal_off(self.facility, self.child, self.guardian, off)
        request.session[self.FLASH] = {'made': [d.isoformat() for d in made], 'skipped': [d.isoformat() for d in skipped],
                                       'off': [m.target_date.isoformat() for m in offs]}
        return redirect(f'{reverse("familyapp:calendar")}?ym={ym}&done=1')


class CalendarDayView(FamilyView):
    """
    日付を押したときの画面（その日の予定・持ち物や参加費などの詳しい内容）。
    申請できる日（締め切り前・休業日でない・まだ予定が無い）なら「利用する」で、その 1 日だけを申請できる
    """
    FLASH = 'family_flash_day'

    def get_day(self, day):
        try:
            return datetime.date.fromisoformat(str(day))
        except ValueError:
            raise Http404 from None

    def get(self, request, day):
        day = self.get_day(day)
        info = yotei.day_info(self.facility, day, self.child)
        window = request_window()
        open_ = window[0] <= day <= window[1]
        closed = bool(info['event'] and info['event'].is_closed)
        free = not info['use'] and not info['absent']
        flash = request.session.pop(self.FLASH, None) if request.GET.get('done') == '1' else None
        return self.page('familyapp/calendar_day.html', **info, ym=f'{day.year}-{day.month:02d}', flash=flash,
                         can_apply=open_ and free and not closed,
                         # 締め切りを過ぎた、この先の日（きょうから、申請できる最初の日の前まで）
                         past_due=free and not closed and timezone.localdate() <= day < window[0])

    def post(self, request, day):
        day = self.get_day(day)
        url = reverse('familyapp:calendar_day', args=[day.isoformat()])
        days = yotei.clean_days([day.isoformat()], request_window())
        made = []
        if days:
            made, _ = yotei.apply_days(self.facility, self.child, days, via='parent',
                                       sender_name=services.sender_label(self.guardian),
                                       account=services.account_of(self.guardian))
        request.session[self.FLASH] = 'made' if made else 'skipped'
        return redirect(url + '?done=1')


# ---------------------------------------------------------------- お知らせ
class NoticeListView(FamilyView):
    """新しいものが上、古いものが下にたまっていく。画面が出たら「読んだ」と記録する"""
    PAGE = 30

    def get(self, request):
        rows = notices.feed(self.facility, self.child)
        total = rows.count()
        items = list(rows if request.GET.get('all') == '1' else rows[:self.PAGE])
        return self.page('familyapp/notice_list.html', items=items, unread=sum(1 for r in items if r.read_at is None),
                         more=total - len(items))

    def post(self, request):
        n = notices.mark_read(self.facility, self.child, self.guardian)
        if request.headers.get('X-Requested-With') == 'fetch':
            return JsonResponse({'ok': True, 'read': n})
        return redirect('familyapp:notice')


# ---------------------------------------------------------------- 個別支援計画
class PlanView(FamilyView):
    """本案（担当者会議を終えた計画）を読む。絞り込みと欄の並びは family/views.py の PlanView と同じ（原案・アセスメントは出さない）"""

    def get(self, request):
        from support_plans import sheet
        from support_plans.models import SupportPlan
        plans = (SupportPlan.objects.filter(facility=self.facility, beneficiary=self.child,
                                            current_step__gte=SupportPlan.STEP_CONSENT)
                 .exclude(status=SupportPlan.STATUS_CLOSED).select_related('manager').order_by('-created_at')[:2])
        cards = []
        for plan in plans:
            draft = plan.get_step(SupportPlan.STEP_DRAFT)
            values = sheet.plan_values(plan, draft)
            labels = [(k, label) for k, label, _w, _h in sheet.plan_cells(self.facility)] + \
                     [(k, sheet.label_of(k, self.facility)) for k in sheet.extra_in_sheet(self.facility)]
            cells = [{'label': label, 'value': (values.get(k) or '').strip()} for k, label in labels]
            goal_cells = sheet.goal_cells(self.facility)
            goals = []
            for row in sheet.goal_rows(plan):
                g = row['goal']
                if g is None:
                    continue
                parts = [{'key': k, 'label': label.replace('（文章）', ''), 'value': (row['values'].get(k) or '').strip()}
                         for k, label, _w, _h in goal_cells if k != 'content']
                parts = [p for p in parts if p['value']]
                # 達成時期は、文で書いてあればそれを、無ければ日付を出す（両方は出さない）
                goals.append({'goal': g, 'kind': g.get_goal_type_display(), 'parts': parts,
                              'show_date': bool(g.target_date) and not any(p['key'] == 'timing' for p in parts)})
            cards.append({'plan': plan, 'draft': draft, 'cells': [c for c in cells if c['value']], 'goals': goals,
                          'agreed': plan.current_step > SupportPlan.STEP_CONSENT})
        return self.page('familyapp/plan.html', cards=cards)
