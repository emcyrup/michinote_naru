"""
支援計画シート（2 枚で 1 組。models.BehaviorSheet）の欄・保存・AI の整文。2026-11-24 成の要望。

- 様式は厚生労働省の例（支援計画シート／支援手順書 兼 記録用紙）の並び。成のいまの様式を受け取ったら合わせ直す
- AI：欄に単語やメモ程度を書いて「文にする」を押すと、その欄の書き方（「・」の行・「→」でつなぐ手順など）に整える。
  メモに無い事実は足さない。ほかの欄に書いてあることは、食い違わないための前提として渡す（利用者の名前は渡さない）
"""
import datetime

from ai_assist.text import clean_ai_text

TASK_MAX = 8
STEP_MAX = 16
PROVIDER_ROWS = 3
NEW_TASK_ROWS = 4
NEW_STEP_ROWS = 8
TEXT_MAX = 4000
CELL_MAX = 1000

# 1 枚目の文の欄：(キー, 見出し, 小さな説明)
PAGE1_FIELDS = [
    ('intake', '情報', '見たこと、聴いたこと、資料などから'),
    ('bio', '生物的なこと', '疾患や障害、気質など'),
    ('psycho', '心理的なこと', '不安、葛藤、希望、感情など'),
    ('social', '社会的なこと', '家庭、施設・学校、地域資源など'),
]

# AI に渡す、欄ごとの書き方
HINTS = {
    'intake': 'インテーク（情報の収集・整理）の「情報」欄。メモにある事実を「・」で始まる短い行に整える。'
              '年齢・障害・身体のこと・行動の様子・できること・苦手なこと・家庭の状況などの事実。評価や推測は書かない',
    'bio': 'アセスメントの「生物的なこと（疾患や障害、気質など）」。「・」で始まる短い行で、障害特性や体のこと・気質からの理解や仮説を書く',
    'psycho': 'アセスメントの「心理的なこと（不安、葛藤、希望、感情など）」。「・」で始まる短い行で、本人の気持ちや混乱しやすい場面の理解や仮説を書く',
    'social': 'アセスメントの「社会的なこと（家庭、施設・学校、地域資源など）」。「・」で始まる短い行で、家庭・学校・地域との関係の理解や仮説を書く',
    'task_title': '支援課題の名前。8〜16 字ほどの短い言い方にする（例：見通しを持って活動に参加する／パニック時の安全確保）。1 行だけ',
    'task_policy': 'その支援課題への「対応・方針（やろうと思うこと）」。「・」で始まる短い行で、具体的な支援の方法（環境・伝え方・道具・連携）を書く',
    'activity': '支援手順書の「活動」欄。2〜6 字の短い名前だけ（例：来所・個別課題・おやつ・帰り）',
    'procedure': '支援手順書の「サービス手順」欄。場所やすること（時間）を「→」でつなぐ。先頭に【スケジュール1：名前】の形の見出しを付けてもよい。'
                 '例：【スケジュール1：朝の準備】静養室（スケジュール）→静養室（着替え）→アラーム（9:50）→作業室',
    'note': '支援手順書の「様子」欄。その時間の本人の様子を、見たままに 1〜2 文で',
    'contact_note': '支援手順書の「連絡事項」。支援者みんなで守ることを「・」で始まる短い行に整える',
    'inquiry_note': '支援手順書の「問い合わせ事項」。確かめたいこと・たずねたいことを「・」で始まる短い行に整える',
}

COMPOSE_PROMPT = """あなたは放課後等デイサービス・児童発達支援の職員を補助するAIです。
強度行動障害のある子どもの「支援計画シート」「支援手順書 兼 記録用紙」の欄に、職員が単語や短いメモを書きました。
それを、その欄に載せる文に整えてください。
- メモに書かれていることだけを使う。メモに無い事実・診断・家庭の事情・数字を足さない
- 前提（ほかの欄に書いてあること）と食い違わないようにする。前提の内容を繰り返して埋めない
- 欄ごとの書き方に合わせる。様式に合わせて「・」で始まる行や「→」でつなぐ書き方を使ってよい
- 「・」の行は、1 行 40 字程度までの言い切り（です・ます調にしなくてよい。例：・大きな音が苦手）
- いまの欄の文があり、メモが「直し方」の指示（短く・やさしく・具体的に など）なら、いまの文をその指示で直す
- 常用漢字とひらがな・カタカナを使う。英語・絵文字・マークダウン（** # ` -）は使わない
- 返すのはその欄に入れる文だけ。見出し・前置き・説明は付けない"""


def _s(value, limit):
    return str(value or '').replace('\r\n', '\n').strip()[:limit]


def blank_rows(sheet):
    """画面に出す行：課題・事業所・手順（足りない分は空の行で埋める）"""
    tasks = list(sheet.tasks or [])
    while len(tasks) < NEW_TASK_ROWS:
        tasks.append({'title': '', 'policy': ''})
    providers = list(sheet.providers or [])[:PROVIDER_ROWS]
    while len(providers) < PROVIDER_ROWS:
        providers.append({'office': '', 'service': '', 'time': '', 'staff': ''})
    steps = list(sheet.steps or [])
    while len(steps) < NEW_STEP_ROWS:
        steps.append({'time': '', 'activity': '', 'procedure': '', 'check': '', 'note': ''})
    return tasks[:TASK_MAX], providers, steps[:STEP_MAX]


