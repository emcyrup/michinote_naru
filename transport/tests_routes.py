import datetime

from django.test import TestCase
from django.urls import reverse

from accounts.models import StaffAccount
from beneficiaries.models import Beneficiary
from facilities.models import Facility
from schedules.models import ScheduledVisit

from . import routes
from .models import DayRoute, DayRouteStop, Driver, RoutePattern, RoutePatternStop, TransportProfile, Vehicle

T = datetime.time
D = datetime.date


class RouteTests(TestCase):
    """便ごとの配車：曜日のパターン → 月へ適応 → 日ごとに直す。来所予定なし・欠席・いつもと違う時刻・便に乗っていない子の印"""

    def setUp(self):
        self.f = Facility.objects.create(name='見本の事業所', use_transport=True)
        self.other = Facility.objects.create(name='よその事業所', use_transport=True)
        self.staff = StaffAccount.objects.create_user('rt_admin', password='pw12345678', facility=self.f, role=StaffAccount.ROLE_ADMIN)
        kw = dict(date_of_birth=D(2016, 4, 1))
        self.taro = Beneficiary.objects.create(facility=self.f, last_name='見本', first_name='太郎', last_name_kana='みほん', first_name_kana='たろう', weekday_tue=True, **kw)
        self.hana = Beneficiary.objects.create(facility=self.f, last_name='見本', first_name='花子', last_name_kana='みほん', first_name_kana='はなこ', **kw)
        self.jiro = Beneficiary.objects.create(facility=self.f, last_name='架空', first_name='次郎', last_name_kana='かくう', first_name_kana='じろう', **kw)
        self.yoso = Beneficiary.objects.create(facility=self.other, last_name='他所', first_name='三郎', last_name_kana='よそ', first_name_kana='さぶろう', **kw)
        self.car = Vehicle.objects.create(facility=self.f, name='ワゴン', capacity=2)
        self.driver = Driver.objects.create(facility=self.f, name='運転 六助')
        self.client.force_login(self.staff)
        self.tue = 1                                         # 2026-12-01 は火曜
        self.day = D(2026, 12, 1)

    def pattern(self, direction='pickup', weekday=None, riders=(), name='1便'):
        p = RoutePattern.objects.create(facility=self.f, weekday=self.tue if weekday is None else weekday, direction=direction,
                                        name=name, vehicle=self.car, driver=self.driver, depart_time=T(14, 30))
        for i, (b, t) in enumerate(riders):
            RoutePatternStop.objects.create(pattern=p, beneficiary=b, time=t, place='見本小', order=i)
        return p

    def test_non_reservation_facility_goes_to_route_board(self):
        self.assertRedirects(self.client.get(reverse('transport:day')), reverse('transport:routes'))
        self.f.use_transport = False
        self.f.save()
        self.assertRedirects(self.client.get(reverse('transport:routes')), reverse('facilities:dashboard'), fetch_redirect_response=False)

    def test_pattern_create_edit_delete(self):
        url = reverse('transport:route_pattern_new')
        html = self.client.get(f'{url}?wd={self.tue}&direction=pickup').content.decode()
        self.assertIn('火曜の迎えの便', html)
        self.assertIn('契約曜日', html)                        # 太郎は火曜が契約曜日
        self.assertNotIn('他所 三郎', html)
        res = self.client.post(url, {'wd': self.tue, 'direction': 'pickup', 'name': '', 'riders': [self.taro.pk]})
        self.assertEqual(res.status_code, 400)
        res = self.client.post(url, {'wd': self.tue, 'direction': 'pickup', 'name': '1便', 'vehicle': self.car.pk, 'driver': self.driver.pk,
                                     'depart_time': '14:30', 'riders': [self.hana.pk, self.taro.pk, self.yoso.pk],
                                     f't_{self.hana.pk}': '15:10', f't_{self.taro.pk}': '14:50', f'p_{self.taro.pk}': '見本小',
                                     f't_{self.yoso.pk}': '15:00'}, follow=True)
        self.assertContains(res, '火曜・迎え「1便」を保存しました（2 人）')
        p = RoutePattern.objects.get()
        self.assertEqual([(s.beneficiary, s.time) for s in p.stops.all()], [(self.taro, T(14, 50)), (self.hana, T(15, 10))])   # 時刻の早い順。よその子は入らない
        self.assertContains(self.client.get(reverse('transport:route_patterns') + f'?wd={self.tue}'), '14:50')
        edit = reverse('transport:route_pattern_edit', args=[p.pk])
        self.client.post(edit, {'name': '学校コース', 'riders': [self.taro.pk], f't_{self.taro.pk}': '15:00'})
        p.refresh_from_db()
        self.assertEqual((p.name, p.vehicle, p.stops.count()), ('学校コース', None, 1))
        self.client.post(edit, {'action': 'delete'})
        self.assertFalse(RoutePattern.objects.exists())
        other = RoutePattern.objects.create(facility=self.other, weekday=0, direction='pickup', name='よその便')
        self.assertEqual(self.client.get(reverse('transport:route_pattern_edit', args=[other.pk])).status_code, 404)

    def test_apply_month(self):
        self.pattern('pickup', riders=[(self.taro, T(14, 50)), (self.hana, T(15, 10))])
        self.pattern('dropoff', riders=[(self.taro, T(17, 30))], name='帰り 1便')
        self.pattern('pickup', weekday=3, riders=[(self.jiro, T(15, 0))])           # 木曜
        # 2026 年 12 月：火曜 5 日・木曜 5 日
        days, made = routes.apply_month(self.f, 2026, 12)
        self.assertEqual((days, made), (10, 15))
        r = DayRoute.objects.get(date=self.day, direction='pickup')
        self.assertEqual((r.name, r.vehicle, r.driver, r.depart_time, r.stops.count()), ('1便', self.car, self.driver, T(14, 30), 2))
        self.assertEqual(r.stops.first().usual_time, T(14, 50))
        self.assertFalse(DayRoute.objects.filter(date=D(2026, 12, 2)).exists())       # 水曜はパターンなし
        # もう一度：入っている日はそのまま。直した日も保つ
        DayRouteStop.objects.filter(route=r, beneficiary=self.taro).update(time=T(16, 0))
        self.assertEqual(routes.apply_month(self.f, 2026, 12), (0, 0))
        self.assertEqual(DayRouteStop.objects.get(route=r, beneficiary=self.taro).time, T(16, 0))
        # 入れ直す
        self.assertEqual(routes.apply_month(self.f, 2026, 12, overwrite=True), (10, 15))
        self.assertEqual(DayRoute.objects.count(), 15)
        self.assertEqual(DayRouteStop.objects.get(route__date=self.day, route__direction='pickup', beneficiary=self.taro).time, T(14, 50))
        # 途中の日から（過ぎた日は変えない）・休業日は除く
        DayRoute.objects.all().delete()
        from family.models import DayEvent
        DayEvent.objects.create(facility=self.f, date=D(2026, 12, 22), is_closed=True)
        days, made = routes.apply_month(self.f, 2026, 12, start=D(2026, 12, 15))
        self.assertEqual(days, 5)                                                     # 火 15・29（22 は休業日）、木 17・24・31
        self.assertFalse(DayRoute.objects.filter(date__lt=D(2026, 12, 15)).exists())
        self.assertFalse(DayRoute.objects.filter(date=D(2026, 12, 22)).exists())

    def test_apply_from_screen(self):
        today = datetime.date.today()
        self.pattern('pickup', weekday=today.weekday(), riders=[(self.taro, T(14, 50))])
        url = reverse('transport:route_patterns')
        res = self.client.post(url, {'wd': today.weekday(), 'ym': f'{today.year}-{today.month:02d}'}, follow=True)
        self.assertContains(res, '便を')
        self.assertTrue(DayRoute.objects.filter(date=today).exists())
        self.assertFalse(DayRoute.objects.filter(date__lt=today).exists())
        res = self.client.post(url, {'wd': today.weekday(), 'ym': f'{today.year}-{today.month:02d}'}, follow=True)
        self.assertContains(res, '写す日はありませんでした')

    def test_day_board_marks(self):
        self.pattern('pickup', riders=[(self.taro, T(14, 50)), (self.hana, T(15, 10)), (self.jiro, T(15, 20))])
        routes.apply_month(self.f, 2026, 12)
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.taro, date=self.day, has_dropoff=True)
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.hana, date=self.day, status='absent')
        r = DayRoute.objects.get(date=self.day)
        DayRouteStop.objects.filter(route=r, beneficiary=self.taro).update(time=T(15, 30))     # この日だけ遅い
        board = routes.day_board(self.f, self.day)
        pickup, dropoff = board['sections']
        marks = {s.beneficiary: (s.no_visit, s.absent, s.changed) for s in pickup['routes'][0].stop_list}
        self.assertEqual(marks, {self.taro: (False, False, True), self.hana: (False, True, False), self.jiro: (True, False, False)})
        self.assertEqual((board['alerts'], board['changed'], pickup['routes'][0].riders, pickup['routes'][0].over), (2, 1, 1, False))
        self.assertEqual(dropoff['unassigned'], [self.taro])            # 送りがあるのに、どの便にも乗っていない
        html = self.client.get(reverse('transport:routes') + '?date=2026-12-01').content.decode()
        for text in ('いつもは 14:50', '来所予定なし', '欠席', 'どの便にも乗っていない子', '見本 太郎', '来所予定が無い（または欠席の）子が 2 人'):
            self.assertIn(text, html)
        self.assertIn('?date=2026-11-30', html)
        self.assertIn('?date=2026-12-02', html)

    def test_capacity_and_profile_based_unassigned(self):
        self.pattern('pickup', riders=[(self.taro, T(14, 50)), (self.hana, T(15, 0)), (self.jiro, T(15, 10))])
        routes.apply_month(self.f, 2026, 12)
        for b in (self.taro, self.hana, self.jiro):
            ScheduledVisit.objects.create(facility=self.f, beneficiary=b, date=self.day)
        TransportProfile.objects.create(beneficiary=self.jiro, dropoff=True, dropoff_time=T(17, 0), dropoff_place='自宅')
        board = routes.day_board(self.f, self.day)
        self.assertTrue(board['sections'][0]['routes'][0].over)          # 2 人乗りに 3 人
        self.assertEqual(board['sections'][1]['unassigned'], [self.jiro])
        self.assertContains(self.client.get(reverse('transport:routes') + '?date=2026-12-01'), '定員をこえています')

    def test_day_route_add_edit_delete_and_usual_time(self):
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.jiro, date=self.day)
        TransportProfile.objects.create(beneficiary=self.jiro, dropoff=True, dropoff_time=T(17, 0), dropoff_place='自宅')
        url = reverse('transport:route_new')
        html = self.client.get(f'{url}?date=2026-12-01&direction=dropoff').content.decode()
        self.assertIn('12月1日（火）の送りの便', html)
        self.assertIn('data-time="17:00"', html)                         # 送迎の設定のいつもの時刻
        self.assertIn('来所予定なし', html)                               # 太郎・花子はこの日の予定が無い
        res = self.client.post(url, {'date': '2026-12-01', 'direction': 'dropoff', 'name': '帰り 1便', 'note': '買い物に寄る',
                                     'riders': [self.jiro.pk], f't_{self.jiro.pk}': '17:30', f'p_{self.jiro.pk}': '祖母宅'}, follow=True)
        self.assertContains(res, '送り「帰り 1便」を保存しました（1 人）')
        r = DayRoute.objects.get()
        s = r.stops.get()
        self.assertEqual((r.direction, r.note, s.time, s.place, s.usual_time, s.time_changed), ('dropoff', '買い物に寄る', T(17, 30), '祖母宅', T(17, 0), True))
        self.assertContains(res, 'いつもは 17:00')
        edit = reverse('transport:route_edit', args=[r.pk])
        self.client.post(edit, {'name': '帰り 1便', 'riders': [self.jiro.pk], f't_{self.jiro.pk}': '17:00'})
        self.assertFalse(r.stops.get().time_changed)                     # いつもの時刻に戻すと印が消える
        self.client.post(edit, {'action': 'delete'})
        self.assertFalse(DayRoute.objects.exists())
        other = DayRoute.objects.create(facility=self.other, date=self.day, direction='pickup', name='よその便')
        self.assertEqual(self.client.get(reverse('transport:route_edit', args=[other.pk])).status_code, 404)

    def test_apply_day_again(self):
        self.pattern('pickup', riders=[(self.taro, T(14, 50))])
        DayRoute.objects.create(facility=self.f, date=self.day, direction='pickup', name='手で作った便')
        res = self.client.post(reverse('transport:routes'), {'date': '2026-12-01', 'action': 'apply_day'}, follow=True)
        self.assertContains(res, '火曜のパターンから、便を 1 つ入れ直しました')
        self.assertEqual(list(DayRoute.objects.values_list('name', flat=True)), ['1便'])

    def test_merge_keeps_routes(self):
        from beneficiaries import merge
        p = self.pattern('pickup', riders=[(self.taro, T(14, 50)), (self.jiro, T(15, 0))])
        routes.apply_month(self.f, 2026, 12)
        merge.merge(self.taro, self.jiro)                                # 同じ便に 2 人とも乗っている → 1 人にまとめる
        self.assertEqual(list(p.stops.values_list('beneficiary_id', flat=True)), [self.taro.pk])
        self.assertEqual(DayRouteStop.objects.filter(route__date=self.day).count(), 1)
