"""
予定管理のビュー
- calendar_view : 月表示カレンダー
- daily_view    : 日付別の出欠入力一覧
- daily_save    : 出欠状態の一括保存
- bulk_create   : 指定月の予定をまとめて作成
"""

import calendar
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from beneficiaries.models import Beneficiary
from .models import ScheduledVisit
from facilities.context_processors import get_terms
from config.utils import month_or_404


class ScheduleEnabledMixin(LoginRequiredMixin):
    """施設設定で「予定」を使わない場合はホームへ戻す"""

    def dispatch(self, request, *args, **kwargs):
        facility = getattr(request.user, 'facility', None)
        if request.user.is_authenticated and facility is not None and not facility.use_schedule:
            messages.info(request, '予定の画面はこの事業所では使わない設定です（施設設定 → 使う機能 で変更できます）。')
            return redirect('facilities:dashboard')
        return super().dispatch(request, *args, **kwargs)


class CalendarView(ScheduleEnabledMixin, View):
    """
    月表示カレンダー画面。
    各日付に当日の利用予定人数を表示し、クリックで日別入力画面へ遷移する。
    """

    def get(self, request, year=None, month=None):
        today = date.today()
        if year is None:
            year = today.year
        if month is None:
            month = today.month

        if month == 1:
            prev_year, prev_month = year - 1, 12
        else:
            prev_year, prev_month = year, month - 1
        if month == 12:
            next_year, next_month = year + 1, 1
        else:
            next_year, next_month = year, month + 1

        facility = request.user.facility
        year, month = month_or_404(year, month)
        first_day = date(year, month, 1)
        last_day  = calendar.monthrange(year, month)[1]
        month_end = date(year, month, last_day)

        visits = ScheduledVisit.objects.filter(
            facility=facility,
            date__gte=first_day,
            date__lte=month_end,
        ).values('date', 'status')

        # 日付ごとにステータス別の件数を集計
        day_stats = {}
        for v in visits:
            d = v['date']
            if d not in day_stats:
                day_stats[d] = {'scheduled': 0, 'attended': 0, 'absent': 0, 'transferred': 0}
            status = v['status']
            if status in day_stats[d]:
                day_stats[d][status] += 1

        # カレンダーグリッドを生成（月曜始まり）
        cal   = calendar.monthcalendar(year, month)
        weeks = []
        for week in cal:
            week_data = []
            for day_num in week:
                if day_num == 0:
                    week_data.append(None)
                else:
                    d     = date(year, month, day_num)
                    stats = day_stats.get(d, {})
                    week_data.append({
                        'date':        d,
                        'day':         day_num,
                        'is_today':    d == today,
                        'is_sunday':   d.weekday() == 6,
                        'is_saturday': d.weekday() == 5,
                        'scheduled':   stats.get('scheduled', 0),
                        'attended':    stats.get('attended', 0),
                        'absent':      stats.get('absent', 0),
                        'transferred': stats.get('transferred', 0),
                    })
            weeks.append(week_data)

        # 当月の予定がすでに作成済みかチェック（一括作成ボタンの表示制御）
        current_month_has_visits = ScheduledVisit.objects.filter(
            facility=facility,
            date__year=year,
            date__month=month,
        ).exists()

        return render(request, 'schedules/calendar.html', {
            'year':                 year,
            'month':                month,
            'first_day':            first_day,
            'weeks':                weeks,
            'prev_year':            prev_year,
            'prev_month':           prev_month,
            'next_year':            next_year,
            'next_month':           next_month,
            'today':                today,
            'current_month_has_visits': current_month_has_visits,
        })


class DailyView(ScheduleEnabledMixin, View):
    """
    日別出欠入力画面。
    その日の在籍中の利用者全員の出欠状態・送迎を一覧表示・編集できる。
    """

    def get(self, request, year, month, day):
        try:
            target_date = date(year, month, day)
        except ValueError:
            return redirect('schedules:calendar')

        facility = request.user.facility
        beneficiaries = Beneficiary.objects.filter(
            facility=facility, status='active',
        ).order_by('last_name_kana', 'first_name_kana')

        visits_qs = ScheduledVisit.objects.filter(
            facility=facility,
            date=target_date,
        ).select_related('beneficiary')
        visit_map = {v.beneficiary_id: v for v in visits_qs}

        rows = []
        for b in beneficiaries:
            visit = visit_map.get(b.pk)
            rows.append({
                'beneficiary': b,
                'visit':       visit,
                'status':      visit.status      if visit else '',
                'has_pickup':  visit.has_pickup  if visit else False,
                'has_dropoff': visit.has_dropoff if visit else False,
                'notes':       visit.notes       if visit else '',
            })

        prev_date = target_date - timedelta(days=1)
        next_date = target_date + timedelta(days=1)

        return render(request, 'schedules/daily.html', {
            'target_date':    target_date,
            'rows':           rows,
            'status_choices': ScheduledVisit.STATUS_CHOICES,
            'prev_date':      prev_date,
            'next_date':      next_date,
            'year':           year,
            'month':          month,
        })


