"""アップロードしたファイルの配信：画像と PDF 以外は開かせない・署名つきアドレスの期限は延ばせない（2026-11-22 セキュリティ診断）"""
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings

from accounts.models import StaffAccount
from beneficiaries.models import Beneficiary, BeneficiaryDocument
from facilities import media, uploads
from facilities.models import Facility

import datetime
import tempfile


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='media-safety-'))
class MediaSafetyTests(TestCase):
    def setUp(self):
        self.f = Facility.objects.create(name='見本の事業所')
        self.staff = StaffAccount.objects.create_user('media_staff', password='pw12345678', facility=self.f)
        self.child = Beneficiary.objects.create(facility=self.f, last_name='見本', first_name='太郎', date_of_birth=datetime.date(2016, 4, 1))
        self.client.force_login(self.staff)

    def put(self, name, body):
        """利用者の書類として、決めた名前でファイルを置く（配信の側だけを確かめるため）"""
        path = default_storage.save(f'beneficiary_documents/2026/11/{name}', ContentFile(body))
        BeneficiaryDocument.objects.create(beneficiary=self.child, file=path, **self.extra())
        return '/media/' + path

    def extra(self):
        names = {f.name for f in BeneficiaryDocument._meta.fields}
        return {k: v for k, v in {'title': '見本', 'name': '見本'}.items() if k in names}

    def test_html_and_svg_are_downloaded_not_opened(self):
        for name, body in (('x.html', b'<script>alert(1)</script>'), ('x.svg', b'<svg onload="alert(1)"/>'), ('x.txt', b'hello')):
            res = self.client.get(self.put(name, body))
            self.assertEqual(res.status_code, 200, name)
            self.assertEqual(res['Content-Type'], 'application/octet-stream', name)
            self.assertTrue(res['Content-Disposition'].startswith('attachment'), name)
            self.assertEqual(res['X-Content-Type-Options'], 'nosniff')
            self.assertIn('sandbox', res['Content-Security-Policy'])

    def test_images_and_pdf_open_inline_with_their_own_type(self):
        for name, ctype in (('a.png', 'image/png'), ('a.JPG', 'image/jpeg'), ('a.pdf', 'application/pdf')):
            res = self.client.get(self.put(name, b'<script>alert(1)</script>'))     # 中身が何でも、種類は拡張子どおり（HTML としては開かない）
            self.assertEqual(res['Content-Type'], ctype, name)
            self.assertNotIn('attachment', res.get('Content-Disposition', ''), name)
            self.assertEqual(res['X-Content-Type-Options'], 'nosniff')
        self.assertNotIn('Content-Security-Policy', self.client.get(self.put('b.pdf', b'%PDF-1.4')))

    def test_upload_names_drop_active_extensions(self):
        for bad in ('evil.html', 'evil.HTM', 'pic.svg', 'a.xhtml', 'run.js', 'x.php'):
            self.assertNotIn('.', uploads.photo_upload_to(None, bad).rsplit('/', 1)[1], bad)
        self.assertTrue(uploads.photo_upload_to(None, 'IMG_0001.JPG').endswith('.jpg'))
        self.assertTrue(uploads.document_upload_to(None, '計画書.pdf').endswith('.pdf'))

    def test_signed_url_lifetime_cannot_be_extended(self):
        url = self.put('s.png', b'png')
        path = url[len('/media/'):]
        self.client.logout()
        signed = media.signed_media_url(path, max_age=60)
        self.assertNotIn('max_age', signed)
        self.assertEqual(self.client.get(signed).status_code, 200)
        later = __import__('time').time() + 120
        with mock.patch('time.time', return_value=later):
            self.assertEqual(self.client.get(signed).status_code, 404)                        # 期限切れ
            self.assertEqual(self.client.get(signed + '&max_age=86400').status_code, 404)     # アドレスに足しても延びない
        # ほかのファイルには使えない
        other = self.put('t.png', b'png')
        self.assertEqual(self.client.get(other + '?' + signed.split('?', 1)[1]).status_code, 404)
