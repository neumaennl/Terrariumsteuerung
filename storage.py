"""
JSON file-based data storage for terrarium readings.
Implements tiered storage with circular buffer and periodic aggregation.
"""

import json
import os
import time
import config
from config import get

# Storage settings
# MicroPython safe existence check (os.path may not exist)
def _exists(path):
    try:
        os.stat(path)
        return True
    except Exception:
        return False

STORAGE_FILE = 'history.json'
ARCHIVE_FILE = 'archive.json'

# Tiered storage configuration (in seconds)
RECENT_RETENTION_SECONDS = get('RECENT_RETENTION_SECONDS', 3 * 86400)     # 3 days
ARCHIVE_AGGREGATE_INTERVAL = get('ARCHIVE_AGGREGATE_INTERVAL', 15 * 60)   # 15 minutes
TOTAL_RETENTION_SECONDS = get('TOTAL_RETENTION_SECONDS', 30 * 86400)      # 30 days

# In-memory buffer
_buffer = []
_archive_buffer = []
_last_flush_time = 0
_last_aggregate_time = 0
_lock = False  # Simple mutex for async safety

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

class Reading:
    """Simple data structure for sensor readings."""
    def __init__(self, ts, temperature, humidity, rpm, fan_pwm, pump_status):
        self.ts = int(ts)
        self.temperature = float(temperature)
        self.humidity = float(humidity)
        self.rpm = int(rpm)
        self.fan_pwm = int(fan_pwm)
        self.pump_status = str(pump_status)
    
    def to_dict(self):
        return {
            'ts': self.ts,
            'temperature': self.temperature,
            'humidity': self.humidity,
            'rpm': self.rpm,
            'fan_pwm': self.fan_pwm,
            'pump_status': self.pump_status
        }


def add_reading(ts, temperature, humidity, rpm, fan_pwm, pump_status):
    """Add a reading to the buffer. Periodically flushes to storage."""
    global _buffer, _last_flush_time, _last_aggregate_time
    
    reading = Reading(ts, temperature, humidity, rpm, fan_pwm, pump_status)
    _buffer.append(reading)
    
    # Trim buffer if too large (keep last N points)
    max_points = get('MAX_HISTORY_POINTS', 10000)
    if len(_buffer) > max_points:
        _buffer = _buffer[-max_points:]
    
    now = time.time()
    
    # Periodically flush to disk
    flush_interval = get('FLUSH_INTERVAL_SECONDS', 300)
    if now - _last_flush_time > flush_interval:
        flush_to_storage()
        _last_flush_time = now
    
    # Periodically aggregate old readings
    aggregate_interval = get('AGGREGATE_INTERVAL_SECONDS', 600)
    if now - _last_aggregate_time > aggregate_interval:
        aggregate_old_readings()
        _last_aggregate_time = now


