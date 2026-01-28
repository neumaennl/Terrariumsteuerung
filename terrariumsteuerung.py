import time
import datetime
import pigpio
import smbus2
import bme280

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

# --- FUNKTIONEN ---

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
    global pulse_count, last_spray_time
    
    # 1. Verbindung herstellen
    gpio = pigpio.pi()
    if not gpio.connected:
        print("Fehler: Konnte nicht zum pigpio Daemon verbinden.")
        return

    # 2. BME280 Setup
    try:
        bus = smbus2.SMBus(I2C_PORT)
        calibration_params = bme280.load_calibration_params(bus, I2C_ADDRESS)
    except Exception as e:
        print(f"BME280 Fehler: {e}")
        return

    # 3. Pin Setup
    gpio.set_mode(PIN_RELAY_FAN, pigpio.OUTPUT)
    gpio.set_mode(PIN_RELAY_PUMP, pigpio.OUTPUT)
    gpio.write(PIN_RELAY_FAN, 0)
    gpio.write(PIN_RELAY_PUMP, 0) # Pumpe sicher aus

    # RPM Input mit Pull-Up (Wichtig für Open Collector!)
    gpio.set_mode(PIN_RPM_FAN, pigpio.INPUT)
    gpio.set_pull_up_down(PIN_RPM_FAN, pigpio.PUD_UP)
    cb = gpio.callback(PIN_RPM_FAN, pigpio.FALLING_EDGE, rpm_callback)

    print("--- Terrarium Controller gestartet ---")
    print(f"Ziel-Feuchte Lüfter: {FAN_TARGET_HUMIDITY}%")
    print(f"Pumpe aktiv unter:   {PUMP_TRIGGER_HUMIDITY}% (Tagsüber)")

    try:
        while True:
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

            # D) BEREGNUNGSANLAGE LOGIK (State Machine)
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
                    # START!
                    gpio.write(PIN_RELAY_PUMP, 1)
                    last_spray_time = now_ts
                    pump_status = "STARTET SPRÜHEN"
                else:
                    pump_status = "BEREIT (Feuchte OK)"

            # E) AUSGABE
            print(f"Temp: {temp:.1f}C | Feuchte: {humidity:.1f}% | RPM: {rpm}")
            print(f"Lüfter: {int(fan_pwm_val)}% | Pumpe: {pump_status}")
            print("-" * 40)

            time.sleep(1.0) # 1 Sekunde Takt

    except KeyboardInterrupt:
        print("\nShutdown...")
        gpio.hardware_PWM(PIN_PWM_FAN, FAN_PWM_FREQ, 0)
        gpio.write(PIN_RELAY_FAN, 0)
        gpio.write(PIN_RELAY_PUMP, 0)
        cb.cancel()
        gpio.stop()

if __name__ == "__main__":
    main()