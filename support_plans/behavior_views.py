"""支援計画シート（2 枚で 1 組）の画面：一覧・作る・書く・写す・消す・印刷・欄ごとの AI の整文（support_plans/behavior.py）"""
import json

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from beneficiaries.models import Beneficiary

from . import behavior
from .models import BehaviorSheet


class SheetMixin(LoginRequiredMixin):
    """自施設のシートだけを扱う"""

    def facility(self):
        f = getattr(self.request.user, 'facility', None)
        if f is None:
            raise Http404
        return f

    def get_sheet(self, pk):
        return get_object_or_404(BehaviorSheet.objects.select_related('beneficiary', 'facility'), pk=pk, facility=self.facility())

    def ai_ok(self, facility):
        return bool(settings.ANTHROPIC_API_KEY) and not facility.plan_no_ai


class SheetListView(SheetMixin, View):
    """在籍中の子ごとに、作ったシート（新しい順）と「作る」"""

    def get(self, request):
        facility = self.facility()
        children = list(Beneficiary.objects.filter(facility=facility, status=Beneficiary.STATUS_ACTIVE)
                        .order_by('last_name_kana', 'first_name_kana', 'pk'))
        by_child = {}
        for s in BehaviorSheet.objects.filter(facility=facility).only('pk', 'beneficiary_id', 'made_on', 'planner', 'updated_at'):
            by_child.setdefault(s.beneficiary_id, []).append(s)
        rows = [{'child': c, 'sheets': by_child.get(c.pk, [])} for c in children]
        rows.sort(key=lambda r: (not r['sheets'],))          # シートのある子を上に（名前の順は保つ）
        return render(request, 'support_plans/behavior_list.html', {'rows': rows})


class SheetCreateView(SheetMixin, View):
    def post(self, request):
        facility = self.facility()
        child = get_object_or_404(Beneficiary, pk=request.POST.get('child'), facility=facility)
        sheet = behavior.new_sheet(facility, child, request.user)
        sheet.updated_by = request.user
        sheet.save()
        return redirect('support_plans:behavior_edit', pk=sheet.pk)


class SheetEditView(SheetMixin, View):
    def context(self, sheet):
        tasks, providers, steps = behavior.blank_rows(sheet)
        # 増やせる行は、はじめから隠して置いておく（「増やす」で出す）
        tasks = [{**t, 'shown': True} for t in tasks] + [{'title': '', 'policy': '', 'shown': False}] * (behavior.TASK_MAX - len(tasks))
        steps = [{**t, 'shown': True} for t in steps] + \
            [{'time': '', 'activity': '', 'procedure': '', 'check': '', 'note': '', 'shown': False}] * (behavior.STEP_MAX - len(steps))
        return {'s': sheet, 'b': sheet.beneficiary, 'tasks': tasks, 'providers': providers, 'steps': steps,
                'page1_fields': [(k, label, sub, getattr(sheet, k)) for k, label, sub in behavior.PAGE1_FIELDS],
                'task_max': behavior.TASK_MAX, 'step_max': behavior.STEP_MAX,
                'ai_ok': self.ai_ok(sheet.facility)}

    def get(self, request, pk):
        return render(request, 'support_plans/behavior_edit.html', self.context(self.get_sheet(pk)))

    def post(self, request, pk):
        sheet = self.get_sheet(pk)
        problem = behavior.save_from_post(sheet, request.POST, request.user)
        if problem:
            messages.error(request, problem)
            return render(request, 'support_plans/behavior_edit.html', self.context(sheet), status=400)
        messages.success(request, '支援計画シートを保存しました。')
        if request.POST.get('then') == 'print':
            return redirect('support_plans:behavior_print', pk=sheet.pk)
        return redirect('support_plans:behavior_edit', pk=sheet.pk)


class SheetCopyView(SheetMixin, View):
    """見直すときに写して作る（前のシートは残る）"""

    def post(self, request, pk):
        new = behavior.copy_of(self.get_sheet(pk), request.user)
        messages.success(request, '写して新しいシートを作りました（作成日はきょう。サービス提供日・チェック・様子は空にしています）。')
        return redirect('support_plans:behavior_edit', pk=new.pk)


class SheetDeleteView(SheetMixin, View):
    def post(self, request, pk):
        sheet = self.get_sheet(pk)
        name = sheet.beneficiary.full_name
        sheet.delete()
        messages.success(request, f'{name} さんの支援計画シートを消しました。')
        return redirect('support_plans:behavior_list')


class SheetPrintView(SheetMixin, View):
    """2 枚で 1 組（1 枚目：支援計画シート・2 枚目：支援手順書 兼 記録用紙）"""

    def get(self, request, pk):
        sheet = self.get_sheet(pk)
        providers = list(sheet.providers or [])
        while len(providers) < behavior.PROVIDER_ROWS:
            providers.append({})
        steps = list(sheet.steps or [])
        return render(request, 'support_plans/behavior_print.html', {
            's': sheet, 'b': sheet.beneficiary, 'tasks': sheet.tasks or [], 'providers': providers[:behavior.PROVIDER_ROWS],
            'steps': steps, 'blank_steps': range(max(0, 8 - len(steps))), 'marks': '①②③④⑤⑥⑦⑧',
            'reiwa': sheet.made_on.year - 2018})


class SheetAiView(SheetMixin, View):
    """欄ごとの整文（JSON）：{field, memo, current, context:{見出し: 文}} → {result}。お試しの回数は 1 回分"""

    def post(self, request, pk):
        from ai_assist import trial
        sheet = self.get_sheet(pk)
        if not self.ai_ok(sheet.facility):
            return JsonResponse({'error': 'この事業所では AI で文を作れません（設定が無いか、使わない設定です）。'}, status=403)
        over = trial.check(sheet.facility)
        if over:
            return JsonResponse({'error': over}, status=403)
        try:
            data = json.loads(request.body.decode('utf-8') or '{}')
        except (ValueError, UnicodeDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        field = str(data.get('field', ''))[:40]
        memo = str(data.get('memo', '')).strip()[:2000]
        current = str(data.get('current', '')).strip()[:4000]
        if not behavior.kind_of(field):
            return JsonResponse({'error': 'この欄は AI で整えられません。'}, status=400)
        if not memo:
            return JsonResponse({'error': '欄に、単語やメモを書いてから押してください。'}, status=400)
        ctx = data.get('context') if isinstance(data.get('context'), dict) else {}
        others = {str(k)[:40]: str(v)[:1500] for k, v in list(ctx.items())[:12]}
        text, error = behavior.compose(field, memo, current, behavior.context_text(sheet.beneficiary, others))
        if error is not None:
            return error
        if not text:
            return JsonResponse({'error': 'AI が文を返しませんでした。もう一度お試しください。'}, status=502)
        trial.use(sheet.facility)
        return JsonResponse({'result': text})
