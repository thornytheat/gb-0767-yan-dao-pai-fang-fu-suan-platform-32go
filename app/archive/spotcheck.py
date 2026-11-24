import hashlib
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.archive.manifest import canonical
from app.errors import InputError
from app.monitor.spotcheck import SPOT_LIMIT, build_board, fill_candidates

LOCK_RETRIES = 50


class SpotCheckStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._immediate() as db:
            db.execute('CREATE TABLE IF NOT EXISTS spot_shifts ('
                       'id TEXT PRIMARY KEY, stack TEXT NOT NULL, status TEXT NOT NULL, '
                       'run_id TEXT NOT NULL, tolerance TEXT NOT NULL, opened_utc TEXT NOT NULL, '
                       'closed_utc TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS spot_vouchers ('
                       'shift_id TEXT NOT NULL, hour TEXT NOT NULL, sequence INTEGER NOT NULL, '
                       'voucher_no TEXT NOT NULL, run_id TEXT NOT NULL, difference_mg_nm3 TEXT NOT NULL, '
                       'mg_nm3 TEXT, reported_mg_nm3 TEXT, local TEXT NOT NULL, issued_utc TEXT NOT NULL, '
                       'PRIMARY KEY (shift_id, hour))')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS spot_voucher_no ON spot_vouchers(voucher_no)')

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def _immediate(self):
        return _ImmediateSession(self)

    # ---------- 只读 ----------
    def _load_shift(self, db, shift_id):
        row = db.execute('SELECT * FROM spot_shifts WHERE id = ?', (shift_id,)).fetchone()
        if row is None:
            raise InputError('SHIFT_UNKNOWN', '找不到抽查班次', 404)
        return dict(row)

    def _vouchers(self, db, shift_id):
        return [dict(row) for row in db.execute(
            'SELECT * FROM spot_vouchers WHERE shift_id = ? ORDER BY sequence', (shift_id,))]

    def board(self, packet_lookup, shift_id):
        with closing(self._connect()) as db:
            shift = self._load_shift(db, shift_id)
            vouchers = self._vouchers(db, shift_id)
        packet = packet_lookup(shift['run_id'])
        board = build_board(packet['results']['reports']['rows'], vouchers, Decimal(shift['tolerance']))
        return _shift_view(shift, board, packet)

    def history(self):
        with closing(self._connect()) as db:
            rows = db.execute('SELECT * FROM spot_shifts ORDER BY rowid DESC LIMIT 100').fetchall()
            result = []
            for row in rows:
                shift = dict(row)
                shift['vouchers'] = self._vouchers(db, shift['id'])
                result.append(shift)
        return result

    # ---------- 写操作：BEGIN IMMEDIATE 串行化 ----------
    def open_shift(self, packet_lookup, run_id):
        with self._immediate() as db:
            packet = _require_packet(packet_lookup, run_id)
            active = db.execute("SELECT id FROM spot_shifts WHERE status = 'open'").fetchall()
            if active:
                raise InputError('SHIFT_OPEN_EXISTS', '已有未交班的抽查班次，请先交班', 409)
            tolerance, stack = _shift_fields(packet)
            sequence = db.execute('SELECT COUNT(*) FROM spot_shifts WHERE run_id = ?', (run_id,)).fetchone()[0] + 1
            shift_id = _shift_id(run_id, sequence)
            db.execute('INSERT INTO spot_shifts (id, stack, status, run_id, tolerance, opened_utc, closed_utc) '
                       'VALUES (?, ?, ?, ?, ?, ?, NULL)',
                       (shift_id, stack, 'open', run_id, tolerance, _now()))
            shift = dict(db.execute('SELECT * FROM spot_shifts WHERE id = ?', (shift_id,)).fetchone())
            vouchers = []
        return _shift_view(shift, build_board(packet['results']['reports']['rows'], vouchers,
                                              Decimal(tolerance)), packet)

    def attach_run(self, packet_lookup, shift_id, run_id):
        with self._immediate() as db:
            shift = self._load_shift(db, shift_id)
            _require_open(shift)
            packet = _require_packet(packet_lookup, run_id)
            parameters = packet['manifest']['parameters']
            if parameters['stack'] != shift['stack']:
                raise InputError('SHIFT_STACK_MISMATCH', '重算留档与班次烟道不符', 422)
            new_rows = packet['results']['reports']['rows']
            old_packet = packet_lookup(shift['run_id'])
            if {row['hour'] for row in new_rows} != {row['hour'] for row in old_packet['results']['reports']['rows']}:
                raise InputError('SHIFT_HOURS_MISMATCH', '重算留档的小时窗口与班次不符', 422)
            vouchers = self._vouchers(db, shift_id)
            candidates = fill_candidates(new_rows, vouchers, Decimal(shift['tolerance']))
            db.execute('UPDATE spot_shifts SET run_id = ? WHERE id = ?', (run_id, shift_id))
            shift = dict(db.execute('SELECT * FROM spot_shifts WHERE id = ?', (shift_id,)).fetchone())
        return _shift_view(shift, build_board(new_rows, vouchers, Decimal(shift['tolerance'])), packet), candidates

    def claim(self, packet_lookup, shift_id, hour):
        with self._immediate() as db:
            shift = self._load_shift(db, shift_id)
            _require_open(shift)
            vouchers = self._vouchers(db, shift_id)
            if any(voucher['hour'] == hour for voucher in vouchers):
                raise InputError('VOUCHER_CLAIMED', '该小时已领走凭证', 409)
            if len(vouchers) >= SPOT_LIMIT:
                raise InputError('SHIFT_FULL', '本班抽查名额已满', 409)
            # 以当前挂接的同一次对账为准：差额与留档来源都取自这一份 packet
            packet = _require_packet(packet_lookup, shift['run_id'])
            rows = packet['results']['reports']['rows']
            current = next((row for row in rows if row['hour'] == hour), None)
            if current is None:
                raise InputError('HOUR_UNKNOWN', '班次窗口内没有该小时', 404)
            if current['code'] != 'DEVIATION' or current['difference_mg_nm3'] is None \
                    or abs(Decimal(current['difference_mg_nm3'])) <= Decimal(shift['tolerance']):
                raise InputError('HOUR_NOT_DEVIATION', '该小时当前未超出容差，不能领取', 422)
            eligible = {row['hour'] for row in fill_candidates(rows, vouchers, Decimal(shift['tolerance']))}
            if hour not in eligible:
                raise InputError('HOUR_NOT_ELIGIBLE', '该小时不在当前空位的补位候选中', 422)
            sequence = len(vouchers) + 1
            voucher_no = f'{shift_id[:8]}-S{sequence:02d}'
            voucher = {'shift_id': shift_id, 'hour': hour, 'sequence': sequence, 'voucher_no': voucher_no,
                       'run_id': shift['run_id'], 'difference_mg_nm3': current['difference_mg_nm3'],
                       'mg_nm3': current['mg_nm3'], 'reported_mg_nm3': current['reported_mg_nm3'],
                       'local': current['local'], 'issued_utc': _now()}
            db.execute('INSERT INTO spot_vouchers (shift_id, hour, sequence, voucher_no, run_id, '
                       'difference_mg_nm3, mg_nm3, reported_mg_nm3, local, issued_utc) VALUES '
                       '(:shift_id, :hour, :sequence, :voucher_no, :run_id, :difference_mg_nm3, '
                       ':mg_nm3, :reported_mg_nm3, :local, :issued_utc)', voucher)
            vouchers = self._vouchers(db, shift_id)
        return _shift_view(shift, build_board(rows, vouchers, Decimal(shift['tolerance'])), packet), voucher

    def close_shift(self, shift_id):
        with self._immediate() as db:
            shift = self._load_shift(db, shift_id)
            _require_open(shift)
            db.execute("UPDATE spot_shifts SET status = 'closed', closed_utc = ? WHERE id = ?",
                       (_now(), shift_id))
            shift = dict(db.execute('SELECT * FROM spot_shifts WHERE id = ?', (shift_id,)).fetchone())
            vouchers = self._vouchers(db, shift_id)
        return shift, vouchers


