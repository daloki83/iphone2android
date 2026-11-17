#!/usr/bin/env python3
"""iPhone → PC → Android: alle Fotos und Videos übertragen, ohne Datum zu verändern.

Schritt 1 kopiert den kompletten DCIM-Ordner des iPhones (Fotos, Videos,
Screenshots, Bildschirmaufnahmen, Live-Photo-Videos …) per ifuse auf den PC.
Schritt 2 überträgt diesen Ordner per adb auf ein Android-Gerät.

Die Dateien werden Byte für Byte kopiert (EXIF/Metadaten bleiben unverändert),
das Änderungsdatum wird auf beiden Wegen übernommen und kontrolliert.
Beide Schritte sind inkrementell: bereits vorhandene Dateien werden übersprungen.

Benötigt: python-gobject (GTK 4), libimobiledevice, usbmuxd, ifuse, android-tools
"""

import json
import os
import posixpath
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

try:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gio, GLib, Gtk, Pango
except (ImportError, ValueError):
    raise SystemExit(
        "GTK 4 für Python fehlt. Installieren mit z. B.\n"
        "  Arch:          sudo pacman -S python-gobject gtk4\n"
        "  Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-4.0\n"
        "  Fedora:        sudo dnf install python3-gobject gtk4"
    )

APP_ID = "local.iphone2android"
CONFIG_FILE = Path(GLib.get_user_config_dir()) / "iphone2android.json"
PICTURES = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) or str(Path.home() / "Bilder")
DEFAULTS = {
    "pc_dir": str(Path(PICTURES) / "iPhone"),
    "android_dir": "/sdcard/DCIM/iPhone",
    "flat": True,
}
# .AAE = iOS-Bearbeitungsinfos, damit kann Android nichts anfangen
SKIP_EXT = {".aae"}
INSTALL_HINTS = [
    ("pacman", "sudo pacman -S --needed libimobiledevice ifuse usbmuxd android-tools"),
    ("apt", "sudo apt install libimobiledevice-utils ifuse usbmuxd adb"),
    ("dnf", "sudo dnf install libimobiledevice-utils ifuse usbmuxd android-tools"),
]
CHUNK = 4 << 20
# Zuordnung PC-Datei → Dateiname am Android (nur bei „alles in einen Ordner“)
NAMES_FILE = ".android-namen.json"


class Cancelled(Exception):
    pass


def run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]} nicht installiert")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"{cmd[0]}: Zeitüberschreitung")


def missing_tools():
    need = ["idevice_id", "idevicepair", "ideviceinfo", "ifuse", "usbmuxd", "adb"]
    return [t for t in need if not shutil.which(t)]


def install_hint():
    for manager, cmd in INSTALL_HINTS:
        if shutil.which(manager):
            return cmd
    return "libimobiledevice, ifuse, usbmuxd und adb über die Paketverwaltung installieren"


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def duration(seconds):
    seconds = int(seconds)
    if seconds < 15:
        return f"{max(seconds, 1)} s"
    if seconds < 120:
        return f"{round(seconds / 5) * 5} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} Min"
    return f"{minutes // 60} Std {minutes % 60:02d} Min"


class Tracker:
    """Meldet Fortschritt mit Restzeit und Geschwindigkeit (gemittelt über die letzten 30 s)."""

    WINDOW = 30

    def __init__(self, total, report):
        self.total = total
        self.report = report
        self.start = time.monotonic()
        self.samples = deque([(self.start, 0)])
        self.last = 0.0
        self.text = ""

    def update(self, done, text=None):
        if text is not None:
            self.text = text
        now = time.monotonic()
        if now - self.last < 0.25:
            return
        self.last = now
        self.samples.append((now, done))
        while len(self.samples) > 2 and now - self.samples[1][0] >= self.WINDOW:
            self.samples.popleft()
        t0, d0 = self.samples[0]
        rate = (done - d0) / (now - t0) if now > t0 else 0
        suffix = ""
        # die ersten Sekunden sind zu ungenau für eine Schätzung
        if now - self.start >= 3 and rate > 0:
            suffix = f"  ·  noch ca. {duration((self.total - done) / rate)} ({human(rate)}/s)"
        self.report(done / self.total if self.total else 1.0, self.text + suffix)

    def elapsed(self):
        return duration(time.monotonic() - self.start)


