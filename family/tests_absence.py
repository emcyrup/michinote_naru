"""欠席時の対応の記録（2026-11-23 成の要望）：連絡帳の欠席の子 → 保護者の欠席連絡と記録の欄 → 印刷・実績記録票の【欠席】【−】"""
import datetime

from django.urls import reverse
from django.utils import timezone

from schedules.models import ScheduledVisit

from .models import AbsenceRecord, FamilyMessage
from .tests import Base


class AbsenceRecordTests(Base):
    def setUp(self):
        super().setUp()
        self.day = timezone.localdate() + datetime.timedelta(days=2)
        self.sc = self.client_class()
        self.sc.force_login(self.staff)
        self.url = reverse('family:staff_absence', args=[self.taro.pk]) + f'?date={self.day.isoformat()}'
        # 5 日続けて来所予定。はじめの 3 日に欠席連絡（インフルエンザ）。1 日目だけ確認ずみ（予定が欠席）
        for i in range(5):
            d = self.day + datetime.timedelta(days=i)
            ScheduledVisit.objects.create(facility=self.f, beneficiary=self.taro, date=d,
                                          status=ScheduledVisit.STATUS_ABSENT if i == 0 else ScheduledVisit.STATUS_SCHEDULED)
        for i in range(3):
            FamilyMessage.objects.create(facility=self.f, beneficiary=self.taro, kind=FamilyMessage.KIND_ABSENCE,
                                         target_date=self.day + datetime.timedelta(days=i), reason=FamilyMessage.REASON_SICK,
                                         body='インフルエンザのため', sender_name='見本 母（母）', sent_on=timezone.localdate(),
                                         confirmed_at=timezone.now() if i == 0 else None)

    def post(self, **data):
        base = {'date': self.day.isoformat(), 'staff_name': '担当A', 'contact_date': '11/22', 'contact_time': '8:30',
                'contact_person': '母', 'supports': ['health', 'next'], 'methods': ['phone'], 'reasons': ['sick'],
                'next_date': (self.day + datetime.timedelta(days=3)).isoformat(), 'note': '熱が下がるまで休む', 'addon': 'yes'}
        base.update(data)
        return self.sc.post(reverse('family:staff_absence', args=[self.taro.pk]), base)

    def test_absent_row_opens_the_record_with_the_parent_message(self):
        html = self.sc.get(reverse('family:staff_book') + f'?date={self.day.isoformat()}').content.decode()
        self.assertIn(self.url.replace('&', '&amp;'), html)                       # 欠席（グレー）の子は、この画面へ
        html = self.sc.get(self.url).content.decode()
        for text in ('欠席します（病欠）', 'インフルエンザのため', '見本 母（母）', '確認済み',
                     '相談援助の概要', '相談支援の方法', '欠席理由', '備考欄', '次回利用予定日', '欠席時対応加算', '該当しない'):
            self.assertIn(text, html)
        self.assertIn('name="contact_person" value="母"', html)                  # 連絡者は「母」から
        self.assertIn('name="reasons" value="sick" checked', html)               # 病欠 → 体調不良
        self.assertNotIn('name="reasons" value="family" checked', html)
        # 次回利用予定日：続けて休む連絡のある 3 日をとばして、4 日目
        self.assertIn(f'name="next_date" value="{(self.day + datetime.timedelta(days=3)).isoformat()}"', html)

    def test_save_print_and_jisseki_marks(self):
        res = self.post(remember_staff='1')
        self.assertEqual(res.status_code, 302)
        rec = AbsenceRecord.objects.get()
        self.assertEqual((rec.staff_name, rec.contact_person, rec.supports, rec.methods, rec.reasons, rec.addon),
                         ('担当A', '母', ['health', 'next'], ['phone'], ['sick'], 'yes'))
        self.f.refresh_from_db()
        self.assertEqual(self.f.absence_staff_default, '担当A')                  # 次からはこの名前を最初に入れる
        other_day = self.day + datetime.timedelta(days=1)
        html = self.sc.get(reverse('family:staff_absence', args=[self.taro.pk]) + f'?date={other_day.isoformat()}').content.decode()
        self.assertIn('name="staff_name" value="担当A"', html)
        # 印刷：紙の様式の並び（☑ と ☐）
        pr = self.sc.get(reverse('family:staff_absence_print', args=[self.taro.pk]) + f'?date={self.day.isoformat()}').content.decode()
        for text in ('欠席時対応記録', '☑</b>体調等に対する支援をした', '☐</b>家庭内における過ごし方を助言した', '☑</b>電話',
                     '☑</b>体調不良', '☐</b>通院', '熱が下がるまで休む', '担当A', '11/22', '8:30'):
            self.assertIn(text, pr)
        # 実績記録票：該当 → 【欠席】・該当しない → 【−】
        month = reverse('schedules:jisseki_month', args=[self.taro.pk, self.day.year, self.day.month])
        self.assertContains(self.sc.get(month), '【欠席】')
        self.post(addon='no')
        self.assertContains(self.sc.get(month), '【−】')
        self.assertContains(self.sc.get(reverse('schedules:jisseki') + f'?date={self.day.isoformat()}'), '【−】')
        self.assertContains(self.sc.get(reverse('family:staff_book') + f'?date={self.day.isoformat()}'), '欠席・対応記録ずみ（加算 該当しない）')

    def test_unset_addon_is_flagged_and_print_needs_a_saved_record(self):
        month = reverse('schedules:jisseki_month', args=[self.taro.pk, self.day.year, self.day.month])
        self.assertContains(self.sc.get(month), '欠席時対応加算が未選択')
        res = self.sc.get(reverse('family:staff_absence_print', args=[self.taro.pk]) + f'?date={self.day.isoformat()}')
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self.post(next_date='あした').status_code, 302)
        self.assertFalse(AbsenceRecord.objects.exists())                         # 読めない日付では保存しない

    def test_billing_skips_the_addon_when_not_applicable(self):
        from billing.views import _suggest_addons
        from facilities.models import AddonMaster
        target = AddonMaster.objects.filter(name='欠席時対応加算').first() or \
            AddonMaster.objects.create(name='欠席時対応加算', addon_type='individual', code='635495', unit_count=94)
        addons = [target]
        visit = ScheduledVisit.objects.get(beneficiary=self.taro, date=self.day)
        self.assertIn(target.pk, _suggest_addons(visit, self.f, self.day, self.taro, addons))
        self.post(addon='no')
        self.assertNotIn(target.pk, _suggest_addons(visit, self.f, self.day, self.taro, addons))

    def test_other_facility_and_feature_off(self):
        other_staff = type(self.staff).objects.create_user('other_admin', password='pw12345678', facility=self.other)
        c = self.client_class()
        c.force_login(other_staff)
        self.assertEqual(c.get(self.url).status_code, 404)
        self.assertEqual(self.client_class().get(self.url).status_code, 302)       # ログインが要る
        self.f.use_family_page = False
        self.f.save()
        self.assertEqual(self.sc.get(self.url).status_code, 404)

    def test_merge_detects_same_day_records(self):
        from beneficiaries import merge
        AbsenceRecord.objects.create(facility=self.f, beneficiary=self.taro, date=self.day)
        AbsenceRecord.objects.create(facility=self.f, beneficiary=self.jiro, date=self.day)
        self.assertTrue(any('欠席時の対応の記録' in c for c in merge.plan(self.taro, self.jiro)['conflicts']))
