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
from .models import FamilyAccess, FamilyMessage


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
        page = self.client.get(reverse('family:page', args=['oshirase']))
        self.assertContains(page, '準備中')
        # できた画面（欠席連絡・その他の連絡）は「準備中」を出さず、その画面へ
        self.assertRedirects(self.client.get(reverse('family:page', args=['absence'])), reverse('family:absence'))
        self.assertIn(f'href="{reverse("family:absence")}"', html)
        self.assertEqual(html.count('class="fm-soon"'), 6)
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

    def test_icon_home(self):
        res = self.client.get(reverse('facilities:dashboard'))
        self.assertRedirects(res, reverse('facilities:home_icons'))
        html = self.client.get(reverse('facilities:home_icons')).content.decode()
        for label in ('連絡帳', '日誌', '保護者からの\n連絡', '実績記録票', '予定', 'お知らせ', '個別支援計画'):
            self.assertIn(f'<span class="ih-label">{label}</span>', html)
        self.assertEqual(html.count('class="ih-icon"'), 7)
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
