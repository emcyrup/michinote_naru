"""
勤務表（シフト）の基本機能（2026-11。施設設定で「勤務表（シフト）を使う」にした事業所）。

流れ：勤務の種類（早番・日勤・休み…）と、勤務表に載せる人を登録 → 人ごとに「曜日の基本」を決める
      → 月の勤務表で「曜日の基本から入れる」→ 日ごとに直す → 印刷。
- ShiftPattern（勤務の種類）：名前・印（マスに出す 1〜2 文字）・時間・休憩。休みの種類は時間を数えない
- ShiftMember（勤務表に載せる人）：ログインする職員でなくてもよいので別に持つ（職員と結びつけることもできる）
- ShiftWeekly（曜日の基本）：人 × 曜日 → 勤務の種類
- ShiftDay（その日の勤務）：人 × 日 → 勤務の種類。月の勤務表の 1 マス
"""
from django.conf import settings
from django.db import models

from facilities.models import Facility

WEEK_JP = ['月', '火', '水', '木', '金', '土', '日']


class ShiftPattern(models.Model):
    COLORS = [('#dbe9f6', '青'), ('#dff0d8', '緑'), ('#fde9c9', 'だいだい'), ('#f3ddea', '桃'), ('#e6e0f3', '紫'),
              ('#ececec', '灰'), ('#fff7c2', '黄')]
    NAME_MAX = 20
    MARK_MAX = 3

    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='shift_patterns')
    name = models.CharField(max_length=NAME_MAX, verbose_name='名前', help_text='例：早番・日勤・遅番・休み')
    mark = models.CharField(max_length=MARK_MAX, verbose_name='印', help_text='勤務表のマスに出す 1〜2 文字（例：早）')
    start_time = models.TimeField(null=True, blank=True, verbose_name='始まり')
    end_time = models.TimeField(null=True, blank=True, verbose_name='終わり')
    break_minutes = models.PositiveSmallIntegerField(default=60, verbose_name='休憩（分）')
    is_off = models.BooleanField(default=False, verbose_name='休みの種類', help_text='休み・有休など。出勤の日数・時間に数えない')
    color = models.CharField(max_length=7, default='#dbe9f6', verbose_name='色')
    order = models.PositiveSmallIntegerField(default=0, verbose_name='並び順')
    is_active = models.BooleanField(default=True, verbose_name='使う')

    class Meta:
        verbose_name = '勤務の種類'
        verbose_name_plural = '勤務の種類'
        ordering = ['order', 'pk']

    def __str__(self):
        return self.name

    @property
    def minutes(self):
        """働く時間（分）。休み・時間の無いものは 0"""
        if self.is_off or not self.start_time or not self.end_time:
            return 0
        m = (self.end_time.hour * 60 + self.end_time.minute) - (self.start_time.hour * 60 + self.start_time.minute)
        return max(0, m - self.break_minutes)

    @property
    def time_label(self):
        if self.is_off or not self.start_time or not self.end_time:
            return ''
        return f'{self.start_time:%H:%M}〜{self.end_time:%H:%M}'


class ShiftMember(models.Model):
    NAME_MAX = 30

    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='shift_members')
    name = models.CharField(max_length=NAME_MAX, verbose_name='名前')
    job = models.CharField(max_length=30, blank=True, verbose_name='職種', help_text='例：児発管・児童指導員・保育士・看護師・運転手')
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='shift_members', verbose_name='ログインする職員')
    order = models.PositiveSmallIntegerField(default=0, verbose_name='並び順')
    is_active = models.BooleanField(default=True, verbose_name='勤務表に載せる')

    class Meta:
        verbose_name = '勤務表に載せる人'
        verbose_name_plural = '勤務表に載せる人'
        ordering = ['order', 'pk']

    def __str__(self):
        return self.name


class ShiftWeekly(models.Model):
    member = models.ForeignKey(ShiftMember, on_delete=models.CASCADE, related_name='weekly')
    weekday = models.PositiveSmallIntegerField(verbose_name='曜日（0=月 … 6=日）')
    pattern = models.ForeignKey(ShiftPattern, on_delete=models.CASCADE, related_name='+')

    class Meta:
        verbose_name = '曜日の基本'
        verbose_name_plural = '曜日の基本'
        constraints = [models.UniqueConstraint(fields=['member', 'weekday'], name='shift_weekly_once')]


class ShiftDay(models.Model):
    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='shift_days')
    member = models.ForeignKey(ShiftMember, on_delete=models.CASCADE, related_name='days')
    date = models.DateField(verbose_name='日付')
    pattern = models.ForeignKey(ShiftPattern, on_delete=models.CASCADE, related_name='days')
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        verbose_name = 'その日の勤務'
        verbose_name_plural = 'その日の勤務'
        constraints = [models.UniqueConstraint(fields=['member', 'date'], name='shift_day_once')]
        indexes = [models.Index(fields=['facility', 'date'], name='shift_day_facility_date')]