def scan(root: Path):
    """Alle Mediendateien unter root als Liste (relativer Pfad, os.stat_result)."""
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith(".") or name.endswith(".part"):
                continue
            if os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            path = Path(dirpath) / name
            try:
                result.append((path.relative_to(root), path.stat()))
            except OSError:
                pass
    return result


# ---------------------------------------------------------------- iPhone


def iphone_udid():
    r = run(["idevice_id", "-l"])
    ids = r.stdout.split()
    return ids[0] if ids else None


def iphone_status():
    if not shutil.which("idevice_id"):
        return None, "libimobiledevice fehlt"
    udid = iphone_udid()
    if not udid:
        return None, "nicht gefunden – per USB anschließen und entsperren"
    if run(["idevicepair", "-u", udid, "validate"]).returncode != 0:
        return None, "nicht gekoppelt – „Koppeln“ drücken, am iPhone „Vertrauen“ tippen"
    name = run(["ideviceinfo", "-u", udid, "-k", "DeviceName"]).stdout.strip() or "iPhone"
    ios = run(["ideviceinfo", "-u", udid, "-k", "ProductVersion"]).stdout.strip()
    return udid, f"{name} (iOS {ios}) bereit" if ios else f"{name} bereit"


class IphoneMount:
    """Hängt den Medienbereich des iPhones per ifuse in ein temporäres Verzeichnis ein."""

    def __init__(self, udid):
        self.udid = udid
        self.path = None

    def __enter__(self):
        cache = Path(GLib.get_user_cache_dir())
        cache.mkdir(parents=True, exist_ok=True)
        self.path = Path(tempfile.mkdtemp(prefix="iphone-", dir=cache))
        r = run(["ifuse", "-u", self.udid, str(self.path)], timeout=60)
        if r.returncode != 0:
            self.path.rmdir()
            raise RuntimeError(f"ifuse fehlgeschlagen: {(r.stderr or r.stdout).strip()}")
        return self.path

    def __exit__(self, *exc):
        for tool in ("fusermount3", "fusermount"):
            if shutil.which(tool) and run([tool, "-u", str(self.path)]).returncode == 0:
                break
        try:
            self.path.rmdir()
        except OSError:
            pass


def copy_file(src: Path, dst: Path, st, cancel, on_bytes):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while chunk := fi.read(CHUNK):
                if cancel.is_set():
                    raise Cancelled
                fo.write(chunk)
                on_bytes(len(chunk))
        if tmp.stat().st_size != st.st_size:
            raise OSError(f"Größe stimmt nicht ({tmp.stat().st_size} statt {st.st_size})")
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def iphone_to_pc(udid, pc_dir: Path, log, progress, cancel):
    with IphoneMount(udid) as mnt:
        dcim = mnt / "DCIM"
        if not dcim.is_dir():
            raise RuntimeError("Kein DCIM-Ordner – iPhone entsperrt und „Vertrauen“ bestätigt?")
        log("Lese Dateiliste vom iPhone …")
        files = scan(dcim)
        log(f"{len(files)} Dateien auf dem iPhone ({human(sum(s.st_size for _, s in files))}).")

        todo, fixed = [], 0
        for rel, st in files:
            dst = pc_dir / rel
            try:
                dst_st = dst.stat()
            except FileNotFoundError:
                todo.append((rel, st))
                continue
            if dst_st.st_size != st.st_size:
                todo.append((rel, st))
            elif dst_st.st_mtime_ns != st.st_mtime_ns:
                os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
                fixed += 1
        total = sum(st.st_size for _, st in todo)
        log(f"{len(files) - len(todo)} schon vorhanden, {len(todo)} neu ({human(total)}).")
        if fixed:
            log(f"Datum bei {fixed} vorhandenen Dateien korrigiert.")
        if not todo:
            progress(1.0, "Nichts zu tun – alles schon auf dem PC.")
            return

        pc_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(pc_dir).free
        if free < total + (512 << 20):
            raise RuntimeError(f"Zu wenig Speicherplatz: {human(total)} nötig, {human(free)} frei.")

        done = 0
        errors = []
        tracker = Tracker(total, progress)

        def on_bytes(n):
            nonlocal done
            done += n
            tracker.update(done)

        for i, (rel, st) in enumerate(todo, 1):
            tracker.update(done, f"{i}/{len(todo)}  {rel}")
            try:
                copy_file(dcim / rel, pc_dir / rel, st, cancel, on_bytes)
            except Cancelled:
                raise
            except OSError as e:
                errors.append(rel)
                log(f"FEHLER {rel}: {e}")
        progress(1.0, f"Fertig: {len(todo) - len(errors)} Dateien kopiert in {tracker.elapsed()}.")
        log(f"iPhone → PC fertig: {len(todo) - len(errors)} kopiert, {len(errors)} Fehler, "
            f"Dauer {tracker.elapsed()}.")


