"""
実績記録票の印刷（放課後等デイサービス提供実績記録票・様式５。2026-11-26 成の様式に合わせる）。

1 枚に 24 日。来所した日と、欠席時対応加算の記録がある欠席の日を並べる。
- サービス提供の状況：来所は空欄・欠席時対応加算「該当」は「欠席」・「該当しない」は「−」
- 提供形態：土日祝は 2（休業日）、ほかは 1（授業終了後）。請求の時間区分が 3（学校休業日だけの区分）の日も 2
- 開始・終了時間：実績の時間（無ければ日誌の入室・退室時間）
- 送迎加算：来所予定の迎え・送り
- 家族支援・延長支援・集中的支援・専門的支援：請求の画面でその日に付けた加算（延長は区分 1〜3）
- 算定時間数・保護者等確認欄・備考は空欄（手で書く）
"""
import calendar
import datetime

from config.jp_holidays import is_holiday

from .daylist import WEEK_JP
from .models import ScheduledVisit

ROWS_PER_PAGE = 24
ADDON_COLUMNS = [('family', '家族支援加算'), ('extend', '延長支援加算'), ('intensive', '集中的支援加算'), ('special', '専門的支援実施加算')]


def _addon_key(name):
    if name.startswith('家族支援加算'):
        return 'family', '1'
    if name.startswith('延長支援加算'):
        level = '3' if '2時間以上' in name else '2' if '1時間以上' in name else '1'
        return 'extend', level
    if name.startswith('集中的支援加算'):
        return 'intensive', '1'
    if name.startswith('専門的支援実施加算'):
        return 'special', '1'
    return None, None


def _billing(facility, child, first, last):
    """{日: (時間区分, {列: 値})}（請求の画面で付けたもの。無ければ空）"""
    try:
        from billing.models import BillingMatrixAddon, BillingMatrixEntry
    except ImportError:          # pragma: no cover
        return {}
    out = {e.date: [e.time_kubun, {}] for e in BillingMatrixEntry.objects.filter(
        facility=facility, beneficiary=child, date__range=(first, last))}
    for a in BillingMatrixAddon.objects.filter(entry__facility=facility, entry__beneficiary=child,
                                               entry__date__range=(first, last), is_applied=True).select_related('addon', 'entry'):
        key, value = _addon_key(a.addon.name)
        if key:
            out[a.entry.date][1][key] = value
    return out


def digits(text, n=10):
    """番号を 1 けたずつのマスに（足りない分は空）"""
    chars = [c for c in str(text or '') if not c.isspace()][:n]
    return chars + [''] * (n - len(chars))


def sheet(facility, child, year, month):
    """1 人 1 か月ぶんの印刷：{'pages': [[行…]…], 'totals': {…}, 見出しの項目…}"""
    from . import jisseki
    first = datetime.date(year, month, 1)
    last = datetime.date(year, month, calendar.monthrange(year, month)[1])
    data = jisseki.month_sheet(facility, child, year, month)
    bill = _billing(facility, child, first, last)
    rows = []
    for r in data['rows']:
        v = r['visit']
        if r['absent']:
            if not r.get('addon'):
                continue                                # 欠席時対応加算を選んでいない欠席は載せない
            state = '欠席' if r['addon'] == 'yes' else '−'
        elif v.status == ScheduledVisit.STATUS_SCHEDULED and not (r['start'] or r['end']):
            continue                                    # まだ来ていない予定の日は載せない
        else:
            state = ''
        kubun, addons = bill.get(v.date, (None, {}))
        holiday = v.date.weekday() >= 5 or is_holiday(v.date) or kubun == 3
        attended = not r['absent']
        rows.append({
            'date': v.date, 'weekday': WEEK_JP[v.date.weekday()], 'state': state,
            'form': ('2' if holiday else '1') if attended else '',
            'start': r['start'] if attended else None, 'end': r['end'] if attended else None,
            'pickup': '1' if attended and v.has_pickup else '', 'dropoff': '1' if attended and v.has_dropoff else '',
            **{key: (addons.get(key, '') if attended or key == 'family' else '') for key, _ in ADDON_COLUMNS},
        })
    totals = {k: sum(1 for r in rows if r[k]) for k in ('pickup', 'dropoff', *[k for k, _ in ADDON_COLUMNS])}
    pages = [rows[i:i + ROWS_PER_PAGE] for i in range(0, len(rows), ROWS_PER_PAGE)] or [[]]
    pages = [{'rows': p, 'blank': range(ROWS_PER_PAGE - len(p)), 'no': i + 1} for i, p in enumerate(pages)]
    cert = child.latest_certificate
    guardian = child.guardians.order_by('pk').first() if hasattr(child, 'guardians') else None
    return {
        'child': child, 'year': year, 'month': month, 'reiwa': year - 2018, 'pages': pages, 'page_count': len(pages),
        'totals': totals, 'cert_digits': digits(cert.certificate_number if cert else ''),
        'granted_days': cert.granted_days if cert and cert.granted_days else '',
        'office_digits': digits(getattr(facility, 'office_number', '')),
        'guardian_name': f'{guardian.last_name} {guardian.first_name}'.strip() if guardian else '',
    }
