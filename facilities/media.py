"""
アップロードファイル（写真・紙の日誌・利用希望の用紙・署名・受給者証・資料・ロゴ）の配信。

/media/ をそのまま公開せず、ログイン中の職員の事業所が持つファイルだけを返す。
LINE に写真を送るときのように外部から取りに来る場合は、有効期限つきの署名 URL を使う。
"""
import posixpath

from django.apps import apps
from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core import signing
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404
from django.views import View

# パスの先頭 → (モデル, ファイル項目, 事業所への絞り込み)
OWNERS = [
    ('facility_logos/',         'facilities.Facility',             'logo',            'pk'),
    ('daily_record_photos/',    'records.DailyRecordPhoto',        'photo',           'facility'),
    ('paper_scans/',            'records.PaperScan',               'image',           'facility'),
    ('signatures/',             'esignatures.EsignatureRecord',    'signature_image', 'facility'),
    ('recipient_certificates/', 'beneficiaries.RecipientCertificate', 'scanned_image', 'beneficiary__facility'),
    ('reference_docs/',         'ai_assist.ReferenceDocument',     'file',            'facility'),
    ('request_scans/',          'reservations.RequestScan',        'image',           'facility'),
    ('beneficiary_assessments/', 'beneficiaries.BeneficiaryAssessment', 'file',       'beneficiary__facility'),
    ('beneficiary_documents/',  'beneficiaries.BeneficiaryDocument',  'file',            'beneficiary__facility'),
]
SALT = 'protected-media'
DEFAULT_MAX_AGE = 30 * 60  # 署名 URL の有効期限（秒）
MAX_MAX_AGE = 24 * 3600
# 画面の中でそのまま開いてよい種類（拡張子 → Content-Type）。ここに無いもの（HTML・SVG など）は、開かずに保存させる：
# アップロードした HTML や SVG が同じサイトの中で開くと、開いた職員の画面で勝手な操作ができてしまう（2026-11-22 セキュリティ診断）
INLINE_TYPES = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp',
    '.heic': 'image/heic', '.heif': 'image/heif', '.bmp': 'image/bmp', '.pdf': 'application/pdf',
}


def clean_path(path):
    """`..` や絶対パスを含まない正規化したパスを返す。不正なら None"""
    path = (path or '').replace('\\', '/').lstrip('/')
    norm = posixpath.normpath(path)
    if not path or norm != path or norm.startswith('..') or '/../' in f'/{norm}/':
        return None
    return norm


def is_owned_by(path, facility):
    """そのパスのファイルが、この事業所のものか"""
    if facility is None:
        return False
    for prefix, model_label, field, lookup in OWNERS:
        if path.startswith(prefix):
            model = apps.get_model(model_label)
            return model.objects.filter(**{field: path, lookup: facility.pk}).exists()
    return False


def signed_media_url(path, max_age=DEFAULT_MAX_AGE):
    """有効期限つきの署名 URL（相対）。外部サービス（LINE など）に渡すときに使う"""
    # 期限は署名の中に入れる（アドレスの max_age を書き換えても延ばせないように）
    max_age = max(1, min(int(max_age), MAX_MAX_AGE))
    token = signing.TimestampSigner(salt=SALT).sign(f'{max_age}:{path}')
    return f'{settings.MEDIA_URL}{path}?sig={token}'


def verify_signature(path, token, max_age=None):
    """署名が合っていて、署名の中に入れた期限の内か。アドレスに付いた max_age は見ない"""
    signer = signing.TimestampSigner(salt=SALT)
    try:
        value = signer.unsign(token, max_age=MAX_MAX_AGE)
        age, _, signed_path = value.partition(':')
        if signed_path != path or not age.isdigit():
            return False
        signer.unsign(token, max_age=int(age))
        return True
    except signing.BadSignature:
        return False


class ProtectedMediaView(View):
    def get(self, request, path):
        path = clean_path(path)
        if path is None:
            raise Http404
        token = request.GET.get('sig')
        if token:
            if not verify_signature(path, token, request.GET.get('max_age', DEFAULT_MAX_AGE)):
                raise Http404
        else:
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not is_owned_by(path, getattr(request.user, 'facility', None)):
                raise Http404
        if not default_storage.exists(path):
            raise Http404
        ext = posixpath.splitext(path)[1].lower()
        inline = INLINE_TYPES.get(ext)
        if inline:
            resp = FileResponse(default_storage.open(path, 'rb'), content_type=inline)
        else:
            # 画像・PDF 以外：中身を解釈させず、ファイルとして保存させる
            resp = FileResponse(default_storage.open(path, 'rb'), content_type='application/octet-stream',
                                as_attachment=True, filename=posixpath.basename(path))
        resp['Cache-Control'] = 'private, max-age=0'
        resp['X-Content-Type-Options'] = 'nosniff'
        if ext != '.pdf':
            # 万一ブラウザが中身を開いても、スクリプトを動かさない（PDF は、ブラウザの表示の部品が動かなくなるので付けない）
            resp['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        else:
            resp._csp_config = {}      # 画面用の決まり（settings.SECURE_CSP）も付けない
            resp._csp_ro_config = {}
        return resp