# ---------------------------------------------------------------- Android


def android_status():
    if not shutil.which("adb"):
        return None, "android-tools (adb) fehlt"
    r = run(["adb", "devices"])
    states = [line.split("\t") for line in r.stdout.splitlines()[1:] if "\t" in line]
    ready = [s for s, state in states if state == "device"]
    if ready:
        serial = ready[0]
        model = run(["adb", "-s", serial, "shell", "getprop ro.product.model"]).stdout.strip()
        release = run(["adb", "-s", serial, "shell", "getprop ro.build.version.release"]).stdout.strip()
        return serial, f"{model or serial} (Android {release}) bereit"
    if any(state == "unauthorized" for _, state in states):
        return None, "USB-Debugging am Handy erlauben (Dialog bestätigen)"
    if states:
        return None, f"Status: {states[0][1]}"
    return None, "nicht gefunden – USB-Debugging aktivieren und anschließen"


def adb(serial, *args, timeout=120):
    return run(["adb", "-s", serial, *args], timeout=timeout)


def android_listing(serial, remote_root):
    """{relativer Pfad: (Größe, mtime)} aller Dateien unter remote_root."""
    cmd = f"find {shlex.quote(remote_root)} -type f -exec stat -c '%s|%Y|%n' {{}} + 2>/dev/null"
    r = adb(serial, "shell", cmd, timeout=600)
    result = {}
    prefix = remote_root.rstrip("/") + "/"
    for line in r.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[2].startswith(prefix):
            result[parts[2][len(prefix):]] = (int(parts[0]), int(parts[1]))
    return result


