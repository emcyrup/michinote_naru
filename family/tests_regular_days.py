"""利用曜日の選び漏れ（2026-11-25 成の要望）：曜日の決まった子は、選んでいない曜日の日を「利用」か「都合欠」に決めてもらう"""
import datetime

from django.utils import timezone

from schedules.models import ScheduledVisit

from . import yotei
from .models import DayEvent, FamilyMessage
from .tests import Base


class RegularDaysTests(Base):
    def setUp(self):
        super().setUp()
        today = timezone.localdate()
        self.first = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)        # 来月の 1 日
        self.last = (self.first + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)
        self.taro.weekday_tue = True
        self.taro.save()
        self.tuesdays = [d for d in (self.first + datetime.timedelta(days=n) for n in range(31))
                         if d <= self.last and d.weekday() == 1]

    def test_missing_days_only_for_children_with_weekdays(self):
        self.assertEqual(yotei.missing_regular_days(self.f, self.taro, self.first, self.last), self.tuesdays)
        self.assertEqual(yotei.missing_regular_days(self.f, self.hana, self.first, self.last), [])      # 曜日なし
        # 選んだ日・休業日・もう予定のある日・もう欠席の連絡がある日は除く
        t = self.tuesdays
        DayEvent.objects.create(facility=self.f, date=t[1], is_closed=True)
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.taro, date=t[2])
        yotei.apply_personal_off(self.f, self.taro, self.g_taro, [t[3]])
        self.assertEqual(yotei.missing_regular_days(self.f, self.taro, self.first, self.last, picked=[t[0]]), t[4:])

    def test_personal_off_shows_on_calendar_and_inbox(self):
        day = self.tuesdays[0]
        made, _ = yotei.apply_personal_off(self.f, self.taro, self.g_taro, [day])
        m = made[0]
        self.assertEqual((m.kind, m.reason, m.target_date), (FamilyMessage.KIND_ABSENCE, FamilyMessage.REASON_PERSONAL, day))
        view = yotei.month_view(self.f, day.year, day.month, child=self.taro, window=(self.first, self.last))
        cell = next(c for w in view['weeks'] for c in w if c.get('iso') == day.isoformat())
        self.assertEqual(cell['off'], '都合欠')
        self.assertFalse(cell['can_pick'])
        self.assertEqual(yotei.day_info(self.f, day, child=self.taro)['off'], '都合欠')
        staff = self.client_class()
        staff.force_login(self.staff)
        from django.urls import reverse
        html = staff.get(reverse('family:staff_inbox') + f'?date={day.isoformat()}').content.decode()
        self.assertIn('欠席（都合欠）', html)
        self.assertIn(yotei.PERSONAL_OFF_BODY, html)


class StaffCalendarNoAgendaTests(Base):
    def test_no_month_agenda_under_the_staff_calendar(self):
        """職員の「予定」：カレンダーの下の「◯月の予定」の一覧は出さない（保護者アプリと同じ。2026-11-27 成の要望）"""
        from django.urls import reverse
        day = timezone.localdate().replace(day=15)
        yotei.save_event(self.f, day, 'クッキング', False, '持ち物：エプロン', self.staff)
        c = self.client_class()
        c.force_login(self.staff)
        html = c.get(reverse('family:staff_calendar')).content.decode()
        self.assertIn('クッキング', html)                      # カレンダーの中には出る
        self.assertNotIn(f'{day.month}月の予定', html)
        self.assertNotIn('<ul class="cal-agenda"', html)
