"""ログインの一時ロックが、どの入口でも効くこと・接続元の IP の読み方（2026-11-22 セキュリティ診断）"""
import datetime

from django.contrib.auth import authenticate
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from facilities.models import Facility

from . import security
from .models import LoginEvent, StaffAccount

PW = 'pw12345678'


class LockEverywhereTests(TestCase):
    def setUp(self):
        self.f = Facility.objects.create(name='見本の事業所')
        self.user = StaffAccount.objects.create_user('lock_staff', password=PW, facility=self.f, role=StaffAccount.ROLE_ADMIN)
        self.root = StaffAccount.objects.create_superuser('lock_root', password=PW)

    def fail(self, username, n, **extra):
        url = reverse('accounts:login')
        return [self.client.post(url, {'username': username, 'password': 'chigau'}, **extra).status_code for _ in range(n)]

    def test_admin_login_is_locked_too(self):
        """管理画面（/admin/login/）からも、ロック中は入れない（以前は何回でも試せた）"""
        url = '/admin/login/?next=/admin/'
        for _ in range(security.USER_TRIES):
            self.client.post(url, {'username': 'lock_root', 'password': 'chigau'})
        res = self.client.post(url, {'username': 'lock_root', 'password': PW})
        self.assertEqual(res.status_code, 200)                                  # 入れずにログインの画面のまま
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(LoginEvent.objects.filter(kind=LoginEvent.KIND_FAIL, username='lock_root').count(), security.USER_TRIES)
        self.assertEqual(LoginEvent.objects.filter(kind=LoginEvent.KIND_LOCK, username='lock_root').count(), 1)
        # ロックが解ければ入れる
        LoginEvent.objects.update(created_at=timezone.now() - datetime.timedelta(minutes=security.LOCK_MINUTES + security.WINDOW_MINUTES + 1))
        res = self.client.post(url, {'username': 'lock_root', 'password': PW})
        self.assertEqual(res.status_code, 302)

    def test_authenticate_itself_refuses_while_locked(self):
        """authenticate() を直に呼ぶ入口（スマホアプリ用の API など）でも同じ"""
        rf = RequestFactory()
        self.assertIsNotNone(authenticate(rf.post('/'), username='lock_staff', password=PW))
        for _ in range(security.USER_TRIES):
            self.assertIsNone(authenticate(rf.post('/'), username='lock_staff', password='chigau'))
        self.assertIsNone(authenticate(rf.post('/'), username='lock_staff', password=PW))
        self.assertIsNone(authenticate(rf.post('/'), username='LOCK_STAFF', password=PW))       # 大文字小文字を変えても同じ
        # ほかの職員は巻きこまれない
        other = StaffAccount.objects.create_user('lock_other', password=PW, facility=self.f)
        self.assertEqual(authenticate(rf.post('/'), username='lock_other', password=PW), other)

    def test_login_screen_still_locks_and_does_not_extend(self):
        codes = self.fail('lock_staff', security.USER_TRIES + 4)
        self.assertEqual(set(codes), {200})
        self.assertEqual(LoginEvent.objects.filter(kind=LoginEvent.KIND_FAIL, username='lock_staff').count(), security.USER_TRIES)
        self.assertEqual(LoginEvent.objects.filter(kind=LoginEvent.KIND_LOCK, username='lock_staff').count(), 4)
        res = self.client.post(reverse('accounts:login'), {'username': 'lock_staff', 'password': PW})
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(res.status_code, 200)


class ClientIpTests(TestCase):
    def ip(self, **meta):
        return security.client_ip(RequestFactory().get('/', **meta))

    def test_reads_the_last_hop_not_the_first(self):
        # nginx（$proxy_add_x_forwarded_for）は、送り手が書いた値のうしろに本当の接続元を足す
        self.assertEqual(self.ip(HTTP_X_FORWARDED_FOR='1.2.3.4, 203.0.113.7', REMOTE_ADDR='127.0.0.1'), '203.0.113.7')
        self.assertEqual(self.ip(HTTP_X_FORWARDED_FOR='203.0.113.7', REMOTE_ADDR='127.0.0.1'), '203.0.113.7')
        self.assertEqual(self.ip(HTTP_X_FORWARDED_FOR=' evil , , 203.0.113.7 ', REMOTE_ADDR='127.0.0.1'), '203.0.113.7')
        self.assertEqual(self.ip(REMOTE_ADDR='198.51.100.9'), '198.51.100.9')
        self.assertEqual(security.client_ip(None), '')

    def test_proxy_count_setting(self):
        with override_settings(TRUSTED_PROXY_COUNT=2):       # nginx の前に CDN などがもう 1 段
            self.assertEqual(self.ip(HTTP_X_FORWARDED_FOR='9.9.9.9, 203.0.113.7, 10.0.0.2', REMOTE_ADDR='127.0.0.1'), '203.0.113.7')
            self.assertEqual(self.ip(HTTP_X_FORWARDED_FOR='203.0.113.7', REMOTE_ADDR='127.0.0.1'), '127.0.0.1')   # 段数に足りなければ信じない
        with override_settings(TRUSTED_PROXY_COUNT=0):       # 中継なし（直接つなぐ）：ヘッダーは信じない
            self.assertEqual(self.ip(HTTP_X_FORWARDED_FOR='1.2.3.4', REMOTE_ADDR='198.51.100.9'), '198.51.100.9')

    def test_spoofed_header_cannot_lock_someone_elses_ip(self):
        """ほかの人の IP を先頭に書いて失敗を重ねても、その IP はロックされない"""
        f = Facility.objects.create(name='見本の事業所')
        StaffAccount.objects.create_user('ip_staff', password=PW, facility=f)
        victim = '198.51.100.50'
        for i in range(security.IP_TRIES + 2):
            self.client.post(reverse('accounts:login'), {'username': f'inai{i}', 'password': 'x'},
                             HTTP_X_FORWARDED_FOR=f'{victim}, 203.0.113.99')
        self.assertIsNone(security.locked_until('', victim))
        self.assertIsNotNone(security.locked_until('', '203.0.113.99'))       # ロックされるのは、実際に送ってきた側
        res = self.client.post(reverse('accounts:login'), {'username': 'ip_staff', 'password': PW}, HTTP_X_FORWARDED_FOR=victim)
        self.assertEqual(res.status_code, 302)
