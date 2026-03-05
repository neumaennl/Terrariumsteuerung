import json
import logging
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

# History resolution policy
RECENT_WINDOW_DAYS = int(os.getenv('TERRARIUM_RECENT_WINDOW_DAYS', '3'))
RECENT_RESOLUTION_SECONDS = int(os.getenv('TERRARIUM_RECENT_RESOLUTION_SECONDS', '30'))
ARCHIVE_RESOLUTION_SECONDS = int(os.getenv('TERRARIUM_ARCHIVE_RESOLUTION_SECONDS', '900'))
COMPACTION_INTERVAL_SECONDS = int(os.getenv('TERRARIUM_COMPACTION_INTERVAL_SECONDS', '600'))

_default_max_rows = (
    int((RETENTION_DAYS * 86400) / ARCHIVE_RESOLUTION_SECONDS) +
    int((RECENT_WINDOW_DAYS * 86400) / RECENT_RESOLUTION_SECONDS) +
    100
)
# Hard row limit; oldest rows are deleted when limit is exceeded.
MAX_DB_ROWS = int(os.getenv('TERRARIUM_MAX_DB_ROWS', str(_default_max_rows)))

if BUFFER_MAX < 1:
    BUFFER_MAX = 1
if BUFFER_FLUSH_INTERVAL < 1:
    BUFFER_FLUSH_INTERVAL = 1
if RETENTION_DAYS < 1:
    RETENTION_DAYS = 1
if RECENT_WINDOW_DAYS < 1:
    RECENT_WINDOW_DAYS = 1
if RECENT_RESOLUTION_SECONDS < 1:
    RECENT_RESOLUTION_SECONDS = 1
if ARCHIVE_RESOLUTION_SECONDS < 1:
    ARCHIVE_RESOLUTION_SECONDS = 1
if COMPACTION_INTERVAL_SECONDS < 60:
    COMPACTION_INTERVAL_SECONDS = 60
if ARCHIVE_RESOLUTION_SECONDS < RECENT_RESOLUTION_SECONDS:
    ARCHIVE_RESOLUTION_SECONDS = RECENT_RESOLUTION_SECONDS

_reading_buffer = []
_buffer_last_flush = 0
_last_compaction_ts = 0
_buffer_lock = threading.Lock()
logger = logging.getLogger(__name__)


def _bucket_ts(ts, step_seconds):
    ts = int(ts)
    step_seconds = int(step_seconds)
    if step_seconds <= 1:
        return ts
    return int(ts / step_seconds) * step_seconds


def _target_resolution_seconds(ts, now=None):
    if now is None:
        now = int(time.time())
    cutoff = int(now) - (RECENT_WINDOW_DAYS * 86400)
    if int(ts) < cutoff:
        return ARCHIVE_RESOLUTION_SECONDS
    return RECENT_RESOLUTION_SECONDS


