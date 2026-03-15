"""
Terrarium Controller for ESP32-C3 with MicroPython.
Manages fan, pump, and sensors with async/await pattern.
"""

import machine
import asyncio
import time
from array import array
import config
from config import get
from ntp_sync import get_current_hour, log_print as print
import bme280_float
import sht4x

# --- Thresholds (loaded from storage) ---
FAN_TARGET_HUMIDITY = 80.0
FAN_SHUTOFF_HUMIDITY = 75.0
FAN_NIGHT_START_HOUR = 21
FAN_NIGHT_END_HOUR = 7
PUMP_TRIGGER_HUMIDITY = 60.0
PUMP_SPRAY_DURATION = 15
PUMP_COOLDOWN_MINUTES = 15
PUMP_NIGHT_START_HOUR = 19
PUMP_NIGHT_END_HOUR = 8

# --- Current state for web API ---
_current_temp = 0.0
_current_humidity = 0.0
_current_rpm = 0
_current_fan_pwm = 0
_current_pump_status = ("INIT", "")

# --- Last spray time ---
_last_spray_time = 0

# --- RPM counter ---
_rpm_pulses = 0

# --- Stops & Control ---
_running = False


def _clamp_fan_target(value):
    try:
        numeric = float(value)
    except Exception:
        return FAN_TARGET_HUMIDITY
    return max(1.0, min(99.0, numeric))


def load_thresholds_from_config():
    """Load thresholds from config.json into memory."""
    global FAN_TARGET_HUMIDITY, FAN_SHUTOFF_HUMIDITY, PUMP_TRIGGER_HUMIDITY, PUMP_SPRAY_DURATION
    global PUMP_COOLDOWN_MINUTES, PUMP_NIGHT_START_HOUR, PUMP_NIGHT_END_HOUR
    
    try:
        FAN_TARGET_HUMIDITY = _clamp_fan_target(get('FAN_TARGET_HUMIDITY', FAN_TARGET_HUMIDITY))
        FAN_SHUTOFF_HUMIDITY = float(get('FAN_SHUTOFF_HUMIDITY', FAN_SHUTOFF_HUMIDITY))
        PUMP_TRIGGER_HUMIDITY = float(get('PUMP_TRIGGER_HUMIDITY', PUMP_TRIGGER_HUMIDITY))
        PUMP_SPRAY_DURATION = int(get('PUMP_SPRAY_DURATION', PUMP_SPRAY_DURATION))
        PUMP_COOLDOWN_MINUTES = int(get('PUMP_COOLDOWN_MINUTES', PUMP_COOLDOWN_MINUTES))
        PUMP_NIGHT_START_HOUR = int(get('PUMP_NIGHT_START_HOUR', PUMP_NIGHT_START_HOUR))
        PUMP_NIGHT_END_HOUR = int(get('PUMP_NIGHT_END_HOUR', PUMP_NIGHT_END_HOUR))
        print("[CTRL] Thresholds loaded from config.json")
    except Exception as e:
        print(f"[CTRL] Error loading thresholds: {e}")


def set_threshold_value(name, value):
    """Set threshold and save to config.json."""
    global FAN_TARGET_HUMIDITY, PUMP_TRIGGER_HUMIDITY, PUMP_SPRAY_DURATION
    global PUMP_COOLDOWN_MINUTES, PUMP_NIGHT_START_HOUR, PUMP_NIGHT_END_HOUR
    
    if name == 'FAN_TARGET_HUMIDITY':
        FAN_TARGET_HUMIDITY = _clamp_fan_target(value)
        value = FAN_TARGET_HUMIDITY
    elif name == 'FAN_SHUTOFF_HUMIDITY':
        FAN_SHUTOFF_HUMIDITY = float(value)
        value = FAN_SHUTOFF_HUMIDITY
    elif name == 'PUMP_TRIGGER_HUMIDITY':
        PUMP_TRIGGER_HUMIDITY = float(value)
    elif name == 'PUMP_SPRAY_DURATION':
        PUMP_SPRAY_DURATION = int(value)
    elif name == 'PUMP_COOLDOWN_MINUTES':
        PUMP_COOLDOWN_MINUTES = int(value)
    elif name == 'PUMP_NIGHT_START_HOUR':
        PUMP_NIGHT_START_HOUR = int(value)
    elif name == 'PUMP_NIGHT_END_HOUR':
        PUMP_NIGHT_END_HOUR = int(value)
    
    config.set(name, value)


