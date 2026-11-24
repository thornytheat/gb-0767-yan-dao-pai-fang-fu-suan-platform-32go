from decimal import Decimal

from app.numbers import display

SPOT_LIMIT = 3


def deviation_rows(reports_rows, tolerance):
    """当前对账中超出容差的小时，按绝对差额降序、小时升序排列。"""
    rows = []
    for row in reports_rows:
        if row['code'] != 'DEVIATION' or row['difference_mg_nm3'] is None:
            continue
        difference = Decimal(row['difference_mg_nm3'])
        if abs(difference) <= tolerance:
            continue
        rows.append({'hour': row['hour'], 'local': row['local'],
                     'mg_nm3': row['mg_nm3'], 'reported_mg_nm3': row['reported_mg_nm3'],
                     'difference_mg_nm3': display(difference),
                     'abs_difference': abs(difference)})
    return sorted(rows, key=lambda item: (-item['abs_difference'], item['hour']))


def fill_candidates(reports_rows, vouchers, tolerance):
    """已领小时照占名额；空位只从本班尚未领过且当前仍超容差的小时中按差额补齐。"""
    claimed = {voucher['hour'] for voucher in vouchers}
    open_slots = SPOT_LIMIT - len(vouchers)
    if open_slots <= 0:
        return []
    pool = [row for row in deviation_rows(reports_rows, tolerance) if row['hour'] not in claimed]
    return pool[:open_slots]


def build_board(reports_rows, vouchers, tolerance):
    """汇总交班看板：已领凭证固定在前，其后是当前空位的待补候选。"""
    by_hour = {row['hour']: row for row in reports_rows}
    claimed = []
    for voucher in vouchers:
        current = by_hour.get(voucher['hour'])
        claimed.append(voucher | {
            'local': current['local'] if current else voucher['local'],
            'current_difference_mg_nm3': None if current is None or current['difference_mg_nm3'] is None
            else display(Decimal(current['difference_mg_nm3'])),
            'still_deviation': current is not None and current['code'] == 'DEVIATION'
            and current['difference_mg_nm3'] is not None
            and abs(Decimal(current['difference_mg_nm3'])) > tolerance})
    candidates = fill_candidates(reports_rows, vouchers, tolerance)
    return {'limit': SPOT_LIMIT, 'claimed': claimed, 'candidates': candidates,
            'open_slots': SPOT_LIMIT - len(vouchers)}
