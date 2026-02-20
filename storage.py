"""
Streaming file-based data storage for terrarium readings.
Uses NDJSON files to avoid loading full history into RAM.
"""

import json
import os
import time
import gc
import config
from ntp_sync import log_print as print


# MicroPython safe existence check (os.path may not exist)
def _exists(path):
    try:
        os.stat(path)
        return True
    except Exception:
        return False


# Storage files (line-delimited JSON)
RECENT_FILE = 'history.ndjson'
ARCHIVE_FILE = 'archive.ndjson'


# Tiered storage configuration (in seconds)
RECENT_RETENTION_SECONDS = config.get('RECENT_RETENTION_SECONDS', 3 * 86400)     # 3 days
ARCHIVE_AGGREGATE_INTERVAL = config.get('ARCHIVE_AGGREGATE_INTERVAL', 15 * 60)   # 15 minutes
TOTAL_RETENTION_SECONDS = config.get('TOTAL_RETENTION_SECONDS', 30 * 86400)      # 30 days

# In-memory pending write buffer (small, bounded)
_buffer = []  # tuples: (ts, temperature, humidity, rpm, fan_pwm, pump_status)
_last_flush_time = 0
_last_aggregate_time = 0
_lock = False  # simple mutex for async safety

# Threshold keys are stored in config.json to avoid duplication
_THRESHOLD_KEYS = [
    'FAN_TARGET_HUMIDITY',
    'PUMP_TRIGGER_HUMIDITY',
    'PUMP_EMERGENCY_OFF',
    'PUMP_SPRAY_DURATION',
    'PUMP_COOLDOWN_MINUTES',
    'NIGHT_START_HOUR',
    'NIGHT_END_HOUR',
]


def _reading_tuple(ts, temperature, humidity, rpm, fan_pwm, pump_status):
    return (
        int(ts),
        float(temperature),
        float(humidity),
        int(rpm),
        int(fan_pwm),
        str(pump_status),
    )


def _tuple_to_dict(reading):
    return {
        'ts': reading[0],
        'temperature': reading[1],
        'humidity': reading[2],
        'rpm': reading[3],
        'fan_pwm': reading[4],
        'pump_status': reading[5],
    }


def _parse_line(line):
    try:
        row = json.loads(line)
        if not isinstance(row, dict):
            return None
        if 'ts' not in row:
            return None
        row['ts'] = int(row.get('ts', 0))
        if row['ts'] <= 0:
            return None
        row['temperature'] = float(row.get('temperature', 0.0))
        row['humidity'] = float(row.get('humidity', 0.0))
        row['rpm'] = int(row.get('rpm', 0))
        row['fan_pwm'] = int(row.get('fan_pwm', 0))
        row['pump_status'] = str(row.get('pump_status', ''))
        if 'sample_count' in row:
            row['sample_count'] = int(row.get('sample_count', 1))
        return row
    except Exception:
        return None


def _append_rows_ndjson(filename, rows):
    if not rows:
        return
    with open(filename, 'a') as f:
        for row in rows:
            f.write(json.dumps(row))
            f.write('\n')


def _replace_file_with_tmp(filename):
    tmp = filename + '.tmp'
    try:
        os.remove(filename)
    except Exception:
        pass
    os.rename(tmp, filename)


def _trim_pending_buffer():
    global _buffer
    max_pending = config.get('MAX_PENDING_POINTS', 256)
    try:
        max_pending = int(max_pending)
    except Exception:
        max_pending = 256
    if max_pending < 32:
        max_pending = 32
    elif max_pending > 1024:
        max_pending = 1024
    if len(_buffer) > max_pending:
        _buffer = _buffer[-max_pending:]


def _trim_archive_file(cutoff_ts):
    if not _exists(ARCHIVE_FILE):
        return

    kept = 0
    with open(ARCHIVE_FILE, 'r') as src, open(ARCHIVE_FILE + '.tmp', 'w') as dst:
        for line in src:
            row = _parse_line(line)
            if not row:
                continue
            if row['ts'] > cutoff_ts:
                dst.write(json.dumps(row))
                dst.write('\n')
                kept += 1

    _replace_file_with_tmp(ARCHIVE_FILE)
    print(f"[STORAGE] Archive trimmed to {kept} entries")


