import datetime

from django.test import TestCase
from django.urls import reverse

from accounts.models import StaffAccount
from facilities.models import Facility

from . import services
from .models import ShiftDay, ShiftMember, ShiftPattern, ShiftWeekly

T = datetime.time


class ShiftTests(TestCase):
    """勤務表（シフト）：勤務の種類・載せる人・曜日の基本 → 月の勤務表（曜日の基本から入れる・直す・保存）→ 印刷"""

    def setUp(self):
        self.f = Facility.objects.create(name='見本の事業所', use_shift=True)
        self.other = Facility.objects.create(name='よその事業所', use_shift=True)
        self.plain = Facility.objects.create(name='使わない事業所')
        self.admin = StaffAccount.objects.create_user('sh_admin', password='pw12345678', facility=self.f, role=StaffAccount.ROLE_ADMIN,
                                                      display_name='管理 花子')
        self.staff = StaffAccount.objects.create_user('sh_staff', password='pw12345678', facility=self.f, display_name='職員 太郎')
        self.client.force_login(self.admin)
        self.month, self.setup_url, self.print_url = reverse('shifts:month'), reverse('shifts:setup'), reverse('shifts:print')

    def base(self):
        services.add_defaults(self.f)
        self.early = ShiftPattern.objects.get(facility=self.f, name='早番')
        self.off = ShiftPattern.objects.get(facility=self.f, name='休み')
        self.a = ShiftMember.objects.create(facility=self.f, name='見本 一郎', job='児童指導員', order=0)
        self.b = ShiftMember.objects.create(facility=self.f, name='見本 二子', order=1)

    def test_defaults_and_pattern_minutes(self):
        self.assertContains(self.client.get(self.month), 'はじめに')
        res = self.client.post(self.setup_url, {'action': 'defaults', 'tab': 'patterns'}, follow=True)
        self.assertContains(res, 'よく使う勤務を 7 種類入れました')
        early = ShiftPattern.objects.get(facility=self.f, name='早番')
        self.assertEqual((early.minutes, early.time_label), (480, '08:30〜17:30'))     # 9 時間 − 休憩 60 分
        self.assertEqual(ShiftPattern.objects.get(facility=self.f, name='休み').minutes, 0)
        self.assertEqual(services.add_defaults(self.f), 0)                             # 2 度は入れない
        self.assertFalse(ShiftPattern.objects.filter(facility=self.other).exists())

    def test_pattern_add_edit_delete(self):
        post = lambda **kw: self.client.post(self.setup_url, {'tab': 'patterns', **kw}, follow=True)   # noqa: E731
        self.assertContains(post(action='pattern_add', name='', mark='x'), '名前と印を入れてください')
        self.assertContains(post(action='pattern_add', name='夜', mark='夜', start_time='18:00', end_time='09:00'), '時間を入れてください')
        self.assertContains(post(action='pattern_add', name='短時間', mark='短', start_time='10:00', end_time='15:00', break_minutes='30',
                                 color='#dff0d8'), '「短時間」を保存しました')
        p = ShiftPattern.objects.get(facility=self.f)
        self.assertEqual((p.minutes, p.color, p.is_active), (270, '#dff0d8', True))
        post(action='pattern_save', pk=p.pk, name='短時間', mark='短', is_off='on', is_active='on')
        p.refresh_from_db()
        self.assertEqual((p.is_off, p.start_time, p.minutes), (True, None, 0))
        # 使われている種類は消さずに「使わない」に
        m = ShiftMember.objects.create(facility=self.f, name='見本 一郎')
        ShiftDay.objects.create(facility=self.f, member=m, date=datetime.date(2026, 12, 1), pattern=p)
        self.assertContains(post(action='pattern_delete', pk=p.pk), '「使わない」にしました')
        p.refresh_from_db()
        self.assertFalse(p.is_active)
        ShiftDay.objects.all().delete()
        self.assertContains(post(action='pattern_delete', pk=p.pk), '消しました')
        self.assertFalse(ShiftPattern.objects.exists())

    def test_members_and_import_staff(self):
        post = lambda **kw: self.client.post(self.setup_url, {'tab': 'members', **kw}, follow=True)   # noqa: E731
        self.assertContains(post(action='member_add', name=''), '名前を入れてください')
        post(action='member_add', name='運転 三郎', job='運転手')
        self.assertContains(post(action='import_staff'), 'ログインする職員から 2 人を足しました')
        self.assertEqual(list(ShiftMember.objects.filter(facility=self.f).values_list('name', flat=True)), ['運転 三郎', '管理 花子', '職員 太郎'])
        self.assertContains(post(action='import_staff'), '足す人はいませんでした')
        m = ShiftMember.objects.get(name='運転 三郎')
        post(action='member_save', pk=m.pk, name='運転 三郎', job='運転手', order='9')        # 「載せる」を外す
        m.refresh_from_db()
        self.assertEqual((m.is_active, m.order), (False, 9))
        post(action='member_delete', pk=m.pk)
        self.assertFalse(ShiftMember.objects.filter(pk=m.pk).exists())

    def test_weekly_apply_edit_and_totals(self):
        self.base()
        data = {'action': 'weekly_save', 'tab': 'weekly'}
        for i in range(5):                                   # 月〜金は早番、土は休み
            data[f'w_{self.a.pk}_{i}'] = self.early.pk
        data[f'w_{self.a.pk}_5'] = self.off.pk
        res = self.client.post(self.setup_url, data, follow=True)
        self.assertContains(res, '曜日の基本を保存しました（6 か所）')
        self.assertEqual(ShiftWeekly.objects.filter(member=self.a).count(), 6)
        # 2026 年 12 月：平日 23 日・土曜 4 日
        res = self.client.post(self.month, {'ym': '2026-12', 'action': 'apply'}, follow=True)
        self.assertContains(res, '曜日の基本から 27 マス入れました')
        table = services.month_table(self.f, 2026, 12)
        row = table['rows'][0]
        self.assertEqual((row['member'], row['work_days'], row['hours']), (self.a, 23, '184:00'))
        self.assertEqual(table['counts'][0], 1)              # 12/1（火）は 1 人
        self.assertEqual(table['counts'][5], 0)              # 12/6（日）は 0 人
        # 日ごとに直す：12/1 を休みに、12/6（日）に早番、二子の 12/1 に早番
        res = self.client.post(self.month, {'ym': '2026-12', 'action': 'save', f'c_{self.a.pk}_1': self.off.pk,
                                            f'c_{self.a.pk}_6': self.early.pk, f'c_{self.b.pk}_1': self.early.pk,
                                            f'c_{self.a.pk}_2': ''}, follow=True)
        self.assertContains(res, '4 マスを保存しました')
        self.assertFalse(ShiftDay.objects.filter(member=self.a, date=datetime.date(2026, 12, 2)).exists())
        # もう一度「曜日の基本から入れる」：空いたマスだけ入る。直したマスはそのまま
        res = self.client.post(self.month, {'ym': '2026-12', 'action': 'apply'}, follow=True)
        self.assertContains(res, '1 マス入れました')
        self.assertEqual(ShiftDay.objects.get(member=self.a, date=datetime.date(2026, 12, 1)).pattern, self.off)
        # 「入っているマスも入れ直す」
        self.client.post(self.month, {'ym': '2026-12', 'action': 'apply', 'overwrite': '1'})
        self.assertEqual(ShiftDay.objects.get(member=self.a, date=datetime.date(2026, 12, 1)).pattern, self.early)
        self.assertTrue(ShiftDay.objects.filter(member=self.a, date=datetime.date(2026, 12, 6)).exists())    # 基本の無い曜日は消さない
        # 印刷
        html = self.client.get(self.print_url + '?ym=2026-12').content.decode()
        self.assertIn('勤務表　2026年12月', html)
        self.assertIn('見本 一郎', html)
        # 空にする
        res = self.client.post(self.month, {'ym': '2026-12', 'action': 'clear'}, follow=True)
        self.assertContains(res, '空にしました')
        self.assertFalse(ShiftDay.objects.exists())

    def test_closed_days_are_skipped(self):
        from family.models import DayEvent
        self.base()
        ShiftWeekly.objects.create(member=self.a, weekday=1, pattern=self.early)               # 火曜
        DayEvent.objects.create(facility=self.f, date=datetime.date(2026, 12, 1), is_closed=True)
        self.assertEqual(services.apply_weekly(self.f, 2026, 12, self.admin), 4)               # 火曜 5 日のうち、休業日の 12/1 を除く
        self.assertFalse(ShiftDay.objects.filter(date=datetime.date(2026, 12, 1)).exists())

    def test_staff_can_view_but_not_edit(self):
        self.base()
        ShiftDay.objects.create(facility=self.f, member=self.a, date=datetime.date(2026, 12, 1), pattern=self.early)
        self.client.force_login(self.staff)
        html = self.client.get(self.month + '?ym=2026-12').content.decode()
        self.assertIn('見本 一郎', html)
        self.assertNotIn('<select', html)
        self.assertNotIn('name="action" value="save"', html)
        res = self.client.post(self.month, {'ym': '2026-12', 'action': 'clear'}, follow=True)
        self.assertContains(res, '管理者と児発管だけです')
        self.assertTrue(ShiftDay.objects.exists())
        self.assertRedirects(self.client.get(self.setup_url), self.month)
        self.assertEqual(self.client.get(self.print_url + '?ym=2026-12').status_code, 200)

    def test_other_facility_and_flag(self):
        self.base()
        yoso_p = ShiftPattern.objects.create(facility=self.other, name='よその早番', mark='他', start_time=T(8), end_time=T(17))
        yoso_m = ShiftMember.objects.create(facility=self.other, name='他所 四郎')
        # よその事業所の種類・人は入れられない
        self.client.post(self.month, {'ym': '2026-12', 'action': 'save', f'c_{self.a.pk}_1': yoso_p.pk, f'c_{yoso_m.pk}_1': self.early.pk})
        self.assertFalse(ShiftDay.objects.exists())
        self.assertEqual(self.client.post(self.setup_url, {'action': 'pattern_delete', 'pk': yoso_p.pk}).status_code, 404)
        self.assertNotContains(self.client.get(self.month), '他所 四郎')
        # 使わない事業所はホームへ。ログインが要る
        user = StaffAccount.objects.create_user('pl_admin', password='pw12345678', facility=self.plain, role=StaffAccount.ROLE_ADMIN)
        self.client.force_login(user)
        self.assertRedirects(self.client.get(self.month), reverse('facilities:dashboard'), fetch_redirect_response=False)
        self.assertNotContains(self.client.get(reverse('facilities:dashboard'), follow=True), '勤務表（シフト）</span>')
        self.client.logout()
        self.assertEqual(self.client.get(self.month).status_code, 302)

    def test_sidebar_and_settings_flag(self):
        self.assertContains(self.client.get(self.month), f'href="{self.month}"')
        self.assertContains(self.client.get(reverse('facilities:settings')), 'name="use_shift"')
