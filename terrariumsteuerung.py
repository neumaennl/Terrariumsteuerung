import os
import time
import datetime
import threading
import sqlite3
import logging

# Third-party hardware libraries
import pigpio
import smbus2
import bme280

# Logging
logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# --- KONFIGURATION ---

# Pins (BCM / GPIO Nummern)
PIN_PWM_FAN = 18    # Pin 12
PIN_RPM_FAN = 17    # Pin 11
PIN_RELAY_FAN = 24  # Pin 18
PIN_RELAY_PUMP = 23 # Pin 16

# I2C Settings
I2C_PORT = 1
I2C_ADDRESS = 0x76 

# Lüfter Settings
FAN_TARGET_HUMIDITY = 80.0
FAN_PWM_FREQ = 25000 

# Beregnungsanlage Settings
PUMP_TRIGGER_HUMIDITY = 60.0  # Unter 60% geht es los
PUMP_EMERGENCY_OFF = 85.0     # Über 85% Not-Aus
PUMP_SPRAY_DURATION = 15      # Sekunden Sprühdauer
PUMP_COOLDOWN_MINUTES = 15    # Minuten Pause nach dem Sprühen
NIGHT_START_HOUR = 22         # Ab 19 Uhr Ruhe
NIGHT_END_HOUR = 8            # Bis 8 Uhr Ruhe

# Globale Variablen
pulse_count = 0
last_spray_time = 0           # Zeitstempel der letzten Beregnung

# --- Web API State ---
_current_temp = 0.0
_current_humidity = 0.0
_current_rpm = 0
_current_fan_pwm = 0
_current_pump_status = ""

# --- Buffered persistence (reduce SD wear) ---
_reading_buffer = []  # list of tuples (ts, temp, humidity, rpm, fan_pwm, pump_status)
_buffer_last_flush = 0
BUFFER_MAX = 15  # flush when this many entries accumulated
BUFFER_FLUSH_INTERVAL = 15  # seconds

# --- Stop control / GPIO refs ---
_stop_event = threading.Event()
_gpio = None
_rpm_cb = None


def stop():
    """Signal the main loop to stop and perform a clean shutdown (idempotent)."""
    try:
        _stop_event.set()
    except Exception:
        pass
    # Try to perform hardware cleanup if available
    try:
        if _rpm_cb is not None:
            _rpm_cb.cancel()
    except Exception:
        pass
    try:
        if _gpio is not None:
            try:
                _gpio.hardware_PWM(PIN_PWM_FAN, FAN_PWM_FREQ, 0)
            except Exception:
                pass
            try:
                _gpio.write(PIN_RELAY_FAN, 0)
                _gpio.write(PIN_RELAY_PUMP, 0)
            except Exception:
                pass
            try:
                _gpio.stop()
            except Exception:
                pass
    except Exception:
        pass
    # Flush pending readings
    try:
        flush_readings()
    except Exception:
        pass

def is_stop_requested():
    return _stop_event.is_set()

import sqlite3
import os

# --- FUNKTIONEN ---
DB_PATH = os.path.join(os.path.dirname(__file__), 'terrarium.db')

def get_db_path():
    return DB_PATH


def _get_conn():
    # Create a short-lived connection per operation to avoid threading issues
    conn = sqlite3.connect(DB_PATH, timeout=10)
    # Use WAL and reasonable sync to reduce frequentfsyncs while keeping decent durability
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA wal_autocheckpoint=1000')
    except Exception:
        pass
    return conn


def flush_readings():
    global _reading_buffer
    if not _reading_buffer:
        return
    conn = _get_conn()
    cur = conn.cursor()
    try:
        cur.executemany('INSERT OR REPLACE INTO readings (ts, temperature, humidity, rpm, fan_pwm, pump_status) VALUES (?, ?, ?, ?, ?, ?)', _reading_buffer)
        conn.commit()
        # Keep DB size bounded: keep last 10000 rows
        try:
            cur.execute('DELETE FROM readings WHERE ts < (SELECT ts FROM readings ORDER BY ts DESC LIMIT 1 OFFSET 9999)')
            conn.commit()
        except Exception:
            pass
        _reading_buffer = []
    except Exception:
        # If DB failed, keep buffer to try later
        pass
    finally:
        conn.close()