def add_reading(ts, temperature, humidity, rpm, fan_pwm, pump_status):
    """Add a reading to pending buffer and flush/aggregate periodically."""
    global _buffer, _last_flush_time, _last_aggregate_time

    _buffer.append(_reading_tuple(ts, temperature, humidity, rpm, fan_pwm, pump_status))
    _trim_pending_buffer()

    now = time.time()

    flush_interval = config.get('FLUSH_INTERVAL_SECONDS', 300)
    try:
        flush_interval = int(flush_interval)
    except Exception:
        flush_interval = 300
    if flush_interval < 5:
        flush_interval = 5

    if now - _last_flush_time >= flush_interval:
        flush_to_storage()
        _last_flush_time = now

    aggregate_interval = config.get('AGGREGATE_INTERVAL_SECONDS', 600)
    try:
        aggregate_interval = int(aggregate_interval)
    except Exception:
        aggregate_interval = 600
    if aggregate_interval < 30:
        aggregate_interval = 30

    if now - _last_aggregate_time >= aggregate_interval:
        aggregate_old_readings()
        _last_aggregate_time = now


def aggregate_old_readings():
    """
    Aggregate recent readings older than RECENT_RETENTION_SECONDS into archive buckets.
    Operates in streaming mode (line-by-line) to keep RAM usage low.
    """
    global _lock

    if _lock:
        return

    _lock = True
    try:
        # Flush pending rows first so aggregation sees all recent entries
        flush_to_storage()

        if not _exists(RECENT_FILE):
            return

        now = int(time.time())
        recent_cutoff = now - int(RECENT_RETENTION_SECONDS)
        total_cutoff = now - int(TOTAL_RETENTION_SECONDS)

        buckets = {}
        kept_recent = 0
        aggregated_input = 0

        with open(RECENT_FILE, 'r') as src, open(RECENT_FILE + '.tmp', 'w') as dst:
            for line in src:
                row = _parse_line(line)
                if not row:
                    continue

                ts = row['ts']

                # Drop anything outside total retention immediately
                if ts <= total_cutoff:
                    continue

                if ts < recent_cutoff:
                    bucket_idx = (ts // int(ARCHIVE_AGGREGATE_INTERVAL)) * int(ARCHIVE_AGGREGATE_INTERVAL)
                    stats = buckets.get(bucket_idx)
                    if stats is None:
                        # count, sum_temp, sum_humidity, sum_rpm, sum_pwm, last_status
                        buckets[bucket_idx] = [1, row['temperature'], row['humidity'], row['rpm'], row['fan_pwm'], row['pump_status']]
                    else:
                        stats[0] += 1
                        stats[1] += row['temperature']
                        stats[2] += row['humidity']
                        stats[3] += row['rpm']
                        stats[4] += row['fan_pwm']
                        stats[5] = row['pump_status']
                    aggregated_input += 1
                else:
                    dst.write(json.dumps(row))
                    dst.write('\n')
                    kept_recent += 1

        _replace_file_with_tmp(RECENT_FILE)

        if buckets:
            aggregated_rows = []
            for bucket_ts in sorted(buckets.keys()):
                stats = buckets[bucket_ts]
                count = stats[0]
                aggregated_rows.append({
                    'ts': int(bucket_ts),
                    'temperature': round(stats[1] / count, 2),
                    'humidity': round(stats[2] / count, 2),
                    'rpm': int(stats[3] / count),
                    'fan_pwm': int(stats[4] / count),
                    'pump_status': stats[5],
                    'sample_count': count,
                })

            _append_rows_ndjson(ARCHIVE_FILE, aggregated_rows)
            _trim_archive_file(total_cutoff)
            print(f"[STORAGE] Aggregated {aggregated_input} readings into {len(aggregated_rows)} buckets")
        else:
            # Still trim archive to total retention
            _trim_archive_file(total_cutoff)

        print(f"[STORAGE] Recent file kept {kept_recent} high-resolution entries")
        gc.collect()

    except Exception as e:
        print(f"[STORAGE] Error aggregating readings: {e}")
    finally:
        _lock = False


def flush_to_storage():
    """Append pending buffer to recent NDJSON file without full-file rewrite."""
    global _buffer, _lock

    if _lock or not _buffer:
        return

    _lock = True
    try:
        rows = [_tuple_to_dict(r) for r in _buffer]
        _append_rows_ndjson(RECENT_FILE, rows)
        print(f"[STORAGE] Flushed {len(rows)} readings to {RECENT_FILE}")
        _buffer = []
    except Exception as e:
        print(f"[STORAGE] Error flushing to storage: {e}")
    finally:
        _lock = False


def load_from_storage():
    """Initialize storage subsystem (streaming mode keeps data on disk)."""
    global _buffer
    try:
        _buffer = []
        print("[STORAGE] Streaming storage initialized")
    except Exception as e:
        print(f"[STORAGE] Error loading storage: {e}")
        _buffer = []


def _append_filtered_with_limit(result, row, start_ts, end_ts, limit):
    ts = row.get('ts', 0)
    if start_ts is not None and ts < start_ts:
        return
    if end_ts is not None and ts > end_ts:
        return

    result.append(row)
    if limit and len(result) > (limit + 64):
        # Trim in chunks to avoid expensive per-item pop(0)
        del result[:len(result) - limit]


def _append_pending_tuple_with_limit(result, tup, start_ts, end_ts, limit):
    ts = tup[0]
    if start_ts is not None and ts < start_ts:
        return
    if end_ts is not None and ts > end_ts:
        return

    # Convert only rows that pass filtering
    result.append({
        'ts': ts,
        'temperature': tup[1],
        'humidity': tup[2],
        'rpm': tup[3],
        'fan_pwm': tup[4],
        'pump_status': tup[5],
    })
    if limit and len(result) > (limit + 64):
        del result[:len(result) - limit]


def get_readings(limit=500, start_ts=None, end_ts=None):
    """
    Get readings from archived and recent storage files in streaming mode.
    Returns combined results sorted by timestamp.
    """
    if limit is not None:
        try:
            limit = int(limit)
        except Exception:
            limit = 500
        if limit < 1:
            limit = 1
        max_limit = config.get('MAX_API_HISTORY_LIMIT', 3000)
        try:
            max_limit = int(max_limit)
        except Exception:
            max_limit = 3000
        if max_limit < 100:
            max_limit = 100
        if limit > max_limit:
            limit = max_limit

    result = []

    # Archive first (older data)
    if _exists(ARCHIVE_FILE):
        try:
            with open(ARCHIVE_FILE, 'r') as f:
                for line in f:
                    row = _parse_line(line)
                    if not row:
                        continue
                    _append_filtered_with_limit(result, row, start_ts, end_ts, limit)
        except Exception as e:
            print(f"[STORAGE] Archive read error: {e}")

    # Then recent file (newer high-resolution data)
    if _exists(RECENT_FILE):
        try:
            with open(RECENT_FILE, 'r') as f:
                for line in f:
                    row = _parse_line(line)
                    if not row:
                        continue
                    _append_filtered_with_limit(result, row, start_ts, end_ts, limit)
        except Exception as e:
            print(f"[STORAGE] Recent read error: {e}")

    # Add pending (not yet flushed) rows
    if _buffer:
        for tup in _buffer:
            _append_pending_tuple_with_limit(result, tup, start_ts, end_ts, limit)

    # Ensure strict timestamp ordering
    result.sort(key=lambda x: x.get('ts', 0))

    # Safety re-apply limit after sort
    if limit and len(result) > limit:
        result = result[-limit:]

    gc.collect()
    return result


def get_thresholds():
    """Get threshold configuration from config.json."""
    all_cfg = config.get_all()
    return {key: all_cfg.get(key) for key in _THRESHOLD_KEYS}


def save_thresholds(thresholds):
    """Save threshold configuration to config.json."""
    try:
        for key in _THRESHOLD_KEYS:
            if key in thresholds:
                config.set(key, thresholds[key])
        print("[STORAGE] Thresholds saved to config.json")
    except Exception as e:
        print(f"[STORAGE] Error saving thresholds: {e}")


def clear_history():
    """Clear all historical data."""
    global _buffer
    _buffer = []

    for filename in (RECENT_FILE, ARCHIVE_FILE):
        try:
            os.remove(filename)
        except Exception:
            pass

    print("[STORAGE] History cleared")
    gc.collect()


# Initialize on module import
load_from_storage()