def get_threshold_value(name):
    """Get current threshold value."""
    if name == 'FAN_TARGET_HUMIDITY':
        return FAN_TARGET_HUMIDITY
    elif name == 'FAN_SHUTOFF_HUMIDITY':
        return FAN_SHUTOFF_HUMIDITY
    elif name == 'PUMP_TRIGGER_HUMIDITY':
        return PUMP_TRIGGER_HUMIDITY
    elif name == 'PUMP_SPRAY_DURATION':
        return PUMP_SPRAY_DURATION
    elif name == 'PUMP_COOLDOWN_MINUTES':
        return PUMP_COOLDOWN_MINUTES
    elif name == 'PUMP_NIGHT_START_HOUR':
        return PUMP_NIGHT_START_HOUR
    elif name == 'PUMP_NIGHT_END_HOUR':
        return PUMP_NIGHT_END_HOUR
    return None


def reset_thresholds_to_defaults():
    """Reset thresholds to defaults and persist them."""
    defaults = config.DEFAULT_CONFIG
    for key in (
        'FAN_TARGET_HUMIDITY',
        'FAN_SHUTOFF_HUMIDITY',
        'PUMP_TRIGGER_HUMIDITY',
        'PUMP_SPRAY_DURATION',
        'PUMP_COOLDOWN_MINUTES',
        'PUMP_NIGHT_START_HOUR',
        'PUMP_NIGHT_END_HOUR',
    ):
        if key in defaults:
            set_threshold_value(key, defaults[key])
    print("[CTRL] Thresholds reset to defaults")


# --- Current state getters for web API ---
def get_temperature():
    return _current_temp

def get_humidity():
    return _current_humidity

def get_rpm():
    return _current_rpm

def get_fan_pwm():
    return _current_fan_pwm


def _format_pump_status(status):
    code, detail = status
    if detail:
        return f"{code} ({detail})"
    return code


def get_pump_status():
    return _format_pump_status(_current_pump_status)


def get_pump_status_parts():
    return _current_pump_status


def _make_pump_status(code, detail=''):
    return (str(code), str(detail) if detail else '')


# --- RPM Interrupt Handler ---
def rpm_callback(pin):
    """Callback for RPM sensor edge."""
    global _rpm_pulses
    _rpm_pulses += 1


def is_fan_night_time():
    """Check if current hour is within night time range for the fan."""
    hour = get_current_hour()
    if hour >= FAN_NIGHT_START_HOUR or hour < FAN_NIGHT_END_HOUR:
        return True
    return False


def is_pump_night_time():
    """Check if current hour is within night time range for the pump."""
    hour = get_current_hour()
    if hour >= PUMP_NIGHT_START_HOUR or hour < PUMP_NIGHT_END_HOUR:
        return True
    return False


def _run_sht4x_heater(sht_sensor, reason):
    """Run a short SHT4X heater cycle and return to high precision mode."""
    try:
        # Full 1-second heat pulse effectively evaporates condensation between infrequent cycles.
        sht_sensor.heater_power = sht4x.HEATER110mW
        sht_sensor.heat_time = sht4x.TEMP_1
        _ = sht_sensor.measurements
        sht_sensor.temperature_precision = sht4x.HIGH_PRECISION
        print(f"[CTRL] SHT4X heater cycle done ({reason})")
        return True
    except Exception as e:
        try:
            sht_sensor.temperature_precision = sht4x.HIGH_PRECISION
        except Exception:
            pass
        print(f"[CTRL] SHT4X heater error ({reason}): {e}")
        return False


