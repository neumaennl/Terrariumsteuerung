"""
Configuration management for terrarium controller.
Stores and retrieves settings from JSON file.
"""

import json
import os

# MicroPython: os.path may be missing. Provide a small exists() helper using os.stat
def _exists(path):
    try:
        os.stat(path)
        return True
    except Exception:
        return False


def _log(*args, **kwargs):
    try:
        import ntp_sync
        ntp_sync.log_print(*args, **kwargs)
    except Exception:
        print(*args, **kwargs)

CONFIG_FILE = 'config.json'

# Default configuration values
DEFAULT_CONFIG = {
    # Time zone (base offset without DST)
    'TIMEZONE_UTC_OFFSET_MINUTES': 60,  # CET = UTC+1
    'DST_ENABLED': True,
    'DST_RULE': 'EU',

    # Pins (ESP32-C3 GPIO numbers)
    'PIN_PWM_FAN': 3,          # GPIO3 for PWM
    'PIN_RPM_FAN': 4,          # GPIO4 for RPM input
    'PIN_RELAY_FAN': 2,        # GPIO2 for relay
    'PIN_RELAY_PUMP': 1,       # GPIO1 for pump relay
    'PIN_BUTTON_PUMP_OVERRIDE': 9,  # GPIO9 button (active-low) for manual pump
    
    # I2C for BME280
    'I2C_PORT': 0,             # I2C port 0
    'I2C_SDA': 5,
    'I2C_SCL': 6,
    'BME280_ADDR': 0x76,
    
    # Fan settings
    'FAN_TARGET_HUMIDITY': 80.0,
    'FAN_PWM_FREQ': 5000,      # ESP32 PWM frequency (Hz)
    'RPM_AVG_WINDOW_SECONDS': 2,  # RPM averaging window (seconds)
    
    # Pump settings
    'PUMP_TRIGGER_HUMIDITY': 60.0,
    'PUMP_SPRAY_DURATION': 15,      # seconds
    'PUMP_COOLDOWN_MINUTES': 15,
    'NIGHT_START_HOUR': 19,
    'NIGHT_END_HOUR': 8,
    
    # Sampling
    'SAMPLE_INTERVAL': 30,      # seconds between samples
    
    # Web interface refresh intervals
    'DATA_REFRESH_INTERVAL': 30,        # seconds between live data refreshes
    'HISTORY_REFRESH_INTERVAL': 300,    # seconds between history chart refreshes (5 minutes)
    
    # Tiered storage configuration
    'RECENT_RETENTION_SECONDS': 3 * 86400,     # 3 days of high-resolution
    'ARCHIVE_AGGREGATE_INTERVAL': 15 * 60,     # 15 minutes for aggregated data
    'TOTAL_RETENTION_SECONDS': 30 * 86400,     # 30 days total
    'FLUSH_INTERVAL_SECONDS': 300,             # 5 minutes between disk flushes
    'AGGREGATE_INTERVAL_SECONDS': 600,         # 10 minutes between aggregation passes
    
    # Legacy (for backward compatibility)
    'HISTORY_RETENTION_DAYS': 7,
    'MAX_HISTORY_POINTS': 10000,
    'MAX_PENDING_POINTS': 256,
    'MAX_API_HISTORY_LIMIT': 3000,

    # Runtime logging
    'DEBUG_PRINT_INTERVAL_SECONDS': 30,
    'MEMORY_SAMPLE_INTERVAL_SECONDS': 60,
    'MEMORY_LOG_INTERVAL_SECONDS': 600,
}

# Current configuration (loaded on module import)
_config = DEFAULT_CONFIG.copy()


def load_config():
    """Load configuration from JSON file, or use defaults if missing."""
    global _config
    try:
        if _exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                user_config = json.load(f)
                _config.update(user_config)
    except Exception as e:
        _log(f"[CONFIG] Error loading config: {e}, using defaults")
    return _config.copy()


def save_config():
    """Save current configuration to JSON file."""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(_config, f)
        _log("[CONFIG] Configuration saved")
    except Exception as e:
        _log(f"[CONFIG] Error saving config: {e}")


def get(key, default=None):
    """Get a configuration value."""
    return _config.get(key, default)


def set(key, value):
    """Set a configuration value."""
    global _config
    _config[key] = value
    save_config()


def get_all():
    """Get all configuration."""
    return _config.copy()


def reset_to_defaults():
    """Reset configuration to defaults."""
    global _config
    _config = DEFAULT_CONFIG.copy()
    save_config()


# Load configuration on module import
load_config()