class _ImmediateSession:
    """BEGIN IMMEDIATE 取写锁；锁冲突时重试，保证并发领取/重算被串行化。"""

    def __init__(self, store):
        self.store = store
        self.db = None

    def __enter__(self):
        for attempt in range(LOCK_RETRIES):
            db = self.store._connect()
            try:
                db.execute('BEGIN IMMEDIATE')
            except sqlite3.OperationalError:
                db.close()
                time.sleep(0.05)
                continue
            self.db = db
            return db
        raise InputError('STORE_BUSY', '留档正忙，请稍后重试', 503)

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.db.commit()
            else:
                self.db.rollback()
        finally:
            self.db.close()


def _require_open(shift):
    if shift['status'] != 'open':
        raise InputError('SHIFT_CLOSED', '班次已交班，不能再改动凭证', 409)


def _require_packet(packet_lookup, run_id):
    try:
        return packet_lookup(run_id)
    except InputError:
        raise InputError('RUN_UNKNOWN', '抽查必须挂接已归档的对账留档', 404) from None


def _shift_fields(packet):
    parameters = packet['manifest']['parameters']
    return str(parameters['tolerance']), parameters['stack']


def _shift_id(run_id, sequence):
    digest = canonical(['spot-shift', run_id, sequence])
    return hashlib.sha256(digest.encode()).hexdigest()[:16]


def _shift_view(shift, board, packet):
    return {'id': shift['id'], 'stack': shift['stack'], 'status': shift['status'],
            'run_id': shift['run_id'], 'tolerance': shift['tolerance'],
            'opened_utc': shift['opened_utc'], 'closed_utc': shift['closed_utc'],
            'board': board, 'reconcile': {'run_id': packet['manifest']['run_id'],
                                          'created_utc': packet['manifest']['created_utc'],
                                          'deviations': packet['results']['reports']['deviations']}}


def _now():
    return datetime.now(UTC).isoformat(timespec='seconds')
