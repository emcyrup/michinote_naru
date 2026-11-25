"""実績記録票の印刷（様式５。2026-11-26 成の様式）"""
import datetime

from django.test import TestCase
from django.urls import reverse

from accounts.models import StaffAccount
from beneficiaries.models import Beneficiary, Guardian
from facilities.models import Facility
from family.models import AbsenceRecord

from .models import ScheduledVisit


class JissekiPrintTests(TestCase):
    def setUp(self):
        self.f = Facility.objects.create(name='見本の事業所', home_icons=True, use_family_page=True, office_number='2799999999')
        self.staff = StaffAccount.objects.create_user('jp_admin', password='pw12345678', facility=self.f, role=StaffAccount.ROLE_ADMIN)
        self.client.force_login(self.staff)
        self.b = Beneficiary.objects.create(facility=self.f, last_name='架空', first_name='一郎', last_name_kana='かくう',
                                            first_name_kana='いちろう', date_of_birth=datetime.date(2016, 4, 1))
        Guardian.objects.create(beneficiary=self.b, last_name='架空', first_name='母')
        self.y, self.m = 2026, 10
        d = lambda n: datetime.date(self.y, self.m, n)                     # noqa: E731
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.b, date=d(6), status='attended', has_pickup=True,
                                      actual_start=datetime.time(14, 0), actual_end=datetime.time(17, 30))
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.b, date=d(10), status='attended',      # 土曜
                                      actual_start=datetime.time(10, 0), actual_end=datetime.time(15, 0), has_dropoff=True)
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.b, date=d(13), status='absent')
        AbsenceRecord.objects.create(facility=self.f, beneficiary=self.b, date=d(13), addon='yes')
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.b, date=d(14), status='absent')       # 加算を選んでいない
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.b, date=d(20), status='absent')
        AbsenceRecord.objects.create(facility=self.f, beneficiary=self.b, date=d(20), addon='no')

    def test_one_child(self):
        html = self.client.get(reverse('schedules:jisseki_print', args=[self.b.pk, self.y, self.m])).content.decode()
        for text in ('放課後等デイサービス提供実績記録票', '（様式５）', '令和　8　年　10　月分', '架空 母　（架空 一郎）', '見本の事業所',
                     '>14:00<', '>17:30<', '>10:00<', '>15:00<', '>欠席<', '>−<', '1回', '保育・教育等移行支援加算'):
            self.assertIn(text, html)
        self.assertIn('<td class="d">6</td><td class="d">火</td><td class="d"></td><td class="d">1</td>', html)   # 平日＝1
        self.assertIn('<td class="d">10</td><td class="d">土</td><td class="d"></td><td class="d">2</td>', html)  # 土日祝＝2
        self.assertNotIn('<td class="d">14</td>', html)                     # 加算を選んでいない欠席は載せない
        self.assertEqual(html.count('<td class="big">9</td>'), 8)            # 事業所番号のマス

    def test_all_children_and_access(self):
        res = self.client.get(reverse('schedules:jisseki_print_all', args=[self.y, self.m]))
        self.assertContains(res, '（架空 一郎）')
        self.assertContains(self.client.get(reverse('schedules:jisseki_month', args=[self.b.pk, self.y, self.m])), '実績記録票を印刷（様式５）')
        other = Facility.objects.create(name='よそ', home_icons=True)
        c = self.client_class()
        c.force_login(StaffAccount.objects.create_user('jp_other', password='pw12345678', facility=other))
        self.assertEqual(c.get(reverse('schedules:jisseki_print', args=[self.b.pk, self.y, self.m])).status_code, 404)
        self.assertNotContains(c.get(reverse('schedules:jisseki_print_all', args=[self.y, self.m])), '架空 一郎')
