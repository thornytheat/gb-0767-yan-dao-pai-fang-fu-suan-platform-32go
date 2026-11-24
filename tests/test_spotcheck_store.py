from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from app.main import create_app

UTC = timezone.utc
STACK = 'K-TEST'
START = datetime(2026, 11, 14, 0, 0, tzinfo=UTC)
HOURS = 3


def _minutes(values):
    lines = ['stack,timestamp,value,unit,basis,moisture,oxygen,status,revision,received']
    for hour, value in enumerate(values):
        for minute in range(60):
            when = START + timedelta(hours=hour, minutes=minute)
            received = when + timedelta(minutes=1)
            lines.append(f'{STACK},{when:%Y-%m-%dT%H:%M}Z,{value},mg/Nm3,dry,-,10,OK,1,'
                         f'{received:%Y-%m-%dT%H:%M}Z')
    return '\n'.join(lines) + '\n'


def _reports(values):
    lines = ['stack,hour,value,ticket']
    for hour, value in enumerate(values):
        when = START + timedelta(hours=hour)
        lines.append(f'{STACK},{when:%Y-%m-%dT%H:%M}Z,{value},T-{hour}')
    return '\n'.join(lines) + '\n'


WINDOW = ('{"stack":"%s","start":"2026-11-14T00:00Z","end":"2026-11-14T03:00Z",'
          '"cutoff":"2026-11-14T05:00Z","reference":"10","threshold":45,"tolerance":"0.50"}') % STACK


def packet(calc, reported):
    return {'files': {'window': WINDOW, 'minutes': _minutes(calc), 'modes': 'stack,start,end,mode\n',
                      'reports': _reports(reported)}}


def archive_run(client, calc, reported):
    response = client.post('/api/runs', json=packet(calc, reported))
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.json['run_id']


def hour_key(index):
    return (START + timedelta(hours=index)).isoformat()


def make_client(tmp_path):
    return create_app(tmp_path / 'spot.sqlite').test_client()


def test_open_shift_lists_top_three_deviations(tmp_path):
    client = make_client(tmp_path)
    # 差额 2.00 / 1.00 / 0.60，全部超差
    run_id = archive_run(client, [30, 30, 30], [28, 29, 29.4])
    response = client.post('/api/spotcheck/shifts', json={'run_id': run_id})
    assert response.status_code == 201
    board = response.json['shift']['board']
    assert board['limit'] == 3 and board['open_slots'] == 3
    assert [item['hour'] for item in board['candidates']] == [hour_key(0), hour_key(1), hour_key(2)]
    assert board['candidates'][0]['difference_mg_nm3'] == '2.00'


def test_only_one_open_shift_per_desk(tmp_path):
    client = make_client(tmp_path)
    run_id = archive_run(client, [30], [28])
    first = client.post('/api/spotcheck/shifts', json={'run_id': run_id})
    assert first.status_code == 201
    second = client.post('/api/spotcheck/shifts', json={'run_id': run_id})
    assert (second.status_code, second.json['code']) == (409, 'SHIFT_OPEN_EXISTS')


def test_preview_run_cannot_open_shift(tmp_path):
    client = make_client(tmp_path)
    response = client.post('/api/spotcheck/shifts', json={'run_id': 'missing'})
    assert (response.status_code, response.json['code']) == (404, 'RUN_UNKNOWN')


def test_claim_issues_voucher_against_same_reconcile(tmp_path):
    client = make_client(tmp_path)
    run_id = archive_run(client, [30, 30, 30], [28, 29, 29.4])
    sid = client.post('/api/spotcheck/shifts', json={'run_id': run_id}).json['shift']['id']
    response = client.post(f'/api/spotcheck/shifts/{sid}/claim', json={'hour': hour_key(0)})
    assert response.status_code == 201
    voucher = response.json['voucher']
    assert (voucher['run_id'], voucher['difference_mg_nm3'], voucher['sequence']) == (run_id, '2.00', 1)
    board = response.json['shift']['board']
    assert [item['hour'] for item in board['claimed']] == [hour_key(0)]
    assert [item['hour'] for item in board['candidates']] == [hour_key(1), hour_key(2)]


def test_concurrent_claims_for_last_slot_issue_one_voucher(tmp_path):
    client = make_client(tmp_path)
    run_id = archive_run(client, [30, 30, 30], [28, 29, 29.4])
    sid = client.post('/api/spotcheck/shifts', json={'run_id': run_id}).json['shift']['id']
    # 先领走两个，剩最后一个名额
    for index in range(2):
        client.post(f'/api/spotcheck/shifts/{sid}/claim', json={'hour': hour_key(index)})
    store = client.application.extensions['spotcheck_store']
    archive = client.application.extensions['monitor_archive']

    def try_claim(_):
        try:
            return store.claim(archive.require, sid, hour_key(2))
        except Exception as error:  # 两人同时抢最后一个名额，必须一人失败
            return error

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(try_claim, range(2)))
    winners = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(winners) == 1 and len(losers) == 1
    assert getattr(losers[0], 'code') in {'VOUCHER_CLAIMED', 'SHIFT_FULL'}
    board = winners[0][0]['board']
    assert len(board['claimed']) == 3 and board['open_slots'] == 0
    assert len({voucher['voucher_no'] for voucher in board['claimed']}) == 3
    # 落库确认只有一张该小时的凭证
    persisted = store.history()[0]['vouchers']
    assert [voucher['hour'] for voucher in persisted].count(hour_key(2)) == 1


