# Steuerungsskript für mein Terrarium

Die Steuerung läuft hauptsächlich auf einem [ESP32-C3 SuperMini mit Display](https://de.aliexpress.com/item/1005007929382296.html).

Daran angeschlossen sind:
- [GYBMEP BME280](https://42project.net/shop/sensoren/temperatursensoren/temperatur-feuchtigkeits-luftdruck-i2c-sensor/) Sensor für Luftfeuchtigkeit und Temperatur (kommuniziert über [I²C](https://de.wikipedia.org/wiki/I%C2%B2C))
- 2 [KY-019](https://www.elektronik-kompendium.de/sites/praxis/bauteil_relaisboard.htm) Relais Module, um die Stromversorgung für Lüfter und Beregnungsanlage zu steuern
- 2 4-Pin CPU Lüfter

Auf einem Raspberry Pi 2 läuft eine Web GUI, über die historische Daten angezeigt werden und Einstellungen verändert werden können. Dazu kommuniziert der Raspberry Pi mit dem ESP32 via REST Schnittstelle. Dieser Teil ist allerdings vollkommen optional und für die eigentliche Steuerung absolut nicht notwendig.

## Autostart

Damit das Skript garantier immer läuft, wurde ein systemd Service erstellt.<br/>
Dazu `terrarium.service` nach `/etc/systemd/system/` kopieren.<br/>
Dann über `sudo systemctl daemon-reload` systemd neu laden.<br/>
Mit `sudo systemctl enable terrarium.service` den Dienst aktivieren (damit er beim hochfahren startet) und wenn gewünscht per `sudo systemctl start terrarium.service` sofort starten.

## Abhängigkeiten

- [offizieller SSD1306 Display Treiber](https://www.google.com/search?q=https://github.com/micropython/micropython/blob/master/drivers/display/ssd1306.py)
- [BME280 Treiber](https://github.com/robert-hh/BME280/blob/master/bme280_float.py)
