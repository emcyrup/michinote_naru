"""保護者ページ（公式 LINE から開く）・アイコンのホーム・日誌の日ごとの一覧（2026-11 事業所「成」の要望）"""
import base64
import datetime
import hashlib
import hmac
import json
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import StaffAccount
from beneficiaries.models import Beneficiary, Guardian
from facilities.models import Facility
from records.models import DailyRecord
from schedules.models import ScheduledVisit

from . import services
from .models import (ContactBook, DayEvent, FamilyAccess, FamilyMessage, Notice, NoticeGroup, NoticeRecipient,
                     ParentNote, UseRequest)


def _body(text, user_id='U1'):
    return json.dumps({'destination': 'Ubot', 'events': [{
        'type': 'message', 'mode': 'active', 'timestamp': 1, 'webhookEventId': 'e1',
        'deliveryContext': {'isRedelivery': False}, 'replyToken': 'r1',
        'source': {'type': 'user', 'userId': user_id},
        'message': {'id': 'm1', 'type': 'text', 'text': text, 'quoteToken': 'q'},
    }]})


def _sig(secret, body):
    return base64.b64encode(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()).decode()


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.f = Facility.objects.create(name='見本の事業所', line_channel_secret='secret-a', line_channel_access_token='tok-a',
                                         use_family_page=True, home_icons=True)
        self.other = Facility.objects.create(name='よその事業所', line_channel_secret='secret-b', line_channel_access_token='tok-b',
                                             use_family_page=True)
        self.staff = StaffAccount.objects.create_user('fam_admin', password='pw12345678', facility=self.f, role=StaffAccount.ROLE_ADMIN)
        kw = dict(date_of_birth=datetime.date(2016, 4, 1))
        self.taro = Beneficiary.objects.create(facility=self.f, last_name='見本', first_name='太郎', last_name_kana='みほん', first_name_kana='たろう', **kw)
        self.hana = Beneficiary.objects.create(facility=self.f, last_name='見本', first_name='花子', last_name_kana='みほん', first_name_kana='はなこ', **kw)
        self.jiro = Beneficiary.objects.create(facility=self.f, last_name='架空', first_name='次郎', last_name_kana='かくう', first_name_kana='じろう', **kw)
        self.g_taro = Guardian.objects.create(beneficiary=self.taro, last_name='見本', first_name='母', line_user_id='U1', line_linked=True)
        self.g_hana = Guardian.objects.create(beneficiary=self.hana, last_name='見本', first_name='母', line_user_id='U1', line_linked=True)
        self.g_jiro = Guardian.objects.create(beneficiary=self.jiro, last_name='架空', first_name='父')   # LINE 未連携

    def enter(self, line_user_id='U1', facility=None):
        access = services.access_for(facility or self.f, line_user_id)
        return access, self.client.get(reverse('family:enter', args=[access.token]))

    def line(self, facility, secret, text, user_id='U1'):
        body = _body(text, user_id)
        url = reverse('line_integration:webhook_facility', args=[facility.pk])
        with mock.patch('linebot.v3.messaging.MessagingApi.reply_message') as reply:
            res = self.client.post(url, data=body, content_type='application/json', HTTP_X_LINE_SIGNATURE=_sig(secret, body))
        return res, reply


class EnterTests(Base):
    def test_link_enters_and_hides_token(self):
        access, res = self.enter()
        self.assertRedirects(res, reverse('family:home'), fetch_redirect_response=False)
        self.assertEqual(res['Referrer-Policy'], 'same-origin')
        self.assertIn('no-store', res['Cache-Control'])
        home = self.client.get(reverse('family:home'))
        self.assertEqual(home.status_code, 200)
        html = home.content.decode()
        self.assertNotIn(access.token, html)                      # 合い言葉は画面にもアドレスにも出さない
        self.assertIn('見本の事業所', html)
        access.refresh_from_db()
        self.assertIsNotNone(access.last_used_at)

    def test_home_has_eight_icons_in_three_columns(self):
        self.enter()
        html = self.client.get(reverse('family:home')).content.decode()
        for label in ('保護者より', '見本の事業所より', '欠席連絡', 'その他の連絡', '実績記録票', '利用日申請', 'お知らせ', '個別支援計画'):
            self.assertIn(label, html)
        self.assertEqual(html.count('class="fm-icon"'), 8)
        self.assertIn('grid-template-columns: repeat(3', html)
        page = self.client.get(reverse('family:page', args=['jisseki']))
        self.assertContains(page, '準備中')
        # できた画面（欠席連絡・その他の連絡）は「準備中」を出さず、その画面へ
        self.assertRedirects(self.client.get(reverse('family:page', args=['absence'])), reverse('family:absence'))
        self.assertIn(f'href="{reverse("family:absence")}"', html)
        self.assertEqual(html.count('class="fm-soon"'), 1)        # 残りは 実績記録票（保護者サイン）だけ
        self.assertEqual(self.client.get(reverse('family:page', args=['nope'])).status_code, 404)

    def test_without_link_is_closed(self):
        self.assertEqual(self.client.get(reverse('family:home')).status_code, 403)
        res = self.client.get(reverse('family:enter', args=['x' * 43]))
        self.assertEqual(res.status_code, 404)
        self.assertContains(res, 'このリンクは使えません', status_code=404)

    def test_siblings_and_only_own_children(self):
        self.enter()
        html = self.client.get(reverse('family:home')).content.decode()
        self.assertIn('見本 太郎', html)
        self.assertIn('見本 花子', html)
        self.assertNotIn('架空 次郎', html)                        # ほかの家庭の子は出ない
        # きょうだいの切り替え。ほかの家庭の子の番号を送っても切り替わらない
        self.client.post(reverse('family:child'), {'child': self.taro.pk})
        self.assertContains(self.client.get(reverse('family:home')), '保護者ページ・見本 太郎')
        self.client.post(reverse('family:child'), {'child': self.jiro.pk})
        self.assertContains(self.client.get(reverse('family:home')), '保護者ページ・見本 太郎')

    def test_rotate_closes_old_link_and_open_session(self):
        access, _ = self.enter()
        old = access.token
        access.rotate()
        self.assertEqual(self.client.get(reverse('family:home')).status_code, 403)       # 開いたままの端末も入り直し
        self.assertEqual(self.client.get(reverse('family:enter', args=[old])).status_code, 404)
        self.assertEqual(self.client.get(reverse('family:enter', args=[access.token])).status_code, 302)

    def test_closed_when_feature_off_unlinked_or_left(self):
        access, _ = self.enter()
        self.f.use_family_page = False
        self.f.save()
        self.assertEqual(self.client.get(reverse('family:home')).status_code, 403)
        self.assertEqual(self.client.get(reverse('family:enter', args=[access.token])).status_code, 404)
        self.f.use_family_page = True
        self.f.save()
        self.enter()
        # 退所した子だけの保護者は入れない
        Beneficiary.objects.filter(pk__in=[self.taro.pk, self.hana.pk]).update(status='inactive')
        self.assertEqual(self.client.get(reverse('family:home')).status_code, 403)
        Beneficiary.objects.filter(pk__in=[self.taro.pk, self.hana.pk]).update(status='active')
        self.enter()
        self.assertEqual(self.client.get(reverse('family:home')).status_code, 200)
        self.assertContains(self.client.post(reverse('family:leave')), 'やめました')
        self.assertEqual(self.client.get(reverse('family:home')).status_code, 403)

    def test_session_expires(self):
        self.enter()
        s = self.client.session
        data = s[services.SESSION_KEY]
        data['at'] = (timezone.now() - datetime.timedelta(days=31)).isoformat()
        s[services.SESSION_KEY] = data
        s.save()
        self.assertEqual(self.client.get(reverse('family:home')).status_code, 403)

    def test_no_access_for_non_guardian(self):
        self.assertIsNone(services.access_for(self.f, 'U-unknown'))
        self.assertIsNone(services.access_for(self.f, ''))
        self.assertIsNone(services.access_for(self.other, 'U1'))    # よその事業所の保護者ではない
        self.assertEqual(FamilyAccess.objects.count(), 0)


class CsrfTests(Base):
    """保護者ページの送信が、ブラウザと同じ条件（CSRF の確認あり・Origin つき）で通る"""

    def test_post_passes_csrf_with_same_origin_policy(self):
        from django.test import Client
        c = Client(enforce_csrf_checks=True)
        access = services.access_for(self.f, 'U1')
        c.get(reverse('family:enter', args=[access.token]))
        page = c.get(reverse('family:message'))
        html = page.content.decode()
        # no-referrer だと、ブラウザは同じサイト内の POST にも Origin: null を付けて CSRF で 403 になる
        self.assertEqual(page['Referrer-Policy'], 'same-origin')
        self.assertIn('<meta name="referrer" content="same-origin">', html)
        self.assertNotIn('no-referrer', html)
        token = html.split('name="csrfmiddlewaretoken" value="')[1].split('"')[0]
        ok = c.post(reverse('family:message'), {'csrfmiddlewaretoken': token, 'use_today': '1', 'body': 'x'}, HTTP_ORIGIN='http://testserver')
        self.assertEqual(ok.status_code, 302)
        ng = c.post(reverse('family:message'), {'csrfmiddlewaretoken': token, 'use_today': '1', 'body': 'x'}, HTTP_ORIGIN='null')
        self.assertEqual(ng.status_code, 403)


class LineTests(Base):
    def test_menu_word_replies_link(self):
        for word in ('メニュー', ' めにゅー ', 'ホーム', 'menu'):
            res, reply = self.line(self.f, 'secret-a', word)
            self.assertEqual(res.status_code, 200)
            text = reply.call_args[0][0].messages[0].text
            access = FamilyAccess.objects.get(facility=self.f, line_user_id='U1')
            self.assertIn(f'/family/enter/{access.token}/', text)
        self.assertEqual(FamilyAccess.objects.count(), 1)           # 何度送っても同じリンク

    def test_menu_word_ignored_when_off_or_unknown(self):
        self.f.use_family_page = False
        self.f.save()
        _, reply = self.line(self.f, 'secret-a', 'メニュー')
        self.assertFalse(reply.called)
        self.f.use_family_page = True
        self.f.save()
        _, reply = self.line(self.f, 'secret-a', 'メニュー', user_id='U-unknown')   # 未連携の人には登録コードの案内
        self.assertNotIn('/family/enter/', reply.call_args[0][0].messages[0].text)
        self.assertEqual(FamilyAccess.objects.count(), 0)

    def test_link_follows_registration(self):
        code = self.g_jiro.line_registration_code
        _, reply = self.line(self.f, 'secret-a', code, user_id='U9')
        text = reply.call_args[0][0].messages[0].text
        self.assertIn('登録しました', text)
        self.assertIn('/family/enter/', text)


class StaffTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def test_send_and_rotate_from_staff_screen(self):
        html = self.client.get(reverse('beneficiaries:detail', args=[self.taro.pk])).content.decode()
        self.assertIn('リンクを LINE で送る', html)
        with mock.patch('family.views.push_text', return_value=(True, '')) as push:
            self.client.post(reverse('family:staff_send_link', args=[self.g_taro.pk]))
        self.assertEqual(push.call_args[0][1], 'U1')
        access = FamilyAccess.objects.get(facility=self.f, line_user_id='U1')
        self.assertIn(access.token, push.call_args[0][2])
        old = access.token
        self.client.post(reverse('family:staff_rotate_link', args=[self.g_taro.pk]))
        access.refresh_from_db()
        self.assertNotEqual(access.token, old)
        # 未連携の保護者には送れない。よその事業所の保護者は 404
        with mock.patch('family.views.push_text') as push:
            self.client.post(reverse('family:staff_send_link', args=[self.g_jiro.pk]))
        self.assertFalse(push.called)
        ben = Beneficiary.objects.create(facility=self.other, last_name='他所', first_name='子', date_of_birth=datetime.date(2016, 4, 1))
        g = Guardian.objects.create(beneficiary=ben, last_name='他所', first_name='母', line_user_id='U5', line_linked=True)
        self.assertEqual(self.client.post(reverse('family:staff_send_link', args=[g.pk])).status_code, 404)

    def test_staff_pages_need_login(self):
        self.client.logout()
        res = self.client.post(reverse('family:staff_send_link', args=[self.g_taro.pk]))
        self.assertEqual(res.status_code, 302)
        self.assertIn('/accounts/login/', res['Location'])

    def test_other_page_is_staff_only(self):
        other = reverse('facilities:home_other')
        self.assertIn(f'href="{other}"', self.client.get(reverse('facilities:home_icons')).content.decode())
        html = self.client.get(other).content.decode()
        for label in ('送迎記録', '研修', '支援計画シート'):
            self.assertIn(f'<span class="ih-label">{label}</span>', html)
        self.assertEqual(html.count('class="ih-soon"'), 3)            # 送迎を使わない設定なら、送迎記録も準備中
        page = self.client.get(reverse('facilities:soon', args=['kenshu'])).content.decode()
        self.assertIn('「研修」の画面は準備中です', page)
        self.assertIn(f'href="{other}"', page)                        # 準備中の案内からは「その他」へ戻る
        # 送迎を使う設定にすると、送迎記録はいまある送迎の画面へ
        self.f.use_transport = True
        self.f.save()
        html = self.client.get(other).content.decode()
        self.assertIn(f'href="{reverse("transport:day")}"', html)
        self.assertEqual(html.count('class="ih-soon"'), 2)
        # 保護者ページには「その他」を出さない
        self.client.logout()
        self.enter()
        self.assertNotIn('その他</span>', self.client.get(reverse('family:home')).content.decode().replace('その他の連絡', ''))
        # アイコンのホームを使わない事業所
        self.f.home_icons = False
        self.f.save()
        self.client.force_login(self.staff)
        self.assertRedirects(self.client.get(other), '/?full=1', fetch_redirect_response=False)

    def test_icon_home(self):
        res = self.client.get(reverse('facilities:dashboard'))
        self.assertRedirects(res, reverse('facilities:home_icons'))
        html = self.client.get(reverse('facilities:home_icons')).content.decode()
        for label in ('連絡帳', '日誌', '保護者からの\n連絡', '実績記録票', '予定', 'お知らせ', '個別支援計画', 'その他'):
            self.assertIn(f'<span class="ih-label">{label}</span>', html)
        self.assertEqual(html.count('class="ih-icon"'), 8)
        self.assertLess(html.index('個別支援計画</span>'), html.index('その他</span>'))        # 個別支援計画の横（次）
        self.assertIn(reverse('records:day'), html)
        # これまでのホームも開ける。準備中の画面
        self.assertEqual(self.client.get(reverse('facilities:dashboard') + '?full=1').status_code, 200)
        self.assertContains(self.client.get(reverse('facilities:soon', args=['renraku'])), '準備中')
        self.assertContains(self.client.get(reverse('facilities:soon', args=['from_family'])), '「保護者からの連絡」の画面は準備中です')
        self.assertEqual(self.client.get(reverse('facilities:soon', args=['nope'])).status_code, 404)
        # オフの事業所はこれまでどおり
        self.f.home_icons = False
        self.f.save()
        self.assertEqual(self.client.get(reverse('facilities:dashboard')).status_code, 200)
        self.assertRedirects(self.client.get(reverse('facilities:home_icons')), '/?full=1', fetch_redirect_response=False)

    def test_settings_checkboxes(self):
        self.f.home_icons = False
        self.f.use_family_page = False
        self.f.save()
        self.assertContains(self.client.get(reverse('facilities:settings')), 'name="use_family_page"')
        self.client.post(reverse('facilities:feature_settings'), {
            'use_line': 'on', 'use_schedule': 'on', 'home_icons': 'on', 'use_family_page': 'on',
            'journal_sections': [k for k, _ in Facility.JOURNAL_SECTIONS]})
        self.f.refresh_from_db()
        self.assertTrue(self.f.home_icons and self.f.use_family_page)

    def test_command(self):
        out = StringIO()
        call_command('family_page', facility=self.f.pk, off=True, stdout=out)
        self.f.refresh_from_db()
        self.assertFalse(self.f.home_icons or self.f.use_family_page)
        call_command('family_page', facility=self.f.pk, stdout=out)
        self.f.refresh_from_db()
        self.assertTrue(self.f.home_icons and self.f.use_family_page)


class DayListTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)
        self.day = datetime.date(2026, 12, 1)
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.taro, date=self.day, status='scheduled')
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.hana, date=self.day, status='absent')
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.jiro, date=self.day + datetime.timedelta(days=1), status='scheduled')

    def test_only_children_scheduled_that_day(self):
        html = self.client.get(reverse('records:day') + '?date=2026-12-01').content.decode()
        self.assertIn('12月1日（火）', html)
        self.assertIn('見本 太郎', html)
        self.assertNotIn('架空 次郎', html)                        # 次の日の子は出ない
        # 欠席の子は下に分けてグレー
        self.assertIn('欠席（1 人）', html)
        self.assertLess(html.index('見本 太郎'), html.index('欠席（1 人）'))
        self.assertLess(html.index('欠席（1 人）'), html.index('見本 花子'))
        self.assertIn('dayitem is-absent', html)
        # ←→
        self.assertIn('?date=2026-11-30', html)
        self.assertIn('?date=2026-12-02', html)
        self.assertIn(f'{reverse("records:list", args=[self.taro.pk])}?new_date=2026-12-01', html)
        nxt = self.client.get(reverse('records:day') + '?date=2026-12-02').content.decode()
        self.assertIn('架空 次郎', nxt)
        self.assertNotIn('見本 太郎', nxt)

    def test_journal_state_and_unscheduled_record(self):
        rec = DailyRecord.objects.create(facility=self.f, beneficiary=self.taro, date=self.day, status=DailyRecord.STATUS_CONFIRMED)
        extra = DailyRecord.objects.create(facility=self.f, beneficiary=self.jiro, date=self.day, status=DailyRecord.STATUS_DRAFT)
        html = self.client.get(reverse('records:day') + '?date=2026-12-01').content.decode()
        self.assertIn('日誌 1 / 1 人 確定', html)
        self.assertIn(f'?selected={rec.pk}', html)
        self.assertIn('予定には無いが、この日の日誌がある子', html)
        self.assertIn(f'?selected={extra.pk}', html)

    def test_bad_date_and_other_facility(self):
        self.assertEqual(self.client.get(reverse('records:day') + '?date=nope').status_code, 200)
        ben = Beneficiary.objects.create(facility=self.other, last_name='他所', first_name='子', date_of_birth=datetime.date(2016, 4, 1))
        ScheduledVisit.objects.create(facility=self.other, beneficiary=ben, date=self.day, status='scheduled')
        self.assertNotIn('他所 子', self.client.get(reverse('records:day') + '?date=2026-12-01').content.decode())


