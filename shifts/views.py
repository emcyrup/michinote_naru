"""勤務表（シフト）の画面：月の勤務表・印刷・設定（勤務の種類／載せる人／曜日の基本）"""
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from accounts.models import StaffAccount

from . import services
from .models import ShiftDay, ShiftMember, ShiftPattern, ShiftWeekly


def can_edit(user):
    """勤務表を作れる人：管理者・児発管（と、設定を変えられる人）。ほかの職員は見るだけ"""
    return user.can_manage_settings or user.role == StaffAccount.ROLE_CHILD_DEV_MANAGER


class ShiftMixin(LoginRequiredMixin):
    """施設設定で「勤務表（シフト）」を使わない事業所はホームへ戻す"""
    need_edit = False

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            facility = getattr(request.user, 'facility', None)
            if facility is None or not facility.use_shift:
                messages.info(request, '勤務表（シフト）は、この事業所では使わない設定です（施設設定 → 使う機能 で変更できます）。')
                return redirect('facilities:dashboard')
            self.facility = facility
            self.editable = can_edit(request.user)
            if (self.need_edit or request.method == 'POST') and not self.editable:
                messages.error(request, '勤務表を作る・直すのは、管理者と児発管だけです。')
                return redirect('shifts:month')
        return super().dispatch(request, *args, **kwargs)


class MonthView(ShiftMixin, View):
    """月の勤務表：人 × 日のマスに勤務の種類を入れる。「曜日の基本から入れる」でまとめて入れてから、日ごとに直す"""

    def get(self, request):
        year, month = services.parse_ym(request.GET.get('ym'))
        table = services.month_table(self.facility, year, month)
        return render(request, 'shifts/month.html', {
            **table, 'editable': self.editable,
            'needs_setup': not table['all_patterns'] or not ShiftMember.objects.filter(facility=self.facility, is_active=True).exists(),
            'has_weekly': ShiftWeekly.objects.filter(member__facility=self.facility).exists(),
        })

    def post(self, request):
        year, month = services.parse_ym(request.POST.get('ym'))
        back = f'{reverse("shifts:month")}?ym={year}-{month:02d}'
        action = request.POST.get('action')
        if action == 'apply':
            # 画面で直したぶんを先に保存してから、空いているマスに曜日の基本を入れる
            services.save_month(self.facility, year, month, request.POST, request.user)
            n = services.apply_weekly(self.facility, year, month, request.user, overwrite=request.POST.get('overwrite') == '1')
            messages.success(request, f'曜日の基本から {n} マス入れました。' if n else '入れるマスはありませんでした（もう入っているか、曜日の基本がまだありません）。')
        elif action == 'clear':
            n = services.clear_month(self.facility, year, month)
            messages.success(request, f'{year}年{month}月の勤務表を空にしました（{n} マス）。')
        else:
            n = services.save_month(self.facility, year, month, request.POST, request.user)
            messages.success(request, f'{n} マスを保存しました。' if n else '変わったところはありません。')
        return redirect(back)


class PrintView(ShiftMixin, View):
    """月の勤務表の印刷（A4 横）"""

    def get(self, request):
        year, month = services.parse_ym(request.GET.get('ym'))
        return render(request, 'shifts/print.html', {**services.month_table(self.facility, year, month), 'facility': self.facility})