async def control_loop(i2c, pin_pwm_fan, pin_relay_fan, pin_relay_pump, pin_rpm_fan, pin_button_pump_override):
    """
    Main control loop: read sensors and control hardware.
    Runs periodically (every SAMPLE_INTERVAL seconds).
    """
    global _current_temp, _current_humidity, _current_rpm, _current_fan_pwm
    global _current_pump_status, _last_spray_time, _rpm_pulses
    
    # Detect and initialize sensor (BME280 at 0x76/0x77, SHT4X at 0x44/0x45)
    try:
        devices = i2c.scan()
        bme_sensor = None
        sht_sensor = None
        print(f"[CTRL] I2C devices: {[hex(d) for d in devices]}")
        if 0x76 in devices or 0x77 in devices:
            addr = 0x76 if 0x76 in devices else 0x77
            bme_sensor = bme280_float.BME280(i2c=i2c, address=addr)
            sensor_type = 'bme280'
            print(f"[CTRL] BME280 initialized at {hex(addr)}")
        elif 0x44 in devices or 0x45 in devices:
            addr = 0x44 if 0x44 in devices else 0x45
            sht_sensor = sht4x.SHT4X(i2c, address=addr)
            sensor_type = 'sht4x'
            print(f"[CTRL] SHT4X initialized at {hex(addr)}")
        else:
            print("[CTRL] No supported sensor found on I2C bus")
            return
    except Exception as e:
        print(f"[CTRL] Error initializing sensor: {e}")
        return
    
    # Setup RPM interrupt
    pin_rpm_fan.irq(trigger=machine.Pin.IRQ_FALLING, handler=rpm_callback)
    print("[CTRL] RPM interrupt enabled")
    
    sample_interval = get('SAMPLE_INTERVAL', 30)
    debug_interval = get('DEBUG_PRINT_INTERVAL_SECONDS', 30)
    if debug_interval < 1:
        debug_interval = 1
    last_sample_time = time.time()
    last_debug_time = 0
    last_rpm_ms = time.ticks_ms()
    rpm_window_ms = int(get('RPM_AVG_WINDOW_SECONDS', 2) * 1000)
    rpm_window_ms = max(200, rpm_window_ms)
    rpm_window_start_ms = last_rpm_ms
    rpm_pulse_accum = 0
    sensor_values = array('f', [0.0, 0.0, 0.0])
    pump_was_on = False

    # SHT4X heater strategy:
    # - periodic cycle every 30 minutes by default
    # - extra cycle after pump run (with minimum spacing)
    heater_interval_sec = int(get('SHT4X_HEATER_INTERVAL_MINUTES', 30)) * 60
    heater_interval_sec = max(300, heater_interval_sec)
    heater_min_gap_sec = int(get('SHT4X_HEATER_MIN_GAP_MINUTES', 5)) * 60
    heater_min_gap_sec = max(60, heater_min_gap_sec)
    sht_last_heater_time = time.time()
    sht_heater_due_after_pump = False
    
    print("[CTRL] Control loop started")
    
    while _running:
        try:
            now = time.time()
            
            # --- A) Read sensor data ---
            try:
                if sensor_type == 'bme280':
                    if bme_sensor is None:
                        raise RuntimeError("BME280 not initialized")
                    bme_sensor.read_compensated_data(sensor_values)
                    temp, humidity = sensor_values[0], sensor_values[2]
                else:
                    if sht_sensor is None:
                        raise RuntimeError("SHT4X not initialized")
                    temp, humidity = sht_sensor.measurements
            except Exception as e:
                print(f"[CTRL] Sensor read error: {e}")
                temp, humidity = 20.0, 50.0
            
            # --- B) Calculate RPM ---
            current_pulses = _rpm_pulses
            _rpm_pulses = 0
            now_ms = time.ticks_ms()
            last_rpm_ms = now_ms
            rpm_pulse_accum += current_pulses
            window_elapsed = time.ticks_diff(now_ms, rpm_window_start_ms)
            if window_elapsed >= rpm_window_ms:
                if window_elapsed > 0 and rpm_pulse_accum > 0:
                    rpm = int((rpm_pulse_accum * 60000) / (2 * window_elapsed))
                else:
                    rpm = 0
                rpm_pulse_accum = 0
                rpm_window_start_ms = now_ms
            else:
                rpm = _current_rpm
            
            # --- C) Fan control ---
            # PWM: (current_humidity - target) / (100 - target) * 100
            if humidity >= FAN_TARGET_HUMIDITY:
                fan_pwm_val = ((humidity - FAN_TARGET_HUMIDITY) / 
                               (100 - FAN_TARGET_HUMIDITY)) * 100
            else:
                fan_pwm_val = 0
            
            fan_pwm_val = min(100, max(0, fan_pwm_val))
            
            # Convert to 0-1023 for ESP32 PWM
            pwm_duty = int((fan_pwm_val / 100.0) * 1023)

            if fan_pwm_val > 0 and not is_fan_night_time():
                pin_relay_fan.on()
                pin_pwm_fan.duty(pwm_duty)
            elif humidity <= FAN_SHUTOFF_HUMIDITY:
                pin_pwm_fan.duty(0)
                pin_relay_fan.off()
            
            # --- D) Pump control ---
            # Button uses pull-up, so pressed = 0. Manual press always wins.
            manual_override_active = (pin_button_pump_override.value() == 0)
            if manual_override_active:
                pin_relay_pump.on()
                pump_status = _make_pump_status("MANUELL", "TASTER")
            else:
                is_pump_on = pin_relay_pump.value()
                pump_status = _make_pump_status("AUS")
                time_since_last = now - _last_spray_time
                cooldown_sec = PUMP_COOLDOWN_MINUTES * 60

                if is_pump_on:
                    duration_on = now - _last_spray_time
                    if duration_on >= PUMP_SPRAY_DURATION:
                        # Spray time finished
                        pin_relay_pump.off()
                        pump_status = _make_pump_status("AUS")
                    else:
                        # Still spraying
                        remaining = int(PUMP_SPRAY_DURATION - duration_on)
                        pump_status = _make_pump_status("AN", f"{remaining}s")
                else:
                    # Pump is off, decide if we should start
                    if is_pump_night_time():
                        pump_status = _make_pump_status("NACHT")
                    elif time_since_last < cooldown_sec:
                        remaining = int((cooldown_sec - time_since_last) / 60)
                        pump_status = _make_pump_status("PAUSE", f"{remaining}m")
                    elif humidity < PUMP_TRIGGER_HUMIDITY:
                        pin_relay_pump.on()
                        _last_spray_time = now
                        pump_status = _make_pump_status("START")
                    else:
                        pump_status = _make_pump_status("BEREIT")

            pump_is_on_now = bool(pin_relay_pump.value())
            if pump_was_on and not pump_is_on_now and sensor_type == 'sht4x':
                sht_heater_due_after_pump = True
            pump_was_on = pump_is_on_now

            # --- E) Optional SHT4X heater cycles ---
            if sensor_type == 'sht4x':
                heater_reason = ''
                time_since_heater = now - sht_last_heater_time
                if time_since_heater >= heater_interval_sec:
                    heater_reason = 'periodic'
                elif sht_heater_due_after_pump and time_since_heater >= heater_min_gap_sec:
                    heater_reason = 'after-pump'

                if heater_reason:
                    if sht_sensor and _run_sht4x_heater(sht_sensor, heater_reason):
                        sht_last_heater_time = now
                        sht_heater_due_after_pump = False
            
            # --- F) Update state for web API ---
            _current_temp = temp
            _current_humidity = humidity
            _current_rpm = rpm
            _current_fan_pwm = int(fan_pwm_val)
            _current_pump_status = pump_status
            
            if now - last_sample_time >= sample_interval:
                last_sample_time = now
            
            # Debug output (throttled to reduce allocation churn)
            if now - last_debug_time >= debug_interval:
                pump_log_text = _format_pump_status(pump_status)
                print(f"[CTRL] T:{temp:.1f}°C H:{humidity:.1f}% RPM:{rpm} "
                    f"PWM:{int(fan_pwm_val)}% Pump:{pump_log_text}")
                last_debug_time = now
            
        except Exception as e:
            print(f"[CTRL] Error in control loop: {e}")
        
        # Sleep until next sample
        await asyncio.sleep(1)


