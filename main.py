"""
Main entry point for Terrarium Controller on ESP32-C3.

Handles:
- WiFi connection
- NTP time synchronization
- Display initialization
- Starting controller and web server
"""

import asyncio
import machine
import network
import json
import time
import os
from ntp_sync import log_print as print

# WiFi credentials file
WIFI_FILE = 'wifi.json'


# MicroPython: os.path may be missing. Provide a small exists() helper using os.stat
def _exists(path):
    try:
        os.stat(path)
        return True
    except Exception:
        return False


def load_wifi_config():
    """Load WiFi configuration (SSID, password, hostname) from JSON file."""
    try:
        with open(WIFI_FILE, 'r') as f:
            config = json.load(f)
            ssid = config.get('ssid')
            password = config.get('password')
            hostname = config.get('hostname', 'terrarium')
            # Validate hostname length (max 9 chars)
            if hostname and len(hostname) > 9:
                print(f"[MAIN] Hostname too long ({len(hostname)} > 9), truncating")
                hostname = hostname[:9]
            return ssid, password, hostname
    except:
        return None, None, 'terrarium'


def save_wifi_config(ssid, password, hostname='terrarium'):
    """Save WiFi configuration to JSON file.
    
    Args:
        ssid: Network SSID
        password: Network password
        hostname: Device hostname (max 9 characters, default: 'terrarium')
    """
    # Validate and truncate hostname if needed
    if hostname and len(hostname) > 9:
        print(f"[MAIN] Hostname too long ({len(hostname)} > 9), truncating to {hostname[:9]}")
        hostname = hostname[:9]
    
    try:
        with open(WIFI_FILE, 'w') as f:
            json.dump({'ssid': ssid, 'password': password, 'hostname': hostname}, f)
        print("[MAIN] WiFi config saved")
    except Exception as e:
        print(f"[MAIN] Error saving WiFi config: {e}")


async def connect_wifi(ssid, password, hostname='terrarium', timeout=20):
    """
    Connect to WiFi network.
    
    Args:
        ssid: Network SSID
        password: Network password
        hostname: Device hostname (max 9 characters, default: 'terrarium')
        timeout: Timeout in seconds
    
    Returns:
        True if connected, False otherwise
    """
    if not ssid or not password:
        print("[MAIN] No WiFi credentials configured")
        return False
    
    try:
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        
        # Reduce TX power for stability
        wlan.config(txpower=15)
        
        # Set hostname (max 9 characters)
        if hostname and len(hostname) <= 9:
            try:
                network.hostname(hostname)
                print(f"[MAIN] Hostname set to: {hostname}")
            except Exception as e:
                print(f"[MAIN] Failed to set hostname: {e}")
        elif hostname:
            print(f"[MAIN] Hostname too long ({len(hostname)} > 9), skipping")
        
        print(f"[MAIN] Connecting to WiFi: {ssid}")
        wlan.connect(ssid, password)

        # Wait for connection (non-blocking)
        start = time.time()
        while not wlan.isconnected():
            if time.time() - start > timeout:
                print("[MAIN] WiFi connection timeout")
                return False
            await asyncio.sleep(0.5)

        print(f"[MAIN] WiFi connected: {wlan.ifconfig()}")
        return True
        
    except Exception as e:
        print(f"[MAIN] WiFi error: {e}")
        return False


async def sync_time():
    """Synchronize system time via NTP."""
    try:
        import ntp_sync
        
        print("[MAIN] Waiting for NTP sync...")
        
        # Try multiple times
        for attempt in range(3):
            if ntp_sync.sync_time():
                return True
            await asyncio.sleep(2)
        
        print("[MAIN] NTP sync failed, continuing anyway")
        return False
        
    except Exception as e:
        print(f"[MAIN] NTP error: {e}")
        return False


async def init_display():
    """Initialize OLED display."""
    try:
        import ssd1306
        
        # I2C for display (uses pins 5/6)
        i2c = machine.I2C(0, scl=machine.Pin(6), sda=machine.Pin(5))
        
        # Initialize display (72x40 pixels)
        oled = ssd1306.SSD1306_I2C(72, 40, i2c)
        oled.fill(0)
        oled.text('Boot...', 0, 0)
        oled.show()
        
        print("[MAIN] Display initialized")
        return oled, i2c
        
    except Exception as e:
        print(f"[MAIN] Display error: {e}")
        return None, None


