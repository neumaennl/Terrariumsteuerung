# Steuerungsskript für mein Terrarium

Die Steuerung läuft hauptsächlich auf einem [ESP32-C3 SuperMini mit Display](https://de.aliexpress.com/item/1005007929382296.html).

Daran angeschlossen sind:

- [SHT40](https://sensirion.com/de/produkte/katalog/SHT40) (vorher [GYBMEP BME280](https://42project.net/shop/sensoren/temperatursensoren/temperatur-feuchtigkeits-luftdruck-i2c-sensor/)) Sensor für Luftfeuchtigkeit und Temperatur (kommuniziert über [I²C](https://de.wikipedia.org/wiki/I%C2%B2C))
- 2 [KY-019](https://www.elektronik-kompendium.de/sites/praxis/bauteil_relaisboard.htm) Relais Module, um die Stromversorgung für Lüfter und Beregnungsanlage zu steuern
- 2 4-Pin CPU Lüfter

Auf einem Raspberry Pi 2 läuft eine Web GUI, über die historische Daten angezeigt werden und Einstellungen verändert werden können. Dazu kommuniziert der Raspberry Pi mit dem ESP32 via REST Schnittstelle. Dieser Teil ist allerdings vollkommen optional und für die eigentliche Steuerung absolut nicht notwendig.

## Autostart

Damit das Skript garantier immer läuft, wurde ein systemd Service erstellt.  
Dazu `terrarium.service` nach `/etc/systemd/system/` kopieren.  
Dann über `sudo systemctl daemon-reload` systemd neu laden.  
Mit `sudo systemctl enable terrarium.service` den Dienst aktivieren (damit er beim hochfahren startet) und wenn gewünscht per `sudo systemctl start terrarium.service` sofort starten.

Optional können für die Historie Umgebungsvariablen im Service gesetzt werden:

- `TERRARIUM_RETENTION_DAYS` (Standard: `31`) löscht nur Daten, die älter als X Tage sind.
- `TERRARIUM_RECENT_WINDOW_DAYS` (Standard: `3`) definiert, wie lange Daten in hoher Auflösung gespeichert werden.
- `TERRARIUM_RECENT_RESOLUTION_SECONDS` (Standard: `30`) Auflösung innerhalb des Recent-Fensters.
- `TERRARIUM_ARCHIVE_RESOLUTION_SECONDS` (Standard: `900`) Auflösung fuer ältere Daten.
- `TERRARIUM_MAX_DB_ROWS` begrenzt die Gesamtzahl Datensaetze (Standard wird aus obigen Werten berechnet).

Beispiel in `terrarium.service` unter `[Service]`:

```ini
Environment=TERRARIUM_RETENTION_DAYS=31
Environment=TERRARIUM_RECENT_WINDOW_DAYS=3
Environment=TERRARIUM_RECENT_RESOLUTION_SECONDS=30
Environment=TERRARIUM_ARCHIVE_RESOLUTION_SECONDS=900
Environment=TERRARIUM_MAX_DB_ROWS=12000
```

## Abhängigkeiten

- [offizieller SSD1306 Display Treiber](https://www.google.com/search?q=https://github.com/micropython/micropython/blob/master/drivers/display/ssd1306.py)
- [BME280 Treiber](https://github.com/robert-hh/BME280/blob/master/bme280_float.py)
- [SHT40 Treiber](https://github.com/jposada202020/MicroPython_SHT4X/blob/master/micropython_sht4x/sht4x.py)
