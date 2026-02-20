"""
NTP time synchronization for ESP32-C3.
Syncs system time with NTP server after WiFi connection.
"""

import ntptime
import time
import config


_raw_print = print

# MicroPython RTC starts at 2000-01-01; treat earlier years as "not set"
MIN_VALID_YEAR = 2023
EPOCH_OFFSET = 946684800  # 1970-01-01 to 2000-01-01 in seconds


def _get_int_config(key, default):
    try:
        return int(config.get(key, default))
    except Exception:
        return default


def _is_leap_year(year):
    return (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)


def _days_in_month(year, month):
    if month == 2:
        return 29 if _is_leap_year(year) else 28
    if month in (4, 6, 9, 11):
        return 30
    return 31


def _weekday(year, month, day):
    # Sakamoto algorithm: returns 0=Sunday ... 6=Saturday
    month_offsets = (0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4)
    calc_year = year
    if month < 3:
        calc_year -= 1
    weekday_sun0 = (calc_year + calc_year // 4 - calc_year // 100 + calc_year // 400 + month_offsets[month - 1] + day) % 7
    # Convert to 0=Monday ... 6=Sunday to align with MicroPython localtime tuple
    return (weekday_sun0 - 1) % 7


def _last_sunday(year, month):
    last_day = _days_in_month(year, month)
    for day in range(last_day, last_day - 7, -1):
        if _weekday(year, month, day) == 6:
            return day
    return last_day


def _is_dst_eu_from_utc(utc_tuple):
    # EU DST: starts last Sunday in March at 01:00 UTC,
    # ends last Sunday in October at 01:00 UTC.
    year, month, day, hour = utc_tuple[0], utc_tuple[1], utc_tuple[2], utc_tuple[3]

    if month < 3 or month > 10:
        return False
    if month > 3 and month < 10:
        return True

    if month == 3:
        change_day = _last_sunday(year, 3)
        if day > change_day:
            return True
        if day < change_day:
            return False
        return hour >= 1

    change_day = _last_sunday(year, 10)
    if day < change_day:
        return True
    if day > change_day:
        return False
    return hour < 1


def get_local_offset_seconds(utc_ts=None):
    """Return local offset in seconds for a given UTC timestamp (RTC epoch)."""
    if utc_ts is None:
        utc_ts = int(time.time())

    base_offset_minutes = _get_int_config('TIMEZONE_UTC_OFFSET_MINUTES', 60)
    offset_seconds = base_offset_minutes * 60

    dst_enabled = bool(config.get('DST_ENABLED', True))
    dst_rule = str(config.get('DST_RULE', 'EU')).upper()

    if dst_enabled and dst_rule == 'EU':
        try:
            utc_tuple = time.localtime(int(utc_ts))
            if _is_dst_eu_from_utc(utc_tuple):
                offset_seconds += 3600
        except Exception:
            pass

    return offset_seconds


def get_localtime(utc_ts=None):
    """Return local time tuple derived from UTC RTC time."""
    if utc_ts is None:
        utc_ts = int(time.time())
    local_ts = int(utc_ts) + int(get_local_offset_seconds(utc_ts))
    return time.localtime(local_ts)


def to_unix_timestamp(rtc_ts=None):
    """Convert RTC timestamp (seconds since 2000-01-01 UTC) to Unix timestamp."""
    if rtc_ts is None:
        rtc_ts = int(time.time())
    return int(rtc_ts) + EPOCH_OFFSET


def get_local_unix_timestamp(utc_ts=None):
    """Return Unix timestamp for local wall-clock time."""
    if utc_ts is None:
        utc_ts = int(time.time())
    return to_unix_timestamp(int(utc_ts) + int(get_local_offset_seconds(utc_ts)))


def format_local_datetime(utc_ts=None):
    """Format local date/time as YYYY-MM-DD HH:MM:SS."""
    current = get_localtime(utc_ts)
    return f"{current[0]}-{current[1]:02d}-{current[2]:02d} {current[3]:02d}:{current[4]:02d}:{current[5]:02d}"


def log_print(*args, sep=' ', end='\n'):
    """Print with local timestamp prefix."""
    message = sep.join([str(arg) for arg in args])
    _raw_print(f"[{format_local_datetime()}] {message}", end=end)


def sync_time(host='pool.ntp.org', timeout_sec=5):
    """
    Synchronize system time with NTP server.
    
    Args:
        host: NTP server hostname (default: pool.ntp.org)
        timeout_sec: Timeout for NTP request
    
    Returns:
        True if successful, False otherwise
    """
    try:
        log_print(f"[NTP] Synchronizing time with {host}...")
        
        # Perform NTP sync
        ntptime.host = host
        ntptime.settime()
        
        # Print confirmation
        current = get_localtime()
        log_print(
            f"[NTP] Time synchronized: {current[0]}-{current[1]:02d}-{current[2]:02d} "
            f"{current[3]:02d}:{current[4]:02d}:{current[5]:02d}"
        )

        # Validate year (avoid false positives when RTC is still at epoch)
        if current[0] < MIN_VALID_YEAR:
            log_print(f"[NTP] Invalid year after sync: {current[0]} (expected >= {MIN_VALID_YEAR})")
            return False

        return True
        
    except OSError as e:
        log_print(f"[NTP] Failed to sync: {e}")
        return False
    except Exception as e:
        log_print(f"[NTP] Unexpected error: {e}")
        return False


def get_time_string():
    """Get current time as formatted string."""
    try:
        return format_local_datetime()
    except:
        return "Unknown"


def is_time_set():
    """Check if system time appears to be set (not RTC epoch)."""
    try:
        current = get_localtime()
        # MicroPython RTC defaults to year 2000; require a sane year
        return current[0] >= MIN_VALID_YEAR
    except:
        return False


def get_current_hour():
    """Get current hour (0-23)."""
    try:
        return get_localtime()[3]
    except:
        return 0