class DailySaveView(ScheduleEnabledMixin, View):
    """
    日別出欠状態の一括保存。
    フォームから送信された全利用者の状態・送迎情報を保存する。
    """

    def post(self, request, year, month, day):
        try:
            target_date = date(year, month, day)
        except ValueError:
            return redirect('schedules:calendar')

        facility      = request.user.facility
        beneficiaries = Beneficiary.objects.filter(
            facility=facility, status='active',
        )

        for b in beneficiaries:
            prefix = f'b_{b.pk}_'
            status = request.POST.get(f'{prefix}status', '').strip()

            # 状態が選択されていない場合は既存レコードを削除
            if not status:
                ScheduledVisit.objects.filter(
                    facility=facility,
                    beneficiary=b,
                    date=target_date,
                ).delete()
                continue

            valid_statuses = [s[0] for s in ScheduledVisit.STATUS_CHOICES]
            if status not in valid_statuses:
                continue

            has_pickup  = f'{prefix}has_pickup'  in request.POST
            has_dropoff = f'{prefix}has_dropoff' in request.POST
            notes       = request.POST.get(f'{prefix}notes', '').strip()

            ScheduledVisit.objects.update_or_create(
                facility=facility,
                beneficiary=b,
                date=target_date,
                defaults={
                    'status':      status,
                    'has_pickup':  has_pickup,
                    'has_dropoff': has_dropoff,
                    'notes':       notes,
                },
            )

        messages.success(
            request,
            f'{target_date.strftime("%Y年%m月%d日")} の出欠情報を保存しました。',
        )
        return redirect('schedules:daily', year=year, month=month, day=day)


class BulkCreateView(ScheduleEnabledMixin, View):
    """
    指定した月の予定をまとめて作成する。
    在籍中の利用者の登録曜日をもとに1ヶ月分の予定を「予定」状態で作成する。
    すでにレコードが存在する日はスキップする（上書きしない）。
    """

    def post(self, request, year, month):
        facility = request.user.facility
        beneficiaries = Beneficiary.objects.filter(
            facility=facility, status='active',
        )

        if not beneficiaries.exists():
            messages.warning(request, f'在籍中の{get_terms(request.user)["beneficiary"]}がいないため、予定を作成できませんでした。')
            return redirect('schedules:calendar_month', year=year, month=month)

        year, month = month_or_404(year, month)
        last_day  = calendar.monthrange(year, month)[1]
        all_dates = [date(year, month, d) for d in range(1, last_day + 1)]

        created_count = 0
        for b in beneficiaries:
            weekday_nums = b.scheduled_weekday_numbers  # [0, 2, 4] = 月水金

            # 通所曜日が1つも設定されていない場合は全日（日曜除く）作成
            has_any_day = bool(weekday_nums)

            for d in all_dates:
                if d.weekday() == 6:  # 日曜は常にスキップ
                    continue
                if has_any_day and d.weekday() not in weekday_nums:
                    continue

                _, created = ScheduledVisit.objects.get_or_create(
                    facility=facility,
                    beneficiary=b,
                    date=d,
                    defaults={'status': ScheduledVisit.STATUS_SCHEDULED},
                )
                if created:
                    created_count += 1

        if created_count > 0:
            messages.success(
                request,
                f'{year}年{month}月の予定を {created_count} 件作成しました（既存分はそのままです）。',
            )
        else:
            messages.info(request, f'{year}年{month}月の予定はすでに全て作成済みです。')

        return redirect('schedules:calendar_month', year=year, month=month)


# =============================================
# 実績記録票（2026-11 事業所「成」の要望。アイコンのホームを使う事業所だけ）
# =============================================
class JissekiMixin(LoginRequiredMixin):
    """アイコンのホームを使う事業所だけが開ける"""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            facility = getattr(request.user, 'facility', None)
            if facility is None or not facility.home_icons:
                from django.http import Http404
                raise Http404
        return super().dispatch(request, *args, **kwargs)


