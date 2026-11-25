"""連絡帳（2026-11-25 成の要望）：保護者からの文は連絡帳を送るまで出し続ける・3 つの書き方（メモから AI／評価して AI／自由に）"""
import datetime
import json
from unittest import mock

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from schedules.models import ScheduledVisit

from . import services
from .models import ContactBook, ParentNote
from .tests import Base


@override_settings(ANTHROPIC_API_KEY='test-key')
class BookWritingTests(Base):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.yesterday = self.today - datetime.timedelta(days=1)
        self.sc = self.client_class()
        self.sc.force_login(self.staff)
        ScheduledVisit.objects.create(facility=self.f, beneficiary=self.taro, date=self.today)
        self.edit = reverse('family:staff_book_edit', args=[self.taro.pk]) + f'?date={self.today.isoformat()}'
        self.ai = reverse('family:staff_book_ai', args=[self.taro.pk]) + f'?date={self.today.isoformat()}'

    def test_parent_note_stays_until_a_book_is_sent(self):
        ParentNote.objects.create(facility=self.f, beneficiary=self.taro, date=self.yesterday, body='きのうは寝不足です', sender_name='見本 母')
        html = self.sc.get(self.edit).content.decode()
        self.assertIn('きのうは寝不足です', html)                                    # ほかの日の文も、返すまで出る
        self.assertIn('の分（まだ連絡帳を返していません）', html)
        lst = self.sc.get(reverse('family:staff_book') + f'?date={self.today.isoformat()}').content.decode()
        self.assertIn('保護者より', lst)
        # 連絡帳を送ると、ほかの日の文は出なくなる（その日の文はいつも出る）
        with mock.patch('family.services.notify_book', return_value=(0, [])):
            self.sc.post(reverse('family:staff_book_edit', args=[self.taro.pk]),
                         {'date': self.today.isoformat(), 'body': '元気に過ごしました', 'action': 'send'})
        self.assertNotIn('きのうは寝不足です', self.sc.get(self.edit).content.decode())
        self.assertEqual(services.pending_note_children(self.f), {})
        ParentNote.objects.create(facility=self.f, beneficiary=self.taro, date=self.today, body='きょうの分です')
        self.assertIn('きょうの分です', self.sc.get(self.edit).content.decode())

    def test_three_ways_to_write(self):
        html = self.sc.get(self.edit).content.decode()
        for text in ('メモ書きから文章作成', '評価して文章作成', '文章作成', 'AI で文にする', '観点を出す'):
            self.assertIn(text, html)
        # メモから：名前は AI に渡さず、{名前} を「〇〇さん」に置き換える
        with mock.patch('ai_assist.quick.ask_ai', return_value=('{名前}は公園ですべり台を何回も楽しみました。', None)) as ask:
            res = self.sc.post(self.ai, json.dumps({'action': 'write', 'memo': '公園 すべり台 何回も'}), content_type='application/json')
        self.assertEqual(res.json()['result'], '太郎さんは公園ですべり台を何回も楽しみました。')
        self.assertNotIn('太郎', ask.call_args[0][1])
        # 評価して：活動 → 観点 → はい／いいえ → 文
        with mock.patch('ai_assist.quick.ask_ai', return_value=('{"viewpoints": ["順番を待てた", "最後まで参加できた"]}', None)):
            res = self.sc.post(self.ai, json.dumps({'action': 'viewpoints', 'activity': 'クッキー作り'}), content_type='application/json')
        self.assertEqual([p['text'] for p in res.json()['viewpoints']], ['順番を待てた', '最後まで参加できた'])
        with mock.patch('ai_assist.quick.ask_ai', return_value=('{名前}さんはクッキー作りで順番を待てました。', None)) as ask:
            res = self.sc.post(self.ai, json.dumps({'action': 'write', 'activity': 'クッキー作り', 'viewpoints': [
                {'text': '順番を待てた', 'answer': 'yes'}, {'text': '最後まで参加できた', 'answer': 'no'}]}), content_type='application/json')
        self.assertEqual(res.json()['result'], '太郎さんはクッキー作りで順番を待てました。')
        prompt = ask.call_args[0][1]
        self.assertIn('順番を待てた → はい', prompt)
        self.assertIn('最後まで参加できた → いいえ', prompt)
        # 空のとき・よその事業所
        self.assertEqual(self.sc.post(self.ai, json.dumps({'action': 'write'}), content_type='application/json').status_code, 400)
        self.assertEqual(self.sc.post(self.ai, json.dumps({'action': 'viewpoints'}), content_type='application/json').status_code, 400)
        other = type(self.staff).objects.create_user('other_book', password='pw12345678', facility=self.other)
        c = self.client_class()
        c.force_login(other)
        self.assertEqual(c.post(self.ai, json.dumps({'action': 'write', 'memo': 'x'}), content_type='application/json').status_code, 404)
        self.assertFalse(ContactBook.objects.exists())                                 # AI は文を作るだけ（保存・送信はしない）
