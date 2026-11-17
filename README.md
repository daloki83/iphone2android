# iPhone → PC → Android

Eine schlanke Linux-App (Python + GTK 4), die **alle Fotos und Videos vom iPhone** über USB auf den PC kopiert und von dort auf ein **Android-Gerät** überträgt. Das gilt auch für Screenshots, Bildschirmaufnahmen und Live-Photo-Videos. Dateien und Datum bleiben dabei unverändert.

> *English:* A small GTK 4 app for Linux that copies every photo and video from an iPhone to the PC via USB (ifuse) and then pushes them to an Android device (adb). Files are copied byte for byte and modification times are preserved and verified on both hops. Runs are incremental. The UI is in German.

## Funktionen

- Die Dateien werden **unverändert** kopiert, Byte für Byte. EXIF-Daten, Aufnahmedatum und Ort bleiben erhalten.
- Das **Änderungsdatum** jeder Datei wird übernommen, am PC und am Android. Am Android wird es nach dem Übertragen geprüft und bei Bedarf korrigiert. Danach liest die Galerie die Dateien neu ein.
- Ein **erneuter Lauf** kopiert nur neue Dateien, du kannst also jederzeit wiederholen.
- Am PC bleibt die Ordnerstruktur des iPhones erhalten (`100APPLE`, `101APPLE` …).
- Am Android landet standardmäßig alles in **einem Ordner** (`DCIM/iPhone`). Das lässt sich im Fenster abschalten. Kommt ein Dateiname doppelt vor, bekommt die spätere Datei `_2`, `_3` … angehängt, z. B. `IMG_0001_2.HEIC`. Inhalt und Datum ändern sich dabei nicht.
- Der Fortschrittsbalken zeigt **Restzeit und Geschwindigkeit**.
- Während der Übertragung werden **Bildschirmsperre und Ruhezustand** des PCs ausgesetzt.

## Installation

Benötigt werden Python 3 mit GTK 4 (PyGObject), libimobiledevice, ifuse, usbmuxd und adb.

**Arch / CachyOS / Manjaro**
```bash
sudo pacman -S --needed python-gobject gtk4 libimobiledevice ifuse usbmuxd android-tools
```

**Debian / Ubuntu**
```bash
sudo apt install python3-gi gir1.2-gtk-4.0 libimobiledevice-utils ifuse usbmuxd adb
```

**Fedora**
```bash
sudo dnf install python3-gobject gtk4 libimobiledevice-utils ifuse usbmuxd android-tools
```

Danach das Repository herunterladen:
```bash
git clone https://github.com/daloki83/iphone2android.git
```

Starter fürs Anwendungsmenü (optional, im Ordner des Repositorys ausführen):
```bash
sed "s|@PFAD@|$PWD|" iphone2android.desktop > ~/.local/share/applications/iphone2android.desktop
```

## Vorbereitung

**iPhone**
- Unter *Einstellungen → Fotos* die Option **„Laden und Originale behalten“** wählen und warten, bis alles aus iCloud geladen ist. Sonst liegen viele Originale gar nicht auf dem iPhone.
- Das Album **„Zuletzt gelöscht“** leeren. Diese Fotos liegen noch im Speicher und würden sonst mitkopiert.
- Das iPhone per USB anschließen und entsperren.

**Android**
- *Einstellungen → Über das Telefon* öffnen und 7× auf **Build-Nummer** tippen. Danach unter *Entwickleroptionen* **USB-Debugging** einschalten.
- Das Gerät anschließen und die Abfrage „USB-Debugging zulassen?“ bestätigen.

## Benutzung

```bash
python3 iphone2android.py
```

1. Wenn das iPhone noch nicht gekoppelt ist: **Koppeln** drücken, am iPhone **Vertrauen** tippen und den Code eingeben.
2. **① iPhone → PC**: Kopiert alles in den PC-Ordner (Standard: `~/Bilder/iPhone`).
3. **② PC → Android**: Überträgt den PC-Ordner nach `/sdcard/DCIM/iPhone`.
4. Oder beides auf einmal mit **① + ② nacheinander**.

Beide Ordner lassen sich im Fenster ändern und werden in `~/.config/iphone2android.json` gespeichert. Die Geräte können auch nacheinander angeschlossen werden: Für ① muss nur das iPhone verbunden sein, für ② nur das Android.

## Gut zu wissen

- **HEIC-Fotos** zeigt Android ab Version 10 normal an. Sie werden bewusst nicht umgewandelt, damit nichts verändert wird.
- **Live Photos** bestehen aus einem Foto (`.HEIC`) und einem kurzen Video (`.MOV`). Am Android erscheinen beide getrennt.
- **Bearbeitete Fotos**: Das iPhone speichert Änderungen in `.AAE`-Dateien, die Android nicht lesen kann. Sie werden übersprungen. Übertragen wird das Original.
- Fotos aus **geteilten Alben** und Fotos, die nur in iCloud liegen, sind über USB nicht erreichbar.
- Falls ein Vorgang abbricht, ihn einfach neu starten. Halb kopierte Dateien werden verworfen.
- Im Ein-Ordner-Modus merkt sich die App die vergebenen Namen in `.android-namen.json` im PC-Ordner. Diese Datei nicht löschen, sonst können Dateien doppelt übertragen werden.
- Wer zwischen „ein Ordner“ und „Unterordner“ wechselt, hat am Android danach beide Varianten, also Fotos doppelt. Den alten Stand dann am Handy löschen. Die App weist im Protokoll darauf hin.
- Sperrt sich eines der Handys während der Übertragung, läuft sie trotzdem weiter. Das iPhone muss nur seit dem letzten Neustart einmal entsperrt worden sein.

## Lizenz

[MIT](LICENSE)
