"""
保護者ページ（公式 LINE から開く、保護者向けの画面）。2026-11 事業所「成」の要望。

保護者のアカウント（ID・パスワード）は作らない。公式 LINE につないだ保護者（Guardian.line_user_id）ごとに、
開くためのリンク（合い言葉つき）を 1 つ持つ。リンクを開くと、その端末に入場の印（セッション）が付き、
以後は合い言葉なしのアドレス（/family/）で使う。きょうだいは同じ LINE の人として 1 つのリンクでまとめて見られる。
"""
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

from beneficiaries.models import Beneficiary
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


class FamilyMessage(models.Model):
    """
    保護者からの連絡（欠席連絡・その他の連絡）。1 件が「1 人の子の 1 日ぶん」。
    保護者が複数の日を選んで送ったときは、日ごとに 1 件ずつ作る（職員は日ごとの画面で確認する）。

    職員の画面「保護者からの連絡」での出し方：
      - 連絡の対象日（target_date）のページに赤い字で出す
      - 先の日付の連絡は、まず届いた日（sent_on）のページに黒い字で出し、職員が「確認」を押すと対象日のページへ移る
      - 欠席連絡は、確認したときにその日の来所予定を「欠席」にする（日ごとの一覧で下にグレーで出る）
    """
    KIND_ABSENCE = 'absence'
    KIND_OTHER = 'other'
    KIND_CHOICES = [(KIND_ABSENCE, '欠席連絡'), (KIND_OTHER, 'その他の連絡')]
    REASON_SICK = 'sick'
    REASON_PERSONAL = 'personal'
    REASON_CHOICES = [(REASON_SICK, '病欠'), (REASON_PERSONAL, '都合欠')]
    VIA_PAGE = 'page'
    VIA_LINE = 'line'
    VIA_CHOICES = [(VIA_PAGE, '保護者ページ'), (VIA_LINE, 'LINE のトーク')]
    BODY_MAX = 1000

    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='family_messages')
    # LINE のトークに直接書かれた連絡は、どの子のことか分からないので空のことがある（きょうだいがいる家庭）
    beneficiary = models.ForeignKey(Beneficiary, on_delete=models.CASCADE, null=True, blank=True,
                                    related_name='family_messages', verbose_name='利用者')
    line_user_id = models.CharField(max_length=100, blank=True, verbose_name='送った人の LINE User ID')
    sender_name = models.CharField(max_length=120, blank=True, verbose_name='送った保護者')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, verbose_name='種類')
    via = models.CharField(max_length=10, choices=VIA_CHOICES, default=VIA_PAGE, verbose_name='届き方')
    target_date = models.DateField(verbose_name='連絡の対象日')
    reason = models.CharField(max_length=10, choices=REASON_CHOICES, blank=True, verbose_name='欠席の理由')
    body = models.TextField(blank=True, verbose_name='内容')
    sent_on = models.DateField(verbose_name='届いた日')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='届いた日時')
    confirmed_at = models.DateTimeField(null=True, blank=True, verbose_name='職員が確認した日時')
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='+', verbose_name='確認した職員')
    # 欠席連絡を確認して来所予定を「欠席」に変えたとき、変える前の状態（確認を取り消すときに戻す）。変えていなければ空
    visit_prev_status = models.CharField(max_length=20, blank=True, verbose_name='確認の前の来所予定の状態')

    class Meta:
        verbose_name = '保護者からの連絡'
        verbose_name_plural = '保護者からの連絡'
        ordering = ['created_at', 'pk']
        indexes = [
            models.Index(fields=['facility', 'target_date'], name='family_msg_target'),
            models.Index(fields=['facility', 'sent_on'], name='family_msg_sent'),
            models.Index(fields=['facility', 'confirmed_at'], name='family_msg_confirmed'),
        ]

    def __str__(self):
        return f'{self.target_date} {self.get_kind_display()}（{self.beneficiary or self.sender_name}）'

    @property
    def is_confirmed(self):
        return self.confirmed_at is not None

    @property
    def is_absence(self):
        return self.kind == self.KIND_ABSENCE

    @property
    def is_future_when_sent(self):
        """届いた日より先の日付の連絡か"""
        return self.target_date > self.sent_on

    @property
    def title(self):
        """一覧に出す短い見出し（欠席：病欠／その他の連絡）"""
        if self.is_absence:
            return f'欠席（{self.get_reason_display()}）' if self.reason else '欠席'
        return 'LINE のトークから' if self.via == self.VIA_LINE else 'その他の連絡'


