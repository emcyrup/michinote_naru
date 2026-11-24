"""保護者アプリ（familyapp。config/urls_family.py で動かす）の画面。データはすべて架空"""
import datetime

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import StaffAccount
from beneficiaries.models import Beneficiary, Guardian
from facilities.models import Facility
from family import accounts, notices, services, yotei
from family.models import ContactBook, FamilyMessage, Notice, NoticeRecipient, ParentNote, UseRequest
from schedules.models import ScheduledVisit

from . import auth, views

GOOD_PW = 'niji-no-hashi-2026'


@override_settings(ROOT_URLCONF='config.urls_family', FAMILY_APP_URL='https://family.example.test')
class FamilyAppTestCase(TestCase):
    def setUp(self):
        self.f = Facility.objects.create(name='見本の事業所', use_family_page=True, home_icons=True, use_line=False)
        self.staff = StaffAccount.objects.create_user('fa_admin', password='pw12345678', facility=self.f, role=StaffAccount.ROLE_ADMIN)
        kw = dict(date_of_birth=datetime.date(2016, 4, 1))
        self.taro = Beneficiary.objects.create(facility=self.f, last_name='見本', first_name='太郎', last_name_kana='みほん', first_name_kana='たろう', **kw)
        self.hana = Beneficiary.objects.create(facility=self.f, last_name='見本', first_name='花子', last_name_kana='みほん', first_name_kana='はなこ', **kw)
        self.jiro = Beneficiary.objects.create(facility=self.f, last_name='よそ', first_name='次郎', last_name_kana='よそ', first_name_kana='じろう', **kw)
        self.mother_taro = Guardian.objects.create(beneficiary=self.taro, last_name='見本', first_name='母', relation='mother', email='haha@example.test')
        self.mother_hana = Guardian.objects.create(beneficiary=self.hana, last_name='見本', first_name='母', relation='mother', email='haha@example.test')
        self.other_parent = Guardian.objects.create(beneficiary=self.jiro, last_name='よそ', first_name='父', relation='father', email='yoso@example.test')
        self.account, _ = accounts.link_guardian(self.mother_taro, self.staff)
        accounts.link_guardian(self.mother_hana, self.staff, confirm=True)   # きょうだい：確かめたうえで同じアカウントへ（2026-11-22〜）
        accounts.set_password(self.account, GOOD_PW)
        self.other_account, _ = accounts.link_guardian(self.other_parent, self.staff)
        accounts.set_password(self.other_account, GOOD_PW)
        self.today = timezone.localdate()

    def login(self, email='haha@example.test', password=GOOD_PW, **extra):
        return self.client.post(reverse('familyapp:login'), {'email': email, 'password': password, **extra})