async def run(i2c=None, oled=None):
    """
    Start the terrarium controller.
    
    Args:
        i2c: machine.I2C object for sensors
        oled: Optional SSD1306 display object for status
    """
    global _running
    
    _running = True
    
    # Get GPIO configuration
    pin_pwm_fan = machine.PWM(machine.Pin(get('PIN_PWM_FAN', 2)))
    pin_pwm_fan.freq(get('FAN_PWM_FREQ', 5000))
    
    pin_relay_fan = machine.Pin(get('PIN_RELAY_FAN', 4), machine.Pin.OUT)
    pin_relay_pump = machine.Pin(get('PIN_RELAY_PUMP', 5), machine.Pin.OUT)
    pin_rpm_fan = machine.Pin(get('PIN_RPM_FAN', 3), machine.Pin.IN, machine.Pin.PULL_UP)
    pin_button_pump_override = machine.Pin(
        get('PIN_BUTTON_PUMP_OVERRIDE', 9), machine.Pin.IN, machine.Pin.PULL_UP
    )
    
    # Ensure pump starts off
    pin_relay_pump.off()
    pin_relay_fan.off()
    
    print("[CTRL] Hardware initialized")
    
    # Load thresholds
    load_thresholds_from_config()
    
    # Run control loop
    try:
        await control_loop(
            i2c,
            pin_pwm_fan,
            pin_relay_fan,
            pin_relay_pump,
            pin_rpm_fan,
            pin_button_pump_override,
        )
    finally:
        # Cleanup
        pin_pwm_fan.duty(0)
        pin_relay_fan.off()
        pin_relay_pump.off()
        _running = False
        print("[CTRL] Stopped")


def stop():
    """Stop the controller."""
    global _running
    _running = False