def test_recompute_keeps_claimed_slot_and_refills_from_new_deviation(tmp_path):
    client = make_client(tmp_path)
    # 初版：h0 差 2.00、h1 差 1.00、h2 在容差内
    first_run = archive_run(client, [30, 30, 30], [28, 29, 29.6])
    sid = client.post('/api/spotcheck/shifts', json={'run_id': first_run}).json['shift']['id']
    claimed = client.post(f'/api/spotcheck/shifts/{sid}/claim', json={'hour': hour_key(0)})
    assert claimed.json['voucher']['run_id'] == first_run
    # 晚到监测重算：h0 回落到容差内（凭证照占），h2 新超差且差额最大，h1 仍超差
    second_run = archive_run(client, [30, 30, 30], [29.7, 29, 26])
    response = client.post(f'/api/spotcheck/shifts/{sid}/reconcile', json={'run_id': second_run})
    assert response.status_code == 200
    view = response.json['shift']
    assert view['run_id'] == second_run
    claimed_hour = view['board']['claimed'][0]
    assert claimed_hour['hour'] == hour_key(0)
    assert claimed_hour['still_deviation'] is False
    assert claimed_hour['current_difference_mg_nm3'] == '0.30'
    # 空位只从本班未领且当前超差的小时补，按差额降序
    assert [item['hour'] for item in view['board']['candidates']] == [hour_key(2), hour_key(1)]
    # 新凭证的差额与来源必须是重算后的同一次对账
    again = client.post(f'/api/spotcheck/shifts/{sid}/claim', json={'hour': hour_key(2)})
    voucher = again.json['voucher']
    assert (voucher['run_id'], voucher['difference_mg_nm3']) == (second_run, '4.00')


def test_claim_rejects_hour_not_currently_deviating(tmp_path):
    client = make_client(tmp_path)
    first_run = archive_run(client, [30, 30], [28, 29.6])
    sid = client.post('/api/spotcheck/shifts', json={'run_id': first_run}).json['shift']['id']
    # h1 在容差内，不能领
    response = client.post(f'/api/spotcheck/shifts/{sid}/claim', json={'hour': hour_key(1)})
    assert (response.status_code, response.json['code']) == (422, 'HOUR_NOT_DEVIATION')


def test_recompute_rejects_different_stack_or_hours(tmp_path):
    client = make_client(tmp_path)
    run_id = archive_run(client, [30, 30, 30], [28, 29, 29.4])
    sid = client.post('/api/spotcheck/shifts', json={'run_id': run_id}).json['shift']['id']
    other_window = WINDOW.replace(STACK, 'K-OTHER')
    response = client.post('/api/runs', json={'files': {'window': other_window,
                                                        'minutes': _minutes([30, 30, 30]),
                                                        'modes': 'stack,start,end,mode\n',
                                                        'reports': _reports([28, 29, 29.4])}})
    other_run = response.json['run_id']
    failed = client.post(f'/api/spotcheck/shifts/{sid}/reconcile', json={'run_id': other_run})
    assert (failed.status_code, failed.json['code']) == (422, 'SHIFT_STACK_MISMATCH')


def test_close_freezes_shift_and_board_still_readable(tmp_path):
    client = make_client(tmp_path)
    run_id = archive_run(client, [30], [28])
    sid = client.post('/api/spotcheck/shifts', json={'run_id': run_id}).json['shift']['id']
    closed = client.post(f'/api/spotcheck/shifts/{sid}/close')
    assert closed.status_code == 200 and closed.json['shift']['status'] == 'closed'
    blocked = client.post(f'/api/spotcheck/shifts/{sid}/claim', json={'hour': hour_key(0)})
    assert blocked.json['code'] == 'SHIFT_CLOSED'
    board = client.get(f'/api/spotcheck/shifts/{sid}')
    assert board.status_code == 200 and board.json['shift']['status'] == 'closed'
    # 交班后可以开新班
    reopened = client.post('/api/spotcheck/shifts', json={'run_id': run_id})
    assert reopened.status_code == 201


def test_reports_page_carries_spot_panel(tmp_path):
    client = make_client(tmp_path)
    run_id = archive_run(client, [30], [28])
    page = client.get(f'/monitor/reports?run={run_id}')
    assert page.status_code == 200 and 'spot-panel' in page.get_data(as_text=True)
    sid = client.post('/api/spotcheck/shifts', json={'run_id': run_id}).json['shift']['id']
    page = client.get(f'/monitor/reports?run={run_id}&shift={sid}')
    text = page.get_data(as_text=True)
    assert '空位待补' in text and '领取凭证' in text