class JissekiDayView(JissekiMixin, View):
    """実績記録票（日ごと）：その日に来る子と、入れた時間。チェックした子にまとめて時間を入れる"""

    def get(self, request):
        from . import jisseki
        from .daylist import day_nav, parse_day
        facility = request.user.facility
        day = parse_day(request.GET.get('date'))
        rows, absent_rows = jisseki.day_rows(facility, day)
        done = sum(1 for r in rows if r['done'])
        warn = sum(1 for r in rows if r['warn'])
        return render(request, 'schedules/jisseki_day.html', {
            **day_nav(day), 'rows': rows, 'absent_rows': absent_rows, 'warn_count': warn,
            'day_nav_note': f'時間の入力 {done} / {len(rows)} 人',
        })

    def post(self, request):
        from django.urls import reverse
        from . import jisseki
        from .daylist import parse_day
        facility = request.user.facility
        day = parse_day(request.POST.get('date'))
        back = reverse('schedules:jisseki') + f'?date={day.isoformat()}'
        ids = [int(x) for x in request.POST.getlist('visit') if x.isdigit()]
        visits = list(ScheduledVisit.objects.filter(facility=facility, date=day, pk__in=ids)
                      .exclude(status=ScheduledVisit.STATUS_ABSENT))
        if not visits:
            messages.error(request, '時間を入れる子に、チェックを付けてください。')
            return redirect(back)
        if request.POST.get('action') == 'clear':
            n = jisseki.set_times(facility, visits, None, None)
            messages.success(request, f'{n} 人の時間を消しました。')
            return redirect(back)
        raw_s, raw_e = request.POST.get('start', ''), request.POST.get('end', '')
        start, end = jisseki.parse_hm(raw_s), jisseki.parse_hm(raw_e)
        problem = jisseki.check_times(start, end, raw_s, raw_e)
        if not problem and not start:
            problem = '始まりの時間を入れてください。'
        if problem:
            messages.error(request, problem)
            return redirect(back)
        n = jisseki.set_times(facility, visits, start, end)
        label = f'{start:%H:%M}〜{end:%H:%M}' if end else f'{start:%H:%M}〜'
        messages.success(request, f'{n} 人に {label} を入れました。')
        return redirect(back)


class JissekiPrintView(JissekiMixin, View):
    """実績記録票の印刷（様式５）。子ども 1 人か、その月に来た子の全員（beneficiary_pk なし）"""

    def get(self, request, year, month, beneficiary_pk=None):
        from . import jisseki_print
        facility = request.user.facility
        month_or_404(year, month)
        if beneficiary_pk:
            children = [get_object_or_404(Beneficiary, pk=beneficiary_pk, facility=facility)]
        else:
            import calendar as _cal
            first, last = date(year, month, 1), date(year, month, _cal.monthrange(year, month)[1])
            ids = ScheduledVisit.objects.filter(facility=facility, date__range=(first, last)).values_list('beneficiary_id', flat=True)
            children = list(Beneficiary.objects.filter(facility=facility, pk__in=set(ids))
                            .order_by('last_name_kana', 'first_name_kana', 'pk'))
        sheets = [jisseki_print.sheet(facility, c, year, month) for c in children]
        sheets = [s for s in sheets if beneficiary_pk or any(p['rows'] for p in s['pages'])]
        return render(request, 'schedules/jisseki_print.html', {
            'sheets': sheets, 'facility': facility, 'year': year, 'month': month, 'reiwa': year - 2018,
            'back': request.GET.get('back', ''), 'one': children[0] if beneficiary_pk else None})


class JissekiMonthView(JissekiMixin, View):
    """実績記録票（子ども 1 人の 1 か月ぶん）：来所予定のある日と時間。ここでも直せる"""

    def setup_objects(self, request, beneficiary_pk, year, month):
        from .daylist import parse_day
        self.facility = request.user.facility
        self.child = get_object_or_404(Beneficiary, pk=beneficiary_pk, facility=self.facility)
        today = date.today()
        self.year, self.month = year or today.year, month or today.month
        month_or_404(self.year, self.month)
        raw_back = request.POST.get('back') or request.GET.get('back')
        self.back = parse_day(raw_back).isoformat() if raw_back else ''

    def get(self, request, beneficiary_pk, year=None, month=None):
        from . import jisseki
        self.setup_objects(request, beneficiary_pk, year, month)
        sheet = jisseki.month_sheet(self.facility, self.child, self.year, self.month)
        return render(request, 'schedules/jisseki_month.html', {
            'child': self.child, 'year': self.year, 'month': self.month, 'back': self.back, **sheet,
        })

    def post(self, request, beneficiary_pk, year=None, month=None):
        from . import jisseki
        self.setup_objects(request, beneficiary_pk, year, month)
        sheet = jisseki.month_sheet(self.facility, self.child, self.year, self.month)
        changed, problems = 0, []
        for r in sheet['rows']:
            v = r['visit']
            if r['absent'] or f's_{v.pk}' not in request.POST:
                continue
            raw_s, raw_e = request.POST.get(f's_{v.pk}', ''), request.POST.get(f'e_{v.pk}', '')
            start, end = jisseki.parse_hm(raw_s), jisseki.parse_hm(raw_e)
            if (start, end) == (r['start'], r['end']):
                continue
            problem = jisseki.check_times(start, end, raw_s, raw_e)
            if problem:
                problems.append(f'{v.date.month}月{v.date.day}日：{problem}')
                continue
            changed += jisseki.set_times(self.facility, [v], start, end)
        for p in problems:
            messages.error(request, p)
        if changed:
            messages.success(request, f'{changed} 日ぶんを保存しました。')
        elif not problems:
            messages.info(request, '変わったところはありません。')
        from django.urls import reverse
        url = reverse('schedules:jisseki_month', args=[self.child.pk, self.year, self.month])
        return redirect(url + (f'?back={self.back}' if self.back else ''))
