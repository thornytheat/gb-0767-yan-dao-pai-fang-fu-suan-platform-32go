from decimal import Decimal

from app.monitor.spotcheck import SPOT_LIMIT, build_board, deviation_rows, fill_candidates

TOLERANCE = Decimal('0.50')


def row(hour, diff, code='DEVIATION', local=None):
    return {'hour': hour, 'local': local or hour[11:16], 'mg_nm3': '30.00',
            'reported_mg_nm3': '29.00', 'difference_mg_nm3': diff, 'code': code}


def test_deviation_rows_filter_and_order():
    rows = [row('2026-01-01T00:00:00+00:00', '0.75'),
            row('2026-01-01T01:00:00+00:00', '1.20'),
            row('2026-01-01T02:00:00+00:00', '0.50', code='MATCH'),
            row('2026-01-01T03:00:00+00:00', None, code='NO_REFERENCE')]
    picked = deviation_rows(rows, TOLERANCE)
    assert [item['hour'] for item in picked] == ['2026-01-01T01:00:00+00:00', '2026-01-01T00:00:00+00:00']
    assert picked[0]['abs_difference'] == Decimal('1.20')


def test_equal_tolerance_is_not_deviation():
    rows = [row('2026-01-01T00:00:00+00:00', '0.50')]
    assert deviation_rows(rows, TOLERANCE) == []


def test_fill_keeps_claimed_slot_and_picks_largest_open():
    rows = [row('2026-01-01T00:00:00+00:00', '0.75'),
            row('2026-01-01T01:00:00+00:00', '2.00'),
            row('2026-01-01T02:00:00+00:00', '1.50'),
            row('2026-01-01T03:00:00+00:00', '0.90')]
    vouchers = [{'hour': '2026-01-01T00:00:00+00:00'}]
    candidates = fill_candidates(rows, vouchers, TOLERANCE)
    assert [item['hour'] for item in candidates] == ['2026-01-01T01:00:00+00:00',
                                                      '2026-01-01T02:00:00+00:00']
    assert len(vouchers) + len(candidates) == SPOT_LIMIT


def test_claimed_hour_never_reoffered_even_if_still_worst():
    rows = [row('2026-01-01T00:00:00+00:00', '9.00'),
            row('2026-01-01T01:00:00+00:00', '0.60')]
    vouchers = [{'hour': '2026-01-01T00:00:00+00:00'}]
    candidates = fill_candidates(rows, vouchers, TOLERANCE)
    assert [item['hour'] for item in candidates] == ['2026-01-01T01:00:00+00:00']


def test_board_marks_claimed_hour_back_within_tolerance():
    first = row('2026-01-01T00:00:00+00:00', '0.75')
    vouchers = [{'hour': first['hour'], 'local': '00:00', 'difference_mg_nm3': '0.75'}]
    # 重算后该小时回到容差内
    recomputed = [first | {'difference_mg_nm3': '0.10', 'code': 'MATCH'}]
    board = build_board(recomputed, vouchers, TOLERANCE)
    assert board['open_slots'] == 2
    assert board['claimed'][0]['still_deviation'] is False
    assert board['claimed'][0]['current_difference_mg_nm3'] == '0.10'
    # 空位不会再把它当候选
    assert board['candidates'] == []


def test_new_deviation_fills_open_slot_after_recompute():
    rows = [row('2026-01-01T00:00:00+00:00', '0.75'),
            row('2026-01-01T01:00:00+00:00', '0.20', code='MATCH')]
    vouchers = [{'hour': rows[0]['hour'], 'local': '00:00', 'difference_mg_nm3': '0.75'}]
    assert fill_candidates(rows, vouchers, TOLERANCE) == []
    recomputed = [rows[0], rows[1] | {'difference_mg_nm3': '3.00', 'code': 'DEVIATION'}]
    candidates = fill_candidates(recomputed, vouchers, TOLERANCE)
    assert [item['hour'] for item in candidates] == [rows[1]['hour']]
