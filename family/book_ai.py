"""
連絡帳（事業所より）の文を AI で作る（2026-11-25 成の要望）。日誌の 2 つの書き方と同じ考え方：

- メモ書きから文章作成：職員の短いメモ（単語でよい）→ 保護者に送る文
- 評価して文章作成：活動 → AI が観点を 3〜4 個出す → 職員が「はい／いいえ」→ その結果から保護者に送る文

子どもの名前は AI に渡さない。AI には「{名前}」と書かせて、こちらで「〇〇さん」に置き換える。
"""
import json
import logging

from ai_assist.text import JAPANESE_RULES, clean_ai_text

logger = logging.getLogger(__name__)

NAME = '{名前}'
MEMO_MAX = 1000
ACTIVITY_MAX = 100
VIEWPOINT_MAX = 6
ANSWERS = {'yes': 'はい（できていた）', 'no': 'いいえ（できていなかった）'}

BOOK_PROMPT = """あなたは放課後等デイサービスの職員を補助するAIです。保護者に送る「連絡帳（事業所より）」の文を書きます。
- その日の子どもの様子を、温かく、保護者にわかりやすく伝える。120〜200字ほど
- 職員のメモ・確認結果にある事実だけを使う。メモに無い出来事・言葉・表情・回数・時間を作らない
- 子どもの名前は「{名前}」と書く（さん付けは要らない。こちらで「〇〇さん」に置き換える）
- 「いいえ」だった観点は、責める書き方をせず、事業所で次にどう関わるかを 1 文で添える
- 返すのは連絡帳の文だけ。見出し・前置き・あいさつの定型句（いつもお世話になっております など）は付けない

""" + JAPANESE_RULES

VIEWPOINT_PROMPT = """あなたは放課後等デイサービスの職員を補助するAIです。
職員が入れた「活動」で、保護者に様子を伝えるために確かめる観点を 3〜4 個出してください。
- 職員が「はい／いいえ」で答えられる形（「〜できた」「〜した」）。各 20 字以内
- 活動の性質に合った、見てわかる行動にする（参加・やりとり・手先・ルール・気持ちの切り替え など）
必ず次の JSON だけを返す（説明・コードフェンスは不要）：
{"viewpoints": ["観点1", "観点2", "観点3"]}"""


def _with_name(text, child):
    name = f'{child.first_name or child.last_name}さん'
    text = text.replace('{名前}さん', name).replace('｛名前｝', NAME)
    return text.replace(NAME, name)


def clean_viewpoints(raw):
    out = []
    for v in (raw if isinstance(raw, list) else [])[:VIEWPOINT_MAX]:
        if not isinstance(v, dict):
            continue
        text = clean_ai_text(str(v.get('text', '')), keep_newlines=False)[:60]
        if text:
            out.append({'text': text, 'answer': v.get('answer') if v.get('answer') in ANSWERS else None})
    return out


def viewpoints(activity):
    """活動 → 観点の一覧（返り値：(観点, エラーの JsonResponse)）"""
    from ai_assist.quick import ask_ai
    raw, error = ask_ai(VIEWPOINT_PROMPT, f'【活動】\n{activity}', 600)
    if error is not None:
        return [], error
    try:
        start, end = raw.find('{'), raw.rfind('}') + 1
        data = json.loads(raw[start:end], strict=False)
        items = data.get('viewpoints') if isinstance(data, dict) else None
    except (ValueError, AttributeError):
        logger.warning('連絡帳の観点の返答が JSON でない: %r', (raw or '')[:200])
        items = None
    points = [clean_ai_text(str(v), keep_newlines=False)[:40] for v in (items or []) if str(v).strip()]
    return [{'text': p, 'answer': None} for p in points[:VIEWPOINT_MAX] if p], None


def write(child, memo='', activity='', points=None):
    """メモ（と活動・観点の確認結果）→ 連絡帳の文（返り値：(文, エラーの JsonResponse)）"""
    from ai_assist.quick import ask_ai
    parts = []
    if activity:
        parts.append(f'【活動】\n{activity}')
    answered = [p for p in (points or []) if p.get('answer')]
    if answered:
        parts.append('【観点ごとの職員の確認結果】\n' + '\n'.join(f'・{p["text"]} → {ANSWERS[p["answer"]]}' for p in answered))
    if memo:
        parts.append(f'【職員のメモ】\n{memo}')
    raw, error = ask_ai(BOOK_PROMPT, '\n\n'.join(parts), 1000)
    if error is not None:
        return '', error
    return _with_name(clean_ai_text(raw), child), None
