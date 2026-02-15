"""
boot.py - ESP32-C3 MicroPython boot script.

This script runs when the ESP32-C3 boots. It sets up the environment
and starts the main application.

Upload this as boot.py to the root directory of the ESP32.
"""

import machine
import sys
import webrepl

print("\n[BOOT] ESP32-C3 Terrarium Controller - Boot")
print("[BOOT] Firmware:", sys.platform)

# Optional: Set clock speed (in MHz)
try:
    machine.freq(160000000)  # 160 MHz
    print("[BOOT] CPU frequency set to 160 MHz")
except:
    pass

webrepl.start()