class ContactTests(Base):
    """保護者からの連絡：欠席連絡・その他の連絡 → 職員の日ごとの画面（赤・黒）→ 確認で保護者に確認済み・欠席を予定へ"""

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.d1 = self.today + datetime.timedelta(days=1)
        self.d7 = self.today + datetime.timedelta(days=7)
        self.enter()
        self.client.post(reverse('family:child'), {'child': self.taro.pk})
        self.staff_client = self.client_class()
        self.staff_client.force_login(self.staff)

    def inbox(self, day):
        return self.staff_client.get(reverse('family:staff_inbox') + f'?date={day.isoformat()}').content.decode()

    def test_absence_form_and_validation(self):
        html = self.client.get(reverse('family:absence')).content.decode()
        for text in ('欠席する日にちを選択してください', f'今日（{self.today.month}月{self.today.day}日）', 'カレンダーから日にちを選択', '次へ', '病欠', '都合欠', '備考', '送信'):
            self.assertIn(text, html)
        # 日にちなし・理由なしは送れない
        res = self.client.post(reverse('family:absence'), {'reason': 'sick'})
        self.assertEqual(res.status_code, 400)
        self.assertContains(res, '日にちを選んでください', status_code=400)
        res = self.client.post(reverse('family:absence'), {'use_today': '1'})
        self.assertContains(res, '理由', status_code=400)
        self.assertEqual(FamilyMessage.objects.count(), 0)

    def test_absence_today_and_several_days(self):
        res = self.client.post(reverse('family:absence'), {'use_today': '1', 'dates': [self.d1.isoformat(), self.d7.isoformat()],
                                                           'reason': 'sick', 'body': '熱があります'})
        self.assertRedirects(res, reverse('family:absence') + '?done=1', fetch_redirect_response=False)
        msgs = list(FamilyMessage.objects.order_by('target_date'))
        self.assertEqual([m.target_date for m in msgs], [self.today, self.d1, self.d7])       # 日ごとに 1 件
        self.assertTrue(all(m.beneficiary_id == self.taro.pk and m.kind == 'absence' and m.reason == 'sick' and m.sent_on == self.today for m in msgs))
        self.assertIn('見本 母', msgs[0].sender_name)
        done = self.client.get(reverse('family:absence') + '?done=1').content.decode()
        self.assertIn('の連絡を送りました', done)
        self.assertIn('未確認', done)
        # 同じ日の欠席をもう一度送っても増えない
        self.client.post(reverse('family:absence'), {'use_today': '1', 'reason': 'personal'})
        self.assertEqual(FamilyMessage.objects.count(), 3)
        self.assertContains(self.client.get(reverse('family:absence') + '?done=1'), 'すでに欠席の連絡をいただいています')

    def test_dates_outside_range_are_dropped(self):
        past = self.today - datetime.timedelta(days=1)
        far = self.today + datetime.timedelta(days=services.DATE_AHEAD_DAYS + 1)
        res = self.client.post(reverse('family:message'), {'dates': [past.isoformat(), far.isoformat(), 'nope'], 'body': 'x'})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(FamilyMessage.objects.count(), 0)

    def test_other_message_needs_body(self):
        html = self.client.get(reverse('family:message')).content.decode()
        self.assertIn('連絡の該当日を選択してください', html)
        self.assertIn('連絡内容', html)
        self.assertEqual(self.client.post(reverse('family:message'), {'use_today': '1', 'body': '  '}).status_code, 400)
        self.client.post(reverse('family:message'), {'use_today': '1', 'body': '今日の帰りの迎えは父にお願いします'})
        m = FamilyMessage.objects.get()
        self.assertEqual((m.kind, m.target_date, m.reason), ('other', self.today, ''))

    def test_staff_sees_today_in_red_and_confirms(self):
        self.client.post(reverse('family:message'), {'use_today': '1', 'body': '今日は習い事があるので 17:30 に送ってください'})
        m = FamilyMessage.objects.get()
        html = self.inbox(self.today)
        self.assertIn('fmsg is-red', html)
        self.assertIn('17:30 に送ってください', html)
        self.assertIn('見本 太郎 さん', html)
        self.assertIn('未確認 1 件', html)
        # ホームのアイコンと左のメニューに未確認の数
        home = self.staff_client.get(reverse('facilities:home_icons')).content.decode()
        self.assertIn('class="ih-badge"', home)
        self.assertIn(reverse('family:staff_inbox'), home)
        # 確認 → 保護者ページに確認済み
        self.staff_client.post(reverse('family:staff_confirm', args=[m.pk]), {'date': self.today.isoformat()})
        m.refresh_from_db()
        self.assertTrue(m.is_confirmed)
        self.assertEqual(m.confirmed_by, self.staff)
        self.assertIn('確認済み', self.inbox(self.today))
        self.assertNotIn('class="ih-badge"', self.staff_client.get(reverse('facilities:home_icons')).content.decode())
        parent = self.client.get(reverse('family:message')).content.decode()
        self.assertIn('fm-state ok', parent)
        # 取り消すと未確認に戻る
        self.staff_client.post(reverse('family:staff_confirm', args=[m.pk]), {'date': self.today.isoformat(), 'undo': '1'})
        m.refresh_from_db()
        self.assertFalse(m.is_confirmed)

    def test_future_message_is_black_on_sent_day_and_moves_on_confirm(self):
        self.client.post(reverse('family:message'), {'dates': [self.d7.isoformat()], 'body': '17:30 送りでお願いします'})
        m = FamilyMessage.objects.get()
        sent_day = self.inbox(self.today)
        self.assertIn('fmsg is-black', sent_day)                    # 届いた日は黒い字
        self.assertNotIn('fmsg is-red', sent_day)
        self.assertIn('この日に届いた、先の日付の連絡', sent_day)
        self.assertIn(f'{self.d7.month}月{self.d7.day}日', sent_day)
        res = self.staff_client.post(reverse('family:staff_confirm', args=[m.pk]), {'date': self.today.isoformat()}, follow=True)
        self.assertContains(res, 'のページに移りました')
        after = self.inbox(self.today)
        self.assertNotIn('17:30 送りでお願いします', after)          # 届いた日のページからは消える
        target = self.inbox(self.d7)
        self.assertIn('fmsg is-red', target)                        # 対象日のページに赤い字
        self.assertIn('17:30 送りでお願いします', target)
        self.assertIn('確認済み', target)

    def test_unconfirmed_on_other_days_is_listed(self):
        self.client.post(reverse('family:message'), {'dates': [self.d7.isoformat()], 'body': 'あとで'})
        html = self.inbox(self.d1)
        self.assertIn('ほかの日に、まだ確認していない連絡があります', html)
        self.assertIn(f'?date={self.today.isoformat()}', html)

    def test_absence_confirm_sets_visit_absent_and_undo_restores(self):
        visit = ScheduledVisit.objects.create(facility=self.f, beneficiary=self.taro, date=self.d1, status='scheduled')
        self.client.post(reverse('family:absence'), {'dates': [self.d1.isoformat(), self.d7.isoformat()], 'reason': 'personal', 'body': '家の用事'})
        m1 = FamilyMessage.objects.get(target_date=self.d1)
        m7 = FamilyMessage.objects.get(target_date=self.d7)
        visit.refresh_from_db()
        self.assertEqual(visit.status, 'scheduled')                 # 確認するまでは変えない
        res = self.staff_client.post(reverse('family:staff_confirm', args=[m1.pk]), {'date': self.today.isoformat()}, follow=True)
        self.assertContains(res, '来所予定を「欠席」にしました')
        visit.refresh_from_db()
        self.assertEqual(visit.status, 'absent')
        self.assertIn('保護者から欠席連絡（都合欠）', visit.notes)
        # 日ごとの一覧（日誌）で下にグレー
        day = self.staff_client.get(reverse('records:day') + f'?date={self.d1.isoformat()}').content.decode()
        self.assertIn('dayitem is-absent', day)
        # 予定の無い日の欠席連絡は、確認しても予定を作らない
        res = self.staff_client.post(reverse('family:staff_confirm', args=[m7.pk]), {'date': self.today.isoformat()}, follow=True)
        self.assertContains(res, '予定は変えていません')
        self.assertFalse(ScheduledVisit.objects.filter(beneficiary=self.taro, date=self.d7).exists())
        # 取り消すと元の状態に戻る
        self.staff_client.post(reverse('family:staff_confirm', args=[m1.pk]), {'date': self.d1.isoformat(), 'undo': '1'})
        visit.refresh_from_db()
        self.assertEqual(visit.status, 'scheduled')

    def test_parent_sees_only_own_child_and_staff_only_own_facility(self):
        self.client.post(reverse('family:message'), {'use_today': '1', 'body': '太郎の連絡'})
        self.client.post(reverse('family:child'), {'child': self.hana.pk})
        self.assertNotIn('太郎の連絡', self.client.get(reverse('family:message')).content.decode())
        m = FamilyMessage.objects.get()
        other_staff = StaffAccount.objects.create_user('other_admin', password='pw12345678', facility=self.other, role=StaffAccount.ROLE_ADMIN)
        c = self.client_class()
        c.force_login(other_staff)
        self.assertNotIn('太郎の連絡', c.get(reverse('family:staff_inbox')).content.decode())
        self.assertEqual(c.post(reverse('family:staff_confirm', args=[m.pk]), {'date': self.today.isoformat()}).status_code, 404)
        # 保護者ページを使わない事業所では受信の画面は無い。ログインなしでは開けない
        self.f.use_family_page = False
        self.f.save()
        self.assertEqual(self.staff_client.get(reverse('family:staff_inbox')).status_code, 404)
        self.assertEqual(self.client_class().get(reverse('family:staff_inbox')).status_code, 302)
        self.assertEqual(self.client_class().post(reverse('family:absence'), {'use_today': '1', 'reason': 'sick'}).status_code, 403)

    def test_line_talk_goes_to_inbox(self):
        # きょうだいがいる家庭（U1）は子どもを決めない。子どもが 1 人の家庭（U9）はその子に結びつける
        self.line(self.f, 'secret-a', '今日は少し遅れます', user_id='U1')
        self.g_jiro.line_user_id, self.g_jiro.line_linked = 'U9', True
        self.g_jiro.save()
        self.line(self.f, 'secret-a', 'よろしくお願いします', user_id='U9')
        a, b = FamilyMessage.objects.order_by('pk')
        self.assertEqual((a.via, a.beneficiary, a.target_date), ('line', None, self.today))
        self.assertEqual(b.beneficiary, self.jiro)
        html = self.inbox(self.today)
        self.assertIn('見本 太郎・見本 花子 のご家庭', html)        # かな順（たろう → はなこ）
        self.assertIn('LINE のトークから', html)
        # 「メニュー」は連絡にしない
        self.line(self.f, 'secret-a', 'メニュー', user_id='U1')
        self.assertEqual(FamilyMessage.objects.count(), 2)


