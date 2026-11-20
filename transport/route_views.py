"""便ごとの配車の画面（来所予定で動く事業所）：その日の配車・その日の便を直す・曜日のパターン・月へ適応"""
import datetime

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from schedules.daylist import day_nav, parse_day

from . import routes
from .models import DIRECTIONS, DayRoute, RoutePattern
from .views import TransportEnabledMixin


def _ym(value):
    try:
        y, m = str(value or '').split('-')[:2]
        y, m = int(y), int(m)
        if 1 <= m <= 12 and 2000 <= y <= 2100:
            return y, m
    except ValueError:
        pass
    today = datetime.date.today()
    return today.year, today.month


class RouteBoardView(TransportEnabledMixin, View):
    """その日の配車：迎え／送りごとに、便（車両・運転手・時刻）と乗る子。←→ で日を替える"""

    def get(self, request):
        facility = request.user.facility
        day = parse_day(request.GET.get('date'))
        board = routes.day_board(facility, day)
        return render(request, 'transport/routes_board.html', {
            **day_nav(day), **board, 'weekday_index': day.weekday(),
            'day_nav_note': f'来所予定 {board["coming"]} 人',
            'has_pattern': RoutePattern.objects.filter(facility=facility, weekday=day.weekday()).exists(),
        })

    def post(self, request):
        """この日だけ、曜日のパターンから入れ直す"""
        facility = request.user.facility
        day = parse_day(request.POST.get('date'))
        if request.POST.get('action') == 'apply_day':
            DayRoute.objects.filter(facility=facility, date=day).delete()
            made = 0
            for p in RoutePattern.objects.filter(facility=facility, weekday=day.weekday()).prefetch_related('stops'):
                from .models import DayRouteStop
                r = DayRoute.objects.create(facility=facility, date=day, direction=p.direction, name=p.name, vehicle=p.vehicle,
                                            driver=p.driver, depart_time=p.depart_time, order=p.order, pattern=p)
                DayRouteStop.objects.bulk_create([DayRouteStop(route=r, beneficiary_id=s.beneficiary_id, time=s.time, place=s.place,
                                                               usual_time=s.time, order=s.order) for s in p.stops.all()])
                made += 1
            messages.success(request, f'{routes.WEEK_JP[day.weekday()]}曜のパターンから、便を {made} つ入れ直しました。' if made
                             else f'{routes.WEEK_JP[day.weekday()]}曜のパターンが、まだありません。')
        return redirect(f'{reverse("transport:routes")}?date={day.isoformat()}')


class DayRouteEditView(TransportEnabledMixin, View):
    """その日の便を作る・直す・消す（車両・運転手・出発の時刻・乗る子と時刻）"""

    def setup_objects(self, request, pk):
        self.facility = request.user.facility
        self.route = get_object_or_404(DayRoute, pk=pk, facility=self.facility) if pk else None
        if self.route:
            self.day, self.direction = self.route.date, self.route.direction
        else:
            self.day = parse_day(request.POST.get('date') or request.GET.get('date'))
            self.direction = routes.clean_direction(request.POST.get('direction') or request.GET.get('direction'))

    def context(self, problem=''):
        stops = {s.beneficiary_id: s for s in self.route.stops.all()} if self.route else {}
        return {'route': self.route, 'kind': 'day', 'day': self.day, 'direction': self.direction,
                'direction_label': routes.direction_label(self.direction), 'problem': problem,
                'riders': routes.rider_rows(self.facility, self.direction, stops, weekday=self.day.weekday(), day=self.day),
                'back': f'{reverse("transport:routes")}?date={self.day.isoformat()}', **routes.masters(self.facility)}

    def get(self, request, pk=None):
        self.setup_objects(request, pk)
        return render(request, 'transport/route_edit.html', self.context())

    def post(self, request, pk=None):
        self.setup_objects(request, pk)
        back = f'{reverse("transport:routes")}?date={self.day.isoformat()}'
        if request.POST.get('action') == 'delete':
            if self.route:
                name = self.route.name
                self.route.delete()
                messages.success(request, f'便「{name}」を消しました。')
            return redirect(back)
        route, problem = routes.save_day_route(self.facility, request.POST, self.route, self.day, self.direction)
        if problem:
            return render(request, 'transport/route_edit.html', self.context(problem), status=400)
        messages.success(request, f'{routes.direction_label(route.direction)}「{route.name}」を保存しました（{route.stops.count()} 人）。')
        return redirect(back)