async def update_display(oled, text_line1='', text_line2='', text_line3='', text_line4=''):
    """Update OLED display with status text."""
    if not oled:
        return
    
    try:
        oled.fill(0)
        y = 0
        line_height = 8
        for text in [text_line1, text_line2, text_line3, text_line4]:
            if text:
                # Truncate to fit 72px display (~9 chars)
                text = str(text)[:9]
                oled.text(text, 0, y)
                y += line_height
        oled.show()
    except Exception as e:
        print(f"[MAIN] Display update error: {e}")


async def main_loop(oled, i2c):
    """
    Main async event loop.
    Runs controller and web server concurrently.
    """
    import terrariumsteuerung as ctrl
    import webserver
    
    print("[MAIN] Starting main event loop")
    
    # Create background tasks
    controller_task = asyncio.create_task(ctrl.run(i2c, oled))
    webserver_task = asyncio.create_task(webserver.run())
    
    # Optional: Display update task
    async def display_loop():
        """Periodically update display with current values."""
        while True:
            try:
                temp = ctrl.get_temperature()
                humidity = ctrl.get_humidity()
                pump_status, pump_detail = ctrl.get_pump_status_parts()
                
                await update_display(
                    oled,
                    f"T:{temp:.1f}'C",
                    f"H:{humidity:.1f}%",
                    pump_status,
                    pump_detail
                )
            except:
                pass
            
            await asyncio.sleep(2)
    
    display_task = asyncio.create_task(display_loop())
    
    # NTP sync task - resync time twice a day
    async def ntp_sync_loop():
        """Periodically resync time with NTP server."""
        sync_interval = 12 * 3600  # 12 hours (twice per day)
        while True:
            await asyncio.sleep(sync_interval)
            try:
                print("[MAIN] Periodic NTP resync...")
                await sync_time()
            except Exception as e:
                print(f"[MAIN] NTP resync error: {e}")
    
    ntp_task = asyncio.create_task(ntp_sync_loop())
    
    # Wait for both tasks (they run until interrupted)
    try:
        await asyncio.gather(controller_task, webserver_task, display_task, ntp_task)
    except KeyboardInterrupt:
        print("[MAIN] Keyboard interrupt")
    except Exception as e:
        print(f"[MAIN] Main loop error: {e}")
    finally:
        # Cleanup
        try:
            ctrl.stop()
            webserver.stop()
        except:
            pass
        
        # Final display message
        await update_display(oled, "Stopped")


async def async_main():
    """Main async startup sequence."""
    print("\n" + "="*40)
    print("Terrariumsteuerung ESP32-C3")
    print("="*40)
    
    # 1. Initialize display
    oled, i2c = await init_display()
    await update_display(oled, "Init...")
    
    # 2. Load and connect WiFi
    ssid, password, hostname = load_wifi_config()
    if ssid:
        await update_display(oled, "Connect...")
        wifi_ok = await connect_wifi(ssid, password, hostname)
        if wifi_ok:
            await update_display(oled, "WiFi OK")
        else:
            await update_display(oled, "No WiFi")
    else:
        print("[MAIN] No WiFi SSID configured in wifi.json")
        print("[MAIN] Create wifi.json with: {\"ssid\": \"...\", \"password\": \"...\", \"hostname\": \"...\"} (hostname max 9 chars)")
        await update_display(oled, "No Config")
    
    # 3. Sync time via NTP
    await update_display(oled, "NTP...")
    await sync_time()
    
    # 4. Start main loop
    await update_display(oled, "Ready")
    await asyncio.sleep(1)
    
    await main_loop(oled, i2c)


def create_default_wifi_config():
    """Create a default wifi.json file if it doesn't exist."""
    if not _exists(WIFI_FILE):
        config = {
            'ssid': 'YourSSID',
            'password': 'YourPassword',
            'hostname': 'terrarium'
        }
        try:
            with open(WIFI_FILE, 'w') as f:
                json.dump(config, f)
            print(f"[MAIN] Created {WIFI_FILE} - please edit with your credentials")
        except Exception as e:
            print(f"[MAIN] Error creating {WIFI_FILE}: {e}")


# Entry point
create_default_wifi_config()
   
try:
    # Run async main
    asyncio.run(async_main())
except KeyboardInterrupt:
    print("\n[MAIN] Interrupted")
except Exception as e:
    print(f"[MAIN] Fatal error: {e}")
    try:
        import sys
        # MicroPython: use sys.print_exception if available
        if hasattr(sys, 'print_exception'):
            sys.print_exception(e)
        else:
            # Fallback to printing repr
            print(repr(e))
    except Exception:
        pass