class BookTests(Base):
    """連絡帳：職員が書いて送信 → 保護者が読む → 職員に確認済み。保護者より → 職員の書く画面に出る"""

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.yesterday = self.today - datetime.timedelta(days=1)
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.taro, date=self.today, status='scheduled')
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.jiro, date=self.today, status='scheduled')
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.hana, date=self.today, status='absent')
        self.enter()
        self.client.post(reverse('family:child'), {'child': self.taro.pk})
        self.sc = self.client_class()
        self.sc.force_login(self.staff)

    def edit_url(self, child, day=None):
        return reverse('family:staff_book_edit', args=[child.pk]) + f'?date={(day or self.today).isoformat()}'

    def send(self, child, body, day=None, action='send', push_ok=True):
        with mock.patch('line_integration.sending.push_text', return_value=(push_ok, '' if push_ok else 'LINE のエラー')) as push:
            res = self.sc.post(reverse('family:staff_book_edit', args=[child.pk]),
                               {'date': (day or self.today).isoformat(), 'body': body, 'action': action}, follow=True)
        return res, push

    def test_staff_list_shows_only_scheduled_children_and_marks(self):
        html = self.sc.get(reverse('family:staff_book')).content.decode()
        self.assertIn('見本 太郎', html)
        self.assertIn('架空 次郎', html)
        self.assertIn('欠席（1 人）', html)
        self.assertLess(html.index('欠席（1 人）'), html.index('見本 花子'))            # 欠席の子は下
        self.assertIn('送信済み 0 / 2 人', html)
        self.assertNotIn('dayitem is-sent', html)
        self.send(self.taro, '今日は公園へ出かけました。')
        html = self.sc.get(reverse('family:staff_book')).content.decode()
        self.assertIn('送信済み 1 / 2 人', html)
        self.assertEqual(html.count('dayitem is-sent'), 1)                              # 送った子だけ色が変わる
        self.assertIn('<i class="bi bi-check-circle-fill"></i> 送信済み', html)
        # ←→
        self.assertIn(f'?date={self.yesterday.isoformat()}', html)
        self.assertNotIn('見本 太郎', self.sc.get(reverse('family:staff_book') + f'?date={self.yesterday.isoformat()}').content.decode())

    def test_draft_is_hidden_from_parent_until_sent(self):
        res, push = self.send(self.taro, '下書きの文', action='draft')
        self.assertContains(res, '下書きを保存しました')
        self.assertFalse(push.called)
        book = ContactBook.objects.get()
        self.assertFalse(book.is_sent)
        self.assertContains(self.sc.get(reverse('family:staff_book')), '下書き')
        self.assertContains(self.client.get(reverse('family:book_from')), 'この日の連絡帳はありません')
        res, push = self.send(self.taro, '今日は公園へ出かけました。')
        self.assertContains(res, '連絡帳を送信しました')
        book.refresh_from_db()
        self.assertTrue(book.is_sent)
        self.assertEqual(book.sent_by, self.staff)
        self.assertContains(self.client.get(reverse('family:book_from')), '今日は公園へ出かけました。')
        # 空のままは送れない
        res, _ = self.send(self.jiro, '   ')
        self.assertEqual(res.status_code, 400)

    def test_send_notifies_line_with_link_only(self):
        res, push = self.send(self.taro, '今日は公園へ出かけました。')
        self.assertEqual(push.call_count, 1)                                            # 同じ LINE の人には 1 回
        to, text = push.call_args[0][1], push.call_args[0][2]
        self.assertEqual(to, 'U1')
        access = FamilyAccess.objects.get(facility=self.f, line_user_id='U1')
        self.assertIn(f'/family/enter/{access.token}/?to=book&c={self.taro.pk}&date={self.today.isoformat()}', text)
        self.assertNotIn('公園', text)                                                  # 文そのものは LINE に流さない
        self.assertContains(res, 'LINE に知らせました')
        # LINE が送れなくても、連絡帳は保護者ページに出る
        res, _ = self.send(self.hana, '花子さんの連絡帳', push_ok=False)
        self.assertContains(res, 'LINE での知らせは送れませんでした')
        self.assertTrue(ContactBook.objects.get(beneficiary=self.hana).is_sent)
        # LINE 未連携の子
        res, push = self.send(self.jiro, '次郎さんの連絡帳')
        self.assertFalse(push.called)
        self.assertContains(res, 'つながっている保護者がいない')

    def test_link_from_line_opens_that_child_and_day(self):
        self.send(self.hana, '花子さんの連絡帳', day=self.yesterday)
        access = FamilyAccess.objects.get(facility=self.f, line_user_id='U1')
        c = self.client_class()
        res = c.get(reverse('family:enter', args=[access.token]) + f'?to=book&c={self.hana.pk}&date={self.yesterday.isoformat()}')
        self.assertRedirects(res, reverse('family:book_from') + f'?date={self.yesterday.isoformat()}', fetch_redirect_response=False)
        self.assertContains(c.get(res['Location']), '花子さんの連絡帳')
        # ほかの家庭の子の番号・おかしな日付は無視する（外のアドレスへも送らない）
        res = c.get(reverse('family:enter', args=[access.token]) + f'?to=book&c={self.jiro.pk}&date=nope&next=https://example.com/')
        self.assertRedirects(res, reverse('family:book_from'), fetch_redirect_response=False)
        self.assertNotContains(c.get(reverse('family:home')), '架空 次郎')

    def test_parent_read_is_recorded_only_by_the_page_script_or_button(self):
        self.send(self.taro, '今日は公園へ出かけました。')
        book = ContactBook.objects.get()
        home = self.client.get(reverse('family:home')).content.decode()
        self.assertIn('class="fm-badge"', home)                                         # ホームに「まだ読んでいない」の数
        page = self.client.get(reverse('family:book_from')).content.decode()
        self.assertIn('確認しました', page)
        book.refresh_from_db()
        self.assertFalse(book.is_read)                                                  # 開いただけ（下見）では付かない
        res = self.client.post(reverse('family:book_from'), {'book': book.pk}, HTTP_X_REQUESTED_WITH='fetch')
        self.assertEqual(res.json()['ok'], True)
        book.refresh_from_db()
        self.assertTrue(book.is_read)
        self.assertIn('見本 母', book.read_by)
        first = book.read_at
        self.client.post(reverse('family:book_from'), {'book': book.pk})                 # 2 回目は時刻を変えない
        book.refresh_from_db()
        self.assertEqual(book.read_at, first)
        self.assertNotIn('class="fm-badge"', self.client.get(reverse('family:home')).content.decode())
        # 職員の一覧と書く画面に「保護者確認済み」
        self.assertContains(self.sc.get(reverse('family:staff_book')), '保護者確認済み')
        self.assertContains(self.sc.get(self.edit_url(self.taro)), '保護者確認済み')

    def test_resend_replaces_text_and_resets_read(self):
        self.send(self.taro, '最初の文')
        book = ContactBook.objects.get()
        self.client.post(reverse('family:book_from'), {'book': book.pk})
        # 送信済みは「下書き保存」では書き換わらない
        res, _ = self.send(self.taro, 'こっそり書き換え', action='draft')
        self.assertContains(res, '送り直す')
        book.refresh_from_db()
        self.assertEqual(book.body, '最初の文')
        # 同じ文で送り直しても何もしない
        res, push = self.send(self.taro, '最初の文')
        self.assertFalse(push.called)
        book.refresh_from_db()
        self.assertTrue(book.is_read)
        res, push = self.send(self.taro, '直した文')
        self.assertTrue(push.called)
        book.refresh_from_db()
        self.assertEqual(book.body, '直した文')
        self.assertFalse(book.is_read)
        self.assertEqual(ContactBook.objects.count(), 1)

    def test_parent_note_reaches_staff_edit_page(self):
        html = self.client.get(reverse('family:book_to')).content.decode()
        self.assertIn('見本 太郎 さんの様子・伝えたいこと', html)
        self.assertIn('送信', html)
        self.assertEqual(self.client.post(reverse('family:book_to'), {'date': self.today.isoformat(), 'body': ' '}).status_code, 400)
        res = self.client.post(reverse('family:book_to'), {'date': self.today.isoformat(), 'body': '朝ごはんをよく食べました'})
        self.assertRedirects(res, reverse('family:book_to') + f'?date={self.today.isoformat()}&sent=1', fetch_redirect_response=False)
        note = ParentNote.objects.get()
        self.assertEqual((note.beneficiary, note.date), (self.taro, self.today))
        self.assertContains(self.client.get(reverse('family:book_to')), '朝ごはんをよく食べました')
        # 職員：一覧に「保護者より」の印、書く画面に文
        self.assertContains(self.sc.get(reverse('family:staff_book')), '保護者より')
        edit = self.sc.get(self.edit_url(self.taro)).content.decode()
        self.assertIn('朝ごはんをよく食べました', edit)
        self.assertIn('見本 母', edit)
        self.assertNotIn('朝ごはんをよく食べました', self.sc.get(self.edit_url(self.jiro)).content.decode())
        # 前の日・次の日へ。書ける範囲の外は読むだけ
        self.assertIn(f'?date={self.yesterday.isoformat()}', self.client.get(reverse('family:book_to')).content.decode())
        old = self.today - datetime.timedelta(days=services.NOTE_BACK_DAYS + 1)
        self.assertContains(self.client.get(reverse('family:book_to') + f'?date={old.isoformat()}'), 'もう書けません')
        self.assertEqual(self.client.post(reverse('family:book_to'), {'date': old.isoformat(), 'body': 'x'}).status_code, 400)
        self.assertEqual(ParentNote.objects.count(), 1)

    def test_scope(self):
        self.send(self.taro, '太郎さんの連絡帳')
        book = ContactBook.objects.get()
        # きょうだいの画面には出ない。ほかの子の連絡帳は確認にできない
        self.client.post(reverse('family:child'), {'child': self.hana.pk})
        self.assertNotContains(self.client.get(reverse('family:book_from')), '太郎さんの連絡帳')
        self.assertEqual(self.client.post(reverse('family:book_from'), {'book': book.pk}, HTTP_X_REQUESTED_WITH='fetch').status_code, 404)
        # よその事業所の職員
        other_staff = StaffAccount.objects.create_user('other_admin2', password='pw12345678', facility=self.other, role=StaffAccount.ROLE_ADMIN)
        c = self.client_class()
        c.force_login(other_staff)
        self.assertEqual(c.get(self.edit_url(self.taro)).status_code, 404)
        self.assertNotContains(c.get(reverse('family:staff_book')), '見本 太郎')
        # ログインなし・保護者ページを使わない事業所
        self.assertEqual(self.client_class().get(reverse('family:staff_book')).status_code, 302)
        self.f.use_family_page = False
        self.f.save()
        self.assertEqual(self.sc.get(reverse('family:staff_book')).status_code, 404)

    def test_journal_parent_message_can_be_copied(self):
        DailyRecord.objects.create(facility=self.f, beneficiary=self.taro, date=self.today, parent_message_draft='日誌に書いた保護者向けの文')
        html = self.sc.get(self.edit_url(self.taro)).content.decode()
        self.assertIn('日誌の「保護者向けメッセージ」を写す', html)
        self.assertIn('日誌に書いた保護者向けの文', html)
        self.assertNotIn('日誌の「保護者向けメッセージ」を写す', self.sc.get(self.edit_url(self.jiro)).content.decode())

    def test_staff_home_icon_opens_book(self):
        html = self.sc.get(reverse('facilities:home_icons')).content.decode()
        self.assertIn(f'href="{reverse("family:staff_book")}"', html)
        self.assertEqual(html.count('class="ih-soon"'), 0)                              # 職員のアイコンは全部できている



