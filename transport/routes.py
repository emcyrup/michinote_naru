"""
便ごとの配車（来所予定で動く事業所）。

曜日ごとの送迎パターン（RoutePattern：便 = 車両 × 運転手 × 乗る子と時刻）→「この月に適応」で、その曜日の日すべてに
その日の便（DayRoute）として写す → 日ごとに直す。
日ごとの一覧では、便に乗っているのに来所予定の無い子・欠席の子、いつもと時刻の違う子、送迎があるのに便に乗っていない子に印を付ける。
"""
import calendar
import datetime

from beneficiaries.models import Beneficiary
from schedules.models import ScheduledVisit

from .models import (DIRECTION_DROPOFF, DIRECTION_LABELS, DIRECTION_PICKUP, DIRECTIONS, DayRoute, DayRouteStop, Driver,
                     RoutePattern, RoutePatternStop, TransportProfile, Vehicle)

WEEK_JP = ['月', '火', '水', '木', '金', '土', '日']
WEEKDAY_FIELDS = ['weekday_mon', 'weekday_tue', 'weekday_wed', 'weekday_thu', 'weekday_fri', 'weekday_sat']


def parse_hm(value):
    value = (value or '').strip()
    if not value:
        return None
    try:
        return datetime.time.fromisoformat(value[:5])
    except ValueError:
        return None


def to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def children(facility):
    return Beneficiary.objects.filter(facility=facility, status=Beneficiary.STATUS_ACTIVE) \
        .order_by('last_name_kana', 'first_name_kana', 'pk')


def masters(facility):
    return {'vehicles': list(Vehicle.objects.filter(facility=facility, is_active=True)),
            'drivers': list(Driver.objects.filter(facility=facility, is_active=True))}


def rider_rows(facility, direction, stops=None, weekday=None, day=None):
    """
    便に乗る子を選ぶ表の行。在籍中の子の全員（その曜日が契約曜日の子・その日に来る子を上に）。
    stops：いま乗っている子 {利用者 ID: 便に乗る子の行}
    """
    stops = stops or {}
    profiles = {p.beneficiary_id: p for p in TransportProfile.objects.filter(beneficiary__facility=facility)}
    visits = {}
    if day is not None:
        visits = {v.beneficiary_id: v for v in ScheduledVisit.objects.filter(facility=facility, date=day)}
    rows = []
    for b in children(facility):
        s, p, v = stops.get(b.pk), profiles.get(b.pk), visits.get(b.pk)
        regular = bool(weekday is not None and weekday < 6 and getattr(b, WEEKDAY_FIELDS[weekday], False))
        coming = bool(v and v.status != ScheduledVisit.STATUS_ABSENT)
        rows.append({
            'beneficiary': b, 'on': s is not None, 'time': s.time if s else None, 'place': s.place if s else '',
            'default_time': p.time(direction) if p else None, 'default_place': p.place(direction) if p else '',
            'regular': regular, 'coming': coming, 'absent': bool(v and not coming), 'has_visit': v is not None,
        })
    rows.sort(key=lambda r: (not r['on'], not (r['coming'] or r['regular'])))
    return rows


def _read_route(facility, post):
    """便の画面から届いた値を整える → (値, 乗る子の並び, 問題の文)"""
    name = (post.get('name') or '').strip()[:RoutePattern.NAME_MAX]
    vehicle = Vehicle.objects.filter(facility=facility, pk=to_int(post.get('vehicle'), -1)).first()
    driver = Driver.objects.filter(facility=facility, pk=to_int(post.get('driver'), -1)).first()
    ids = {b.pk for b in children(facility)}
    riders = []
    for raw in post.getlist('riders'):
        pk = to_int(raw)
        if pk in ids:
            riders.append({'pk': pk, 'time': parse_hm(post.get(f't_{pk}')), 'place': (post.get(f'p_{pk}') or '').strip()[:100]})
    riders.sort(key=lambda r: (r['time'] is None, r['time'] or datetime.time(0, 0)))
    problem = '' if name else '便の名前を入れてください（例：1便）。'
    return {'name': name, 'vehicle': vehicle, 'driver': driver, 'depart_time': parse_hm(post.get('depart_time'))}, riders, problem


