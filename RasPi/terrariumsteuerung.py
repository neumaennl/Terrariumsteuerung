import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request

DB_PATH = os.path.join(os.path.dirname(__file__), 'terrarium.db')

# Buffered persistence configuration (to reduce microSD wear)
BUFFER_MAX = int(os.getenv('TERRARIUM_BUFFER_MAX', '10'))
BUFFER_FLUSH_INTERVAL = int(os.getenv('TERRARIUM_BUFFER_FLUSH_INTERVAL', '300'))
RETENTION_DAYS = int(os.getenv('TERRARIUM_RETENTION_DAYS', '31'))
MAX_DB_ROWS = int(os.getenv('TERRARIUM_MAX_DB_ROWS', '10000'))

if BUFFER_MAX < 1:
    BUFFER_MAX = 1
if BUFFER_FLUSH_INTERVAL < 1:
    BUFFER_FLUSH_INTERVAL = 1
if RETENTION_DAYS < 1:
    RETENTION_DAYS = 1

_reading_buffer = []
_buffer_last_flush = 0
_buffer_lock = threading.Lock()


def get_db_path():
    return DB_PATH


def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA wal_autocheckpoint=1000')
    except Exception:
        pass
    return conn


def flush_readings():
    """Atomically write buffered readings and trim old data / max rows."""
    global _reading_buffer, _buffer_last_flush

    with _buffer_lock:
        if not _reading_buffer:
            return
        pending = list(_reading_buffer)

    conn = _get_conn()
    cur = conn.cursor()
    try:
        cur.executemany(
            'INSERT OR REPLACE INTO readings (ts, temperature, humidity, rpm, fan_pwm, pump_status) VALUES (?, ?, ?, ?, ?, ?)',
            pending,
        )
        conn.commit()

        try:
            cutoff = int(time.time()) - (RETENTION_DAYS * 86400)
            cur.execute('DELETE FROM readings WHERE ts < ?', (cutoff,))
            conn.commit()
        except Exception:
            pass

        if MAX_DB_ROWS > 0:
            try:
                cur.execute(
                    'DELETE FROM readings WHERE ts < (SELECT ts FROM readings ORDER BY ts DESC LIMIT 1 OFFSET ?)',
                    (MAX_DB_ROWS - 1,),
                )
                conn.commit()
            except Exception:
                pass

        with _buffer_lock:
            if len(_reading_buffer) >= len(pending):
                del _reading_buffer[:len(pending)]
            else:
                _reading_buffer = []
            _buffer_last_flush = time.time()
    except Exception:
        pass
    finally:
        conn.close()


def buffer_reading(ts, temperature, humidity, rpm, fan_pwm, pump_status):
    global _reading_buffer, _buffer_last_flush

    with _buffer_lock:
        _reading_buffer.append((int(ts), float(temperature), float(humidity), int(rpm), int(fan_pwm), str(pump_status)))
        now = time.time()
        if _buffer_last_flush == 0:
            _buffer_last_flush = now
        should_flush = len(_reading_buffer) >= BUFFER_MAX or ((now - _buffer_last_flush) >= BUFFER_FLUSH_INTERVAL)

    if should_flush:
        flush_readings()


def init_db():
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS readings (
            ts INTEGER PRIMARY KEY,
            temperature REAL,
            humidity REAL,
            rpm INTEGER,
            fan_pwm INTEGER,
            pump_status TEXT
        )
        '''
    )
    conn.commit()
    conn.close()


def save_reading(ts, temperature, humidity, rpm, fan_pwm, pump_status):
    buffer_reading(ts, temperature, humidity, rpm, fan_pwm, pump_status)


def get_history(start=None, end=None, max_points=2000):
    """Fetch history. If start/end provided (epoch seconds), fetch that range.
    If the number of rows exceeds max_points, aggregate into time buckets to reduce points."""
    conn = _get_conn()
    cur = conn.cursor()
    params = []
    query = 'SELECT ts, temperature, humidity, rpm, fan_pwm, pump_status FROM readings'

    if start is not None and end is not None:
        query += ' WHERE ts BETWEEN ? AND ?'
        params.extend([int(start), int(end)])

    query += ' ORDER BY ts ASC'

    cur.execute(query, tuple(params))
    rows = cur.fetchall()
    conn.close()

    # If too many, aggregate into buckets
    if max_points and len(rows) > max_points and start is not None and end is not None and end > start:
        bucket_size = int((end - start) / max_points) + 1
        buckets = {}
        for row in rows:
            ts = int(row[0])
            idx = (ts - int(start)) // bucket_size
            bucket = buckets.setdefault(
                idx,
                {
                    'ts_sum': 0,
                    'count': 0,
                    'temp_sum': 0.0,
                    'hum_sum': 0.0,
                    'rpm_sum': 0,
                    'fan_sum': 0,
                },
            )
            bucket['ts_sum'] += ts
            bucket['temp_sum'] += row[1]
            bucket['hum_sum'] += row[2]
            bucket['rpm_sum'] += row[3]
            bucket['fan_sum'] += row[4]
            bucket['count'] += 1

        result = []
        for idx in sorted(buckets.keys()):
            bucket = buckets[idx]
            count = bucket['count']
            result.append(
                {
                    'ts': int(bucket['ts_sum'] / count),
                    'temperature': bucket['temp_sum'] / count,
                    'humidity': bucket['hum_sum'] / count,
                    'rpm': int(bucket['rpm_sum'] / count),
                    'fan_pwm': int(bucket['fan_sum'] / count),
                    'pump_status': '',
                }
            )
        return result

    return [
        {
            'ts': row[0],
            'temperature': row[1],
            'humidity': row[2],
            'rpm': row[3],
            'fan_pwm': row[4],
            'pump_status': row[5],
        }
        for row in rows
    ]


def _request_json(url, method='GET', payload=None, timeout=5):
    data = None
    headers = {'Accept': 'application/json'}

    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'

    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode('utf-8')
        if not body:
            return {}
        return json.loads(body)


def fetch_esp_data(esp_base_url, timeout=5):
    url = esp_base_url.rstrip('/') + '/api/data'
    return _request_json(url, method='GET', payload=None, timeout=timeout)


def push_esp_settings(esp_base_url, settings, timeout=5):
    url = esp_base_url.rstrip('/') + '/api/settings'
    return _request_json(url, method='POST', payload=settings, timeout=timeout)


def reset_esp_thresholds(esp_base_url, timeout=5):
    url = esp_base_url.rstrip('/') + '/api/reset_thresholds_defaults'
    try:
        return _request_json(url, method='POST', payload={}, timeout=timeout)
    except urllib.error.HTTPError:
        url = esp_base_url.rstrip('/') + '/api/reset-thresholds-defaults'
        return _request_json(url, method='POST', payload={}, timeout=timeout)


def now_ts():
    return int(time.time())


init_db()