class TopbarTests(Base):
    """画面の上：ホームがアイコンの並びの事業所では、左上が「ホームへ戻る」、メニュー（三）は右端（2026-11-22 成の要望）"""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def bar(self, url):
        html = self.client.get(url).content.decode()
        bar = html[html.index('<div class="topbar'):]
        return bar[:bar.index('<div class="page-content">')] if '<div class="page-content">' in bar else bar[:3000]

    def test_back_to_home_on_the_left_and_menu_on_the_right(self):
        bar = self.bar(reverse('family:staff_book'))
        self.assertIn('id="topbarHome"', bar)
        self.assertIn(f'href="{reverse("facilities:home_icons")}"', bar)
        self.assertEqual(bar.count('id="sidebarToggle"'), 1)
        self.assertLess(bar.index('id="topbarHome"'), bar.index('topbar-title'))          # 戻る → 題名 → メモ → メニュー の順
        self.assertLess(bar.index('btn-memo'), bar.index('id="sidebarToggle"'))
        # ホームの画面には「戻る」は出さない（メニューは右端のまま）
        home = self.bar(reverse('facilities:home_icons'))
        self.assertNotIn('id="topbarHome"', home)
        self.assertLess(home.index('btn-memo'), home.index('id="sidebarToggle"'))

    def test_other_facilities_keep_the_menu_on_the_left(self):
        self.f.home_icons = False
        self.f.save()
        bar = self.bar(reverse('family:staff_book'))
        self.assertNotIn('id="topbarHome"', bar)
        self.assertEqual(bar.count('id="sidebarToggle"'), 1)
        self.assertLess(bar.index('id="sidebarToggle"'), bar.index('topbar-title'))