class ContactBook(models.Model):
    """
    連絡帳（事業所より）。1 件が「1 人の子の 1 日ぶん」。
    職員が書いて「送信」すると保護者ページの「連絡帳（事業所名）より」に出る。下書きのあいだは保護者には見えない。
    保護者がその日の連絡帳を開くと read_at が入り、職員の一覧に「確認済み」と出る。
    日誌（records.DailyRecord）とは別のもの（日誌は事業所の中の記録。連絡帳は保護者に見せる文）。
    """
    STATUS_DRAFT = 'draft'
    STATUS_SENT = 'sent'
    STATUS_CHOICES = [(STATUS_DRAFT, '下書き'), (STATUS_SENT, '送信済み')]
    BODY_MAX = 2000

    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='contact_books')
    beneficiary = models.ForeignKey(Beneficiary, on_delete=models.CASCADE, related_name='contact_books', verbose_name='利用者')
    date = models.DateField(verbose_name='日付')
    body = models.TextField(blank=True, verbose_name='内容')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_DRAFT, verbose_name='状態')
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name='送信した日時')
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='+', verbose_name='送信した職員')
    read_at = models.DateTimeField(null=True, blank=True, verbose_name='保護者が確認した日時')
    read_by = models.CharField(max_length=120, blank=True, verbose_name='確認した保護者')
    line_notified = models.BooleanField(default=False, verbose_name='LINE で知らせた')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name='最後に書いた職員')

    class Meta:
        verbose_name = '連絡帳（事業所より）'
        verbose_name_plural = '連絡帳（事業所より）'
        constraints = [models.UniqueConstraint(fields=['beneficiary', 'date'], name='contact_book_one_per_day')]
        indexes = [models.Index(fields=['facility', 'date'], name='contact_book_day')]

    def __str__(self):
        return f'{self.date} {self.beneficiary}（{self.get_status_display()}）'

    @property
    def is_sent(self):
        return self.status == self.STATUS_SENT

    @property
    def is_read(self):
        return self.read_at is not None


class ParentNote(models.Model):
    """
    連絡帳（保護者より）。保護者が、その日の子どもの様子・伝えたいことを書いて送る（1 日に何回送ってもよい）。
    職員は、連絡帳を書く画面でその日のぶんを読む。確認の返事が要る連絡（欠席・お迎えの変更など）は FamilyMessage のほう。
    """
    BODY_MAX = 1000

    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='parent_notes')
    beneficiary = models.ForeignKey(Beneficiary, on_delete=models.CASCADE, related_name='parent_notes', verbose_name='利用者')
    date = models.DateField(verbose_name='日付')
    body = models.TextField(verbose_name='内容')
    line_user_id = models.CharField(max_length=100, blank=True, verbose_name='送った人の LINE User ID')
    sender_name = models.CharField(max_length=120, blank=True, verbose_name='送った保護者')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='送った日時')

    class Meta:
        verbose_name = '連絡帳（保護者より）'
        verbose_name_plural = '連絡帳（保護者より）'
        ordering = ['created_at', 'pk']
        indexes = [models.Index(fields=['facility', 'date'], name='parent_note_day'),
                   models.Index(fields=['beneficiary', 'date'], name='parent_note_child_day')]

    def __str__(self):
        return f'{self.date} {self.beneficiary}（保護者より）'


class DayEvent(models.Model):
    """
    予定のカレンダーの 1 日ぶん（休業日・活動の予定・お出かけ先）。職員が入れ、保護者ページにも同じものが出る。
    title は日付の下に出す短い文。detail は日付を押したときに出す詳しい内容（持ち物・参加費など）。
    """
    TITLE_MAX = 40
    DETAIL_MAX = 1000

    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='day_events')
    date = models.DateField(verbose_name='日付')
    title = models.CharField(max_length=TITLE_MAX, blank=True, verbose_name='予定（日付の下に出す）')
    is_closed = models.BooleanField(default=False, verbose_name='休業日')
    detail = models.TextField(blank=True, verbose_name='詳しい内容（持ち物・参加費など）')
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name='最後に書いた職員')

    class Meta:
        verbose_name = '予定（カレンダー）'
        verbose_name_plural = '予定（カレンダー）'
        ordering = ['date']
        constraints = [models.UniqueConstraint(fields=['facility', 'date'], name='day_event_one_per_day')]

    def __str__(self):
        return f'{self.date} {"休業日 " if self.is_closed else ""}{self.title}'

    @property
    def label(self):
        """日付の下に出す文（休業日で予定の文が無ければ「休業日」）"""
        return self.title or ('休業日' if self.is_closed else '')


class UseRequest(models.Model):
    """
    利用日申請。保護者が保護者ページのカレンダーから送る。職員が代わりに入れることもある（口頭・紙で受けたとき）。
    申請した日は、そのまま来所予定（schedules.ScheduledVisit）になり、実績記録票にも出る。これはその控え（だれが・いつ）。
    """
    VIA_PARENT = 'parent'
    VIA_STAFF = 'staff'
    VIA_CHOICES = [(VIA_PARENT, '保護者ページ'), (VIA_STAFF, '職員が代わりに')]

    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='use_requests')
    beneficiary = models.ForeignKey(Beneficiary, on_delete=models.CASCADE, related_name='use_requests', verbose_name='利用者')
    date = models.DateField(verbose_name='利用する日')
    via = models.CharField(max_length=10, choices=VIA_CHOICES, default=VIA_PARENT, verbose_name='申請のしかた')
    line_user_id = models.CharField(max_length=100, blank=True, verbose_name='送った人の LINE User ID')
    sender_name = models.CharField(max_length=120, blank=True, verbose_name='申請した保護者')
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='+', verbose_name='代わりに入れた職員')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='申請した日時')
    seen_at = models.DateTimeField(null=True, blank=True, verbose_name='職員が確認した日時')
    seen_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='+', verbose_name='確認した職員')

    class Meta:
        verbose_name = '利用日申請'
        verbose_name_plural = '利用日申請'
        ordering = ['date', 'pk']
        indexes = [models.Index(fields=['facility', 'seen_at'], name='use_request_unseen'),
                   models.Index(fields=['beneficiary', 'date'], name='use_request_child_day')]

    def __str__(self):
        return f'{self.date} {self.beneficiary}（{self.get_via_display()}）'