def new_sheet(facility, child, staff):
    """新しいシート（保存前）。事業所①に、この事業所の名前を入れておく"""
    from .models import BehaviorSheet
    name = ''
    if staff is not None:
        name = (staff.get_full_name() or '').strip()
    return BehaviorSheet(facility=facility, beneficiary=child, made_on=datetime.date.today(), planner=name,
                         providers=[{'office': facility.name, 'service': '放課後等デイサービス', 'time': '', 'staff': ''}])


def copy_of(sheet, staff):
    """見直しのために写す（作成日はきょう。サービス提供日・チェック・様子は空にする）"""
    from .models import BehaviorSheet
    steps = [{**s, 'check': '', 'note': ''} for s in (sheet.steps or [])]
    return BehaviorSheet.objects.create(
        facility=sheet.facility, beneficiary=sheet.beneficiary, made_on=datetime.date.today(), planner=sheet.planner,
        intake=sheet.intake, bio=sheet.bio, psycho=sheet.psycho, social=sheet.social, tasks=list(sheet.tasks or []),
        providers=list(sheet.providers or []), steps=steps, contact_note=sheet.contact_note, inquiry_note=sheet.inquiry_note,
        updated_by=staff)


def save_from_post(sheet, data, staff):
    """画面の入力を入れて保存する。読めない日付は（問題の文）を返して保存しない"""
    def day(name, required=False):
        raw = (data.get(name) or '').strip()
        if not raw:
            return (None, '作成日を入れてください。') if required else (None, '')
        try:
            return datetime.date.fromisoformat(raw), ''
        except ValueError:
            return None, '日付が読めません。'
    made_on, problem = day('made_on', required=True)
    if problem:
        return problem
    service_date, problem = day('service_date')
    if problem:
        return problem
    sheet.made_on, sheet.service_date = made_on, service_date
    sheet.planner = _s(data.get('planner'), 60)
    for key, _label, _sub in PAGE1_FIELDS:
        setattr(sheet, key, _s(data.get(key), TEXT_MAX))
    sheet.tasks = [t for t in (
        {'title': _s(data.get(f'task_title_{i}'), 200), 'policy': _s(data.get(f'task_policy_{i}'), CELL_MAX)}
        for i in range(TASK_MAX)) if t['title'] or t['policy']]
    sheet.providers = [{'office': _s(data.get(f'prov_office_{i}'), 80), 'service': _s(data.get(f'prov_service_{i}'), 60),
                        'time': _s(data.get(f'prov_time_{i}'), 30), 'staff': _s(data.get(f'prov_staff_{i}'), 60)}
                       for i in range(PROVIDER_ROWS)]
    sheet.steps = [s for s in (
        {'time': _s(data.get(f'step_time_{i}'), 30), 'activity': _s(data.get(f'step_activity_{i}'), 40),
         'procedure': _s(data.get(f'step_procedure_{i}'), CELL_MAX), 'check': '1' if data.get(f'step_check_{i}') else '',
         'note': _s(data.get(f'step_note_{i}'), CELL_MAX)}
        for i in range(STEP_MAX)) if any(s.values())]
    sheet.contact_note = _s(data.get('contact_note'), TEXT_MAX)
    sheet.inquiry_note = _s(data.get('inquiry_note'), TEXT_MAX)
    sheet.updated_by = staff
    sheet.save()
    return ''


def kind_of(field):
    """画面の欄の名前（task_policy_2 など）→ 書き方のキー（task_policy）"""
    for key in HINTS:
        if field == key or field.startswith(key + '_') or field.startswith('step_' + key + '_'):
            return key
    return ''


def context_text(child, others):
    """前提として渡す文（名前は渡さない）。others：{見出し: 文}"""
    lines = []
    if child.date_of_birth:
        today = datetime.date.today()
        age = today.year - child.date_of_birth.year - ((today.month, today.day) < (child.date_of_birth.month, child.date_of_birth.day))
        if 0 < age < 30:
            lines.append(f'【本人】{age} 歳')
    for label, text in others.items():
        text = (text or '').strip()
        if text:
            lines.append(f'【{label}】\n{text[:1500]}')
    return '\n'.join(lines) or '（前提はまだありません）'


def compose(field, memo, current, context):
    """1 つの欄を整える。返り値（文, エラーの JsonResponse）"""
    from ai_assist.quick import ask_ai
    kind = kind_of(field)
    content = (f'{context}\n\n【欄の書き方】{HINTS.get(kind, "その欄に載せる文として")}\n'
               + (f'【いまの欄の文】\n{current}\n' if current and current != memo else '')
               + f'【メモ】\n{memo}')
    raw, error = ask_ai(COMPOSE_PROMPT, content, 1200)
    if error is not None:
        return '', error
    text = clean_ai_text(raw)
    if kind in ('task_title', 'activity'):
        text = text.splitlines()[0].strip('・ 　') if text else ''
    return text, None