def save_pattern(facility, post, pattern=None, weekday=0, direction=DIRECTION_PICKUP):
    values, riders, problem = _read_route(facility, post)
    if problem:
        return None, problem
    if pattern is None:
        pattern = RoutePattern(facility=facility, weekday=weekday, direction=direction,
                               order=RoutePattern.objects.filter(facility=facility, weekday=weekday, direction=direction).count())
    for k, v in values.items():
        setattr(pattern, k, v)
    pattern.save()
    pattern.stops.all().delete()
    RoutePatternStop.objects.bulk_create([RoutePatternStop(pattern=pattern, beneficiary_id=r['pk'], time=r['time'], place=r['place'], order=i)
                                          for i, r in enumerate(riders)])
    return pattern, ''


def save_day_route(facility, post, route=None, day=None, direction=DIRECTION_PICKUP):
    values, riders, problem = _read_route(facility, post)
    if problem:
        return None, problem
    usual = {}
    if route is None:
        route = DayRoute(facility=facility, date=day, direction=direction,
                         order=DayRoute.objects.filter(facility=facility, date=day, direction=direction).count())
    else:
        usual = {s.beneficiary_id: s.usual_time for s in route.stops.all()}
    for k, v in values.items():
        setattr(route, k, v)
    route.note = (post.get('note') or '').strip()[:100]
    route.save()
    # その日だけ足した子の「いつもの時刻」は、利用者の送迎の設定の目安の時刻
    profiles = {p.beneficiary_id: p.time(route.direction) for p in TransportProfile.objects.filter(beneficiary_id__in=[r['pk'] for r in riders])}
    route.stops.all().delete()
    DayRouteStop.objects.bulk_create([DayRouteStop(route=route, beneficiary_id=r['pk'], time=r['time'], place=r['place'], order=i,
                                                   usual_time=usual.get(r['pk']) or profiles.get(r['pk']))
                                      for i, r in enumerate(riders)])
    return route, ''


def weekday_board(facility, weekday):
    """曜日のパターンの一覧：[(迎え／送り, 見出し, [便…])]"""
    patterns = list(RoutePattern.objects.filter(facility=facility, weekday=weekday).select_related('vehicle', 'driver')
                    .prefetch_related('stops__beneficiary'))
    out = []
    for direction, label in DIRECTIONS:
        rows = []
        for p in patterns:
            if p.direction != direction:
                continue
            stops = list(p.stops.all())
            p.stop_list = stops
            p.over = bool(p.vehicle and p.vehicle.capacity and len(stops) > p.vehicle.capacity)
            rows.append(p)
        out.append({'direction': direction, 'label': label, 'routes': rows})
    return out


def pattern_counts(facility):
    """曜日ごとの便の数（タブに出す）"""
    counts = [0] * 7
    for wd in RoutePattern.objects.filter(facility=facility).values_list('weekday', flat=True):
        counts[wd] += 1
    return counts


def closed_days(facility, first, last):
    try:
        from family.models import DayEvent
        return set(DayEvent.objects.filter(facility=facility, date__range=(first, last), is_closed=True).values_list('date', flat=True))
    except Exception:  # noqa: BLE001
        return set()