class LoginTests(FamilyAppTestCase):
    def test_pages_need_login_and_staff_screens_are_not_served(self):
        for name in ('home', 'book_from', 'book_to', 'absence', 'message', 'calendar', 'notice', 'plan', 'record'):
            r = self.client.get(reverse(f'familyapp:{name}'))
            self.assertEqual(r.status_code, 302, name)
            self.assertTrue(r['Location'].startswith(reverse('familyapp:login')), name)
            self.assertEqual(r['Cache-Control'], 'no-store')
        # 職員向けの画面・管理画面は、この URL には無い
        for path in ('/accounts/login/', '/admin/', '/users/', '/family/', '/family/staff/inbox/'):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.client.get('/healthz/').status_code, 200)

    def test_login_logout(self):
        r = self.client.get(reverse('familyapp:login'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Cache-Control'], 'no-store')
        r = self.login(password='chigau-password')
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, 'メールアドレスかパスワードが違います', status_code=400)
        r = self.login(email='inai@example.test')
        self.assertContains(r, 'メールアドレスかパスワードが違います', status_code=400)   # あるかないかは出さない
        r = self.login(email=' HAHA@example.test ')
        self.assertRedirects(r, reverse('familyapp:home'))
        # セッションにはアカウントの番号・印だけ
        self.assertEqual(self.client.session[auth.SESSION_ACCOUNT], self.account.pk)
        self.assertNotIn(GOOD_PW, str(dict(self.client.session)))
        r = self.client.get(reverse('familyapp:home'))
        self.assertContains(r, '見本 太郎')
        self.assertContains(r, '連絡帳')
        self.assertEqual(r['Cache-Control'], 'no-store')
        self.assertRedirects(self.client.get(reverse('familyapp:login')), reverse('familyapp:home'))
        self.assertEqual(self.client.get(reverse('familyapp:logout')).status_code, 405)   # ログアウトは POST だけ
        self.client.post(reverse('familyapp:logout'))
        self.assertEqual(self.client.get(reverse('familyapp:home')).status_code, 302)

    def test_next_is_kept_only_inside_this_site(self):
        r = self.client.get(reverse('familyapp:notice'))
        self.assertIn('next=%2Fnotice%2F', r['Location'])
        self.assertRedirects(self.login(next='/notice/'), '/notice/')
        self.client.post(reverse('familyapp:logout'))
        for bad in ('https://evil.example/', '//evil.example/', 'javascript:alert(1)'):
            self.assertRedirects(self.login(next=bad), reverse('familyapp:home'), msg_prefix=bad)
            self.client.post(reverse('familyapp:logout'))

    def test_stopped_account_is_out_from_the_next_request(self):
        self.login()
        self.assertEqual(self.client.get(reverse('familyapp:home')).status_code, 200)
        accounts.set_active(self.account, False)
        self.assertEqual(self.client.get(reverse('familyapp:home')).status_code, 302)
        accounts.set_active(self.account, True)
        self.assertEqual(self.client.get(reverse('familyapp:home')).status_code, 302)   # 入り直しが要る
        self.assertEqual(self.login().status_code, 302)

    def test_password_change_ends_other_sessions(self):
        self.login()
        accounts.set_password(self.account, 'atarashii-kotoba-77')
        self.assertEqual(self.client.get(reverse('familyapp:home')).status_code, 302)

    def test_family_page_off_closes_the_app(self):
        self.login()
        self.f.use_family_page = False
        self.f.save(update_fields=['use_family_page'])
        self.assertEqual(self.client.get(reverse('familyapp:home')).status_code, 302)
        self.assertEqual(self.login().status_code, 400)


class SetupTests(FamilyAppTestCase):
    def setUp(self):
        super().setUp()
        father = Guardian.objects.create(beneficiary=self.taro, last_name='見本', first_name='父', relation='father', email='chichi@example.test')
        self.new_account, _ = accounts.link_guardian(father, self.staff)
        self.url = accounts.setup_url(self.new_account)
        self.path = self.url.replace('https://family.example.test', '')

    def test_setup_path_matches_the_link_staff_hand_out(self):
        token = accounts.setup_token(self.new_account)
        self.assertEqual(reverse('familyapp:setup', args=[token]), accounts.SETUP_PATH.format(token=token))

    def test_set_password_then_logged_in_and_link_is_used_up(self):
        r = self.client.get(self.path)
        self.assertContains(r, 'chichi@example.test')
        self.assertEqual(r['Cache-Control'], 'no-store')
        self.assertEqual(r['Referrer-Policy'], 'same-origin')
        r = self.client.post(self.path, {'password': GOOD_PW, 'password2': GOOD_PW + 'x'})
        self.assertContains(r, '同じではありません', status_code=400)
        r = self.client.post(self.path, {'password': '12345678', 'password2': '12345678'})
        self.assertEqual(r.status_code, 400)                        # ありふれている・数字だけ
        self.new_account.refresh_from_db()
        self.assertFalse(self.new_account.has_password)
        r = self.client.post(self.path, {'password': GOOD_PW, 'password2': GOOD_PW})
        self.assertRedirects(r, reverse('familyapp:home') + '?welcome=1')
        self.assertEqual(self.client.session[auth.SESSION_ACCOUNT], self.new_account.pk)
        # 1 回使ったリンクは使えない
        self.client.post(reverse('familyapp:logout'))
        self.assertEqual(self.client.get(self.path).status_code, 404)
        self.assertEqual(self.client.post(self.path, {'password': 'betsu-no-kotoba-9', 'password2': 'betsu-no-kotoba-9'}).status_code, 404)
        self.assertEqual(self.login(email='chichi@example.test').status_code, 302)

    def test_bad_or_stopped_link(self):
        self.assertEqual(self.client.get('/account/setup/dame/').status_code, 404)
        accounts.set_active(self.new_account, False)
        self.assertEqual(self.client.get(self.path).status_code, 404)


class ScreenTests(FamilyAppTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def pick(self, child):
        return self.client.post(reverse('familyapp:child'), {'child': child.pk})

    # ---- きょうだい・ほかの家庭
    def test_sibling_switch_and_other_family_is_never_reachable(self):
        self.assertContains(self.client.get(reverse('familyapp:home')), '見本 花子')
        self.pick(self.hana)
        r = self.client.get(reverse('familyapp:book_to'))
        self.assertContains(r, '見本 花子 さんの様子')
        # よその子の番号を送っても切り替わらない
        self.pick(self.jiro)
        r = self.client.get(reverse('familyapp:book_to'))
        self.assertContains(r, '見本 花子 さんの様子')
        self.assertNotContains(self.client.get(reverse('familyapp:home')), 'よそ 次郎')

    # ---- 連絡帳（成より）
    def test_book_from_shows_only_sent_and_mark_read(self):
        book, _ = services.save_book(self.f, self.taro, self.today, 'きょうは公園で遊びました。', self.staff, send=True)
        services.save_book(self.f, self.taro, self.today - datetime.timedelta(days=1), '下書きの文', self.staff, send=False)
        theirs, _ = services.save_book(self.f, self.jiro, self.today, 'よその子の連絡帳', self.staff, send=True)
        r = self.client.get(reverse('familyapp:home'))
        self.assertContains(r, 'まだ読んでいないもの 1 件')
        r = self.client.get(reverse('familyapp:book_from'))
        self.assertContains(r, 'きょうは公園で遊びました。')
        self.assertNotContains(r, 'よその子の連絡帳')
        r = self.client.get(reverse('familyapp:book_from') + f'?date={self.today - datetime.timedelta(days=1)}')
        self.assertNotContains(r, '下書きの文')
        self.assertContains(r, 'この日の連絡帳はありません')
        # 開いただけ（GET）では確認済みにならない。画面のスクリプトか「確認しました」が POST を送る
        book.refresh_from_db()
        self.assertFalse(book.is_read)
        r = self.client.post(reverse('familyapp:book_from'), {'book': theirs.pk}, headers={'X-Requested-With': 'fetch'})
        self.assertEqual(r.status_code, 404)
        theirs.refresh_from_db()
        self.assertFalse(theirs.is_read)
        r = self.client.post(reverse('familyapp:book_from'), {'book': book.pk}, headers={'X-Requested-With': 'fetch'})
        self.assertTrue(r.json()['ok'])
        book.refresh_from_db()
        self.assertTrue(book.is_read)
        self.assertIn('見本 母', book.read_by)

    def test_book_read_on_open_switch(self):
        services.save_book(self.f, self.taro, self.today, '本文', self.staff, send=True)
        self.assertContains(self.client.get(reverse('familyapp:book_from')), "'X-Requested-With': 'fetch'")
        old = views.BOOK_READ_ON_OPEN
        views.BOOK_READ_ON_OPEN = False
        try:
            r = self.client.get(reverse('familyapp:book_from'))
            self.assertNotContains(r, "'X-Requested-With': 'fetch'")
            self.assertContains(r, '確認しました')
        finally:
            views.BOOK_READ_ON_OPEN = old

    # ---- 連絡帳（保護者より）
    def test_book_to(self):
        url = reverse('familyapp:book_to')
        r = self.client.post(url, {'date': self.today.isoformat(), 'body': ' 朝ごはんをよく食べました。 '})
        self.assertRedirects(r, f'{url}?date={self.today.isoformat()}&sent=1')
        note = ParentNote.objects.get()
        self.assertEqual((note.beneficiary, note.body, note.account), (self.taro, '朝ごはんをよく食べました。', self.account))
        self.assertContains(self.client.get(url), '朝ごはんをよく食べました。')
        self.assertEqual(self.client.post(url, {'date': self.today.isoformat(), 'body': '  '}).status_code, 400)
        old = self.today - datetime.timedelta(days=services.NOTE_BACK_DAYS + 1)
        self.assertEqual(self.client.post(url, {'date': old.isoformat(), 'body': '古い日'}).status_code, 400)
        self.assertEqual(ParentNote.objects.count(), 1)

    # ---- 欠席連絡・その他の連絡
    def test_absence_goes_to_staff_inbox_and_becomes_absent_after_staff_confirms(self):
        day = self.today + datetime.timedelta(days=2)
        visit = ScheduledVisit.objects.create(facility=self.f, beneficiary=self.taro, date=day)
        url = reverse('familyapp:absence')
        self.assertEqual(self.client.post(url, {'dates': [day.isoformat()], 'body': '熱'}).status_code, 400)   # 理由が無い
        self.assertEqual(self.client.post(url, {'reason': 'sick'}).status_code, 400)                           # 日にちが無い
        r = self.client.post(url, {'use_today': '1', 'dates': [day.isoformat(), '2000-01-01'], 'reason': 'sick', 'body': '熱があります'})
        self.assertRedirects(r, url + '?done=1', fetch_redirect_response=False)
        rows = FamilyMessage.objects.order_by('target_date')
        self.assertEqual([m.target_date for m in rows], [self.today, day])
        self.assertTrue(all(m.beneficiary == self.taro and m.account == self.account and m.kind == FamilyMessage.KIND_ABSENCE for m in rows))
        visit.refresh_from_db()
        self.assertNotEqual(visit.status, ScheduledVisit.STATUS_ABSENT)      # 職員が確認するまでは変わらない
        r = self.client.get(url + '?done=1')
        self.assertContains(r, 'の連絡を送りました')
        self.assertContains(r, '未確認')
        # 同じ日をもう一度送っても増えない
        self.client.post(url, {'dates': [day.isoformat()], 'reason': 'personal'})
        self.assertEqual(FamilyMessage.objects.count(), 2)
        services.confirm_message(rows[1], self.staff)
        visit.refresh_from_db()
        self.assertEqual(visit.status, ScheduledVisit.STATUS_ABSENT)
        self.assertContains(self.client.get(url), '確認済み')

    def test_other_message_and_only_this_childs_history(self):
        url = reverse('familyapp:message')
        self.assertEqual(self.client.post(url, {'use_today': '1', 'body': ''}).status_code, 400)
        self.assertRedirects(self.client.post(url, {'use_today': '1', 'body': '17:30 に迎えに行きます'}), url + '?done=1')
        m = FamilyMessage.objects.get()
        self.assertEqual((m.kind, m.beneficiary, m.target_date), (FamilyMessage.KIND_OTHER, self.taro, self.today))
        services.create_messages(self.f, self.jiro, self.other_parent, FamilyMessage.KIND_OTHER, [self.today], body='よその家の連絡')
        r = self.client.get(url)
        self.assertContains(r, '17:30 に迎えに行きます')
        self.assertNotContains(r, 'よその家の連絡')

    # ---- 予定と利用日申請
    def test_calendar_and_request(self):
        day = self.today + datetime.timedelta(days=3)
        closed = self.today + datetime.timedelta(days=4)
        yotei.save_event(self.f, day, 'クッキング', False, '持ち物：エプロン', self.staff)
        yotei.save_event(self.f, closed, '', True, '', self.staff)
        url = reverse('familyapp:calendar')
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertContains(self.client.get(f'{url}?ym={day.year}-{day.month:02d}'), 'クッキング')
        self.assertEqual(self.client.get(f'{url}?ym={day.year}-{day.month:02d}&apply=1').status_code, 200)
        r = self.client.get(reverse('familyapp:calendar_day', args=[day.isoformat()]))
        self.assertContains(r, '持ち物：エプロン')
        self.assertEqual(self.client.get(reverse('familyapp:calendar_day', args=['nani'])).status_code, 404)
        ym = f'{day.year}-{day.month:02d}'
        r = self.client.post(url, {'ym': ym, 'dates': []})
        self.assertRedirects(r, f'{url}?ym={ym}&apply=1&err=1')
        r = self.client.post(url, {'ym': ym, 'dates': [day.isoformat(), closed.isoformat(), self.today.isoformat()]})
        self.assertRedirects(r, f'{url}?ym={ym}&done=1', fetch_redirect_response=False)
        # 承認なしで来所予定になる。休業日・きょうは入らない
        self.assertEqual(list(ScheduledVisit.objects.filter(beneficiary=self.taro).values_list('date', flat=True)), [day])
        req = UseRequest.objects.get()
        self.assertEqual((req.beneficiary, req.account), (self.taro, self.account))
        self.assertContains(self.client.get(f'{url}?ym={ym}&done=1'), '利用日を申請しました')

    # ---- お知らせ
    def test_notices_newest_first_and_marked_read(self):
        def send(title, children=None):
            n = Notice.objects.create(facility=self.f, title=title, body=title + 'の本文',
                                      target_kind=Notice.TARGET_CHILDREN if children else Notice.TARGET_ALL, created_by=self.staff)
            if children:
                n.children.set(children)
            notices.send(n, self.staff)
            return n
        send('ふるいお知らせ')
        send('あたらしいお知らせ')
        send('よその子だけのお知らせ', [self.jiro])
        Notice.objects.create(facility=self.f, title='下書きのお知らせ', body='x', created_by=self.staff)
        self.assertContains(self.client.get(reverse('familyapp:home')), 'まだ読んでいないもの 2 件')
        r = self.client.get(reverse('familyapp:notice'))
        body = r.content.decode()
        self.assertLess(body.index('あたらしいお知らせ'), body.index('ふるいお知らせ'))
        self.assertNotContains(r, 'よその子だけのお知らせ')
        self.assertNotContains(r, '下書きのお知らせ')
        r = self.client.post(reverse('familyapp:notice'), headers={'X-Requested-With': 'fetch'})
        self.assertEqual(r.json(), {'ok': True, 'read': 2})
        self.assertEqual(notices.unread_count(self.f, self.taro), 0)
        self.assertEqual(NoticeRecipient.objects.filter(beneficiary=self.jiro, read_at__isnull=True).count(), 3)

    # ---- 個別支援計画・実績記録票・そのほか
    def test_plan_record_manifest_and_404(self):
        self.assertContains(self.client.get(reverse('familyapp:plan')), '確認できる個別支援計画は、まだありません')
        self.assertContains(self.client.get(reverse('familyapp:record')), '準備中')
        r = self.client.get(reverse('familyapp:manifest'))
        self.assertEqual(r.json()['start_url'], '/')
        self.assertIn('見本の事業所', r.json()['name'])
        self.assertEqual(self.client.get('/nai/').status_code, 404)

    def test_dev_banner_only_on_dev_and_local_hosts(self):
        hosts = ['st-naru-family.example.test', 'naru-family.example.test', 'localhost']
        with self.settings(ALLOWED_HOSTS=hosts + ['testserver']):
            for url in (reverse('familyapp:home'), reverse('familyapp:absence')):
                self.assertContains(self.client.get(url, headers={'host': hosts[0]}), '開発環境（試し用）')
                self.assertContains(self.client.get(url, headers={'host': 'localhost:8001'}), '開発環境（試し用）')
                self.assertNotContains(self.client.get(url, headers={'host': hosts[1]}), '開発環境（試し用）')
            self.client.post(reverse('familyapp:logout'))
            self.assertContains(self.client.get(reverse('familyapp:login'), headers={'host': hosts[0]}), '開発環境（試し用）')
            self.assertNotContains(self.client.get(reverse('familyapp:login'), headers={'host': hosts[1]}), '開発環境（試し用）')

    def test_session_expired_fetch_gets_401_not_a_login_page(self):
        book, _ = services.save_book(self.f, self.taro, self.today, '本文', self.staff, send=True)
        self.client.post(reverse('familyapp:logout'))
        r = self.client.post(reverse('familyapp:book_from'), {'book': book.pk}, headers={'X-Requested-With': 'fetch'})
        self.assertEqual(r.status_code, 401)
        book.refresh_from_db()
        self.assertFalse(book.is_read)
