"""保護者アプリ（別アプリ。メールアドレスとパスワードで入る）のアカウント：family/accounts.py と職員の画面"""
import datetime
from unittest import mock

from django.core import mail
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import StaffAccount
from beneficiaries.models import Beneficiary, Guardian
from facilities.models import Facility
from schedules.models import ScheduledVisit

from . import accounts, services, yotei
from .models import FamilyAccount, FamilyAccountLink, FamilyMessage, ParentNote, UseRequest

GOOD_PW = 'niji-no-hashi-2026'


@override_settings(FAMILY_APP_URL='https://family.example.test/')
class FamilyAccountTests(TestCase):
    def setUp(self):
        # LINE連携はオフ（成の環境と同じ）。保護者ページの印だけオン
        self.f = Facility.objects.create(name='見本の事業所', use_family_page=True, home_icons=True, use_line=False)
        self.other = Facility.objects.create(name='よその事業所', use_family_page=True, use_line=False)
        self.staff = StaffAccount.objects.create_user('acc_admin', password='pw12345678', facility=self.f, role=StaffAccount.ROLE_ADMIN)
        kw = dict(date_of_birth=datetime.date(2016, 4, 1))
        self.taro = Beneficiary.objects.create(facility=self.f, last_name='見本', first_name='太郎', last_name_kana='みほん', first_name_kana='たろう', **kw)
        self.hana = Beneficiary.objects.create(facility=self.f, last_name='見本', first_name='花子', last_name_kana='みほん', first_name_kana='はなこ', **kw)
        self.mother_taro = Guardian.objects.create(beneficiary=self.taro, last_name='見本', first_name='母', relation='mother', email='Haha@Example.test ')
        self.mother_hana = Guardian.objects.create(beneficiary=self.hana, last_name='見本', first_name='母', relation='mother', email='haha@example.test')
        self.father_taro = Guardian.objects.create(beneficiary=self.taro, last_name='見本', first_name='父', relation='father', email='chichi@example.test')
        self.no_mail = Guardian.objects.create(beneficiary=self.hana, last_name='見本', first_name='祖母', relation='other')

    def ready(self, guardian, password=GOOD_PW):
        account, _ = accounts.link_guardian(guardian, self.staff)
        accounts.set_password(account, password)
        return account

    # ---- ひも付け
    def test_siblings_share_one_account_and_parents_get_their_own(self):
        a1, created1 = accounts.link_guardian(self.mother_taro, self.staff)
        a2, created2 = accounts.link_guardian(self.mother_hana, self.staff, confirm=True)
        a3, _ = accounts.link_guardian(self.father_taro, self.staff)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(a1.pk, a2.pk)                    # 同じメールアドレス（大文字・空白のちがいは同じ）＝同じ人
        self.assertEqual(a1.email, 'haha@example.test')
        self.assertNotEqual(a1.pk, a3.pk)                 # 父は別のアカウント
        self.assertEqual([g.beneficiary for g in accounts.guardians_of(a1)], [self.taro, self.hana])
        self.assertEqual([g.beneficiary for g in accounts.guardians_of(a3)], [self.taro])
        # もう一度押しても増えない
        accounts.link_guardian(self.mother_taro, self.staff)
        self.assertEqual(FamilyAccountLink.objects.count(), 3)
        self.assertEqual(FamilyAccount.objects.count(), 2)

    def test_link_needs_a_valid_email_and_same_email_in_another_facility_is_separate(self):
        with self.assertRaises(accounts.AccountError):
            accounts.link_guardian(self.no_mail, self.staff)
        self.no_mail.email = 'まちがい'
        with self.assertRaises(accounts.AccountError):
            accounts.link_guardian(self.no_mail, self.staff)
        a1 = self.ready(self.mother_taro)
        ben = Beneficiary.objects.create(facility=self.other, last_name='他所', first_name='子', date_of_birth=datetime.date(2016, 4, 1))
        g = Guardian.objects.create(beneficiary=ben, last_name='他所', first_name='母', email='haha@example.test')
        a2 = self.ready(g, 'betsu-no-aikotoba-77')
        self.assertNotEqual(a1.pk, a2.pk)
        self.assertEqual(accounts.authenticate('haha@example.test', GOOD_PW), a1)                       # パスワードの合うほうだけ
        self.assertEqual(accounts.authenticate('haha@example.test', 'betsu-no-aikotoba-77'), a2)
        self.assertIsNone(accounts.authenticate('haha@example.test', GOOD_PW, facility=self.other))

    def test_changing_email_needs_unlink_and_unlinked_account_is_stopped(self):
        account = self.ready(self.father_taro)
        self.father_taro.email = 'atarashii@example.test'
        self.father_taro.save()
        with self.assertRaises(accounts.AccountError):
            accounts.link_guardian(self.father_taro, self.staff)
        accounts.unlink_guardian(self.father_taro)
        account.refresh_from_db()
        self.assertFalse(account.is_active)               # だれにも結びつかないアカウントは止まる
        self.assertIsNone(accounts.authenticate('chichi@example.test', GOOD_PW))
        new, created = accounts.link_guardian(Guardian.objects.get(pk=self.father_taro.pk), self.staff)
        self.assertTrue(created)
        self.assertEqual(new.email, 'atarashii@example.test')

    def test_left_children_are_not_listed(self):
        account = self.ready(self.mother_taro)
        accounts.link_guardian(self.mother_hana, self.staff, confirm=True)
        self.hana.status = Beneficiary.STATUS_INACTIVE
        self.hana.save()
        self.assertEqual([g.beneficiary for g in accounts.guardians_of(account)], [self.taro])
        self.taro.status = Beneficiary.STATUS_GRADUATED
        self.taro.save()
        self.assertIsNone(accounts.session_context(account))

    # ---- パスワード・入る
    def test_setup_link_works_once_and_expires(self):
        account, _ = accounts.link_guardian(self.mother_taro, self.staff)
        self.assertFalse(account.has_password)
        self.assertIsNone(accounts.authenticate('haha@example.test', ''))
        url = accounts.setup_url(account)
        self.assertTrue(url.startswith('https://family.example.test/account/setup/'))
        token = url.rstrip('/').rsplit('/', 1)[1]
        self.assertEqual(accounts.check_setup_token(token), account)
        self.assertIsNone(accounts.check_setup_token(token + 'x'))
        self.assertIsNone(accounts.check_setup_token(''))
        accounts.set_password(account, GOOD_PW)
        self.assertIsNone(accounts.check_setup_token(token))          # 1 回使ったリンクは使えない
        account.refresh_from_db()
        self.assertTrue(account.has_password)
        self.assertIsNotNone(account.password_set_at)
        self.assertNotIn(GOOD_PW, account.password)                   # そのままでは持たない
        # 期限（7 日）を過ぎたリンク・止めたアカウントのリンクは使えない
        token2 = accounts.setup_token(account)
        later = timezone.now() + datetime.timedelta(days=accounts.SETUP_DAYS, minutes=1)
        with mock.patch('time.time', return_value=later.timestamp()):
            self.assertIsNone(accounts.check_setup_token(token2))
        accounts.set_active(account, False)
        self.assertIsNone(accounts.check_setup_token(token2))

    def test_weak_passwords_are_refused(self):
        account, _ = accounts.link_guardian(self.mother_taro, self.staff)
        for bad in ('short1', '12345678901', 'password', 'haha@example.test'):
            with self.assertRaises(ValidationError, msg=bad):
                accounts.set_password(account, bad)
        account.refresh_from_db()
        self.assertFalse(account.has_password)

    def test_authenticate_and_lock_after_failures(self):
        account = self.ready(self.mother_taro)
        self.assertEqual(accounts.authenticate(' HAHA@example.test', GOOD_PW), account)
        account.refresh_from_db()
        self.assertIsNotNone(account.last_login_at)
        self.assertIsNone(accounts.authenticate('haha@example.test', 'chigau-password'))
        self.assertIsNone(accounts.authenticate('inai@example.test', GOOD_PW))
        for _ in range(accounts.MAX_FAILURES):
            accounts.authenticate('haha@example.test', 'chigau-password')
        account.refresh_from_db()
        self.assertTrue(account.is_locked)
        self.assertIsNone(accounts.authenticate('haha@example.test', GOOD_PW))     # 止まっている間は正しくても入れない
        FamilyAccount.objects.filter(pk=account.pk).update(locked_until=timezone.now() - datetime.timedelta(seconds=1))
        self.assertEqual(accounts.authenticate('haha@example.test', GOOD_PW), account)

    def test_stopped_account_and_facility_switch(self):
        account = self.ready(self.mother_taro)
        accounts.set_active(account, False)
        self.assertIsNone(accounts.authenticate('haha@example.test', GOOD_PW))
        self.assertIsNone(accounts.session_context(account))
        accounts.set_active(account, True)
        self.assertEqual(accounts.authenticate('haha@example.test', GOOD_PW), account)
        self.f.use_family_page = False
        self.f.save()
        self.assertIsNone(accounts.session_context(FamilyAccount.objects.get(pk=account.pk)))

    # ---- 保護者アプリからの記録（いまある関数に、返ってきた guardian をそのまま渡す）
    def test_records_from_the_app_keep_the_sender_account(self):
        account = self.ready(self.mother_taro)
        accounts.link_guardian(self.mother_hana, self.staff, confirm=True)
        ctx = accounts.session_context(account, child_id=self.hana.pk)
        self.assertEqual(ctx['child'], self.hana)
        self.assertEqual(ctx['children'], [self.taro, self.hana])
        self.assertEqual(accounts.session_context(account, child_id=999999)['child'], self.taro)   # 自分の子でなければ最初の子
        guardian, child = ctx['guardian'], ctx['child']
        day = timezone.localdate() + datetime.timedelta(days=3)
        ScheduledVisit.objects.create(facility=self.f, beneficiary=child, date=day)
        made, _ = services.create_messages(self.f, child, guardian, FamilyMessage.KIND_ABSENCE, [day], reason='sick', body='熱があります')
        self.assertEqual(made[0].account, account)
        self.assertEqual(made[0].sender_name, '見本 母（母）')
        self.assertEqual(made[0].line_user_id, '')
        services.confirm_message(made[0], self.staff)
        self.assertEqual(ScheduledVisit.objects.get(beneficiary=child, date=day).status, ScheduledVisit.STATUS_ABSENT)
        note = services.add_parent_note(self.f, child, guardian, timezone.localdate(), 'よく眠れました')
        self.assertEqual(note.account, account)
        day2 = timezone.localdate() + datetime.timedelta(days=5)
        made_days, skipped = yotei.apply_days(self.f, child, [day2], via=UseRequest.VIA_PARENT,
                                              sender_name=services.sender_label(guardian), account=services.account_of(guardian))
        self.assertEqual((made_days, skipped), ([day2], []))
        self.assertEqual(UseRequest.objects.get(beneficiary=child, date=day2).account, account)
        # LINE から入った保護者（アカウントなし）の記録は、これまでどおり account が空
        self.assertIsNone(services.account_of(self.no_mail))
        self.assertEqual(ParentNote.objects.filter(account__isnull=True).count(), 0)

    # ---- 職員の画面
    def test_staff_screen_without_line(self):
        self.client.force_login(self.staff)
        detail = reverse('beneficiaries:detail', args=[self.taro.pk])
        html = self.client.get(detail).content.decode()
        self.assertIn('保護者アプリ：', html)
        self.assertIn('アカウントを作る', html)
        self.assertNotIn('リンクを LINE で送る', html)
        post = lambda g, action: self.client.post(reverse('family:staff_account', args=[g.pk]), {'action': action}, follow=True)   # noqa: E731
        html = post(self.mother_taro, 'create').content.decode()
        self.assertIn('アカウントを作りました', html)
        self.assertIn('パスワード未設定', html)
        account = FamilyAccount.objects.get(facility=self.f, email='haha@example.test')
        self.assertEqual(account.created_by, self.staff)
        html = post(self.mother_taro, 'link').content.decode()
        self.assertIn('https://family.example.test/account/setup/', html)
        account.refresh_from_db()
        self.assertIsNotNone(account.invited_at)
        # メールの設定が無い環境では送らず、リンクを渡すよう案内する
        html = post(self.mother_taro, 'mail').content.decode()
        self.assertIn('メールを送る設定がこの環境にありません', html)
        with self.settings(EMAIL_HOST='smtp.example.test', EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'):
            html = post(self.mother_taro, 'mail').content.decode()
        self.assertIn('へ、パスワード設定のリンクを送りました', html)
        self.assertEqual(mail.outbox[0].to, ['haha@example.test'])
        self.assertIn('https://family.example.test/account/setup/', mail.outbox[0].body)
        accounts.set_password(account, GOOD_PW)
        self.assertIn('利用中', self.client.get(detail).content.decode())
        self.assertIn('停止中', post(self.mother_taro, 'stop').content.decode())
        self.assertIn('停止中です', post(self.mother_taro, 'link').content.decode())
        post(self.mother_taro, 'resume')
        post(self.mother_taro, 'unlink')
        self.assertFalse(FamilyAccountLink.objects.filter(guardian=self.mother_taro).exists())
        # メールアドレスの無い保護者・アカウントの無い保護者への操作
        self.assertIn('メールアドレスが入っていません', post(self.no_mail, 'create').content.decode())
        self.assertIn('まだ保護者アプリのアカウントがありません', post(self.father_taro, 'link').content.decode())

    def test_staff_screen_guards(self):
        self.client.force_login(self.staff)
        ben = Beneficiary.objects.create(facility=self.other, last_name='他所', first_name='子', date_of_birth=datetime.date(2016, 4, 1))
        g = Guardian.objects.create(beneficiary=ben, last_name='他所', first_name='母', email='yoso@example.test')
        self.assertEqual(self.client.post(reverse('family:staff_account', args=[g.pk]), {'action': 'create'}).status_code, 404)
        self.assertFalse(FamilyAccount.objects.exists())
        with self.settings(FAMILY_APP_URL=''):
            accounts.link_guardian(self.mother_taro, self.staff)
            html = self.client.post(reverse('family:staff_account', args=[self.mother_taro.pk]), {'action': 'link'}, follow=True).content.decode()
            self.assertIn('FAMILY_APP_URL', html)
        self.f.use_family_page = False
        self.f.save()
        self.client.post(reverse('family:staff_account', args=[self.father_taro.pk]), {'action': 'create'})
        self.assertFalse(FamilyAccountLink.objects.filter(guardian=self.father_taro).exists())
        self.client.logout()
        self.assertEqual(self.client.post(reverse('family:staff_account', args=[self.mother_taro.pk]), {'action': 'stop'}).status_code, 302)
        self.assertTrue(FamilyAccount.objects.get(email='haha@example.test').is_active)

    def test_staff_screens_open_without_line(self):
        """LINE連携がオフでも、職員の「連絡帳」「保護者からの連絡」「お知らせ」「予定」は開ける（ホームのアイコンも準備中にならない）"""
        self.client.force_login(self.staff)
        for name in ('family:staff_book', 'family:staff_inbox', 'family:staff_notice', 'family:staff_calendar'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        html = self.client.get('/home/').content.decode()
        for name in ('family:staff_book', 'family:staff_inbox', 'family:staff_notice'):
            self.assertIn(f'href="{reverse(name)}"', html, name)

    # ---- 2026-11-22 セキュリティ診断の続き
    def test_same_email_needs_confirmation_before_joining_another_family(self):
        """同じメールアドレスのアカウントがもうあるとき：だれの子のアカウントかを見せて、確かめてから結びつける（入れまちがいで別の家庭に見えないように）"""
        account, _ = accounts.link_guardian(self.mother_taro, self.staff)
        with self.assertRaises(accounts.NeedsConfirm) as cm:
            accounts.link_guardian(self.mother_hana, self.staff)
        self.assertIn('見本 太郎', str(cm.exception))
        self.assertFalse(FamilyAccountLink.objects.filter(guardian=self.mother_hana).exists())
        # 職員の画面：1 回目は知らせるだけ。同じ保護者でもう一度押すと結びつく
        self.client.force_login(self.staff)
        url = reverse('family:staff_account', args=[self.mother_hana.pk])
        html = self.client.post(url, {'action': 'create'}, follow=True).content.decode()
        self.assertIn('もうあります', html)
        self.assertIn('見本 太郎', html)
        self.assertFalse(FamilyAccountLink.objects.filter(guardian=self.mother_hana).exists())
        html = self.client.post(url, {'action': 'create'}, follow=True).content.decode()
        self.assertIn('同じメールアドレスのアカウント', html)
        self.assertEqual(FamilyAccountLink.objects.get(guardian=self.mother_hana).account, account)

    def test_reissuing_or_stopping_kills_earlier_links(self):
        account, _ = accounts.link_guardian(self.mother_taro, self.staff)
        t1 = accounts.setup_url(account).rstrip('/').rsplit('/', 1)[1]
        account.refresh_from_db()
        self.assertEqual(accounts.check_setup_token(t1), account)
        t2 = accounts.setup_url(account).rstrip('/').rsplit('/', 1)[1]            # 出し直す
        self.assertIsNone(accounts.check_setup_token(t1))                         # 前のリンクは使えない
        self.assertEqual(accounts.check_setup_token(t2), FamilyAccount.objects.get(pk=account.pk))
        accounts.set_active(FamilyAccount.objects.get(pk=account.pk), False)
        accounts.set_active(FamilyAccount.objects.get(pk=account.pk), True)       # 止めて戻しても、前のリンクは使えないまま
        self.assertIsNone(accounts.check_setup_token(t2))
        # 合い言葉の中に、パスワードのハッシュは入っていない
        fresh = FamilyAccount.objects.get(pk=account.pk)
        from django.core import signing
        data = signing.loads(accounts.setup_token(fresh), salt=accounts.SETUP_SALT)
        self.assertNotIn(data['s'], fresh.password)

    def test_password_reset_link_is_for_admin_and_manager_only(self):
        """パスワードを設定ずみのアカウントのリンク（＝決め直し）は、管理者と児発管だけ。出したことは記録に残る"""
        from accounts.models import SecurityAlert
        account = self.ready(self.mother_taro)
        worker = StaffAccount.objects.create_user('acc_staff', password='pw12345678', facility=self.f, role=StaffAccount.ROLE_STAFF)
        url = reverse('family:staff_account', args=[self.mother_taro.pk])
        self.client.force_login(worker)
        for action in ('link', 'mail'):
            html = self.client.post(url, {'action': action}, follow=True).content.decode()
            self.assertIn('管理者と児発管だけ', html)
            self.assertNotIn('/account/setup/', html)
        self.assertEqual(accounts.authenticate('haha@example.test', GOOD_PW), account)       # パスワードはそのまま
        # まだパスワードを決めていないアカウントのリンクは、職員も出せる（最初の案内）
        accounts.link_guardian(self.father_taro, self.staff)
        html = self.client.post(reverse('family:staff_account', args=[self.father_taro.pk]), {'action': 'link'}, follow=True).content.decode()
        self.assertIn('/account/setup/', html)
        # 児発管は出せる。記録に残る（メールアドレス・リンクは書かない）
        manager = StaffAccount.objects.create_user('acc_jihatsu', password='pw12345678', facility=self.f, role=StaffAccount.ROLE_CHILD_DEV_MANAGER)
        self.client.force_login(manager)
        html = self.client.post(url, {'action': 'link'}, follow=True).content.decode()
        self.assertIn('/account/setup/', html)
        row = SecurityAlert.objects.filter(facility=self.f).order_by('-pk').first()
        self.assertIn('パスワードを決め直すリンクを出しました', row.text)
        self.assertNotIn('example.test', row.text)
        self.assertNotIn('/account/setup/', row.text)

    def test_wrong_password_count_and_timing_guard(self):
        from unittest import mock as m
        account, _ = accounts.link_guardian(self.mother_taro, self.staff)
        # パスワード未設定のアカウントでも、ハッシュの計算をする（招待ずみかどうかを、応答の速さから読み取られないように）
        with m.patch('family.accounts._burn') as burn:
            self.assertIsNone(accounts.authenticate('haha@example.test', 'nandemo'))
        self.assertTrue(burn.called)
        accounts.set_password(account, GOOD_PW)
        for _ in range(3):
            accounts.authenticate('haha@example.test', 'chigau')
        self.assertEqual(FamilyAccount.objects.get(pk=account.pk).failed_count, 3)
        self.assertEqual(accounts.authenticate('haha@example.test', GOOD_PW), account)
        self.assertEqual(FamilyAccount.objects.get(pk=account.pk).failed_count, 0)

