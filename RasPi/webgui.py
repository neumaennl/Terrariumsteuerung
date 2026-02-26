from flask import Flask, render_template, request, redirect, url_for, jsonify
import logging
import terrariumsteuerung
import threading

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)


def get_data():
    return {
        'temperature': terrariumsteuerung.get_temperature(),
        'humidity': terrariumsteuerung.get_humidity(),
        'rpm': terrariumsteuerung.get_rpm(),
        'fan_pwm': terrariumsteuerung.get_fan_pwm(),
        'pump_status': terrariumsteuerung.get_pump_status(),
        'FAN_TARGET_HUMIDITY': terrariumsteuerung.get_threshold('FAN_TARGET_HUMIDITY'),
        'PUMP_TRIGGER_HUMIDITY': terrariumsteuerung.get_threshold('PUMP_TRIGGER_HUMIDITY'),
        'PUMP_EMERGENCY_OFF': terrariumsteuerung.get_threshold('PUMP_EMERGENCY_OFF'),
        'PUMP_SPRAY_DURATION': terrariumsteuerung.get_threshold('PUMP_SPRAY_DURATION'),
        'PUMP_COOLDOWN_MINUTES': terrariumsteuerung.get_threshold('PUMP_COOLDOWN_MINUTES'),
        'NIGHT_START_HOUR': terrariumsteuerung.get_threshold('NIGHT_START_HOUR'),
        'NIGHT_END_HOUR': terrariumsteuerung.get_threshold('NIGHT_END_HOUR'),
    }


@app.route('/')
def index():
    return render_template('index.html', data=get_data())


@app.route('/update', methods=['POST'])
def update():
    # Update thresholds from form
    for key in ['FAN_TARGET_HUMIDITY', 'PUMP_TRIGGER_HUMIDITY', 'PUMP_EMERGENCY_OFF', 'PUMP_SPRAY_DURATION', 'PUMP_COOLDOWN_MINUTES', 'NIGHT_START_HOUR', 'NIGHT_END_HOUR']:
        if key in request.form:
            try:
                value = float(request.form[key]) if '.' in request.form[key] else int(request.form[key])
                terrariumsteuerung.set_threshold(key, value)
            except Exception:
                pass
    return redirect(url_for('index'))


@app.route('/api/data')
def api_data():
    return jsonify(get_data())


@app.route('/api/history')
def api_history():
    # Optional parameters: start,end (epoch seconds), limit, max_points
    start = request.args.get('start', default=None, type=float)
    end = request.args.get('end', default=None, type=float)
    limit = request.args.get('limit', default=500, type=int)
    max_points = request.args.get('max_points', default=2000, type=int)
    try:
        if start is not None and end is not None:
            data = terrariumsteuerung.get_history(start=start, end=end, max_points=max_points)
        else:
            data = terrariumsteuerung.get_history(limit=limit)
    except Exception:
        logger.exception('Error fetching history')
        data = []
    return jsonify(data)


@app.route('/api/reload_thresholds', methods=['POST'])
@app.route('/api/reload-thresholds', methods=['POST'])
def api_reload_thresholds():
    try:
        terrariumsteuerung.load_thresholds()
        return jsonify({'ok': True}), 200
    except Exception:
        logger.exception('Failed to reload thresholds')
        return jsonify({'ok': False}), 500


# Singleton for controller thread
_controller_thread = None
_controller_lock = threading.Lock()


def start_controller():
    global _controller_thread
    with _controller_lock:
        if _controller_thread is None or not _controller_thread.is_alive():
            # Start as non-daemon so we can join on shutdown
            _controller_thread = threading.Thread(target=terrariumsteuerung.main, daemon=False)
            _controller_thread.start()
            logger.info('Controller thread started')


def stop_controller(timeout=5.0):
    try:
        terrariumsteuerung.stop()
    except Exception:
        logger.exception('Error while signaling controller to stop')
    # Join the thread to wait for clean shutdown
    try:
        if _controller_thread is not None:
            _controller_thread.join(timeout)
            logger.info('Controller thread joined')
    except Exception:
        logger.exception('Error while joining controller thread')


if __name__ == '__main__':
    # Register signal and atexit handlers for clean shutdown
    import signal, atexit

    def _on_exit(signum=None, frame=None):
        logger.info('Shutting down: stopping controller...')
        stop_controller()
        # Ensure the process exits after cleanup (works both for signals and atexit)
        import sys, os
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)

    signal.signal(signal.SIGINT, _on_exit)
    signal.signal(signal.SIGTERM, _on_exit)
    atexit.register(_on_exit)

    start_controller()
    # Disable the reloader to avoid multiple processes and ensure signal handlers work
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
