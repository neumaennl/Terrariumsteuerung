"""
NTP time synchronization for ESP32-C3.
Syncs system time with NTP server after WiFi connection.
"""

import ntptime
import time

# MicroPython RTC starts at 2000-01-01; treat earlier years as "not set"
MIN_VALID_YEAR = 2023


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
        print(f"[NTP] Synchronizing time with {host}...")
        
        # Perform NTP sync
        ntptime.host = host
        ntptime.settime()
        
        # Print confirmation
        current = time.localtime()
        print(f"[NTP] Time synchronized: {current[0]}-{current[1]:02d}-{current[2]:02d} "
            f"{current[3]:02d}:{current[4]:02d}:{current[5]:02d}")

        # Validate year (avoid false positives when RTC is still at epoch)
        if current[0] < MIN_VALID_YEAR:
            print(f"[NTP] Invalid year after sync: {current[0]} (expected >= {MIN_VALID_YEAR})")
            return False

        return True
        
    except OSError as e:
        print(f"[NTP] Failed to sync: {e}")
        return False
    except Exception as e:
        print(f"[NTP] Unexpected error: {e}")
        return False


def get_time_string():
    """Get current time as formatted string."""
    try:
        current = time.localtime()
        return f"{current[0]}-{current[1]:02d}-{current[2]:02d} " \
               f"{current[3]:02d}:{current[4]:02d}:{current[5]:02d}"
    except:
        return "Unknown"


def is_time_set():
    """Check if system time appears to be set (not RTC epoch)."""
    try:
        current = time.localtime()
        # MicroPython RTC defaults to year 2000; require a sane year
        return current[0] >= MIN_VALID_YEAR
    except:
        return False


def get_current_hour():
    """Get current hour (0-23)."""
    try:
        return time.localtime()[3]
    except:
        return 0