def apply_month(facility, year, month, overwrite=False, start=None):
    """
    曜日のパターンを、その月の日に写す。もう便のある日（迎え／送りごと）はそのまま（overwrite なら作り直す）。
    休業日には写さない。start を渡すと、その日からあとだけ（過ぎた日の配車を変えないため）。(写した日数, 作った便の数) を返す
    """
    first = datetime.date(year, month, 1)
    last = datetime.date(year, month, calendar.monthrange(year, month)[1])
    patterns = {}
    for p in RoutePattern.objects.filter(facility=facility).prefetch_related('stops'):
        patterns.setdefault((p.weekday, p.direction), []).append(p)
    if not patterns:
        return 0, 0
    closed = closed_days(facility, first, last)
    have = set(DayRoute.objects.filter(facility=facility, date__range=(first, last)).values_list('date', 'direction'))
    days, made = set(), 0
    day = max(first, start) if start else first
    while day <= last:
        for direction, _ in DIRECTIONS:
            todo = patterns.get((day.weekday(), direction))
            if not todo or day in closed:
                continue
            if (day, direction) in have:
                if not overwrite:
                    continue
                DayRoute.objects.filter(facility=facility, date=day, direction=direction).delete()
            for i, p in enumerate(todo):
                r = DayRoute.objects.create(facility=facility, date=day, direction=direction, name=p.name, vehicle=p.vehicle,
                                            driver=p.driver, depart_time=p.depart_time, order=i, pattern=p)
                DayRouteStop.objects.bulk_create([DayRouteStop(route=r, beneficiary_id=s.beneficiary_id, time=s.time, place=s.place,
                                                               usual_time=s.time, order=s.order) for s in p.stops.all()])
                made += 1
            days.add(day)
        day += datetime.timedelta(days=1)
    return len(days), made


def day_board(facility, day):
    """
    その日の配車の一覧。便ごとに乗る子を並べ、次の印を付ける：
      no_visit＝便に乗っているのに来所予定が無い／absent＝欠席／changed＝いつもと時刻が違う
    unassigned＝その日に来て送迎があるのに、どの便にも乗っていない子
    """
    visits = {v.beneficiary_id: v for v in ScheduledVisit.objects.filter(facility=facility, date=day).select_related('beneficiary')}
    routes = list(DayRoute.objects.filter(facility=facility, date=day).select_related('vehicle', 'driver')
                  .prefetch_related('stops__beneficiary'))
    profiles = {p.beneficiary_id: p for p in TransportProfile.objects.filter(beneficiary_id__in=list(visits))}
    sections, alerts = [], 0
    for direction, label in DIRECTIONS:
        rows, riding = [], set()
        for r in routes:
            if r.direction != direction:
                continue
            stops = []
            for s in r.stops.all():
                v = visits.get(s.beneficiary_id)
                s.no_visit = v is None
                s.absent = bool(v and v.status == ScheduledVisit.STATUS_ABSENT)
                s.changed = s.time_changed
                if s.no_visit or s.absent:
                    alerts += 1
                riding.add(s.beneficiary_id)
                stops.append(s)
            r.stop_list = stops
            r.riders = sum(1 for s in stops if not (s.no_visit or s.absent))
            r.over = bool(r.vehicle and r.vehicle.capacity and r.riders > r.vehicle.capacity)
            rows.append(r)
        unassigned = []
        for b_id, v in visits.items():
            if v.status == ScheduledVisit.STATUS_ABSENT or b_id in riding:
                continue
            p = profiles.get(b_id)
            wants = (v.has_pickup if direction == DIRECTION_PICKUP else v.has_dropoff) or bool(p and p.has(direction))
            if wants:
                unassigned.append(v.beneficiary)
        unassigned.sort(key=lambda b: (b.last_name_kana, b.first_name_kana, b.pk))
        sections.append({'direction': direction, 'label': label, 'routes': rows, 'unassigned': unassigned})
    return {'sections': sections, 'alerts': alerts, 'coming': sum(1 for v in visits.values() if v.status != ScheduledVisit.STATUS_ABSENT),
            'has_routes': bool(routes), 'changed': sum(1 for r in routes for s in r.stop_list if s.changed)}


def direction_label(direction):
    return DIRECTION_LABELS.get(direction, '')


def clean_direction(value):
    return value if value in (DIRECTION_PICKUP, DIRECTION_DROPOFF) else DIRECTION_PICKUP
