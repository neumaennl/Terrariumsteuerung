# Steuerungsskript für mein Terrarium

Aktuell ausgerichtet auf [ESP32-C3 SuperMini mit Display](https://de.aliexpress.com/item/1005007929382296.html).
- [GYBMEP BME280](https://42project.net/shop/sensoren/temperatursensoren/temperatur-feuchtigkeits-luftdruck-i2c-sensor/) Sensor für Luftfeuchtigkeit und Temperatur (kommuniziert über [I²C](https://de.wikipedia.org/wiki/I%C2%B2C))
- 2 [KY-019](https://www.elektronik-kompendium.de/sites/praxis/bauteil_relaisboard.htm) Relais Module, um die Stromversorgung für Lüfter und Beregnungsanlage zu steuern
- 2 4-Pin CPU Lüfter

## Abhängigkeiten

- [offizieller SSD1306 Display Treiber](https://www.google.com/search?q=https://github.com/micropython/micropython/blob/master/drivers/display/ssd1306.py)
- [BME280 Treiber](https://github.com/robert-hh/BME280/blob/master/bme280_float.py)