def _compact_old_readings(cur, now=None):
    """Compact rows older than RECENT_WINDOW_DAYS into ARCHIVE_RESOLUTION_SECONDS buckets."""
    if now is None:
        now = int(time.time())

    cutoff = int(now) - (RECENT_WINDOW_DAYS * 86400)
    if cutoff <= 0:
        return

    cur.execute(
        '''
        WITH bucketed AS (
            SELECT
                (ts / ?) * ? AS bucket_ts,
                ts,
                temperature,
                humidity,
                rpm,
                fan_pwm,
                pump_status
            FROM readings
            WHERE ts < ?
        ),
        agg AS (
            SELECT
                bucket_ts,
                AVG(temperature) AS temperature,
                AVG(humidity) AS humidity,
                CAST(AVG(rpm) AS INTEGER) AS rpm,
                CAST(AVG(fan_pwm) AS INTEGER) AS fan_pwm
            FROM bucketed
            GROUP BY bucket_ts
        )
        SELECT
            agg.bucket_ts AS ts,
            agg.temperature,
            agg.humidity,
            agg.rpm,
            agg.fan_pwm,
            COALESCE(
                (SELECT b2.pump_status FROM bucketed b2 WHERE b2.bucket_ts = agg.bucket_ts ORDER BY b2.ts DESC LIMIT 1),
                ''
            ) AS pump_status
        FROM agg
        ORDER BY agg.bucket_ts ASC
        ''',
        (ARCHIVE_RESOLUTION_SECONDS, ARCHIVE_RESOLUTION_SECONDS, cutoff),
    )
    compacted = cur.fetchall()

    cur.execute('DELETE FROM readings WHERE ts < ?', (cutoff,))
    if compacted:
        cur.executemany(
            'INSERT OR REPLACE INTO readings (ts, temperature, humidity, rpm, fan_pwm, pump_status) VALUES (?, ?, ?, ?, ?, ?)',
            compacted,
        )


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
    global _reading_buffer, _buffer_last_flush, _last_compaction_ts

    with _buffer_lock:
        if not _reading_buffer:
            return
        pending = list(_reading_buffer)

    conn = _get_conn()
    cur = conn.cursor()
    committed = False
    now = int(time.time())
    compaction_due = _last_compaction_ts == 0 or ((now - int(_last_compaction_ts)) >= COMPACTION_INTERVAL_SECONDS)
    compaction_succeeded = False

    try:
        conn.execute('BEGIN IMMEDIATE')

        cur.executemany(
            'INSERT OR REPLACE INTO readings (ts, temperature, humidity, rpm, fan_pwm, pump_status) VALUES (?, ?, ?, ?, ?, ?)',
            pending,
        )

        if compaction_due:
            try:
                _compact_old_readings(cur, now=now)
                compaction_succeeded = True
            except Exception as exc:
                logger.warning('Compaction failed; preserving buffered readings for later retry: %s', exc)

        try:
            cutoff = now - (RETENTION_DAYS * 86400)
            cur.execute('DELETE FROM readings WHERE ts < ?', (cutoff,))
        except Exception as exc:
            logger.warning('Retention cleanup failed; continuing without deleting old rows: %s', exc)

        if MAX_DB_ROWS > 0:
            try:
                cur.execute(
                    'DELETE FROM readings WHERE ts < (SELECT ts FROM readings ORDER BY ts DESC LIMIT 1 OFFSET ?)',
                    (MAX_DB_ROWS - 1,),
                )
            except Exception as exc:
                logger.warning('Max-row trim failed; continuing without trimming rows: %s', exc)

        conn.commit()
        committed = True

        if compaction_due and compaction_succeeded:
            _last_compaction_ts = now

        with _buffer_lock:
            if len(_reading_buffer) >= len(pending):
                del _reading_buffer[:len(pending)]
            else:
                _reading_buffer = []
            _buffer_last_flush = time.time()
    except Exception as exc:
        logger.warning('Flush failed; keeping %d buffered readings for retry: %s', len(pending), exc)
        try:
            if not committed:
                conn.rollback()
        except Exception as rollback_exc:
            logger.warning('Rollback failed after flush error: %s', rollback_exc)
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()


def buffer_reading(ts, temperature, humidity, rpm, fan_pwm, pump_status):
    global _reading_buffer, _buffer_last_flush

    now = int(time.time())
    step = _target_resolution_seconds(ts, now=now)
    bucketed_ts = _bucket_ts(ts, step)

    with _buffer_lock:
        _reading_buffer.append((bucketed_ts, float(temperature), float(humidity), int(rpm), int(fan_pwm), str(pump_status)))
        now_float = time.time()
        if _buffer_last_flush == 0:
            _buffer_last_flush = now_float
        should_flush = len(_reading_buffer) >= BUFFER_MAX or ((now_float - _buffer_last_flush) >= BUFFER_FLUSH_INTERVAL)

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