class SetupView(ShiftMixin, View):
    """勤務表の設定：勤務の種類・勤務表に載せる人・曜日の基本"""
    need_edit = True

    def get(self, request):
        return render(request, 'shifts/setup.html', {
            'patterns': ShiftPattern.objects.filter(facility=self.facility),
            'active_patterns': ShiftPattern.objects.filter(facility=self.facility, is_active=True),
            'members': ShiftMember.objects.filter(facility=self.facility).select_related('staff'),
            'weekly': services.weekly_table(self.facility), 'colors': ShiftPattern.COLORS,
            'name_max': ShiftPattern.NAME_MAX, 'mark_max': ShiftPattern.MARK_MAX, 'member_max': ShiftMember.NAME_MAX,
        })

    def post(self, request):
        p = request.POST
        action = p.get('action', '')
        back = redirect(reverse('shifts:setup') + (f'#{p.get("tab")}' if p.get('tab') in ('patterns', 'members', 'weekly') else ''))
        if action == 'defaults':
            n = services.add_defaults(self.facility)
            messages.success(request, f'よく使う勤務を {n} 種類入れました。時間は事業所に合わせて直してください。' if n else '勤務の種類は、もう登録されています。')
        elif action == 'import_staff':
            n = services.import_staff(self.facility)
            messages.success(request, f'ログインする職員から {n} 人を足しました。' if n else '足す人はいませんでした（もう載っています）。')
        elif action in ('pattern_add', 'pattern_save'):
            obj = get_object_or_404(ShiftPattern, pk=p.get('pk') or 0, facility=self.facility) if action == 'pattern_save' \
                else ShiftPattern(facility=self.facility, order=ShiftPattern.objects.filter(facility=self.facility).count())
            name, mark = (p.get('name') or '').strip()[:ShiftPattern.NAME_MAX], (p.get('mark') or '').strip()[:ShiftPattern.MARK_MAX]
            start, end = services.parse_hm(p.get('start_time')), services.parse_hm(p.get('end_time'))
            is_off = 'is_off' in p
            if not name or not mark:
                messages.error(request, '勤務の種類の名前と印を入れてください。')
            elif not is_off and (not start or not end or end <= start):
                messages.error(request, f'「{name}」の時間を入れてください（終わりは始まりより後）。休みの種類なら「休み」に印を付けます。')
            else:
                try:
                    brk = max(0, min(600, int(p.get('break_minutes') or 0)))
                except ValueError:
                    brk = 0
                obj.name, obj.mark, obj.is_off = name, mark, is_off
                obj.start_time, obj.end_time = (None, None) if is_off else (start, end)
                obj.break_minutes = 0 if is_off else brk
                obj.color = p.get('color') if p.get('color') in dict(ShiftPattern.COLORS) else obj.color
                obj.is_active = True if action == 'pattern_add' else 'is_active' in p
                obj.save()
                messages.success(request, f'勤務の種類「{name}」を保存しました。')
        elif action == 'pattern_delete':
            obj = get_object_or_404(ShiftPattern, pk=p.get('pk') or 0, facility=self.facility)
            if ShiftDay.objects.filter(pattern=obj).exists():
                obj.is_active = False
                obj.save(update_fields=['is_active'])
                messages.info(request, f'「{obj.name}」は勤務表で使われているので、消さずに「使わない」にしました（これまでの勤務表には残ります）。')
            else:
                try:
                    name = obj.name
                    obj.delete()
                    messages.success(request, f'勤務の種類「{name}」を消しました。')
                except ProtectedError:
                    messages.error(request, 'この勤務の種類は使われているので消せません。')
        elif action in ('member_add', 'member_save'):
            obj = get_object_or_404(ShiftMember, pk=p.get('pk') or 0, facility=self.facility) if action == 'member_save' \
                else ShiftMember(facility=self.facility, order=ShiftMember.objects.filter(facility=self.facility).count())
            name = (p.get('name') or '').strip()[:ShiftMember.NAME_MAX]
            if not name:
                messages.error(request, '名前を入れてください。')
            else:
                obj.name, obj.job = name, (p.get('job') or '').strip()[:30]
                if action == 'member_save':
                    obj.is_active = 'is_active' in p
                    try:
                        obj.order = max(0, min(999, int(p.get('order') or 0)))
                    except ValueError:
                        pass
                obj.save()
                messages.success(request, f'「{name}」を保存しました。')
        elif action == 'member_delete':
            obj = get_object_or_404(ShiftMember, pk=p.get('pk') or 0, facility=self.facility)
            if ShiftDay.objects.filter(member=obj).exists():
                obj.is_active = False
                obj.save(update_fields=['is_active'])
                messages.info(request, f'「{obj.name}」は勤務表に入っているので、消さずに「載せない」にしました（これまでの勤務表には残ります）。')
            else:
                name = obj.name
                obj.delete()
                messages.success(request, f'「{name}」を消しました。')
        elif action == 'weekly_save':
            n = services.save_weekly(self.facility, p)
            messages.success(request, f'曜日の基本を保存しました（{n} か所）。' if n else '変わったところはありません。')
        return back