def buffer_reading(ts, temperature, humidity, rpm, fan_pwm, pump_status):
    global _reading_buffer, _buffer_last_flush
    _reading_buffer.append((int(ts), temperature, humidity, rpm, int(fan_pwm), pump_status))
    now = time.time()
    if len(_reading_buffer) >= BUFFER_MAX or (now - _buffer_last_flush) >= BUFFER_FLUSH_INTERVAL:
        flush_readings()
        _buffer_last_flush = now


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
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS thresholds (
            name TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()
    conn.close()


def save_reading(ts, temperature, humidity, rpm, fan_pwm, pump_status):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute('INSERT OR REPLACE INTO readings (ts, temperature, humidity, rpm, fan_pwm, pump_status) VALUES (?, ?, ?, ?, ?, ?)',
                (int(ts), temperature, humidity, rpm, int(fan_pwm), pump_status))
    conn.commit()
    try:
        # Keep DB size bounded: keep last 10000 rows
        cur.execute('DELETE FROM readings WHERE ts < (SELECT ts FROM readings ORDER BY ts DESC LIMIT 1 OFFSET 9999)')
        conn.commit()
    except Exception:
        pass
    conn.close()


def save_threshold(name, value):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute('INSERT OR REPLACE INTO thresholds (name, value) VALUES (?, ?)', (name, str(value)))
    conn.commit()
    conn.close()
    logger.info("Saved threshold %s=%s", name, value)


def load_thresholds():
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute('SELECT name, value FROM thresholds')
    rows = cur.fetchall()
    conn.close()
    for name, value in rows:
        try:
            if name in ('PUMP_SPRAY_DURATION', 'PUMP_COOLDOWN_MINUTES', 'NIGHT_START_HOUR', 'NIGHT_END_HOUR'):
                v = int(value)
            else:
                v = float(value)
        except Exception:
            v = value
        # use internal setter to avoid double persistence loop
        _apply_threshold_local(name, v)
        logger.info("Loaded threshold %s=%s", name, v)


def _apply_threshold_local(name, value):
    """Set threshold only in-memory without persisting (used by load)."""
    global FAN_TARGET_HUMIDITY, PUMP_TRIGGER_HUMIDITY, PUMP_EMERGENCY_OFF, PUMP_SPRAY_DURATION, PUMP_COOLDOWN_MINUTES, NIGHT_START_HOUR, NIGHT_END_HOUR
    if name == 'FAN_TARGET_HUMIDITY':
        FAN_TARGET_HUMIDITY = float(value)
    elif name == 'PUMP_TRIGGER_HUMIDITY':
        PUMP_TRIGGER_HUMIDITY = float(value)
    elif name == 'PUMP_EMERGENCY_OFF':
        PUMP_EMERGENCY_OFF = float(value)
    elif name == 'PUMP_SPRAY_DURATION':
        PUMP_SPRAY_DURATION = int(value)
    elif name == 'PUMP_COOLDOWN_MINUTES':
        PUMP_COOLDOWN_MINUTES = int(value)
    elif name == 'NIGHT_START_HOUR':
        NIGHT_START_HOUR = int(value)
    elif name == 'NIGHT_END_HOUR':
        NIGHT_END_HOUR = int(value)


def get_history(limit=500):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute('SELECT ts, temperature, humidity, rpm, fan_pwm, pump_status FROM readings ORDER BY ts DESC LIMIT ?', (limit,))
    rows = cur.fetchall()
    conn.close()
    # Return in ascending order
    return [dict(ts=r[0], temperature=r[1], humidity=r[2], rpm=r[3], fan_pwm=r[4], pump_status=r[5]) for r in reversed(rows)]

# Initialize DB and load thresholds (safe to call on import)
try:
    init_db()
    load_thresholds()
except Exception:
    pass

def rpm_callback(gpio, level, tick):
    global pulse_count
    pulse_count += 1

def is_night_time():
    now = datetime.datetime.now()
    # Beispiel: Wenn 19 <= Stunde ODER Stunde < 8
    if now.hour >= NIGHT_START_HOUR or now.hour < NIGHT_END_HOUR:
        return True
    return False

def main():
    global pulse_count, last_spray_time, _current_temp, _current_humidity, _current_rpm, _current_fan_pwm, _current_pump_status
    
    # 1. Verbindung herstellen
    gpio = pigpio.pi()
    if not gpio.connected:
        logger.error("Konnte nicht zum pigpio Daemon verbinden.")
        return

    # 2. BME280 Setup
    try:
        bus = smbus2.SMBus(I2C_PORT)
        calibration_params = bme280.load_calibration_params(bus, I2C_ADDRESS)
    except Exception:
        logger.exception("BME280 initialisierung fehlgeschlagen")
        return

    # 3. Pin Setup
    gpio.set_mode(PIN_RELAY_FAN, pigpio.OUTPUT)
    gpio.set_mode(PIN_RELAY_PUMP, pigpio.OUTPUT)
    gpio.write(PIN_RELAY_FAN, 0)
    gpio.write(PIN_RELAY_PUMP, 0) # Pumpe sicher aus

    # RPM Input mit Pull-Up (Wichtig für Open Collector!)
    gpio.set_mode(PIN_RPM_FAN, pigpio.INPUT)
    gpio.set_pull_up_down(PIN_RPM_FAN, pigpio.PUD_UP)
    rpm_cb = gpio.callback(PIN_RPM_FAN, pigpio.FALLING_EDGE, rpm_callback)
    # Store references for external stop/cleanup
    global _gpio, _rpm_cb
    _gpio = gpio
    _rpm_cb = rpm_cb

    logger.info("--- Terrarium Controller gestartet ---")
    logger.info(f"Ziel-Feuchte Lüfter: {FAN_TARGET_HUMIDITY}%")
    logger.info(f"Pumpe aktiv unter:   {PUMP_TRIGGER_HUMIDITY}% (Tagsüber)")

    try:
        while not _stop_event.is_set():
            # A) DATEN LESEN
            data = bme280.sample(bus, I2C_ADDRESS, calibration_params)
            humidity = data.humidity
            temp = data.temperature
            
            # Aktuelle Zeit
            now_ts = time.time()
            is_night = is_night_time()

            # B) LÜFTER STEUERUNG (Priorität: Schimmelvermeidung)
            # Logik: (Ist - Ziel) / (100 - Ziel) * 100. Beispiel: (85 - 80) / (100 - 80) * 100 = 25% PWM
            fan_pwm_val = max(0, humidity - FAN_TARGET_HUMIDITY) / (100 - FAN_TARGET_HUMIDITY) * 100
            if fan_pwm_val > 100: fan_pwm_val = 100

            # Umrechnung für pigpio (0 - 1M)
            duty = int((fan_pwm_val / 100.0) * 1000000)
            if fan_pwm_val > 0:
                gpio.write(PIN_RELAY_FAN, 1) # Strom an
                gpio.hardware_PWM(PIN_PWM_FAN, FAN_PWM_FREQ, duty)
            else:
                gpio.hardware_PWM(PIN_PWM_FAN, FAN_PWM_FREQ, 0)
                gpio.write(PIN_RELAY_FAN, 0) # Strom aus

            # C) RPM BERECHNUNG
            current_pulses = pulse_count
            pulse_count = 0
            rpm = 0
            if current_pulses > 0:
                rpm = int((current_pulses * 60) / 2)

            # D) BEREGNUNGSANLAGE LOGIK
            pump_status = "AUS"
            # Sicherheits-Check: Ist Pumpe gerade an?
            is_pump_physically_on = (gpio.read(PIN_RELAY_PUMP) == 1)
            # Laufende Sprühung kontrollieren
            if is_pump_physically_on:
                # Not-Aus Check (zu hohe Luftfeuchtigkeit)
                if humidity >= PUMP_EMERGENCY_OFF:
                    gpio.write(PIN_RELAY_PUMP, 0)
                    pump_status = "NOT-AUS (Zu feucht)"
                else:
                    # Wie lange läuft sie schon?
                    duration_on = now_ts - last_spray_time
                    if duration_on >= PUMP_SPRAY_DURATION: # Zeit abgelaufen
                        gpio.write(PIN_RELAY_PUMP, 0)
                        pump_status = "AUS (Sprühstoß beendet)"
                    else:
                        pump_status = f"AN ({int(duration_on)}s / {PUMP_SPRAY_DURATION}s)"

            # Neue Sprühung starten?
            else:
                # Bedingungen:
                # - Nicht Nacht
                # - Zu trocken
                # - Cooldown abgelaufen (Zeit seit letztem Start > Cooldown + Sprühdauer)
                time_since_last = now_ts - last_spray_time
                cooldown_sec = PUMP_COOLDOWN_MINUTES * 60
                if is_night:
                    pump_status = "PAUSE (Nachtmodus)"
                elif time_since_last < cooldown_sec:
                    remaining = int((cooldown_sec - time_since_last) / 60)
                    pump_status = f"PAUSE (Cooldown: {remaining} min)"
                elif humidity < PUMP_TRIGGER_HUMIDITY:
                    gpio.write(PIN_RELAY_PUMP, 1)
                    last_spray_time = now_ts
                    pump_status = "STARTET SPRÜHEN"
                else:
                    pump_status = "BEREIT (Feuchte OK)"

            # --- Web API State Update ---
            _current_temp = temp
            _current_humidity = humidity
            _current_rpm = rpm
            _current_fan_pwm = int(fan_pwm_val)
            _current_pump_status = pump_status

            # Buffer reading (flush periodically to reduce SD writes)
            try:
                buffer_reading(now_ts, temp, humidity, rpm, int(fan_pwm_val), pump_status)
            except Exception:
                pass

            # E) AUSGABE (debug)
            logger.debug(f"Temp: {temp:.1f}C | Feuchte: {humidity:.1f}% | RPM: {rpm}")
            logger.debug(f"Lüfter: {int(fan_pwm_val)}% | Pumpe: {pump_status}")
            logger.debug('-' * 40)

            time.sleep(1.0) # 1 Sekunde Takt

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, stopping...")
        _stop_event.set()
    except Exception:
        logger.exception("Unhandled exception in main")
    finally:
        # Ensure cleanup and flush on any exit path
        try:
            flush_readings()
        except Exception:
            pass
        try:
            if gpio is not None:
                try:
                    gpio.hardware_PWM(PIN_PWM_FAN, FAN_PWM_FREQ, 0)
                except Exception:
                    pass
                try:
                    gpio.write(PIN_RELAY_FAN, 0)
                    gpio.write(PIN_RELAY_PUMP, 0)
                except Exception:
                    pass
                try:
                    if _rpm_cb is not None:
                        _rpm_cb.cancel()
                except Exception:
                    pass
                try:
                    gpio.stop()
                except Exception:
                    pass
        except Exception:
            pass
        
def get_temperature():
    return _current_temp

def get_humidity():
    return _current_humidity

def get_rpm():
    return _current_rpm

def get_fan_pwm():
    return _current_fan_pwm

def get_pump_status():
    return _current_pump_status

# Threshold getter/setter
def set_threshold(name, value, persist=True):
    """Set threshold in memory and optionally persist to DB.
    Use persist=False to avoid writing back while loading from DB."""
    global FAN_TARGET_HUMIDITY, PUMP_TRIGGER_HUMIDITY, PUMP_EMERGENCY_OFF, PUMP_SPRAY_DURATION, PUMP_COOLDOWN_MINUTES, NIGHT_START_HOUR, NIGHT_END_HOUR
    if name == 'FAN_TARGET_HUMIDITY':
        FAN_TARGET_HUMIDITY = float(value)
    elif name == 'PUMP_TRIGGER_HUMIDITY':
        PUMP_TRIGGER_HUMIDITY = float(value)
    elif name == 'PUMP_EMERGENCY_OFF':
        PUMP_EMERGENCY_OFF = float(value)
    elif name == 'PUMP_SPRAY_DURATION':
        PUMP_SPRAY_DURATION = int(value)
    elif name == 'PUMP_COOLDOWN_MINUTES':
        PUMP_COOLDOWN_MINUTES = int(value)
    elif name == 'NIGHT_START_HOUR':
        NIGHT_START_HOUR = int(value)
    elif name == 'NIGHT_END_HOUR':
        NIGHT_END_HOUR = int(value)
    # Persist if requested
    if persist:
        try:
            save_threshold(name, value)
        except Exception:
            logger.exception('Failed to persist threshold %s', name)

def get_threshold(name):
    if name == 'FAN_TARGET_HUMIDITY':
        return FAN_TARGET_HUMIDITY
    elif name == 'PUMP_TRIGGER_HUMIDITY':
        return PUMP_TRIGGER_HUMIDITY
    elif name == 'PUMP_EMERGENCY_OFF':
        return PUMP_EMERGENCY_OFF
    elif name == 'PUMP_SPRAY_DURATION':
        return PUMP_SPRAY_DURATION
    elif name == 'PUMP_COOLDOWN_MINUTES':
        return PUMP_COOLDOWN_MINUTES
    elif name == 'NIGHT_START_HOUR':
        return NIGHT_START_HOUR
    elif name == 'NIGHT_END_HOUR':
        return NIGHT_END_HOUR
    return None

if __name__ == "__main__":
    main()