class CalendarTests(Base):
    """予定のカレンダー（職員と保護者で同じもの）・利用日申請（保護者／職員が代わりに）→ 来所予定・実績記録票へ"""

    def setUp(self):
        super().setUp()
        from . import yotei
        self.yotei = yotei
        self.today = timezone.localdate()
        self.d1 = self.today + datetime.timedelta(days=3)
        self.d2 = self.today + datetime.timedelta(days=4)
        self.ym = f'{self.d1.year}-{self.d1.month:02d}'
        self.enter()
        self.client.post(reverse('family:child'), {'child': self.taro.pk})
        self.sc = self.client_class()
        self.sc.force_login(self.staff)
        self.cal = reverse('family:calendar')
        self.scal = reverse('family:staff_calendar')

    def test_calendar_starts_on_sunday(self):
        view = self.yotei.month_view(self.f, 2026, 12)
        self.assertEqual(view['week_head'][0], '日')
        self.assertEqual([c['date'].weekday() for c in view['weeks'][0]], [6, 0, 1, 2, 3, 4, 5])
        self.assertEqual(view['weeks'][0][2]['date'], datetime.date(2026, 12, 1))     # 2026-12-01 は火曜
        self.assertFalse(view['weeks'][0][0]['in_month'])
        self.assertEqual((view['prev_ym'], view['next_ym']), ('2026-11', '2027-01'))

    def test_staff_edits_month_and_parent_sees_same(self):
        i1, i2 = self.d1.isoformat(), self.d2.isoformat()
        res = self.sc.post(self.scal, {'ym': self.ym, 'action': 'save', f't_{i1}_0': '動物園へおでかけ',
                                       f't_{i2}_0': '', f'c_{i2}': 'on'}, follow=True)
        self.assertContains(res, '2 日ぶんの予定を保存しました。')
        self.assertContains(res, '動物園へおでかけ')
        ev = DayEvent.objects.get(facility=self.f, date=self.d2)
        self.assertEqual((ev.is_closed, ev.label), (True, '休業日'))
        html = self.client.get(f'{self.cal}?ym={self.ym}').content.decode()
        self.assertIn('動物園へおでかけ', html)
        self.assertIn('休業日', html)
        self.assertNotIn('name="t_', html)           # 保護者は見るだけ
        # 何も変えずに保存 → 変わらない。空にすると消える
        res = self.sc.post(self.scal, {'ym': self.ym, 'action': 'save', f't_{i1}_0': '動物園へおでかけ'}, follow=True)
        self.assertContains(res, '変わったところはありません。')
        self.sc.post(self.scal, {'ym': self.ym, 'action': 'save', f't_{i1}_0': ''})
        self.assertFalse(DayEvent.objects.filter(date=self.d1).exists())

    def test_detail_is_written_in_the_month_editor_and_day_page_is_read_only(self):
        """詳しい内容は、編集の画面で日ごとに開いて書く。日付を押した画面は見るだけ（2026-11-22 成の要望）"""
        i1 = self.d1.isoformat()
        html = self.sc.get(f'{self.scal}?ym={self.ym}&edit=1').content.decode()
        self.assertIn(f'name="t_{i1}_0"', html)
        self.assertIn(f'name="m_{i1}_0"', html)                                   # 詳しい内容の欄（はじめは閉じている）
        self.assertIn(f'id="m-{i1}-0" hidden', html)
        self.assertIn(f'aria-controls="m-{i1}-0"', html)                          # 開く・閉じるボタン
        self.sc.post(self.scal, {'ym': self.ym, 'action': 'save', f't_{i1}_0': 'クッキング', f'm_{i1}_0': '持ち物：エプロン\n参加費：300 円'})
        ev = DayEvent.objects.get(date=self.d1)
        self.assertEqual((ev.title, ev.detail), ('クッキング', '持ち物：エプロン\n参加費：300 円'))
        # 入っている日は、開くボタンに印が付く
        self.assertIn('class="ce-more has-detail"', self.sc.get(f'{self.scal}?ym={self.ym}&edit=1').content.decode())
        # 日付を押した画面：内容は読めるが、入力欄も保存ボタンも無い。送っても受け付けない
        url = reverse('family:staff_calendar_day', args=[i1])
        html = self.sc.get(url).content.decode()
        self.assertIn('クッキング', html)
        self.assertIn('参加費：300 円', html)
        view = html[html.index('id="cd-view"'):]
        view = view[:view.index('この日に来る予定の子')]
        for word in ('<textarea', '<input', '<form', '保存する'):
            self.assertNotIn(word, view, word)
        self.assertIn(f'?ym={self.ym}&amp;edit=1#d-{i1}', html)                   # 「編集する」は編集の画面のその日へ
        self.assertEqual(self.sc.post(url, {'title': '書きかえ', 'detail': 'x'}).status_code, 405)
        self.assertEqual(DayEvent.objects.get(date=self.d1).title, 'クッキング')
        html = self.client.get(reverse('family:calendar_day', args=[i1])).content.decode()
        self.assertIn('クッキング', html)
        self.assertIn('参加費：300 円', html)
        self.assertNotIn('<textarea', html)
        self.assertIn('利用の予定はありません', html)
        self.assertEqual(self.client.get(reverse('family:calendar_day', args=['2026-13-40'])).status_code, 404)
        self.assertEqual(self.sc.get(reverse('family:staff_calendar_day', args=['abc'])).status_code, 404)

    def test_parent_applies_and_visit_is_created(self):
        html = self.client.get(f'{self.cal}?ym={self.ym}&apply=1').content.decode()
        self.assertIn(f'value="{self.d1.isoformat()}"', html)
        res = self.client.post(self.cal, {'ym': self.ym, 'dates': [self.d1.isoformat(), self.d2.isoformat()]}, follow=True)
        self.assertContains(res, '利用日を申請しました')
        v = ScheduledVisit.objects.get(beneficiary=self.taro, date=self.d1)
        self.assertEqual((v.facility, v.status), (self.f, 'scheduled'))
        self.assertEqual(UseRequest.objects.filter(beneficiary=self.taro, via='parent', seen_at__isnull=True).count(), 2)
        self.assertFalse(ScheduledVisit.objects.filter(beneficiary=self.hana).exists())      # きょうだいには入れない
        # 申請した日は、もう選べない（利用の印が付く）
        html = self.client.get(f'{self.cal}?ym={self.ym}&apply=1').content.decode()
        self.assertNotIn(f'value="{self.d1.isoformat()}"', html)
        self.assertIn('cal-mark is-use', html)
        # 実績記録票の日ごとの一覧に出る
        self.assertContains(self.sc.get(reverse('schedules:jisseki') + f'?date={self.d1.isoformat()}'), '見本 太郎')
        # 同じ日をもう一度送っても増えない
        res = self.client.post(self.cal, {'ym': self.ym, 'dates': [self.d1.isoformat()]}, follow=True)
        self.assertContains(res, '申請できませんでした')
        self.assertEqual(UseRequest.objects.count(), 2)

    def test_parent_cannot_apply_today_past_far_future_or_closed_day(self):
        DayEvent.objects.create(facility=self.f, date=self.d2, is_closed=True)
        far = self.today + datetime.timedelta(days=200)
        res = self.client.post(self.cal, {'ym': self.ym, 'dates': [
            self.today.isoformat(), (self.today - datetime.timedelta(days=1)).isoformat(), far.isoformat(), 'abc']})
        self.assertIn('err=1', res['Location'])
        res = self.client.post(self.cal, {'ym': self.ym, 'dates': [self.d2.isoformat()]}, follow=True)
        self.assertContains(res, '申請できませんでした')
        self.assertFalse(ScheduledVisit.objects.exists())
        self.assertFalse(UseRequest.objects.exists())
        html = self.client.get(f'{self.cal}?ym={self.ym}&apply=1').content.decode()
        self.assertNotIn(f'value="{self.d2.isoformat()}"', html)
        self.assertNotIn(f'value="{self.today.isoformat()}"', html)

    def test_staff_sees_new_requests_and_marks_seen(self):
        self.client.post(self.cal, {'ym': self.ym, 'dates': [self.d1.isoformat()]})
        html = self.sc.get(self.scal).content.decode()
        self.assertIn('新しい利用日申請が届いています', html)
        self.assertIn('見本 太郎', html)
        self.assertEqual(self.yotei.unseen_count(self.f), 1)
        home = self.sc.get(reverse('facilities:home_icons')).content.decode()
        self.assertIn('ih-badge', home)
        res = self.sc.post(self.scal, {'ym': self.ym, 'action': 'seen'}, follow=True)
        self.assertContains(res, '確認済みにしました')
        self.assertNotContains(res, '新しい利用日申請が届いています')
        self.assertEqual(self.yotei.unseen_count(self.f), 0)
        self.assertEqual(UseRequest.objects.get().seen_by, self.staff)

    def test_staff_applies_on_behalf(self):
        url = reverse('family:staff_apply')
        html = self.sc.get(url).content.decode()
        self.assertIn('見本 太郎', html)
        self.assertNotIn('name="dates"', html)
        html = self.sc.get(f'{url}?ym={self.ym}&child={self.jiro.pk}').content.decode()
        self.assertIn(f'value="{self.d1.isoformat()}"', html)
        past = self.today - datetime.timedelta(days=2)
        res = self.sc.post(url, {'ym': self.ym, 'child': self.jiro.pk, 'dates': [self.d1.isoformat(), past.isoformat()]}, follow=True)
        self.assertContains(res, '架空 次郎 さんの利用日を 2 日ぶん入れました')
        self.assertEqual(ScheduledVisit.objects.filter(beneficiary=self.jiro).count(), 2)
        r = UseRequest.objects.filter(beneficiary=self.jiro).first()
        self.assertEqual((r.via, r.staff, r.seen_at is not None), ('staff', self.staff, True))
        self.assertEqual(self.yotei.unseen_count(self.f), 0)          # 職員が入れたものは、知らせに出さない
        # 日にちを選ばずに送る／よその事業所の子
        self.assertContains(self.sc.post(url, {'ym': self.ym, 'child': self.jiro.pk}, follow=True), '日にちを選んでから')
        yoso = Beneficiary.objects.create(facility=self.other, last_name='他所', first_name='三郎', last_name_kana='よそ',
                                          first_name_kana='さぶろう', date_of_birth=datetime.date(2016, 4, 1))
        self.assertEqual(self.sc.post(url, {'ym': self.ym, 'child': yoso.pk, 'dates': [self.d1.isoformat()]}).status_code, 404)

    def test_staff_calendar_shows_counts_and_is_per_facility(self):
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.taro, date=self.d1)
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.jiro, date=self.d1)
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.hana, date=self.d1, status='absent')
        DayEvent.objects.create(facility=self.other, date=self.d1, title='よその予定')
        html = self.sc.get(f'{self.scal}?ym={self.ym}').content.decode()
        self.assertIn('<span class="cal-count">2人</span>', html)
        self.assertNotIn('よその予定', html)
        self.assertNotIn('よその予定', self.client.get(f'{self.cal}?ym={self.ym}').content.decode())
        day = self.sc.get(reverse('family:staff_calendar_day', args=[self.d1.isoformat()])).content.decode()
        self.assertIn('この日に来る予定の子（2 人）', day)

    def test_parent_calendar_shows_own_child_only(self):
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.jiro, date=self.d1)
        html = self.client.get(f'{self.cal}?ym={self.ym}').content.decode()
        self.assertEqual(html.count('cal-mark is-use'), 1)            # 凡例の 1 つだけ（よその子の予定は出ない）
        self.assertNotIn('class="cal-count"', html)
        self.assertNotIn('架空 次郎', html)

    def test_gates(self):
        plain = Facility.objects.create(name='ふつうの事業所')
        staff = StaffAccount.objects.create_user('plain_admin2', password='pw12345678', facility=plain, role=StaffAccount.ROLE_ADMIN)
        c = self.client_class()
        c.force_login(staff)
        for name in ('family:staff_calendar', 'family:staff_apply'):
            self.assertEqual(c.get(reverse(name)).status_code, 404)
        self.assertEqual(self.client_class().get(self.scal).status_code, 302)                   # ログインが要る
        self.assertEqual(self.client_class().get(self.cal).status_code, 403)                    # 入場の印が要る
        self.assertEqual(self.client_class().post(self.cal, {'dates': [self.d1.isoformat()]}).status_code, 403)


    def test_plans_per_group_on_the_same_day(self):
        """小学生と未就学で内容が違う日：予定ごとに見せるグループを選ぶ。保護者には、自分の子のグループの予定だけ"""
        i1 = self.d1.isoformat()
        sho = NoticeGroup.objects.create(facility=self.f, name='小学生')
        mi = NoticeGroup.objects.create(facility=self.f, name='未就学')
        yoso = NoticeGroup.objects.create(facility=self.other, name='よそのグループ')
        sho.members.add(self.taro)
        mi.members.add(self.hana)
        html = self.sc.get(f'{self.scal}?ym={self.ym}&edit=1').content.decode()
        self.assertIn(f'name="g_{i1}_0" value="all" checked', html)               # はじめは全員
        self.assertIn(f'name="g_{i1}_0" value="{sho.pk}"', html)
        self.assertNotIn('よそのグループ', html)
        self.assertIn('＋ 活動・お出かけ先を増やす', html)
        res = self.sc.post(self.scal, {
            'ym': self.ym, 'action': 'save',
            f't_{i1}_0': 'プログラミング', f'm_{i1}_0': '持ち物：タブレット', f'g_{i1}_0': [str(sho.pk), str(yoso.pk)],
            f't_{i1}_1': 'リトミック', f'm_{i1}_1': '持ち物：うわばき', f'g_{i1}_1': [str(mi.pk)],
            f't_{i1}_2': 'おやつ作り', f'g_{i1}_2': ['all', str(sho.pk)],            # 「全員」があれば全員
        }, follow=True)
        self.assertContains(res, '1 日ぶんの予定を保存しました。')
        evs = list(DayEvent.objects.filter(facility=self.f, date=self.d1))
        self.assertEqual([(e.title, e.audience) for e in evs], [('プログラミング', '小学生'), ('リトミック', '未就学'), ('おやつ作り', '全員')])
        # 職員のカレンダー：全部と、見せる相手
        html = self.sc.get(f'{self.scal}?ym={self.ym}').content.decode()
        self.assertIn('プログラミング（小学生）／リトミック（未就学）／おやつ作り', html)
        day = self.sc.get(reverse('family:staff_calendar_day', args=[i1])).content.decode()
        for word in ('プログラミング', 'リトミック', 'おやつ作り', '持ち物：タブレット', '持ち物：うわばき', '小学生', '未就学', '全員'):
            self.assertIn(word, day, word)
        # 編集の画面を開き直すと、3 件とも出て、選んだグループに印
        html = self.sc.get(f'{self.scal}?ym={self.ym}&edit=1').content.decode()
        self.assertIn(f'name="t_{i1}_2" value="おやつ作り"', html)
        self.assertIn(f'name="g_{i1}_1" value="{mi.pk}" checked', html)
        self.assertNotIn(f'name="g_{i1}_1" value="all" checked', html)
        # 保護者（太郎＝小学生）：小学生向けと全員向けだけ
        cal = self.client.get(f'{self.cal}?ym={self.ym}').content.decode()
        self.assertIn('プログラミング／おやつ作り', cal)
        self.assertNotIn('リトミック', cal)
        self.assertNotIn('（小学生）', cal)                                          # 保護者には、見せる相手は出さない
        page = self.client.get(reverse('family:calendar_day', args=[i1])).content.decode()
        self.assertIn('持ち物：タブレット', page)
        self.assertNotIn('うわばき', page)
        # きょうだい（花子＝未就学）に切り替えると、未就学向けと全員向け
        self.client.post(reverse('family:child'), {'child': self.hana.pk})
        cal = self.client.get(f'{self.cal}?ym={self.ym}').content.decode()
        self.assertIn('リトミック／おやつ作り', cal)
        self.assertNotIn('プログラミング', cal)
        page = self.client.get(reverse('family:calendar_day', args=[i1])).content.decode()
        self.assertIn('持ち物：うわばき', page)
        self.assertNotIn('タブレット', page)
        # 関数でも同じ（保護者アプリが使う入口）
        info = self.yotei.day_info(self.f, self.d1, self.taro)
        self.assertEqual([e.title for e in info['events']], ['プログラミング', 'おやつ作り'])
        self.assertEqual(info['event'].title, 'プログラミング／おやつ作り')
        self.assertEqual(self.yotei.day_info(self.f, self.d1, self.jiro)['event'].title, 'おやつ作り')      # どのグループでもない子
        # 1 件を空にして保存すると、その 1 件だけ消える
        self.sc.post(self.scal, {'ym': self.ym, 'action': 'save', f't_{i1}_0': 'プログラミング', f'm_{i1}_0': '持ち物：タブレット',
                                 f'g_{i1}_0': [str(sho.pk)], f't_{i1}_1': '', f'm_{i1}_1': '', f't_{i1}_2': 'おやつ作り', f'g_{i1}_2': ['all']})
        self.assertEqual([e.title for e in DayEvent.objects.filter(date=self.d1)], ['プログラミング', 'おやつ作り'])

    def test_closed_day_applies_to_everyone_and_limits(self):
        i1 = self.d1.isoformat()
        sho = NoticeGroup.objects.create(facility=self.f, name='小学生')
        sho.members.add(self.taro)
        # 休業日の印は、グループ向けの予定しか無い日でも、全員に効く
        self.yotei.save_day(self.f, self.d1, True, [{'title': '小学生だけのお楽しみ会', 'detail': '', 'group_ids': [sho.pk]}], self.staff)
        for child in (self.taro, self.hana):
            cell = next(c for w in self.yotei.month_view(self.f, self.d1.year, self.d1.month, child=child,
                                                         window=self.yotei.parent_window())['weeks'] for c in w if c.get('iso') == i1)
            self.assertTrue(cell['is_closed'])
            self.assertFalse(cell['can_pick'])
        self.assertEqual(self.yotei.day_info(self.f, self.d1, self.hana)['event'].label, '休業日')
        self.assertEqual(self.yotei.day_info(self.f, self.d1, self.taro)['event'].label, '小学生だけのお楽しみ会')
        made, skipped = self.yotei.apply_days(self.f, self.hana, [self.d1], via='staff', staff=self.staff)
        self.assertEqual((made, skipped), ([], [self.d1]))
        # 1 日に持てるのは MAX_PER_DAY 件まで。グループを消すと、その予定は全員向けに戻る
        n = DayEvent.MAX_PER_DAY
        self.yotei.save_day(self.f, self.d2, False, [{'title': f'予定{i}', 'detail': '', 'group_ids': [sho.pk]} for i in range(n + 3)], self.staff)
        self.assertEqual(DayEvent.objects.filter(date=self.d2).count(), n)
        sho.delete()
        self.assertEqual({e.audience for e in DayEvent.objects.filter(date=self.d2)}, {'全員'})
        # 以前の入口（1 日 1 件・全員向け）も使える
        self.assertTrue(self.yotei.save_event(self.f, self.d2, 'まとめて 1 件', False, 'くわしく', self.staff))
        self.assertEqual([(e.title, e.detail) for e in DayEvent.objects.filter(date=self.d2)], [('まとめて 1 件', 'くわしく')])
        self.assertFalse(self.yotei.save_event(self.f, self.d2, 'まとめて 1 件', False, 'くわしく', self.staff))

