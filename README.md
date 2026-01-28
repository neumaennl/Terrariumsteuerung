# Steuerungsskript für mein Terrarium

Aktuell ausgerichtet auf Raspberry Pi 2.
- [GYBMEP BME280](https://42project.net/shop/sensoren/temperatursensoren/temperatur-feuchtigkeits-luftdruck-i2c-sensor/) Sensor für Luftfeuchtigkeit und Temperatur (kommuniziert über [I²C](https://de.wikipedia.org/wiki/I%C2%B2C))
- 2 [KY-019](https://www.elektronik-kompendium.de/sites/praxis/bauteil_relaisboard.htm) Relais Module, um die Stromversorgung für Lüfter und Beregnungsanlage zu steuern
- 2 4-Pin CPU Lüfter

## Abhängigkeiten

```bash
sudo apt-get update
sudo apt-get install python3-pigpio python3-smbus i2c-tools python3-bme280
sudo systemctl enable pigpiod
sudo systemctl start pigpiod
```
Nicht vergessen, über `sudo raspi-config` in __Interface Options__ > __I2C__ das I²C Interface der Raspberry Pi zu aktivieren.

## Autostart

Damit das Skript garantier immer läuft, wurde ein systemd Service erstellt.<br/>
Dazu `terrarium.service` nach `/etc/systemd/system/` kopieren.<br/>
Dann über `sudo systemctl daemon-reload` systemd neu laden.<br/>
Mit `sudo systemctl enable terrarium.service` den Dienst aktivieren (damit er beim hochfahren startet) und wenn gewünscht per `sudo systemctl start terrarium.service` sofort starten.