class PatternBoardView(TransportEnabledMixin, View):
    """曜日ごとの送迎パターン：便を登録し、「この月に適応」でその曜日の日すべてに写す"""

    def get(self, request):
        facility = request.user.facility
        weekday = routes.to_int(request.GET.get('wd'), datetime.date.today().weekday())
        weekday = weekday if 0 <= weekday <= 6 else 0
        today = datetime.date.today()
        nxt = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
        counts = routes.pattern_counts(facility)
        return render(request, 'transport/route_patterns.html', {
            'weekday': weekday, 'weekday_label': routes.WEEK_JP[weekday], 'sections': routes.weekday_board(facility, weekday),
            'tabs': [{'i': i, 'label': routes.WEEK_JP[i], 'n': counts[i]} for i in range(7)], 'total': sum(counts),
            'months': [{'ym': f'{d.year}-{d.month:02d}', 'label': f'{d.year}年{d.month}月'} for d in (today, nxt)],
            **routes.masters(facility),
        })

    def post(self, request):
        facility = request.user.facility
        weekday = routes.to_int(request.POST.get('wd'), 0)
        year, month = _ym(request.POST.get('ym'))
        overwrite = request.POST.get('overwrite') == '1'
        # 今月に写すときは、きょうからあとだけ（過ぎた日の配車は変えない）
        days, made = routes.apply_month(facility, year, month, overwrite=overwrite, start=datetime.date.today())
        if made:
            messages.success(request, f'{year}年{month}月の {days} 日に、便を {made} つ写しました。日ごとの配車で直せます。')
        else:
            messages.info(request, '写す日はありませんでした（もう便の入っている日はそのままです。入れ直すときは「入っている日も入れ直す」に印を付けます）。')
        return redirect(f'{reverse("transport:route_patterns")}?wd={weekday}')


class PatternEditView(TransportEnabledMixin, View):
    """曜日のパターンの便を作る・直す・消す"""

    def setup_objects(self, request, pk):
        self.facility = request.user.facility
        self.pattern = get_object_or_404(RoutePattern, pk=pk, facility=self.facility) if pk else None
        if self.pattern:
            self.weekday, self.direction = self.pattern.weekday, self.pattern.direction
        else:
            wd = routes.to_int(request.POST.get('wd') or request.GET.get('wd'), 0)
            self.weekday = wd if 0 <= wd <= 6 else 0
            self.direction = routes.clean_direction(request.POST.get('direction') or request.GET.get('direction'))

    def context(self, problem=''):
        stops = {s.beneficiary_id: s for s in self.pattern.stops.all()} if self.pattern else {}
        return {'route': self.pattern, 'kind': 'pattern', 'weekday': self.weekday, 'weekday_label': routes.WEEK_JP[self.weekday],
                'direction': self.direction, 'direction_label': routes.direction_label(self.direction), 'problem': problem,
                'riders': routes.rider_rows(self.facility, self.direction, stops, weekday=self.weekday),
                'back': f'{reverse("transport:route_patterns")}?wd={self.weekday}', **routes.masters(self.facility)}

    def get(self, request, pk=None):
        self.setup_objects(request, pk)
        return render(request, 'transport/route_edit.html', self.context())

    def post(self, request, pk=None):
        self.setup_objects(request, pk)
        back = f'{reverse("transport:route_patterns")}?wd={self.weekday}'
        if request.POST.get('action') == 'delete':
            if self.pattern:
                name = self.pattern.name
                self.pattern.delete()
                messages.success(request, f'便「{name}」を消しました（もう写した日の配車はそのままです）。')
            return redirect(back)
        pattern, problem = routes.save_pattern(self.facility, request.POST, self.pattern, self.weekday, self.direction)
        if problem:
            return render(request, 'transport/route_edit.html', self.context(problem), status=400)
        messages.success(request, f'{routes.WEEK_JP[pattern.weekday]}曜・{routes.direction_label(pattern.direction)}「{pattern.name}」を保存しました（{pattern.stops.count()} 人）。')
        return redirect(back)