class NoticeTests(Base):
    """お知らせ：職員が書く（全員／グループ／児童個別・下書き・送信）→ 保護者ページに新しい順で出る"""

    def setUp(self):
        super().setUp()
        from . import notices
        self.notices = notices
        self.enter()
        self.client.post(reverse('family:child'), {'child': self.taro.pk})
        self.sc = self.client_class()
        self.sc.force_login(self.staff)
        self.new = reverse('family:staff_notice_new')
        self.feed = reverse('family:notice')

    def post(self, data, url=None, push_ok=True):
        with mock.patch('line_integration.sending.push_text', return_value=(push_ok, '' if push_ok else 'LINE のエラー')) as push:
            res = self.sc.post(url or self.new, data, follow=True)
        return res, push

    def test_send_to_all_and_parent_reads(self):
        res, push = self.post({'title': '12 月の行事について', 'body': 'クリスマス会をします。\n詳しくは予定をご覧ください。',
                               'target_kind': 'all', 'action': 'send', 'notify_line': '1'})
        self.assertContains(res, 'お知らせ「12 月の行事について」を 3 人ぶん送信しました。')
        self.assertContains(res, '保護者の LINE に知らせました（1 人）')          # LINE は U1 だけ。きょうだいでも 1 通
        self.assertEqual(push.call_count, 1)
        text = push.call_args[0][2]
        self.assertIn('12 月の行事について', text)
        self.assertNotIn('クリスマス会', text)                                    # 本文は LINE に流さない
        self.assertIn('?to=notice&c=', text)
        n = Notice.objects.get()
        self.assertEqual((n.status, n.target_label, n.line_count, n.recipients.count()), ('sent', '全員', 1, 3))
        # 保護者：ホームに数字 → 開くと出る → 読んだ記録
        self.assertIn('class="fm-badge"', self.client.get(reverse('family:home')).content.decode())
        html = self.client.get(self.feed).content.decode()
        self.assertIn('12 月の行事について', html)
        self.assertIn('クリスマス会をします。', html)
        self.assertIn('新着', html)
        res = self.client.post(self.feed, HTTP_X_REQUESTED_WITH='fetch')
        self.assertEqual(res.json(), {'ok': True, 'read': 1})
        self.assertNotIn('新着', self.client.get(self.feed).content.decode())
        self.assertNotIn('class="fm-badge"', self.client.get(reverse('family:home')).content.decode())
        # 職員：読んだ人数
        self.assertContains(self.sc.get(reverse('family:staff_notice')), '読んだ 1 / 3 人')
        self.assertContains(self.sc.get(reverse('family:staff_notice_edit', args=[n.pk])), '読んだ 1 人')

    def test_line_link_opens_notice_page(self):
        access = services.access_for(self.f, 'U1')
        c = self.client_class()
        res = c.get(reverse('family:enter', args=[access.token]) + f'?to=notice&c={self.hana.pk}')
        self.assertRedirects(res, self.feed, fetch_redirect_response=False)
        self.assertEqual(c.session['family_child'], self.hana.pk)

    def test_newest_first(self):
        for title in ('1 つめ', '2 つめ', '3 つめ'):
            self.post({'title': title, 'body': '本文', 'target_kind': 'all', 'action': 'send'})
        html = self.client.get(self.feed).content.decode()
        self.assertLess(html.index('3 つめ'), html.index('2 つめ'))
        self.assertLess(html.index('2 つめ'), html.index('1 つめ'))

    def test_draft_is_hidden_then_sent(self):
        res, push = self.post({'title': '下書きの題', 'body': '', 'target_kind': 'all', 'action': 'draft'})
        self.assertContains(res, '下書きを保存しました')
        push.assert_not_called()
        n = Notice.objects.get()
        self.assertEqual((n.status, n.recipients.count()), ('draft', 0))
        self.assertNotIn('下書きの題', self.client.get(self.feed).content.decode())
        self.assertContains(self.sc.get(reverse('family:staff_notice')), '下書きの題')
        # 本文なしでは送れない → 書いて送信
        url = reverse('family:staff_notice_edit', args=[n.pk])
        res, _ = self.post({'title': '下書きの題', 'body': '', 'target_kind': 'all', 'action': 'send'}, url)
        self.assertEqual(res.status_code, 400)
        self.assertIn('本文を書いてください。', res.content.decode())
        res, _ = self.post({'title': '直した題', 'body': '本文です', 'target_kind': 'all', 'action': 'send'}, url)
        self.assertContains(res, '送信しました')
        self.assertEqual(Notice.objects.count(), 1)
        self.assertIn('直した題', self.client.get(self.feed).content.decode())
        # 送信済みは書き換えられない
        res, _ = self.post({'title': 'あとから', 'body': 'x', 'target_kind': 'all', 'action': 'send'}, url)
        self.assertContains(res, '書き換えられません')
        self.assertEqual(Notice.objects.get().title, '直した題')

    def test_send_to_children_only(self):
        res, push = self.post({'title': '個別の連絡', 'body': '次郎さんへ', 'target_kind': 'children',
                               'children': [self.jiro.pk], 'action': 'send', 'notify_line': '1'})
        self.assertContains(res, '1 人ぶん送信しました')
        self.assertContains(res, 'LINE での知らせは送っていません')              # 次郎の保護者は LINE 未連携
        push.assert_not_called()
        self.assertEqual(Notice.objects.get().target_label, '架空 次郎 さん')
        self.assertNotIn('個別の連絡', self.client.get(self.feed).content.decode())     # 太郎の保護者には出ない
        # 子を選ばずに送信
        res, _ = self.post({'title': 'x', 'body': 'y', 'target_kind': 'children', 'action': 'send'})
        self.assertEqual(res.status_code, 400)
        self.assertIn('配信先の子を選んでください。', res.content.decode())

    def test_group_create_and_send(self):
        groups = reverse('family:staff_notice_groups')
        res = self.sc.post(groups, {'name': '低学年', 'members': [self.taro.pk, self.jiro.pk], 'action': 'save'}, follow=True)
        self.assertContains(res, 'グループ「低学年」を保存しました（2 人）。')
        g = NoticeGroup.objects.get()
        self.assertEqual(self.sc.post(groups, {'name': '低学年', 'members': [self.taro.pk], 'action': 'save'}).status_code, 400)
        self.assertEqual(self.sc.post(groups, {'name': '', 'members': [self.taro.pk], 'action': 'save'}).status_code, 400)
        self.assertEqual(self.sc.post(groups, {'name': 'からっぽ', 'action': 'save'}).status_code, 400)
        res, _ = self.post({'title': '低学年のみなさんへ', 'body': '本文', 'target_kind': 'group', 'group': g.pk, 'action': 'send'})
        self.assertContains(res, '2 人ぶん送信しました')
        n = Notice.objects.get()
        self.assertEqual(n.target_label, 'グループ：低学年')
        self.assertEqual(set(n.recipients.values_list('beneficiary_id', flat=True)), {self.taro.pk, self.jiro.pk})
        # グループを直しても・消しても、送った相手は変わらない
        self.sc.post(reverse('family:staff_notice_group', args=[g.pk]), {'name': '低学年', 'members': [self.hana.pk], 'action': 'save'})
        self.assertEqual(set(g.members.all()), {self.hana})
        self.sc.post(reverse('family:staff_notice_group', args=[g.pk]), {'action': 'delete'})
        self.assertEqual(NoticeRecipient.objects.filter(notice=n).count(), 2)
        self.assertIn('低学年のみなさんへ', self.client.get(self.feed).content.decode())
        # グループを選ばずに送信
        res, _ = self.post({'title': 'x', 'body': 'y', 'target_kind': 'group', 'action': 'send'})
        self.assertIn('配信先のグループを選んでください。', res.content.decode())

    def test_withdraw_and_delete_draft(self):
        self.post({'title': '取り下げる', 'body': '本文', 'target_kind': 'all', 'action': 'send'})
        n = Notice.objects.get()
        res, _ = self.post({'action': 'delete'}, reverse('family:staff_notice_edit', args=[n.pk]))
        self.assertContains(res, '取り下げました')
        self.assertNotIn('取り下げる', self.client.get(self.feed).content.decode())
        self.assertFalse(NoticeRecipient.objects.exists())

    def test_gates_and_other_facility(self):
        other_notice = Notice.objects.create(facility=self.other, title='よその知らせ', body='x', status='sent', sent_at=timezone.now())
        NoticeRecipient.objects.create(notice=other_notice, beneficiary=self.taro)        # まちがった宛先でも、よその事業所のものは出さない
        self.assertNotIn('よその知らせ', self.client.get(self.feed).content.decode())
        self.assertEqual(self.sc.get(reverse('family:staff_notice_edit', args=[other_notice.pk])).status_code, 404)
        self.assertNotContains(self.sc.get(reverse('family:staff_notice')), 'よその知らせ')
        yoso = Beneficiary.objects.create(facility=self.other, last_name='他所', first_name='三郎', last_name_kana='よそ',
                                          first_name_kana='さぶろう', date_of_birth=datetime.date(2016, 4, 1))
        res, _ = self.post({'title': 'x', 'body': 'y', 'target_kind': 'children', 'children': [yoso.pk], 'action': 'send'})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.client_class().get(self.feed).status_code, 403)
        self.assertEqual(self.client_class().get(reverse('family:staff_notice')).status_code, 302)
        plain = Facility.objects.create(name='ふつうの事業所')
        staff = StaffAccount.objects.create_user('plain_admin3', password='pw12345678', facility=plain, role=StaffAccount.ROLE_ADMIN)
        c = self.client_class()
        c.force_login(staff)
        for name in ('family:staff_notice', 'family:staff_notice_new', 'family:staff_notice_groups'):
            self.assertEqual(c.get(reverse(name)).status_code, 404)