def adb_push(serial, files, target, cancel):
    """target ist ein Ordner mit / am Ende oder bei einer einzelnen Datei der volle Zielpfad."""
    proc = subprocess.Popen(
        ["adb", "-s", serial, "push", *map(str, files), target],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    while True:
        try:
            out, _ = proc.communicate(timeout=0.3)
            break
        except subprocess.TimeoutExpired:
            if cancel.is_set():
                proc.terminate()
                proc.wait()
                raise Cancelled
    if proc.returncode != 0:
        raise RuntimeError(out.strip().splitlines()[-1] if out.strip() else "adb push fehlgeschlagen")


def flat_names(pc_dir: Path, files):
    """Eindeutige Dateinamen für einen einzigen Android-Ordner ({relativer Pfad: Name}).

    Doppelte Namen bekommen _2, _3 … angehängt. Vergebene Namen werden im PC-Ordner
    gespeichert, damit spätere Läufe keine Datei unter neuem Namen erneut übertragen.
    """
    path = pc_dir / NAMES_FILE
    try:
        names = json.loads(path.read_text())
    except (OSError, ValueError):
        names = {}
    # der Android-Speicher unterscheidet nicht zwischen Groß- und Kleinschreibung
    used = {n.lower() for n in names.values()}
    changed = False
    for rel, _ in files:
        if rel.as_posix() in names:
            continue
        stem, ext = os.path.splitext(rel.name)
        name, n = rel.name, 1
        while name.lower() in used:
            n += 1
            name = f"{stem}_{n}{ext}"
        names[rel.as_posix()] = name
        used.add(name.lower())
        changed = True
    if changed:
        path.write_text(json.dumps(names, indent=1, ensure_ascii=False))
    return names


def pc_to_android(serial, pc_dir: Path, remote_root, flat, log, progress, cancel):
    remote_root = remote_root.rstrip("/")
    if not pc_dir.is_dir():
        raise RuntimeError(f"Ordner {pc_dir} existiert nicht – zuerst Schritt ① ausführen.")
    files = scan(pc_dir)
    log(f"{len(files)} Dateien am PC ({human(sum(s.st_size for _, s in files))}).")
    # (relativer Pfad am PC, stat, relativer Pfad am Android)
    if flat:
        names = flat_names(pc_dir, files)
        targets = [(rel, st, names[rel.as_posix()]) for rel, st in files]
        renamed = sum(1 for rel, _, name in targets if name != rel.name)
        if renamed:
            log(f"{renamed} Dateien bekommen wegen gleicher Namen ein _2, _3 … angehängt.")
    else:
        targets = [(rel, st, rel.as_posix()) for rel, st in files]
    log("Lese Dateiliste vom Android-Gerät …")
    remote = android_listing(serial, remote_root)
    if flat and any("/" in r for r in remote):
        log(f"Hinweis: In {remote_root} liegen noch Unterordner von einem früheren Lauf. "
            "Die werden nicht angefasst – ggf. am Handy löschen, sonst gibt es Fotos doppelt.")
    todo = [t for t in targets if remote.get(t[2], (None,))[0] != t[1].st_size]
    total = sum(st.st_size for _, st, _ in todo)
    log(f"{len(files) - len(todo)} schon vorhanden, {len(todo)} neu ({human(total)}).")

    # Dateien mit unverändertem Namen pro Ordner in Paketen pushen (deutlich schneller
    # als einzeln), umbenannte einzeln mit vollem Zielpfad
    jobs, groups = [], {}
    for t in todo:
        parent = posixpath.dirname(t[2])
        remote_dir = f"{remote_root}/{parent}" if parent else remote_root
        if posixpath.basename(t[2]) == t[0].name:
            groups.setdefault(remote_dir, []).append(t)
        else:
            jobs.append((remote_dir, [t], f"{remote_root}/{t[2]}"))
    for remote_dir, items in groups.items():
        i = 0
        while i < len(items):
            batch, size = [], 0
            while i < len(items) and len(batch) < 50 and (not batch or size < (128 << 20)):
                batch.append(items[i])
                size += items[i][1].st_size
                i += 1
            jobs.append((remote_dir, batch, remote_dir + "/"))
    jobs.sort(key=lambda job: job[1][0][0])

    done = 0
    errors = 0
    created = set()
    tracker = Tracker(total, progress)
    for remote_dir, batch, target in jobs:
        if remote_dir not in created:
            adb(serial, "shell", f"mkdir -p {shlex.quote(remote_dir)}")
            created.add(remote_dir)
        size = sum(st.st_size for _, st, _ in batch)
        what = batch[0][0].as_posix() + (f" … ({len(batch)} Dateien)" if len(batch) > 1 else "")
        tracker.update(done, what)
        try:
            adb_push(serial, [pc_dir / rel for rel, _, _ in batch], target, cancel)
        except RuntimeError as e:
            errors += len(batch)
            log(f"FEHLER bei {what}: {e}")
        done += size

    # Datum kontrollieren und ggf. korrigieren
    progress(1.0, "Prüfe Datum auf dem Android-Gerät …")
    remote = android_listing(serial, remote_root)
    wrong = [(st, r) for _, st, r in targets if r in remote and remote[r][1] != int(st.st_mtime)]
    if wrong:
        log(f"Korrigiere Datum bei {len(wrong)} Dateien …")
        for k in range(0, len(wrong), 50):
            cmds = "; ".join(
                f"touch -m -d @{int(st.st_mtime)} {shlex.quote(remote_root + '/' + r)}"
                for st, r in wrong[k:k + 50]
            )
            r = adb(serial, "shell", cmds)
            if r.returncode != 0:
                log(f"Warnung beim Datum setzen: {(r.stderr or r.stdout).strip()[:200]}")
    missing = [r for _, _, r in targets if r not in remote]

    # Medienscanner anstoßen, damit die Galerie die Dateien (mit richtigem Datum) anzeigt
    folders = {remote_root} | {f"{remote_root}/{posixpath.dirname(r)}" for _, _, r in targets if "/" in r}
    for folder in sorted(folders):
        adb(serial, "shell",
            f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d {shlex.quote('file://' + folder)} >/dev/null")

    progress(1.0, f"Fertig: {len(todo) - errors} Dateien übertragen in {tracker.elapsed()}.")
    log(f"PC → Android fertig: {len(todo) - errors} übertragen, {errors} Fehler, Dauer {tracker.elapsed()}"
        + (f", {len(missing)} fehlen noch auf dem Gerät." if missing else "."))


# ---------------------------------------------------------------- Sperre verhindern


class Inhibitor:
    """Verhindert während der Übertragung Bildschirmsperre, Abschalten und Ruhezustand des PCs.

    Nutzt direkt die D-Bus-Dienste von KDE/GNOME (ScreenSaver, PowerManagement) und als
    Rückfall die GTK-Funktion (xdg-Portal). Stürzt das Programm ab, gibt der Desktop
    die Sperre von selbst wieder frei.
    """

    SERVICES = [
        # (Dienst, Objekt, Schnittstelle)
        ("org.freedesktop.ScreenSaver", "/ScreenSaver", "org.freedesktop.ScreenSaver"),
        ("org.freedesktop.PowerManagement.Inhibit", "/org/freedesktop/PowerManagement/Inhibit",
         "org.freedesktop.PowerManagement.Inhibit"),
    ]
    REASON = "Fotos werden übertragen"

    def __init__(self, app):
        self.app = app
        self.dbus_cookies = []
        self.gtk_cookie = 0

    def acquire(self, window):
        if self.dbus_cookies or self.gtk_cookie:
            return True
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        except GLib.Error:
            bus = None
        for name, path, iface in self.SERVICES if bus else []:
            try:
                reply = bus.call_sync(name, path, iface, "Inhibit",
                                      GLib.Variant("(ss)", ("iPhone → Android", self.REASON)),
                                      GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 3000, None)
                self.dbus_cookies.append((bus, name, path, iface, reply.unpack()[0]))
            except GLib.Error:
                pass
        if len(self.dbus_cookies) < len(self.SERVICES):
            flags = Gtk.ApplicationInhibitFlags.IDLE | Gtk.ApplicationInhibitFlags.SUSPEND
            self.gtk_cookie = self.app.inhibit(window, flags, self.REASON)
        return bool(self.dbus_cookies or self.gtk_cookie)

    def release(self):
        for bus, name, path, iface, cookie in self.dbus_cookies:
            try:
                bus.call_sync(name, path, iface, "UnInhibit", GLib.Variant("(u)", (cookie,)),
                              None, Gio.DBusCallFlags.NONE, 3000, None)
            except GLib.Error:
                pass
        self.dbus_cookies = []
        if self.gtk_cookie:
            self.app.uninhibit(self.gtk_cookie)
            self.gtk_cookie = 0


# ---------------------------------------------------------------- GUI


def load_config():
    try:
        return {**DEFAULTS, **json.loads(CONFIG_FILE.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_config(cfg):
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except OSError:
        pass


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="iPhone → Android")
        self.set_default_size(720, 560)
        self.cfg = load_config()
        self.udid = self.serial = None
        self.worker = None
        self.cancel = threading.Event()
        self.inhibitor = Inhibitor(app)
        self.checking = False

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(16)
        self.set_child(box)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        box.append(grid)

        def label(text, bold=False):
            lbl = Gtk.Label(xalign=0)
            lbl.set_markup(f"<b>{text}</b>" if bold else GLib.markup_escape_text(text))
            return lbl

        grid.attach(label("iPhone", True), 0, 0, 1, 1)
        self.iphone_lbl = label("…")
        self.iphone_lbl.set_hexpand(True)
        grid.attach(self.iphone_lbl, 1, 0, 1, 1)
        self.pair_btn = Gtk.Button(label="Koppeln")
        self.pair_btn.connect("clicked", self.on_pair)
        grid.attach(self.pair_btn, 2, 0, 1, 1)

        grid.attach(label("Android", True), 0, 1, 1, 1)
        self.android_lbl = label("…")
        grid.attach(self.android_lbl, 1, 1, 1, 1)
        refresh = Gtk.Button(label="Aktualisieren")
        refresh.connect("clicked", lambda *_: self.refresh_status())
        grid.attach(refresh, 2, 1, 1, 1)

        grid.attach(label("Ordner am PC", True), 0, 2, 1, 1)
        self.pc_entry = Gtk.Entry(text=self.cfg["pc_dir"], hexpand=True)
        grid.attach(self.pc_entry, 1, 2, 1, 1)
        choose = Gtk.Button(label="Wählen …")
        choose.connect("clicked", self.on_choose)
        grid.attach(choose, 2, 2, 1, 1)

        grid.attach(label("Ordner am Android", True), 0, 3, 1, 1)
        self.android_entry = Gtk.Entry(text=self.cfg["android_dir"])
        grid.attach(self.android_entry, 1, 3, 2, 1)

        self.flat_check = Gtk.CheckButton(
            label="Am Android alles in einen Ordner legen (ohne 100APPLE, 101APPLE …)",
            active=self.cfg["flat"],
        )
        grid.attach(self.flat_check, 1, 4, 2, 1)

        buttons = Gtk.Box(spacing=8, homogeneous=True)
        box.append(buttons)
        self.btn_copy = Gtk.Button(label="① iPhone → PC")
        self.btn_push = Gtk.Button(label="② PC → Android")
        self.btn_both = Gtk.Button(label="① + ② nacheinander")
        self.btn_both.add_css_class("suggested-action")
        self.btn_cancel = Gtk.Button(label="Abbrechen", sensitive=False)
        self.btn_copy.connect("clicked", lambda *_: self.start(copy=True, push=False))
        self.btn_push.connect("clicked", lambda *_: self.start(copy=False, push=True))
        self.btn_both.connect("clicked", lambda *_: self.start(copy=True, push=True))
        self.btn_cancel.connect("clicked", self.on_cancel)
        for b in (self.btn_copy, self.btn_push, self.btn_both, self.btn_cancel):
            buttons.append(b)

        self.progressbar = Gtk.ProgressBar(show_text=True, text="Bereit")
        self.progressbar.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(self.progressbar)

        self.logview = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        scroller = Gtk.ScrolledWindow(vexpand=True, child=self.logview)
        box.append(scroller)

        hint = Gtk.Label(xalign=0, wrap=True)
        hint.add_css_class("dim-label")
        hint.set_markup(
            "<small>Tipp: Am iPhone unter <i>Einstellungen → Fotos</i> „Laden und Originale behalten“ "
            "wählen, sonst liegen viele Originale nur in iCloud. Fotos in „Zuletzt gelöscht“ "
            "werden mitkopiert – vorher leeren, wenn sie nicht mit sollen.</small>"
        )
        box.append(hint)

        missing = missing_tools()
        if missing:
            self.log(f"Es fehlen Programme: {', '.join(missing)}\nInstallieren mit:\n  {install_hint()}\n")
        self.refresh_status()
        GLib.timeout_add_seconds(3, self.auto_refresh)
        self.connect("close-request", self.on_close)

    # --- Hilfen, threadsicher über idle_add

    def log(self, text):
        def do():
            buf = self.logview.get_buffer()
            buf.insert(buf.get_end_iter(), time.strftime("%H:%M:%S  ") + text + "\n")
            buf.place_cursor(buf.get_end_iter())
            self.logview.scroll_mark_onscreen(buf.get_insert())
        GLib.idle_add(do)

    def progress(self, fraction, text):
        def do():
            self.progressbar.set_fraction(max(0.0, min(1.0, fraction)))
            self.progressbar.set_text(text)
        GLib.idle_add(do)

    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    def update_buttons(self):
        busy = self.busy()
        self.btn_copy.set_sensitive(not busy and self.udid is not None)
        self.btn_push.set_sensitive(not busy and self.serial is not None)
        self.btn_both.set_sensitive(not busy and self.udid is not None and self.serial is not None)
        self.btn_cancel.set_sensitive(busy)
        self.pair_btn.set_sensitive(not busy and shutil.which("idevicepair") is not None)
        self.pc_entry.set_sensitive(not busy)
        self.android_entry.set_sensitive(not busy)
        self.flat_check.set_sensitive(not busy)

    # --- Gerätestatus

    def refresh_status(self):
        if self.checking:
            return
        self.checking = True

        def work():
            iphone = iphone_status()
            android = android_status()

            def done():
                self.checking = False
                self.udid, itext = iphone
                self.serial, atext = android
                self.iphone_lbl.set_text(("✔ " if self.udid else "✘ ") + itext)
                self.android_lbl.set_text(("✔ " if self.serial else "✘ ") + atext)
                self.update_buttons()
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def auto_refresh(self):
        if not self.busy():
            self.refresh_status()
        return True

    def on_pair(self, *_):
        def work():
            self.log("Koppeln … bitte am iPhone „Vertrauen“ tippen und Code eingeben.")
            for _ in range(20):
                r = run(["idevicepair", "pair"])
                out = (r.stdout + r.stderr).strip()
                if r.returncode == 0 or "Please accept" not in out and "passcode" not in out.lower():
                    break
                time.sleep(2)
            self.log(out or "idevicepair: keine Ausgabe")
            GLib.idle_add(self.refresh_status)
        threading.Thread(target=work, daemon=True).start()

    def on_choose(self, *_):
        dialog = Gtk.FileDialog(title="Ordner am PC wählen")
        current = Path(self.pc_entry.get_text()).expanduser()
        while not current.exists() and current != current.parent:
            current = current.parent
        dialog.set_initial_folder(Gio.File.new_for_path(str(current)))

        def chosen(dlg, result):
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            if folder:
                self.pc_entry.set_text(folder.get_path())
        dialog.select_folder(self, None, chosen)

    # --- Ablauf

    def start(self, copy, push):
        self.cfg["pc_dir"] = self.pc_entry.get_text().strip()
        self.cfg["android_dir"] = self.android_entry.get_text().strip() or DEFAULTS["android_dir"]
        self.cfg["flat"] = self.flat_check.get_active()
        save_config(self.cfg)
        pc_dir = Path(self.cfg["pc_dir"]).expanduser()
        android_dir = self.cfg["android_dir"]
        flat = self.cfg["flat"]
        udid, serial = self.udid, self.serial
        self.cancel.clear()

        def work():
            try:
                if copy:
                    self.log(f"── ① iPhone → {pc_dir}")
                    iphone_to_pc(udid, pc_dir, self.log, self.progress, self.cancel)
                if push:
                    self.log(f"── ② {pc_dir} → Android {android_dir}")
                    pc_to_android(serial, pc_dir, android_dir, flat, self.log, self.progress, self.cancel)
            except Cancelled:
                self.log("Abgebrochen.")
                self.progress(0, "Abgebrochen")
            except Exception as e:  # noqa: BLE001 – jede Fehlermeldung im Log zeigen
                self.log(f"FEHLER: {e}")
                self.progress(0, "Fehler – siehe Protokoll")
            finally:
                GLib.idle_add(self.finished)

        if self.inhibitor.acquire(self):
            self.log("Bildschirmsperre und Ruhezustand sind bis zum Ende ausgesetzt.")
        else:
            self.log("Warnung: Bildschirmsperre konnte nicht ausgesetzt werden.")
        self.worker = threading.Thread(target=work)
        self.worker.start()
        self.update_buttons()

    def on_cancel(self, *_):
        self.cancel.set()
        self.progress(self.progressbar.get_fraction(), "Breche ab …")

    def finished(self):
        self.inhibitor.release()
        self.update_buttons()

    def on_close(self, *_):
        # laufenden Vorgang sauber beenden, damit das iPhone wieder ausgehängt wird
        self.cancel.set()
        return False


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.present()


if __name__ == "__main__":
    App().run(None)