def aggregate_old_readings():
    """
    Convert readings older than RECENT_RETENTION_SECONDS to aggregated summaries.
    Grouped by ARCHIVE_AGGREGATE_INTERVAL (e.g., 15-minute buckets).
    """
    global _buffer, _archive_buffer, _lock
    
    if _lock or not _buffer:
        return
    
    _lock = True
    try:
        now = time.time()
        cutoff_time = now - RECENT_RETENTION_SECONDS
        
        # Find readings to aggregate
        old_readings = [r for r in _buffer if r.ts < cutoff_time]
        
        if not old_readings:
            return
        
        # Group into buckets
        buckets = {}
        for reading in old_readings:
            # Calculate bucket timestamp (e.g., round to nearest 15 minutes)
            bucket_idx = (reading.ts // ARCHIVE_AGGREGATE_INTERVAL) * ARCHIVE_AGGREGATE_INTERVAL
            
            if bucket_idx not in buckets:
                buckets[bucket_idx] = {
                    'readings': [],
                    'ts': bucket_idx
                }
            buckets[bucket_idx]['readings'].append(reading)
        
        # Aggregate each bucket
        aggregated = []
        for bucket_idx in sorted(buckets.keys()):
            bucket = buckets[bucket_idx]
            readings = bucket['readings']
            count = len(readings)
            
            # Calculate averages
            avg_temp = sum(r.temperature for r in readings) / count
            avg_humidity = sum(r.humidity for r in readings) / count
            avg_rpm = sum(r.rpm for r in readings) // count
            avg_pwm = sum(r.fan_pwm for r in readings) // count
            last_status = readings[-1].pump_status
            
            aggregated.append({
                'ts': bucket_idx,
                'temperature': round(avg_temp, 2),
                'humidity': round(avg_humidity, 2),
                'rpm': avg_rpm,
                'fan_pwm': avg_pwm,
                'pump_status': last_status,
                'sample_count': count  # Track how many readings were averaged
            })
        
        # Add to archive buffer
        _archive_buffer.extend(aggregated)
        
        # Trim old archive entries (keep only what's needed)
        total_cutoff = now - TOTAL_RETENTION_SECONDS
        _archive_buffer = [r for r in _archive_buffer if r['ts'] > total_cutoff]
        
        # Remove aggregated readings from recent buffer
        _buffer = [r for r in _buffer if r.ts >= cutoff_time]
        
        print(f"[STORAGE] Aggregated {len(old_readings)} readings into {len(aggregated)} buckets")
        
        # Save immediately
        flush_archive_to_storage()
        
    except Exception as e:
        print(f"[STORAGE] Error aggregating readings: {e}")
    finally:
        _lock = False


def flush_archive_to_storage():
    """Write archive buffer to archive.json file."""
    try:
        if not _archive_buffer:
            return
        
        data = {
            'readings': _archive_buffer,
            'last_update': time.time(),
            'aggregate_interval': ARCHIVE_AGGREGATE_INTERVAL,
            'note': 'Aggregated readings (averaged into buckets)'
        }
        
        _atomic_write_json(ARCHIVE_FILE, data)
        print(f"[STORAGE] Archive flushed with {len(_archive_buffer)} entries")
    except Exception as e:
        print(f"[STORAGE] Error flushing archive: {e}")


def _atomic_write_json(filename, data):
    """Safely write JSON data to a file using atomic operations (write .tmp → rename)."""
    try:
        # Write to temp file first
        with open(filename + '.tmp', 'w') as f:
            json.dump(data, f)
        
        # Replace old file
        try:
            os.remove(filename)
        except:
            pass
        os.rename(filename + '.tmp', filename)
        
        return True
    except Exception as e:
        print(f"[STORAGE] Error in atomic write to {filename}: {e}")
        return False


def flush_to_storage():
    """Write buffer to storage file without blocking."""
    global _buffer, _lock
    
    if _lock or not _buffer:
        return
    
    _lock = True
    try:
        # Trim old entries from recent buffer (keep only recent)
        cutoff_time = time.time() - RECENT_RETENTION_SECONDS
        _buffer = [r for r in _buffer if r.ts > cutoff_time]
        
        # Write to file
        data = {
            'readings': [r.to_dict() for r in _buffer],
            'last_update': time.time(),
            'retention_seconds': RECENT_RETENTION_SECONDS,
            'note': 'Recent high-resolution readings'
        }
        
        # Use atomic write helper
        _atomic_write_json(STORAGE_FILE, data)
        print(f"[STORAGE] Flushed {len(_buffer)} recent readings to disk")
    except Exception as e:
        # If DB failed, keep buffer to try later
        print(f"[STORAGE] Error flushing to storage: {e}")
    finally:
        _lock = False



def load_from_storage():
    """Load readings from both recent and archive files into memory buffers."""
    global _buffer, _archive_buffer
    
    try:
        # Load recent readings
        if _exists(STORAGE_FILE):
            with open(STORAGE_FILE, 'r') as f:
                data = json.load(f)
                readings = data.get('readings', [])
                _buffer = []
                for r in readings:
                    reading = Reading(
                        r['ts'], r['temperature'], r['humidity'],
                        r['rpm'], r['fan_pwm'], r['pump_status']
                    )
                    _buffer.append(reading)
                print(f"[STORAGE] Loaded {len(_buffer)} recent readings from disk")
        
        # Load archived/aggregated readings
        if _exists(ARCHIVE_FILE):
            with open(ARCHIVE_FILE, 'r') as f:
                data = json.load(f)
                _archive_buffer = data.get('readings', [])
                print(f"[STORAGE] Loaded {len(_archive_buffer)} archived readings from disk")
    
    except Exception as e:
        print(f"[STORAGE] Error loading from storage: {e}")
        _buffer = []
        _archive_buffer = []


def get_readings(limit=500, start_ts=None, end_ts=None):
    """
    Get readings from both recent and archive buffers.
    Returns combined results, optionally filtered by time range.
    Recent readings (high resolution) are kept separate from archived (aggregated).
    """
    # Get recent readings
    recent = [r.to_dict() for r in _buffer.copy()]
    
    # Get archived readings
    archived = []
    for r in _archive_buffer:
        if isinstance(r, dict):
            archived.append(r)
        else:
            # Handle if it's a Reading object (shouldn't happen, but just in case)
            archived.append(r.to_dict() if hasattr(r, 'to_dict') else r)

    result = archived + recent

    print(f"[STORAGE] Fetching readings: {len(recent)} recent, {len(archived)} archived before filtering")
    print(f"[STORAGE] Time range: start={start_ts}, end={end_ts}")
    print(f"[STORAGE] Sample readings: {result[:3]} ... {result[-3:]}")
    
    # Apply time filters
    if start_ts is not None:
        result = [r for r in result if r['ts'] >= start_ts]
    if end_ts is not None:
        result = [r for r in result if r['ts'] <= end_ts]

    print(f"[STORAGE] Readings after filtering: {len(result)} total (archived + recent)")
    
    # Sort by timestamp
    result = sorted(result, key=lambda x: x['ts'])
    
    # Apply limit if specified
    if limit:
        result = result[-limit:]
    
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
    global _buffer, _archive_buffer
    _buffer = []
    _archive_buffer = []
    try:
        os.remove(STORAGE_FILE)
    except:
        pass
    try:
        os.remove(ARCHIVE_FILE)
    except:
        pass
    print("[STORAGE] History cleared")


# Load on module import
load_from_storage()
