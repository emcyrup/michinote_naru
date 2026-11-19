import datetime

from django.test import TestCase
from django.urls import reverse

from accounts.models import StaffAccount
from beneficiaries.models import Beneficiary
from facilities.models import Facility
from records.models import DailyRecord

from . import jisseki
from .models import ScheduledVisit

T = datetime.time


class JissekiTests(TestCase):
    """実績記録票：日ごとの一覧でまとめて時間を入れる・連絡帳を送ったのに未入力なら警告・1 か月ぶんの表"""

    def setUp(self):
        self.f = Facility.objects.create(name='見本の事業所', home_icons=True, use_family_page=True)
        self.other = Facility.objects.create(name='よその事業所', home_icons=True)
        self.plain = Facility.objects.create(name='ふつうの事業所')
        self.staff = StaffAccount.objects.create_user('jz_admin', password='pw12345678', facility=self.f, role=StaffAccount.ROLE_ADMIN)
        kw = dict(date_of_birth=datetime.date(2016, 4, 1))
        self.taro = Beneficiary.objects.create(facility=self.f, last_name='見本', first_name='太郎', last_name_kana='みほん', first_name_kana='たろう', **kw)
        self.hana = Beneficiary.objects.create(facility=self.f, last_name='見本', first_name='花子', last_name_kana='みほん', first_name_kana='はなこ', **kw)
        self.jiro = Beneficiary.objects.create(facility=self.f, last_name='架空', first_name='次郎', last_name_kana='かくう', first_name_kana='じろう', **kw)
        self.yoso = Beneficiary.objects.create(facility=self.other, last_name='他所', first_name='三郎', last_name_kana='よそ', first_name_kana='さぶろう', **kw)
        self.today = datetime.date.today()
        self.v_taro = ScheduledVisit.objects.create(facility=self.f, beneficiary=self.taro, date=self.today)
        self.v_jiro = ScheduledVisit.objects.create(facility=self.f, beneficiary=self.jiro, date=self.today)
        self.v_hana = ScheduledVisit.objects.create(facility=self.f, beneficiary=self.hana, date=self.today, status='absent')
        self.v_yoso = ScheduledVisit.objects.create(facility=self.other, beneficiary=self.yoso, date=self.today)
        self.client.force_login(self.staff)
        self.url = reverse('schedules:jisseki')

    def post(self, visits, start='16:00', end='18:00', action='set', day=None):
        return self.client.post(self.url, {'date': (day or self.today).isoformat(), 'visit': [v.pk for v in visits],
                                           'start': start, 'end': end, 'action': action}, follow=True)

    def test_list_shows_names_only_until_times_are_entered(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('見本 太郎', html)
        self.assertIn('架空 次郎', html)
        self.assertIn('欠席（1 人）', html)
        self.assertNotIn('他所 三郎', html)
        self.assertNotIn('class="jz-time"', html)
        self.assertIn('時間の入力 0 / 2 人', html)
        # 欠席の子にはチェックが無い
        self.assertEqual(html.count('name="visit"'), 2)

    def test_bulk_set_times_marks_attended_and_shows_time(self):
        res = self.post([self.v_taro, self.v_jiro])
        self.assertContains(res, '2 人に 16:00〜18:00 を入れました。')
        self.assertContains(res, '(16:00〜18:00)', count=2)
        self.assertContains(res, '時間の入力 2 / 2 人')
        self.v_taro.refresh_from_db()
        self.assertEqual((self.v_taro.actual_start, self.v_taro.actual_end, self.v_taro.status), (T(16, 0), T(18, 0), 'attended'))

    def test_bulk_ignores_absent_and_other_facility(self):
        res = self.post([self.v_hana, self.v_yoso])
        self.assertContains(res, 'チェックを付けてください')
        self.v_hana.refresh_from_db(); self.v_yoso.refresh_from_db()
        self.assertIsNone(self.v_hana.actual_start)
        self.assertIsNone(self.v_yoso.actual_start)
        self.assertEqual(self.v_hana.status, 'absent')

    def test_bad_times_are_rejected(self):
        for start, end, msg in [('18:00', '16:00', '始まりより後に'), ('', '18:00', '始まりの時間を入れてください'),
                                ('', '', '始まりの時間を入れてください'), ('abc', '18:00', '始まりの時間が読めません')]:
            res = self.post([self.v_taro], start, end)
            self.assertContains(res, msg)
        self.v_taro.refresh_from_db()
        self.assertIsNone(self.v_taro.actual_start)
        self.assertEqual(self.v_taro.status, 'scheduled')

    def test_start_only_is_partial(self):
        res = self.post([self.v_taro], '16:00', '')
        self.assertContains(res, '1 人に 16:00〜 を入れました。')
        self.assertContains(res, '終わりの時間が未入力')
        self.assertContains(res, '時間の入力 0 / 2 人')

    def test_clear(self):
        self.post([self.v_taro])
        res = self.post([self.v_taro], action='clear')
        self.assertContains(res, '1 人の時間を消しました。')
        self.v_taro.refresh_from_db()
        self.assertEqual((self.v_taro.actual_start, self.v_taro.actual_end), (None, None))
        self.assertEqual(self.v_taro.status, 'attended')        # 状態は戻さない（出欠は予定の画面で）

    def test_future_day_keeps_scheduled(self):
        day = self.today + datetime.timedelta(days=3)
        v = ScheduledVisit.objects.create(facility=self.f, beneficiary=self.taro, date=day)
        self.post([v], day=day)
        v.refresh_from_db()
        self.assertEqual((v.actual_start, v.status), (T(16, 0), 'scheduled'))

    def test_warns_when_contact_book_sent_without_times(self):
        from family.models import ContactBook
        ContactBook.objects.create(facility=self.f, beneficiary=self.taro, date=self.today, body='きょうの様子', status=ContactBook.STATUS_SENT)
        ContactBook.objects.create(facility=self.f, beneficiary=self.jiro, date=self.today, body='下書き', status=ContactBook.STATUS_DRAFT)
        html = self.client.get(self.url).content.decode()
        self.assertIn('時間が入っていない子が 1 人います', html)
        self.assertEqual(html.count('jz-row is-warn'), 1)
        html = self.post([self.v_taro]).content.decode()
        self.assertNotIn('時間が入っていない子が', html)

    def test_journal_times_are_shown_and_kept_in_step(self):
        rec = DailyRecord.objects.create(facility=self.f, beneficiary=self.taro, date=self.today, entry_time=T(15, 30), exit_time=T(17, 30))
        html = self.client.get(self.url).content.decode()
        self.assertIn('(15:30〜17:30)', html)
        self.assertIn('日誌より', html)
        self.post([self.v_taro], '15:00', '17:00')
        rec.refresh_from_db()
        self.assertEqual((rec.entry_time, rec.exit_time), (T(15, 0), T(17, 0)))
        self.assertNotIn('日誌より', self.client.get(self.url).content.decode())

    def test_day_arrows(self):
        day = datetime.date(2026, 12, 1)
        html = self.client.get(self.url + '?date=2026-12-01').content.decode()
        self.assertIn('12月1日', html)
        self.assertIn('?date=2026-11-30', html)
        self.assertIn('?date=2026-12-02', html)
        self.assertEqual(jisseki.day_rows(self.f, day), ([], []))

    def test_month_sheet_and_save(self):
        first = self.today.replace(day=1)
        other_day = first if first != self.today else first + datetime.timedelta(days=1)
        v2 = ScheduledVisit.objects.create(facility=self.f, beneficiary=self.taro, date=other_day, has_pickup=True)
        url = reverse('schedules:jisseki_month', args=[self.taro.pk, self.today.year, self.today.month])
        html = self.client.get(url).content.decode()
        self.assertIn('見本 太郎', html)
        self.assertIn(f'name="s_{self.v_taro.pk}"', html)
        self.assertIn(f'name="s_{v2.pk}"', html)
        self.assertIn('迎え', html)
        res = self.client.post(url, {f's_{self.v_taro.pk}': '16:00', f'e_{self.v_taro.pk}': '18:15',
                                     f's_{v2.pk}': '18:00', f'e_{v2.pk}': '17:00', 'back': self.today.isoformat()}, follow=True)
        self.assertContains(res, '1 日ぶんを保存しました。')
        self.assertContains(res, '始まりより後に')
        self.assertContains(res, '2:15')
        self.v_taro.refresh_from_db(); v2.refresh_from_db()
        self.assertEqual(self.v_taro.actual_end, T(18, 15))
        self.assertIsNone(v2.actual_start)
        # 何も変えずに保存
        res = self.client.post(url, {f's_{self.v_taro.pk}': '16:00', f'e_{self.v_taro.pk}': '18:15',
                                     f's_{v2.pk}': '', f'e_{v2.pk}': ''}, follow=True)
        self.assertContains(res, '変わったところはありません。')

    def test_month_sheet_counts(self):
        self.post([self.v_taro], '16:00', '18:30')
        sheet = jisseki.month_sheet(self.f, self.taro, self.today.year, self.today.month)
        self.assertEqual((sheet['days_used'], sheet['total'], sheet['days_missing']), (1, '2:30', 0))
        sheet = jisseki.month_sheet(self.f, self.jiro, self.today.year, self.today.month)
        self.assertEqual((sheet['days_used'], sheet['total'], sheet['days_missing']), (0, '', 1))
        sheet = jisseki.month_sheet(self.f, self.hana, self.today.year, self.today.month)
        self.assertEqual((sheet['days_absent'], sheet['days_missing']), (1, 0))

    def test_other_facility_child_and_bad_month_are_404(self):
        self.assertEqual(self.client.get(reverse('schedules:jisseki_child', args=[self.yoso.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('schedules:jisseki_month', args=[self.taro.pk, 2026, 13])).status_code, 404)
        self.assertEqual(self.client.get(reverse('schedules:jisseki_child', args=[self.taro.pk])).status_code, 200)

    def test_facility_without_icon_home_cannot_open(self):
        staff = StaffAccount.objects.create_user('plain_admin', password='pw12345678', facility=self.plain, role=StaffAccount.ROLE_ADMIN)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertNotIn('実績記録票', self.client.get('/', follow=True).content.decode())

    def test_login_required(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_home_icon_and_sidebar_link(self):
        html = self.client.get(reverse('facilities:home_icons')).content.decode()
        self.assertIn(f'href="{self.url}"', html)