def _round_up_to_multiple(value, base):
    value = int(value)
    base = max(1, int(base))
    return ((value + base - 1) // base) * base


def _fetch_segment_rows(cur, seg_start, seg_end, point_budget, min_step_seconds):
    """Fetch one segment either raw or time-bucketed according to its budget."""
    if point_budget < 1:
        point_budget = 1

    cur.execute('SELECT COUNT(*) FROM readings WHERE ts BETWEEN ? AND ?', (seg_start, seg_end))
    row_count = int(cur.fetchone()[0])
    if row_count <= 0:
        return []

    cur.execute('SELECT MIN(ts), MAX(ts) FROM readings WHERE ts BETWEEN ? AND ?', (seg_start, seg_end))
    min_max = cur.fetchone()
    if not min_max or min_max[0] is None or min_max[1] is None:
        return []
    data_min_ts = int(min_max[0])
    data_max_ts = int(min_max[1])

    if row_count <= point_budget:
        cur.execute(
            'SELECT ts, temperature, humidity, rpm, fan_pwm, pump_status FROM readings WHERE ts BETWEEN ? AND ? ORDER BY ts ASC',
            (seg_start, seg_end),
        )
        return cur.fetchall()

    # Use actual data span, not requested range span, so large empty windows
    # do not force overly coarse buckets.
    seg_span = max(1, data_max_ts - data_min_ts + 1)
    needed_step = (seg_span + point_budget - 1) // point_budget
    bucket_seconds = _round_up_to_multiple(max(needed_step, min_step_seconds), min_step_seconds)

    cur.execute(
        '''
        WITH bucketed AS (
            SELECT
                (ts / ?) * ? AS bucket_ts,
                ts,
                temperature,
                humidity,
                rpm,
                fan_pwm,
                pump_status
            FROM readings
            WHERE ts BETWEEN ? AND ?
        ),
        agg AS (
            SELECT
                bucket_ts,
                AVG(temperature) AS temperature,
                AVG(humidity) AS humidity,
                CAST(AVG(rpm) AS INTEGER) AS rpm,
                CAST(AVG(fan_pwm) AS INTEGER) AS fan_pwm
            FROM bucketed
            GROUP BY bucket_ts
        )
        SELECT
            agg.bucket_ts AS ts,
            agg.temperature,
            agg.humidity,
            agg.rpm,
            agg.fan_pwm,
            COALESCE(
                (SELECT b2.pump_status FROM bucketed b2 WHERE b2.bucket_ts = agg.bucket_ts ORDER BY b2.ts DESC LIMIT 1),
                ''
            ) AS pump_status
        FROM agg
        ORDER BY agg.bucket_ts ASC
        ''',
        (bucket_seconds, bucket_seconds, seg_start, seg_end),
    )
    return cur.fetchall()


def get_history(start=None, end=None, max_points=2000):
    """Fetch history and downsample while preserving archive/recent tier behavior."""
    conn = _get_conn()
    cur = conn.cursor()
    try:
        if max_points is None:
            max_points = 2000
        max_points = int(max_points)
        if max_points < 1:
            max_points = 1
        if max_points > 50000:
            max_points = 50000

        # Resolve time window: explicit request or full DB range.
        if start is not None and end is not None:
            start_ts = int(start)
            end_ts = int(end)
            if end_ts < start_ts:
                start_ts, end_ts = end_ts, start_ts
        else:
            cur.execute('SELECT MIN(ts), MAX(ts) FROM readings')
            row = cur.fetchone()
            if not row or row[0] is None or row[1] is None:
                return []
            start_ts = int(row[0])
            end_ts = int(row[1])

        if end_ts < start_ts:
            return []

        # Split range so recent and archive data can be downsampled with different minimum steps.
        now = int(time.time())
        recent_cutoff = now - (RECENT_WINDOW_DAYS * 86400)

        archive_range = None
        if start_ts < recent_cutoff:
            archive_end = min(end_ts, recent_cutoff - 1)
            if archive_end >= start_ts:
                archive_range = (start_ts, archive_end)

        recent_range = None
        if end_ts >= recent_cutoff:
            recent_start = max(start_ts, recent_cutoff)
            if end_ts >= recent_start:
                recent_range = (recent_start, end_ts)

        cur.execute('SELECT COUNT(*) FROM readings WHERE ts BETWEEN ? AND ?', (start_ts, end_ts))
        total_rows = int(cur.fetchone()[0])
        if total_rows <= 0:
            return []

        # No downsampling needed.
        if total_rows <= max_points:
            cur.execute(
                'SELECT ts, temperature, humidity, rpm, fan_pwm, pump_status FROM readings WHERE ts BETWEEN ? AND ? ORDER BY ts ASC',
                (start_ts, end_ts),
            )
            rows = cur.fetchall()
        else:
            segments = []
            for seg_name, seg in [('archive', archive_range), ('recent', recent_range)]:
                if seg is None:
                    continue
                cur.execute('SELECT COUNT(*) FROM readings WHERE ts BETWEEN ? AND ?', seg)
                seg_count = int(cur.fetchone()[0])
                if seg_count > 0:
                    segments.append((seg_name, seg, seg_count))

            if not segments:
                return []

            # Distribute point budget proportional to rows per segment, at least one each.
            budgets = []
            assigned = 0
            for seg_name, seg, seg_count in segments:
                budget = (max_points * seg_count) // total_rows
                if budget < 1:
                    budget = 1
                budgets.append([seg_name, seg, seg_count, budget])
                assigned += budget

            # Trim excess budgets from largest segments first.
            while assigned > max_points and budgets:
                idx = max(range(len(budgets)), key=lambda i: budgets[i][2])
                if budgets[idx][3] > 1:
                    budgets[idx][3] -= 1
                    assigned -= 1
                else:
                    break

            # Add missing budget slots to largest segments first.
            while assigned < max_points and budgets:
                idx = max(range(len(budgets)), key=lambda i: budgets[i][2])
                budgets[idx][3] += 1
                assigned += 1

            rows = []
            for seg_name, seg, _seg_count, budget in budgets:
                min_step = ARCHIVE_RESOLUTION_SECONDS if seg_name == 'archive' else RECENT_RESOLUTION_SECONDS
                rows.extend(_fetch_segment_rows(cur, seg[0], seg[1], budget, min_step))

            rows.sort(key=lambda r: int(r[0]))

        return [
            {
                'ts': int(row[0]),
                'temperature': float(row[1]),
                'humidity': float(row[2]),
                'rpm': int(row[3]),
                'fan_pwm': int(row[4]),
                'pump_status': row[5],
            }
            for row in rows
        ]
    finally:
        conn.close()


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
