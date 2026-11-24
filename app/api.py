from flask import Blueprint, current_app, jsonify, request

from app.archive.certificate import issue
from app.archive.run import archive_run
from app.errors import InputError
from app.monitor.inspection import STAGES, Inspection
from app.monitor.source import sample

api = Blueprint('api', __name__, url_prefix='/api')


def archive():
    return current_app.extensions['monitor_archive']


def spot_store():
    return current_app.extensions['spotcheck_store']


@api.get('/samples/<name>/<stage>')
def preview(name, stage):
    if stage not in STAGES:
        raise InputError('STAGE_UNKNOWN', '找不到监测工序', 404)
    return jsonify(getattr(Inspection(sample(name)), stage))


@api.post('/runs')
def create_run():
    packet, reused = archive_run(archive(), request.get_json(silent=True))
    return jsonify(run_id=packet['manifest']['run_id'], reused=reused, manifest=packet['manifest'],
                   totals=packet['results']['hours']['totals'], deviations=packet['results']['reports']['deviations'])


@api.get('/runs')
def runs():
    return jsonify(runs=archive().history())


@api.get('/runs/<run_id>/<stage>')
def snapshot(run_id, stage):
    if stage not in STAGES:
        raise InputError('STAGE_UNKNOWN', '找不到监测工序', 404)
    return jsonify(archive().require(run_id)['results'][stage])


@api.get('/export/<run_id>')
def export(run_id):
    response = jsonify(issue(archive().require(run_id)))
    response.headers['Content-Disposition'] = f'attachment; filename="monitor-{run_id}.json"'
    return response


@api.post('/spotcheck/shifts')
def open_spot_shift():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get('run_id'), str):
        raise InputError('BAD_PACKET', '需要提供已归档的 run_id')
    shift = spot_store().open_shift(archive().require, payload['run_id'])
    return jsonify(shift=shift), 201


@api.get('/spotcheck/shifts')
def spot_shifts():
    return jsonify(shifts=spot_store().history())


@api.get('/spotcheck/shifts/<shift_id>')
def spot_shift(shift_id):
    return jsonify(shift=spot_store().board(archive().require, shift_id))


@api.post('/spotcheck/shifts/<shift_id>/reconcile')
def spot_reconcile(shift_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get('run_id'), str):
        raise InputError('BAD_PACKET', '需要提供重算后的 run_id')
    shift, candidates = spot_store().attach_run(archive().require, shift_id, payload['run_id'])
    return jsonify(shift=shift, filled_candidates=candidates)


@api.post('/spotcheck/shifts/<shift_id>/claim')
def spot_claim(shift_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get('hour'), str):
        raise InputError('BAD_PACKET', '需要指定领取的小时 hour')
    shift, voucher = spot_store().claim(archive().require, shift_id, payload['hour'])
    return jsonify(shift=shift, voucher=voucher), 201


@api.post('/spotcheck/shifts/<shift_id>/close')
def spot_close(shift_id):
    shift, vouchers = spot_store().close_shift(shift_id)
    return jsonify(shift=shift, vouchers=vouchers)
