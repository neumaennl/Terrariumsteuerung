from flask import Flask, jsonify, redirect, render_template, request, url_for
import logging
import threading
import time

import terrariumsteuerung

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

ESP_BASE_URL = 'http://terrarium.local'
ESP_TIMEOUT_SECONDS = 5
POLL_INTERVAL_SECONDS = 3

_last_snapshot = {}
_last_poll_error = ''
_snapshot_lock = threading.Lock()
_poller_stop = threading.Event()
_poller_thread = None


def _default_data():
    return {
        'temperature': 0.0,
        'humidity': 0.0,
        'rpm': 0,
        'fan_pwm': 0,
        'pump_status': 'INIT',
        'FAN_TARGET_HUMIDITY': 80.0,
        'PUMP_TRIGGER_HUMIDITY': 60.0,
        'PUMP_EMERGENCY_OFF': 85.0,
        'PUMP_SPRAY_DURATION': 15,
        'PUMP_COOLDOWN_MINUTES': 15,
        'NIGHT_START_HOUR': 19,
        'NIGHT_END_HOUR': 8,
        'timestamp': terrariumsteuerung.now_ts(),
        'esp_online': False,
        'esp_error': 'Noch keine Daten vom ESP empfangen',
    }


def _store_snapshot(snapshot, online, error_text=''):
    global _last_snapshot, _last_poll_error
    with _snapshot_lock:
        merged = _default_data()
        merged.update(snapshot or {})
        merged['esp_online'] = bool(online)
        merged['esp_error'] = error_text
        _last_snapshot = merged
        _last_poll_error = error_text


def _get_snapshot():
    with _snapshot_lock:
        if not _last_snapshot:
            return _default_data()
        return dict(_last_snapshot)


def poll_esp_once():
    try:
        data = terrariumsteuerung.fetch_esp_data(ESP_BASE_URL, timeout=ESP_TIMEOUT_SECONDS)
        ts = int(data.get('timestamp', terrariumsteuerung.now_ts()))
        terrariumsteuerung.save_reading(
            ts=ts,
            temperature=data.get('temperature', 0.0),
            humidity=data.get('humidity', 0.0),
            rpm=data.get('rpm', 0),
            fan_pwm=data.get('fan_pwm', 0),
            pump_status=data.get('pump_status', ''),
        )
        _store_snapshot(data, online=True, error_text='')
        return True
    except Exception as exc:
        error_text = str(exc)
        logger.warning('ESP polling failed: %s', error_text)
        existing = _get_snapshot()
        _store_snapshot(existing, online=False, error_text=error_text)
        return False


def poller_loop():
    interval = int(POLL_INTERVAL_SECONDS)
    if interval < 1:
        interval = 1

    logger.info('Starting ESP poller: base_url=%s interval=%ss', ESP_BASE_URL, interval)
    while not _poller_stop.is_set():
        start = time.time()
        poll_esp_once()
        elapsed = time.time() - start
        wait_time = max(0.0, interval - elapsed)
        if _poller_stop.wait(wait_time):
            break


@app.route('/')
def index():
    return render_template('index.html', data=_get_snapshot())


@app.route('/update', methods=['POST'])
def update():
    payload = {}
    keys = [
        'FAN_TARGET_HUMIDITY',
        'PUMP_TRIGGER_HUMIDITY',
        'PUMP_EMERGENCY_OFF',
        'PUMP_SPRAY_DURATION',
        'PUMP_COOLDOWN_MINUTES',
        'NIGHT_START_HOUR',
        'NIGHT_END_HOUR',
    ]

    for key in keys:
        if key not in request.form:
            continue
        raw = request.form[key]
        try:
            payload[key] = float(raw) if '.' in raw else int(raw)
        except Exception:
            payload[key] = raw

    try:
        terrariumsteuerung.push_esp_settings(ESP_BASE_URL, payload, timeout=ESP_TIMEOUT_SECONDS)
        poll_esp_once()
    except Exception as exc:
        logger.exception('Failed to forward settings')
        return jsonify({'ok': False, 'error': str(exc)}), 500

    return redirect(url_for('index'))


@app.route('/api/data')
def api_data():
    data = _get_snapshot()
    data['poll_interval_seconds'] = int(POLL_INTERVAL_SECONDS)
    data['esp_base_url'] = ESP_BASE_URL
    return jsonify(data)


@app.route('/api/history')
def api_history():
    start = request.args.get('start', default=None, type=float)
    end = request.args.get('end', default=None, type=float)
    max_points = request.args.get('max_points', default=2000, type=int)

    try:
        data = terrariumsteuerung.get_history(start=start, end=end, max_points=max_points)
    except Exception:
        logger.exception('Error fetching history')
        data = []

    return jsonify(data)


@app.route('/api/settings', methods=['POST'])
def api_settings():
    payload = request.get_json(silent=True) or {}
    try:
        result = terrariumsteuerung.push_esp_settings(ESP_BASE_URL, payload, timeout=ESP_TIMEOUT_SECONDS)
        poll_esp_once()
        return jsonify({'ok': True, 'esp_response': result}), 200
    except Exception as exc:
        logger.exception('Failed to proxy settings to ESP')
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/reset_thresholds_defaults', methods=['POST'])
def api_reset_thresholds_defaults():
    try:
        result = terrariumsteuerung.reset_esp_thresholds(ESP_BASE_URL, timeout=ESP_TIMEOUT_SECONDS)
        poll_esp_once()
        return jsonify({'ok': True, 'esp_response': result}), 200
    except Exception as exc:
        logger.exception('Failed to reset thresholds on ESP')
        return jsonify({'ok': False, 'error': str(exc)}), 500


def start_poller():
    global _poller_thread
    if _poller_thread is not None and _poller_thread.is_alive():
        return

    _poller_stop.clear()
    _poller_thread = threading.Thread(target=poller_loop, daemon=True)
    _poller_thread.start()


def stop_poller(timeout=5.0):
    _poller_stop.set()
    if _poller_thread is not None:
        _poller_thread.join(timeout)
    terrariumsteuerung.flush_readings()


if __name__ == '__main__':
    import atexit
    import signal

    def _on_exit(signum=None, frame=None):
        logger.info('Shutting down poller...')
        stop_poller()
        import os
        import sys

        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)

    signal.signal(signal.SIGINT, _on_exit)
    signal.signal(signal.SIGTERM, _on_exit)
    atexit.register(_on_exit)

    start_poller()
    poll_esp_once()
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
