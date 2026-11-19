"""
保護者ページ（公式 LINE から開く、保護者向けの画面）。2026-11 事業所「成」の要望。

保護者のアカウント（ID・パスワード）は作らない。公式 LINE につないだ保護者（Guardian.line_user_id）ごとに、
開くためのリンク（合い言葉つき）を 1 つ持つ。リンクを開くと、その端末に入場の印（セッション）が付き、
以後は合い言葉なしのアドレス（/family/）で使う。きょうだいは同じ LINE の人として 1 つのリンクでまとめて見られる。
"""
import secrets

from django.db import models
from django.utils import timezone

from facilities.models import Facility


def _new_token():
    # 43 文字（256 ビット）。当て推量では開けない長さ
    return secrets.token_urlsafe(32)


class FamilyAccess(models.Model):
    """保護者（LINE の 1 人）が保護者ページを開くためのリンク"""
    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='family_accesses')
    line_user_id = models.CharField(max_length=100, verbose_name='LINE User ID')
    token = models.CharField(max_length=64, unique=True, default=_new_token, verbose_name='リンクの合い言葉')
    created_at = models.DateTimeField(auto_now_add=True)
    rotated_at = models.DateTimeField(null=True, blank=True, verbose_name='リンクを作り直した日時')
    last_used_at = models.DateTimeField(null=True, blank=True, verbose_name='最後に開いた日時')

    class Meta:
        verbose_name = '保護者ページのリンク'
        verbose_name_plural = '保護者ページのリンク'
        constraints = [models.UniqueConstraint(fields=['facility', 'line_user_id'], name='family_access_one_per_line_user')]

    def __str__(self):
        return f'{self.facility} の保護者ページ（{self.line_user_id[:6]}…）'

    def rotate(self):
        """リンクを作り直す（前のリンクは開けなくなる。開いたままの端末も次の操作で入り直しになる）"""
        self.token = _new_token()
        self.rotated_at = timezone.now()
        self.save(update_fields=['token', 'rotated_at'])
        return self