class PlanPageTests(Base):
    """個別支援計画：職員の更新の一覧（今月＝赤・来月＝オレンジ）・保護者ページで本案を読む"""

    def setUp(self):
        super().setUp()
        from support_plans.models import PlanGoal, SupportPlan
        self.SupportPlan, self.PlanGoal = SupportPlan, PlanGoal
        self.today = datetime.date.today()
        self.enter()
        self.client.post(reverse('family:child'), {'child': self.taro.pk})
        self.sc = self.client_class()
        self.sc.force_login(self.staff)

    def plan(self, child, step=5, status='active', end=None, title='第1期 個別支援計画'):
        p = self.SupportPlan.objects.create(facility=self.f, beneficiary=child, title=title, current_step=step, status=status,
                                            manager=self.staff, created_by=self.staff)
        d = p.get_step(2)
        d.period_start, d.period_end = (end - datetime.timedelta(days=180) if end else None), end
        d.policy, d.family_wishes = '安心して過ごせる場をつくります。', '友だちと遊びたい。'
        d.save()
        self.PlanGoal.objects.create(plan=p, goal_type='long', content='気持ちを言葉で伝えられる', target_date=end)
        self.PlanGoal.objects.create(plan=p, goal_type='short', content='順番を待てる', support_content='絵カードで見通しを示す',
                                     frequency='週3回', form_extra={'staff': '保育士'})
        return p

    def test_renewal_colors(self):
        from support_plans import renewal
        today = datetime.date(2026, 11, 20)
        self.plan(self.taro, end=datetime.date(2026, 11, 30))                 # 今月 → 赤
        self.plan(self.hana, end=datetime.date(2026, 12, 15))                 # 来月 → オレンジ
        rows = {r['beneficiary'].pk: r for r in renewal.renewal_rows(self.f, today)}
        self.assertEqual((rows[self.taro.pk]['color'], rows[self.taro.pk]['label']), ('red', '今月更新'))
        self.assertEqual((rows[self.hana.pk]['color'], rows[self.hana.pk]['label']), ('orange', '来月更新'))
        self.assertEqual((rows[self.jiro.pk]['color'], rows[self.jiro.pk]['label'], rows[self.jiro.pk]['plan']), ('black', '計画がありません', None))
        self.assertEqual(renewal.renewal_counts(list(rows.values())), {'red': 1, 'orange': 1, 'none': 1})
        # 12 月に見ると、太郎は期限切れ（赤）、花子は今月（赤）。年をまたぐ来月
        rows = {r['beneficiary'].pk: r for r in renewal.renewal_rows(self.f, datetime.date(2026, 12, 1))}
        self.assertEqual((rows[self.taro.pk]['color'], rows[self.taro.pk]['label']), ('red', '期限が過ぎています'))
        self.assertEqual(rows[self.hana.pk]['label'], '今月更新')
        self.plan(self.jiro, end=datetime.date(2027, 1, 10))
        rows = {r['beneficiary'].pk: r for r in renewal.renewal_rows(self.f, datetime.date(2026, 12, 1))}
        self.assertEqual(rows[self.jiro.pk]['color'], 'orange')
        # 先の計画は黒。次の計画を作成中なら、そちらへ
        rows = {r['beneficiary'].pk: r for r in renewal.renewal_rows(self.f, datetime.date(2026, 6, 1))}
        self.assertEqual((rows[self.taro.pk]['color'], rows[self.taro.pk]['label']), ('black', ''))
        nxt = self.plan(self.taro, step=2, status='in_progress', title='第2期 個別支援計画')
        rows = {r['beneficiary'].pk: r for r in renewal.renewal_rows(self.f, today)}
        self.assertEqual((rows[self.taro.pk]['plan'], rows[self.taro.pk]['color']), (nxt, 'red'))

    def test_renewal_page(self):
        p = self.plan(self.taro, end=self.today)
        html = self.sc.get(reverse('support_plans:renewal')).content.decode()
        self.assertIn('rn-row is-red', html)
        self.assertIn(reverse('support_plans:detail', args=[p.pk]), html)
        self.assertIn(f'{reverse("support_plans:create")}?beneficiary={self.jiro.pk}', html)
        self.assertIn(f'href="{reverse("support_plans:renewal")}"', self.sc.get(reverse('facilities:home_icons')).content.decode())
        plain = Facility.objects.create(name='ふつうの事業所')
        staff = StaffAccount.objects.create_user('plain_admin4', password='pw12345678', facility=plain, role=StaffAccount.ROLE_ADMIN)
        c = self.client_class()
        c.force_login(staff)
        self.assertEqual(c.get(reverse('support_plans:renewal')).status_code, 404)

    def test_parent_sees_final_plan_only(self):
        url = reverse('family:plan')
        self.assertIn('まだありません', self.client.get(url).content.decode())
        self.plan(self.taro, step=2, status='in_progress', title='作成中の計画')         # 原案 → 見せない
        self.plan(self.hana, end=self.today, title='花子の計画')                          # きょうだいの計画 → いまは太郎を見ている
        self.assertIn('まだありません', self.client.get(url).content.decode())
        self.plan(self.taro, end=self.today + datetime.timedelta(days=90), title='太郎の計画')
        html = self.client.get(url).content.decode()
        for text in ('太郎の計画', 'いまの計画', '安心して過ごせる場をつくります。', '友だちと遊びたい。', '気持ちを言葉で伝えられる',
                     '順番を待てる', '絵カードで見通しを示す', '週3回', '保育士'):
            self.assertIn(text, html)
        self.assertNotIn('作成中の計画', html)
        self.assertNotIn('花子の計画', html)
        # 説明・同意の前の本案も見られる（印を変える）。終了した計画は出さない
        self.plan(self.taro, step=4, status='in_progress', title='次の計画')
        self.plan(self.taro, status='closed', title='終わった計画')
        html = self.client.get(url).content.decode()
        self.assertIn('次の計画', html)
        self.assertIn('同意の前', html)
        self.assertNotIn('終わった計画', html)
        self.assertEqual(self.client_class().get(url).status_code, 403)


class MergeWithFamilyDataTests(Base):
    """重複した利用者をまとめる（beneficiaries/merge.py）とき、お知らせ・グループ・連絡帳・利用日申請がこわれないこと"""

    def test_merge_moves_notices_groups_and_requests(self):
        from beneficiaries import merge
        keep, drop = self.taro, self.jiro
        both = Notice.objects.create(facility=self.f, title='全員へ', body='x', status='sent', sent_at=timezone.now())
        only = Notice.objects.create(facility=self.f, title='次郎だけ', body='y', status='sent', sent_at=timezone.now())
        draft = Notice.objects.create(facility=self.f, title='下書き', target_kind='children')
        draft.children.add(drop)
        NoticeRecipient.objects.create(notice=both, beneficiary=keep, read_at=timezone.now())
        NoticeRecipient.objects.create(notice=both, beneficiary=drop)
        NoticeRecipient.objects.create(notice=only, beneficiary=drop)
        group = NoticeGroup.objects.create(facility=self.f, name='低学年')
        group.members.add(drop)
        day = timezone.localdate() + datetime.timedelta(days=5)
        UseRequest.objects.create(facility=self.f, beneficiary=drop, date=day)
        ScheduledVisit.objects.create(facility=self.f, beneficiary=drop, date=day)
        ContactBook.objects.create(facility=self.f, beneficiary=drop, date=day, body='z')
        self.assertEqual(merge.plan(keep, drop)['conflicts'], [])
        merge.merge(keep, drop)
        self.assertFalse(Beneficiary.objects.filter(pk=drop.pk).exists())
        self.assertEqual(set(NoticeRecipient.objects.filter(beneficiary=keep).values_list('notice__title', flat=True)), {'全員へ', '次郎だけ'})
        self.assertIsNotNone(NoticeRecipient.objects.get(notice=both).read_at)          # 残すほうの「読んだ」を保つ
        self.assertEqual(set(group.members.all()), {keep})
        self.assertEqual(set(draft.children.all()), {keep})
        self.assertEqual(UseRequest.objects.get().beneficiary, keep)
        self.assertEqual(ContactBook.objects.get().beneficiary, keep)

    def test_same_day_contact_books_block_merge(self):
        from beneficiaries import merge
        day = timezone.localdate()
        ContactBook.objects.create(facility=self.f, beneficiary=self.taro, date=day, body='a')
        ContactBook.objects.create(facility=self.f, beneficiary=self.jiro, date=day, body='b')
        conflicts = merge.plan(self.taro, self.jiro)['conflicts']
        self.assertTrue(any('連絡帳' in c for c in conflicts))
        with self.assertRaises(ValueError):
            merge.merge(self.taro, self.jiro)
