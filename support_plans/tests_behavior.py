"""支援計画シート（2 枚で 1 組）：作る・書く・写す・消す・印刷・欄ごとの AI の整文（2026-11-24 成の要望）"""
import json
from datetime import date
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import StaffAccount
from beneficiaries.models import Beneficiary
from facilities.models import Facility

from .models import BehaviorSheet


@override_settings(ANTHROPIC_API_KEY='test-key')
class BehaviorSheetTests(TestCase):
    def setUp(self):
        self.f = Facility.objects.create(name='見本の事業所')
        self.user = StaffAccount.objects.create_user('bs_staff', password='pass12345', facility=self.f,
                                                     role=StaffAccount.ROLE_ADMIN, last_name='見本', first_name='職員')
        self.client.force_login(self.user)
        self.b = Beneficiary.objects.create(facility=self.f, last_name='架空', first_name='一郎', last_name_kana='かくう',
                                            first_name_kana='いちろう', date_of_birth=date(2015, 4, 1))

    def create(self):
        res = self.client.post(reverse('support_plans:behavior_create'), {'child': self.b.pk})
        sheet = BehaviorSheet.objects.get()
        self.assertRedirects(res, reverse('support_plans:behavior_edit', args=[sheet.pk]))
        return sheet

    def form(self, **extra):
        data = {'made_on': '2026-11-24', 'planner': '見本 職員', 'intake': '・11歳 自閉症', 'bio': '・音に敏感',
                'psycho': '・見通しがないと不安', 'social': '・家庭では落ち着いている',
                'task_title_0': '見通しを持って過ごす', 'task_policy_0': '・写真カードで予定を伝える',
                'task_title_2': 'クールダウンの場所', 'task_policy_2': '・静かな部屋を用意する',
                'service_date': '2026-11-25', 'prov_office_0': '見本の事業所', 'prov_service_0': '放課後等デイサービス',
                'prov_time_0': '14:00-17:30', 'prov_staff_0': '担当A',
                'step_time_0': '14:00-14:30', 'step_activity_0': '来所', 'step_procedure_0': '玄関→静養室（着替え）',
                'step_time_3': '17:00-17:30', 'step_activity_3': '帰り', 'step_procedure_3': '静養室→玄関→送迎',
                'contact_note': '・活動の切り替えは静養室で', 'inquiry_note': ''}
        data.update(extra)
        return data

    def test_list_create_edit_print(self):
        html = self.client.get(reverse('support_plans:behavior_list')).content.decode()
        self.assertIn('架空 一郎', html)
        sheet = self.create()
        html = self.client.get(reverse('support_plans:behavior_edit', args=[sheet.pk])).content.decode()
        self.assertIn('value="見本の事業所"', html)                    # 事業所①に、この事業所の名前
        self.assertIn('value="放課後等デイサービス"', html)
        for text in ('1 枚目　支援計画シート', '生物的なこと', '心理的なこと', '社会的なこと', '支援課題', '対応・方針',
                     '2 枚目　支援手順書 兼 記録用紙', 'サービス手順', '連絡事項', '問い合わせ事項', '文にする'):
            self.assertIn(text, html)
        res = self.client.post(reverse('support_plans:behavior_edit', args=[sheet.pk]), self.form())
        self.assertEqual(res.status_code, 302)
        sheet.refresh_from_db()
        self.assertEqual([t['title'] for t in sheet.tasks], ['見通しを持って過ごす', 'クールダウンの場所'])   # 空の行は詰める
        self.assertEqual([s['activity'] for s in sheet.steps], ['来所', '帰り'])
        self.assertEqual(sheet.service_date, date(2026, 11, 25))
        pr = self.client.get(reverse('support_plans:behavior_print', args=[sheet.pk])).content.decode()
        for text in ('支援計画シート　氏名（架空 一郎）　支援計画者（見本 職員）', '①　見通しを持って過ごす', '②　クールダウンの場所',
                     '・写真カードで予定を伝える', '支援手順書 兼 記録用紙', '2026年11月25日', '玄関→静養室（着替え）',
                     '事業所名①', '14:00-17:30', '・活動の切り替えは静養室で'):
            self.assertIn(text, pr)
        # 読めない日付は保存しない
        res = self.client.post(reverse('support_plans:behavior_edit', args=[sheet.pk]), self.form(made_on='きのう', intake='x'))
        self.assertEqual(res.status_code, 400)
        sheet.refresh_from_db()
        self.assertEqual(sheet.intake, '・11歳 自閉症')

    def test_copy_and_delete(self):
        sheet = self.create()
        self.client.post(reverse('support_plans:behavior_edit', args=[sheet.pk]), self.form(step_check_0='✓', step_note_0='落ち着いていた'))
        res = self.client.post(reverse('support_plans:behavior_copy', args=[sheet.pk]))
        new = BehaviorSheet.objects.exclude(pk=sheet.pk).get()
        self.assertRedirects(res, reverse('support_plans:behavior_edit', args=[new.pk]))
        self.assertEqual(new.intake, '・11歳 自閉症')
        sheet.refresh_from_db()
        self.assertEqual(sheet.steps[0]['check'], '1')                                  # チェックは印だけ
        self.assertEqual((new.steps[0]['check'], new.steps[0]['note']), ('', ''))        # 記録の欄は空に
        self.client.post(reverse('support_plans:behavior_delete', args=[sheet.pk]))
        self.assertEqual(list(BehaviorSheet.objects.values_list('pk', flat=True)), [new.pk])

    def test_ai_turns_memo_into_text(self):
        sheet = self.create()
        url = reverse('support_plans:behavior_ai', args=[sheet.pk])
        with mock.patch('ai_assist.quick.ask_ai', return_value=('・大きな音が苦手で、耳をふさぐ\n・給食の時間に混乱しやすい', None)) as ask:
            res = self.client.post(url, json.dumps({'field': 'psycho', 'memo': '大きな音 苦手 耳ふさぐ 給食 混乱',
                                                    'context': {'情報': '・11歳'}}), content_type='application/json')
        self.assertEqual(res.json()['result'], '・大きな音が苦手で、耳をふさぐ\n・給食の時間に混乱しやすい')
        prompt = ask.call_args[0][1]
        self.assertIn('大きな音 苦手 耳ふさぐ', prompt)
        self.assertIn('心理的なこと', prompt)                                            # 欄の書き方
        self.assertIn('【情報】', prompt)                                                 # ほかの欄を前提に
        self.assertNotIn('架空', prompt)                                                  # 名前は渡さない
        # 支援課題の名前は 1 行だけ
        with mock.patch('ai_assist.quick.ask_ai', return_value=('・見通しを持って過ごす\n・補足', None)):
            res = self.client.post(url, json.dumps({'field': 'task_title_1', 'memo': '見通し'}), content_type='application/json')
        self.assertEqual(res.json()['result'], '見通しを持って過ごす')
        # 空のメモ・整えられない欄
        self.assertEqual(self.client.post(url, json.dumps({'field': 'bio', 'memo': ' '}), content_type='application/json').status_code, 400)
        self.assertEqual(self.client.post(url, json.dumps({'field': 'made_on', 'memo': 'x'}), content_type='application/json').status_code, 400)

    def test_ai_respects_trial_limit_and_off_switch(self):
        sheet = self.create()
        url = reverse('support_plans:behavior_ai', args=[sheet.pk])
        body = json.dumps({'field': 'bio', 'memo': '音 苦手'})
        self.f.trial_ai_limit, self.f.trial_ai_used = 1, 0
        self.f.save()
        with mock.patch('ai_assist.quick.ask_ai', return_value=('・音が苦手', None)):
            self.assertEqual(self.client.post(url, body, content_type='application/json').status_code, 200)
            self.assertEqual(self.client.post(url, body, content_type='application/json').status_code, 403)   # 使い切り
        self.f.refresh_from_db()
        self.f.trial_ai_limit, self.f.plan_no_ai = 0, True
        self.f.save()
        self.assertEqual(self.client.post(url, body, content_type='application/json').status_code, 403)
        self.assertIn('var aiOn = false;', self.client.get(reverse('support_plans:behavior_edit', args=[sheet.pk])).content.decode())

    def test_other_facility_cannot_open(self):
        sheet = self.create()
        other = Facility.objects.create(name='よその事業所')
        user = StaffAccount.objects.create_user('bs_other', password='pass12345', facility=other)
        c = self.client_class()
        c.force_login(user)
        for name in ('behavior_edit', 'behavior_print', 'behavior_copy', 'behavior_delete'):
            res = c.post(reverse(f'support_plans:{name}', args=[sheet.pk])) if name in ('behavior_copy', 'behavior_delete') \
                else c.get(reverse(f'support_plans:{name}', args=[sheet.pk]))
            self.assertEqual(res.status_code, 404, name)
        self.assertEqual(c.post(reverse('support_plans:behavior_create'), {'child': self.b.pk}).status_code, 404)
        self.assertEqual(BehaviorSheet.objects.count(), 1)
