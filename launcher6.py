import collections
import hashlib
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import ssl
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

import minecraft_launcher_lib as mll

# Carpeta compartida: versiones, librerías, assets y Java (se descargan una sola vez)
MC_DIR = mll.utils.get_minecraft_directory().replace("minecraft", "mi_launcher_mc")
os.makedirs(MC_DIR, exist_ok=True)

# Cada perfil tiene su propia carpeta de juego (mods, mundos, capturas, options...)
INSTANCES_DIR = os.path.join(MC_DIR, "instances")
CONFIG_FILE = os.path.join(MC_DIR, "launcher_config.json")
MAX_LOG_LINES = 5000

FILTROS = [
    ("Release", "release"),
    ("Snapshot", "snapshot"),
    ("Beta", "old_beta"),
    ("Alpha", "old_alpha"),
    ("Todas", "all"),
]
LOADERS = [("Vanilla", "vanilla"), ("Fabric", "fabric"), ("Forge", "forge")]
LOADER_NAMES = {valor: texto for texto, valor in LOADERS}

DEFAULT_CONFIG = {
    "username": "Jugador",
    "ram": 4,
    "account": "offline",
    "ms_client_id": "",
    "filtro": "release",
    "current": "Default",
    "profiles": {"Default": {"version": None, "loader": "vanilla"}},
}


# ---------- utilidades ----------
def offline_uuid(name: str) -> str:
    """UUID offline estándar (el mismo que usan los servidores en modo offline)."""
    h = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())
    h[6] = (h[6] & 0x0F) | 0x30
    h[8] = (h[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(h)))


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for k in cfg:
            if k in data:
                cfg[k] = data[k]
    except Exception:
        pass
    if not isinstance(cfg["profiles"], dict) or not cfg["profiles"]:
        cfg["profiles"] = json.loads(json.dumps(DEFAULT_CONFIG["profiles"]))
    for p in cfg["profiles"].values():  # RAM por perfil: antes era global (4 = valor por defecto -> auto)
        if isinstance(p, dict):
            p.setdefault("ram", "auto" if cfg.get("ram") in (None, 4) else cfg["ram"])
    if cfg["current"] not in cfg["profiles"]:
        cfg["current"] = next(iter(cfg["profiles"]))
    return cfg


def save_config(cfg):
    try:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_FILE)
    except OSError:
        pass


def open_folder(path):
    os.makedirs(path, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# ---------- seguridad de red ----------
# Se verifican siempre los certificados HTTPS. Pon True SOLO si un antivirus/proxy te rompe los
# certificados y aceptas el riesgo (alguien en tu red podría alterar lo que descargas).
ALLOW_INSECURE_SSL = False


def make_ssl_context():
    if ALLOW_INSECURE_SSL:
        return ssl._create_unverified_context()
    ctx = ssl.create_default_context()  # almacén de certificados del sistema (también el de Windows)
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())  # + certifi, por si el sistema no trae las raíces
    except Exception:
        pass
    return ctx


SSL_CTX = make_ssl_context()


class InstallCancelled(Exception):
    """El usuario canceló la instalación o descarga."""


# ---------- rendimiento ----------
def total_ram_gb():
    """RAM física total en GB (0 si no se puede saber)."""
    try:
        if sys.platform.startswith("win"):
            import ctypes

            class MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MemStatus()
            m.dwLength = ctypes.sizeof(MemStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullTotalPhys / 2 ** 30
        if sys.platform == "darwin":
            return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"])) / 2 ** 30
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 2 ** 20
    except Exception:
        pass
    return 0


def auto_ram_gb():
    """RAM para el juego según el PC: ~45% de la RAM total, entre 2 y 6 GB (más no mejora y alarga las pausas del GC)."""
    t = total_ram_gb()
    if t <= 0:
        return 4
    if t < 3:
        return 1
    return max(2, min(6, round(t * 0.45)))


# Flags de G1 (los de Aikar) que funcionan desde Java 8 hasta el más reciente.
G1_BASE = ["-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled", "-XX:MaxGCPauseMillis=200",
           "-XX:+UnlockExperimentalVMOptions", "-XX:+DisableExplicitGC", "-XX:+PerfDisableSharedMem"]
G1_TUNED = ["-XX:G1NewSizePercent=30", "-XX:G1MaxNewSizePercent=40", "-XX:G1HeapRegionSize=8M",
            "-XX:G1ReservePercent=20", "-XX:G1HeapWastePercent=5", "-XX:G1MixedGCCountTarget=4",
            "-XX:InitiatingHeapOccupancyPercent=15", "-XX:G1MixedGCLiveThresholdPercent=90",
            "-XX:G1RSetUpdatingPauseTimePercent=5", "-XX:SurvivorRatio=32", "-XX:MaxTenuringThreshold=1"]


def build_jvm_args(ram, optimize, extra=""):
    args = [f"-Xmx{ram}G", f"-Xms{max(1, ram // 2)}G"]
    if optimize:
        args += G1_BASE + (G1_TUNED if ram >= 4 else [])  # con poca RAM, solo los flags básicos
    return args + (extra or "").split()


def default_options():
    """options.txt inicial según el hardware. Solo se escribe si el perfil aún no tiene uno."""
    ram, cores = total_ram_gb(), os.cpu_count() or 2
    if ram >= 12 and cores >= 6:
        dist, fps = 12, 120
    elif ram >= 7 and cores >= 4:
        dist, fps = 10, 120
    else:
        dist, fps = 6, 60
    return [f"renderDistance:{dist}", f"simulationDistance:{dist}", f"maxFps:{fps}", "enableVsync:false",
            "graphicsMode:0", "fancyGraphics:false", "particles:1", "biomeBlendRadius:1",
            "entityShadows:false", "mipmapLevels:2", "entityDistanceScaling:0.75"]


def write_default_options(gamedir):
    path = os.path.join(gamedir, "options.txt")
    if os.path.exists(path):
        return False  # nunca se pisan los ajustes del jugador
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(default_options()) + "\n")
        return True
    except OSError:
        return False


# Mods de rendimiento que se instalan solos (solo los que tengan versión compatible)
PERF_MODS = {
    "fabric": ["sodium", "lithium", "ferrite-core", "modernfix", "entityculling", "immediatelyfast"],
    "forge": ["embeddium", "ferrite-core", "modernfix"],
}
ICONS = ["⛏", "🗡", "🏰", "🌲", "💎", "🔥", "🚀", "🧪", "🎮", "🛠"]

JVM_FAIL_RE = re.compile(r"Unrecognized VM option|Could not create the Java Virtual Machine|"
                         r"Invalid maximum heap size|Invalid initial heap size|Could not reserve enough space")


# ---------- versiones instaladas (modo sin conexión) ----------
def installed_mc_versions():
    """Versiones base de Minecraft que ya están descargadas (para poder jugar sin internet)."""
    vdir = os.path.join(MC_DIR, "versions")
    try:
        names = os.listdir(vdir)
    except OSError:
        return []
    info, bases = {}, set()
    for n in names:
        try:
            with open(os.path.join(vdir, n, n + ".json"), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        info[n] = d
        bases.add(d.get("inheritsFrom") or n)
    out = []
    for b in bases:
        d = info.get(b, {})
        out.append({"id": b, "type": d.get("type", "release"), "t": d.get("releaseTime", "")})
    out.sort(key=lambda v: v["t"], reverse=True)
    return [{"id": v["id"], "type": v["type"]} for v in out]


# ---------- diagnóstico de crashes ----------
CRASH_RULES = [
    (r"OutOfMemoryError|Java heap space|GC overhead limit",
     "Se quedó sin memoria. Sube la RAM del perfil (ahora {ram} GB) o quita mods/shaders pesados."),
    (r"Could not reserve enough space|Invalid maximum heap size|Invalid initial heap size|"
     r"Could not create the Java Virtual Machine|Unrecognized VM option",
     "Java no pudo arrancar con esos parámetros. Baja la RAM del perfil (ahora {ram} GB), usa 'Auto' "
     "o revisa los argumentos JVM extra."),
    (r"UnsupportedClassVersionError|class file version|requires Java \d+|"
     r"compiled by a more recent version of the Java Runtime",
     "Versión de Java incorrecta para el juego o un mod. Borra la carpeta 'runtime' del launcher para que "
     "baje el Java adecuado, o revisa que los mods sean para tu versión."),
    (r"Incompatible mods found|Mod resolution failed|Some of your mods are incompatible|"
     r"Missing or unsupported mandatory dependencies|requires .{0,80}which is missing|Could not find required mod",
     "Hay mods incompatibles o faltan dependencias (a menudo Fabric API u otra librería). "
     "Revisa la pestaña Mods → Instalados."),
    (r"Mixin apply failed|MixinApplyError|Mixin transformation of|InvalidInjectionException|MixinTransformerError",
     "Dos mods chocan entre sí (error de Mixin). Desactiva los últimos mods que añadiste."),
    (r"NoSuchMethodError|NoSuchFieldError|NoClassDefFoundError|ClassNotFoundException",
     "Algún mod no es compatible con esta versión del juego o del loader (le falta una clase o método)."),
    (r"GLFW error \d+|Pixel format not accepted|Failed to find pixel format|UnsatisfiedLinkError",
     "Problema con los gráficos o las librerías nativas. Actualiza los drivers de video; si tienes dos GPU, "
     "usa la dedicada; revisa que el antivirus no bloquee Java."),
]


def diagnose_crash(gamedir, since, tail, ram):
    """Lee la salida del juego y el último crash report y explica la causa probable -> (texto, ruta_report)."""
    text = "".join(tail)
    report = None
    try:
        cdir = os.path.join(gamedir, "crash-reports")
        recientes = [os.path.join(cdir, n) for n in os.listdir(cdir) if n.endswith(".txt")]
        recientes = [p for p in recientes if os.path.getmtime(p) >= since - 2]
        if recientes:
            report = max(recientes, key=os.path.getmtime)
            with open(report, encoding="utf-8", errors="replace") as f:
                text += "\n" + f.read(300_000)
    except OSError:
        pass
    hs = []
    try:
        hs = [n for n in os.listdir(gamedir)
              if n.startswith("hs_err_pid") and os.path.getmtime(os.path.join(gamedir, n)) >= since - 2]
    except OSError:
        pass

    causas = [msg.format(ram=ram) for pat, msg in CRASH_RULES if re.search(pat, text)]
    if hs:
        causas.append(f"La máquina virtual de Java se cerró de golpe ({hs[0]}). Suele ser el driver de video, "
                      "falta de RAM o un mod con código nativo.")
    lineas = ["Causas probables:"] + [f"• {c}" for c in causas[:3]] if causas else \
        ["No pude identificar la causa automáticamente."]

    m = re.search(r"Suspected Mods?:[ \t]*([^\n]*)((?:\n[ \t]{2,}[^\n]+)*)", text)
    if m:
        s = re.sub(r"\s+", " ", m.group(1) + " " + m.group(2)).strip()
        if s and not s.lower().startswith(("none", "unknown")):
            lineas.append("Mods sospechosos: " + s[:300])
    arreglos = []
    for f in re.findall(r"^[ \t]*- ((?:Install|Replace|Remove|Update|Upgrade) [^\n]{3,150})$", text, re.M):
        if f not in arreglos:
            arreglos.append(f)
    if arreglos:
        lineas.append("Fabric sugiere:")
        lineas += ["  - " + a for a in arreglos[:5]]
    if report:
        lineas.append("Crash report: " + report)
    return "\n".join(lineas), report


# ---------- cuenta Microsoft (flujo de código de dispositivo, sin dependencias extra) ----------
AUTH_FILE = os.path.join(MC_DIR, "ms_auth.json")


def load_auth():
    try:
        with open(AUTH_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_auth(data):
    """Guarda el token de sesión con permisos solo para tu usuario (en Windows depende de tu carpeta de usuario)."""
    tmp = AUTH_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, AUTH_FILE)


def clear_auth():
    try:
        os.remove(AUTH_FILE)
    except OSError:
        pass


def http_json(url, form=None, js=None, bearer=None):
    """POST/GET que devuelve (código_http, json) sin lanzar error por códigos 4xx."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif js is not None:
        data = json.dumps(js).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            body = {}
        return e.code, body
    except OSError as e:
        raise RuntimeError(f"Sin conexión con los servidores de Microsoft/Xbox: {e}") from e


class MicrosoftAuth:
    BASE = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
    SCOPE = "XboxLive.signin offline_access"
    XERR = {2148916233: "Esa cuenta Microsoft no tiene un perfil de Xbox. Crea uno en xbox.com e inténtalo de nuevo.",
            2148916235: "Xbox Live no está disponible en tu país/región.",
            2148916238: "Cuenta de menor de edad: un adulto debe añadirla a una Familia de Microsoft."}

    def __init__(self, client_id):
        self.client_id = (client_id or "").strip()
        if not self.client_id:
            raise RuntimeError("Falta el Client ID de tu app de Azure (botón 'Cuenta…').")

    def start_device_flow(self):
        code, d = http_json(f"{self.BASE}/devicecode", form={"client_id": self.client_id, "scope": self.SCOPE})
        if code != 200:
            raise RuntimeError(d.get("error_description") or f"Microsoft rechazó el Client ID (error {code}).")
        return d

    def poll_device_flow(self, d, cancelled=lambda: False):
        fin = time.time() + d.get("expires_in", 900)
        espera = d.get("interval", 5)
        while time.time() < fin:
            time.sleep(espera)
            if cancelled():
                raise InstallCancelled()
            code, t = http_json(f"{self.BASE}/token", form={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self.client_id, "device_code": d["device_code"]})
            if code == 200:
                return t
            err = t.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                espera += 5
                continue
            raise RuntimeError(t.get("error_description") or err or "Error al iniciar sesión.")
        raise RuntimeError("El código expiró. Vuelve a intentarlo.")

    def refresh(self, refresh_token):
        code, t = http_json(f"{self.BASE}/token", form={
            "grant_type": "refresh_token", "client_id": self.client_id,
            "refresh_token": refresh_token, "scope": self.SCOPE})
        if code != 200:
            raise RuntimeError("La sesión de Microsoft expiró. Vuelve a iniciar sesión (botón 'Cuenta…').")
        return t

    def minecraft_login(self, ms_access_token):
        """Token de Microsoft -> Xbox Live -> XSTS -> Minecraft. Devuelve (token_mc, nombre, uuid)."""
        c, x = http_json("https://user.auth.xboxlive.com/user/authenticate", js={
            "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                           "RpsTicket": "d=" + ms_access_token},
            "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"})
        if c != 200:
            raise RuntimeError(f"Xbox Live rechazó el inicio de sesión (error {c}).")
        c, s = http_json("https://xsts.auth.xboxlive.com/xsts/authorize", js={
            "Properties": {"SandboxId": "RETAIL", "UserTokens": [x["Token"]]},
            "RelyingParty": "rp://api.minecraftservices.com/", "TokenType": "JWT"})
        if c != 200:
            raise RuntimeError(self.XERR.get(s.get("XErr"), f"XSTS rechazó la sesión (error {c}, XErr={s.get('XErr')})."))
        uhs = s["DisplayClaims"]["xui"][0]["uhs"]
        c, m = http_json("https://api.minecraftservices.com/authentication/login_with_xbox",
                         js={"identityToken": f"XBL3.0 x={uhs};{s['Token']}"})
        if c == 403:
            raise RuntimeError("Mojang rechazó tu app ('Invalid app registration'). Las apps de Azure deben "
                               "ser aprobadas para usar la API de Minecraft: aka.ms/AppRegInfo")
        if c != 200:
            raise RuntimeError(f"Minecraft rechazó el inicio de sesión (error {c}).")
        c, p = http_json("https://api.minecraftservices.com/minecraft/profile", bearer=m["access_token"])
        if c == 404:
            raise RuntimeError("Esa cuenta no tiene Minecraft Java Edition.")
        if c != 200:
            raise RuntimeError(f"No se pudo leer el perfil de Minecraft (error {c}).")
        return m["access_token"], p["name"], str(uuid.UUID(p["id"]))

    def session(self):
        """Renueva la sesión guardada y devuelve (nombre, uuid, token) listos para lanzar el juego."""
        saved = load_auth()
        if not saved.get("refresh_token"):
            raise RuntimeError("No has iniciado sesión con Microsoft (botón 'Cuenta…').")
        t = self.refresh(saved["refresh_token"])
        token, name, uid = self.minecraft_login(t["access_token"])
        save_auth({"refresh_token": t.get("refresh_token", saved["refresh_token"]), "name": name, "uuid": uid})
        return name, uid, token


def incompatible_mods(root, mods_dir, ver, loader):
    """Mods activos que (según el índice guardado, sin red) no declaran compatibilidad con esta versión/loader."""
    try:
        with open(os.path.join(root, "modrinth_index.json"), encoding="utf-8") as f:
            idx = json.load(f)
    except Exception:
        return []
    malos = []
    for base, e in idx.items():
        if not os.path.exists(os.path.join(mods_dir, base)):  # solo los activos (.jar)
            continue
        gv, ld = e.get("game_versions"), e.get("loaders")
        if gv is None:
            continue
        if ver not in gv or (ld and loader not in ld):
            malos.append(e.get("title") or base)
    return malos


# ---------- Modrinth (API pública v2, sin API key) ----------
MODRINTH_API = "https://api.modrinth.com/v2"
MODRINTH_CDN = "https://cdn.modrinth.com/"
# Modrinth pide un User-Agent que identifique tu app (idealmente con un contacto). Edítalo a tu gusto.
USER_AGENT = "MiniLauncher/1.0 (launcher personal de Minecraft)"
SORTS = [("Relevancia", "relevance"), ("Descargas", "downloads"), ("Seguidores", "follows"),
         ("Recientes", "newest"), ("Actualizados", "updated")]
SIDE_NAMES = {"required": "requerido", "optional": "opcional", "unsupported": "no soportado"}


def modrinth(path, params=None, body=None):
    """Llama a la API de Modrinth. GET por defecto, POST si se pasa `body`."""
    url = MODRINTH_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RuntimeError("Modrinth: demasiadas peticiones, espera un momento e intenta de nuevo.") from e
        raise RuntimeError(f"Modrinth respondió con error {e.code}.") from e
    except OSError as e:
        raise RuntimeError(f"No se pudo conectar con Modrinth: {e}") from e


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(url, dest, sha1=None, progress=None):
    """Descarga a un .part, verifica el hash y recién entonces lo mueve al destino."""
    if not url.startswith(MODRINTH_CDN):
        raise RuntimeError("URL de descarga no confiable (no es del CDN de Modrinth).")
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha1()
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            for chunk in iter(lambda: r.read(64 * 1024), b""):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if sha1 and h.hexdigest().lower() != sha1.lower():
            raise RuntimeError("El archivo descargado está corrupto (el hash no coincide).")
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def pick_file(version):
    files = version.get("files") or []
    if not files:
        raise RuntimeError("Esa versión no tiene archivos para descargar.")
    return next((f for f in files if f.get("primary")), files[0])


def safe_filename(name):
    """Evita rutas raras (../) que pudiera traer el nombre de archivo."""
    name = os.path.basename(str(name).replace("\\", "/")).strip()
    if not name or name in (".", ".."):
        raise RuntimeError("Nombre de archivo inválido.")
    return name


def best_version(project_id, loader, mc_ver):
    """Última versión compatible con el loader y la versión de Minecraft (prefiere release > beta > alpha)."""
    versions = modrinth(f"/project/{project_id}/version",
                        {"loaders": json.dumps([loader]), "game_versions": json.dumps([mc_ver])})
    for tipo in ("release", "beta", "alpha"):
        for v in versions:
            if v.get("version_type") == tipo:
                return v
    return None


def fmt_num(n):
    for limit, suf in ((1e6, "M"), (1e3, "K")):
        if n >= limit:
            s = f"{n / limit:.1f}"
            return (s[:-2] if s.endswith(".0") else s) + suf
    return str(n)


# ---------- tema visual (oscuro, estilo launcher moderno) ----------
C = {"bg": "#0f1115", "side": "#13161b", "panel": "#181c23", "card": "#1e232c", "card_hover": "#262c37",
     "border": "#2a313c", "text": "#e8ebf1", "muted": "#8b94a4", "accent": "#1bd96a", "accent_hover": "#17b85a",
     "accent_fg": "#05150b", "input": "#11141a", "warn": "#f5a524", "danger": "#ff7b7b"}
FONT = "Segoe UI"
PLACEHOLDER_COLORS = ["#3b5b8c", "#6b4a8c", "#8c5a3b", "#3b8c6b", "#8c3b5a", "#5a7a3b", "#3b7a8c", "#8c7a3b"]

try:  # Pillow es opcional: sin él no se muestran imágenes (se ven iniciales de colores)
    from PIL import Image, ImageChops, ImageDraw, ImageTk
    HAS_PIL = True
except Exception:  # pragma: no cover
    HAS_PIL = False

ICON_CACHE = os.path.join(MC_DIR, "cache", "images")


def apply_theme(root):
    root.configure(bg=C["bg"])
    root.option_add("*Toplevel.background", C["panel"])
    root.option_add("*TCombobox*Listbox.background", C["input"])
    root.option_add("*TCombobox*Listbox.foreground", C["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", C["accent_fg"])
    root.option_add("*TCombobox*Listbox.font", (FONT, 10))
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=C["panel"], foreground=C["text"], fieldbackground=C["input"],
                 bordercolor=C["border"], lightcolor=C["border"], darkcolor=C["border"],
                 troughcolor=C["panel"], focuscolor=C["panel"], font=(FONT, 10))
    st.configure("TFrame", background=C["panel"])
    st.configure("TLabel", background=C["panel"], foreground=C["text"])
    st.configure("TButton", background=C["card_hover"], foreground=C["text"], borderwidth=0, padding=(12, 6))
    st.map("TButton", background=[("active", "#323a48")])
    st.configure("Accent.TButton", background=C["accent"], foreground=C["accent_fg"], borderwidth=0, padding=(12, 6))
    st.map("Accent.TButton", background=[("active", C["accent_hover"])])
    for name in ("TEntry", "TSpinbox", "TCombobox"):
        st.configure(name, fieldbackground=C["input"], foreground=C["text"], insertcolor=C["text"],
                     bordercolor=C["border"], lightcolor=C["border"], darkcolor=C["border"], padding=6,
                     arrowcolor=C["muted"], background=C["card_hover"])
        st.map(name, bordercolor=[("focus", C["accent"])], lightcolor=[("focus", C["accent"])],
               darkcolor=[("focus", C["accent"])])
    st.map("TCombobox", fieldbackground=[("readonly", C["input"])], foreground=[("readonly", C["text"])],
           selectbackground=[("readonly", C["input"])], selectforeground=[("readonly", C["text"])])
    st.configure("TCheckbutton", background=C["panel"], foreground=C["text"], indicatorbackground=C["input"],
                 indicatorforeground=C["accent"])
    st.map("TCheckbutton", background=[("active", C["panel"])], indicatorbackground=[("selected", C["accent"])])
    for o in ("Vertical", "Horizontal"):
        st.configure(f"{o}.TScrollbar", background=C["card_hover"], troughcolor=C["panel"], bordercolor=C["panel"],
                     lightcolor=C["card_hover"], darkcolor=C["card_hover"], arrowcolor=C["muted"], gripcount=0,
                     relief="flat", arrowsize=12)
        st.map(f"{o}.TScrollbar", background=[("active", "#3a4252")])
    st.configure("Horizontal.TProgressbar", troughcolor=C["card"], background=C["accent"], bordercolor=C["card"],
                 lightcolor=C["accent"], darkcolor=C["accent"], thickness=6)


def shorten(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
           x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


BUTTON_STYLES = {"accent": (C["accent"], C["accent_fg"], C["accent_hover"]),
                 "ghost": ("#2a303b", C["text"], "#353d4b"),
                 "danger": ("#3a1d22", C["danger"], "#522a31")}
BUTTON_DISABLED = ("#20252d", "#5d6675")


class RoundButton(tk.Canvas):
    """Botón con esquinas redondeadas. Acepta config(text=, state=, style=, command=)."""

    def __init__(self, master, text="", command=None, style="accent", width=120, height=36, radius=10,
                 font=(FONT, 10, "bold"), state="normal", parent_bg=None):
        super().__init__(master, width=width, height=height, bg=parent_bg or C["bg"], highlightthickness=0, bd=0)
        self._rb = {"text": text, "command": command, "style": style, "state": state, "radius": radius,
                    "font": font, "hover": False}
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        self.bind("<Configure>", lambda e: self._draw())
        self._draw()

    def _set_hover(self, v):
        self._rb["hover"] = v
        self._draw()

    def _click(self, e):
        inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
        if inside and self._rb["state"] == "normal" and self._rb["command"]:
            self._rb["command"]()

    def _draw(self):
        d = self._rb
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            w, h = int(self.cget("width")), int(self.cget("height"))
        base, fg, hover = BUTTON_STYLES.get(d["style"], BUTTON_STYLES["accent"])
        if d["state"] != "normal":
            base, fg = BUTTON_DISABLED
        elif d["hover"]:
            base = hover
        round_rect(self, 1, 1, w - 1, h - 1, d["radius"], fill=base, outline=base)
        self.create_text(w / 2, h / 2, text=d["text"], fill=fg, font=d["font"])
        super().configure(cursor="hand2" if d["state"] == "normal" else "arrow")

    def configure(self, cnf=None, **kw):
        if cnf:
            kw.update(cnf)
        cambio = False
        for k in ("text", "command", "style", "state"):
            if k in kw:
                self._rb[k] = kw.pop(k)
                cambio = True
        if kw:
            super().configure(**kw)
        if cambio:
            self._draw()

    config = configure


class Segmented(tk.Frame):
    """Selector segmentado (como las pestañas tipo 'píldora' de los launchers modernos) ligado a un StringVar."""

    def __init__(self, master, options, variable, command=None, small=False):
        super().__init__(master, bg=C["input"], highlightthickness=1, highlightbackground=C["border"])
        self.var, self.command, self.items = variable, command, {}
        font = (FONT, 9) if small else (FONT, 10, "bold")
        for texto, valor in options:
            b = tk.Label(self, text=texto, font=font, padx=8 if small else 14, pady=4 if small else 6,
                         cursor="hand2", bg=C["input"], fg=C["muted"])
            b.pack(side="left", padx=1, pady=1)
            b.bind("<Button-1>", lambda e, v=valor: self.pick(v))
            b.bind("<Enter>", lambda e, v=valor: self.hover(v, True))
            b.bind("<Leave>", lambda e, v=valor: self.hover(v, False))
            self.items[valor] = b
        self.var.trace_add("write", lambda *_: self.refresh())
        self.refresh()

    def pick(self, valor):
        self.var.set(valor)
        if self.command:
            self.command()

    def hover(self, valor, on):
        if self.var.get() != valor:
            self.items[valor].config(bg=C["card_hover"] if on else C["input"])

    def refresh(self):
        for valor, b in self.items.items():
            activo = self.var.get() == valor
            b.config(bg=C["accent"] if activo else C["input"], fg=C["accent_fg"] if activo else C["muted"])


class ScrollFrame(tk.Frame):
    """Zona con scroll vertical (rueda del ratón incluida) para listas de tarjetas."""

    def __init__(self, master, bg=None):
        bg = bg or C["bg"]
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_all(ev, self._wheel, add="+")

    def _wheel(self, e):
        try:
            w = self.winfo_containing(e.x_root, e.y_root)
        except (KeyError, tk.TclError):
            return
        if w is None or not (str(w) == str(self) or str(w).startswith(str(self) + ".")):
            return
        if self.inner.winfo_height() <= self.canvas.winfo_height():
            return
        if getattr(e, "num", 0) == 4:
            n = -3
        elif getattr(e, "num", 0) == 5:
            n = 3
        elif sys.platform.startswith("win"):
            n = -int(e.delta / 120) * 3
        else:
            n = -int(e.delta)
        self.canvas.yview_scroll(n, "units")

    def clear(self):
        for w in self.inner.winfo_children():
            w.destroy()
        self.canvas.yview_moveto(0)


class ImageLoader:
    """Descarga imágenes de Modrinth en segundos planos (caché en disco), las recorta con esquinas
    redondeadas y las entrega ya listas al hilo de la interfaz. Solo acepta https://*.modrinth.com."""
    MAX_BYTES = 3_000_000

    def __init__(self, root):
        self.root = root
        self.mem = {}
        self.waiting = {}
        self.pool = ThreadPoolExecutor(max_workers=4)
        try:
            os.makedirs(ICON_CACHE, exist_ok=True)
        except OSError:
            pass

    def get(self, url, size, callback, radius=10):
        """callback(PhotoImage) se llama en el hilo de Tk cuando la imagen está lista."""
        if not HAS_PIL or not url:
            return
        key = (url, size, radius)
        if key in self.mem:
            callback(self.mem[key])
            return
        if key in self.waiting:
            self.waiting[key].append(callback)
            return
        self.waiting[key] = [callback]

        def work():
            try:
                img = self._load(url, size, radius)
            except Exception:
                img = None
            self.root.after(0, lambda: self._done(key, img))

        self.pool.submit(work)

    def _done(self, key, img):
        cbs = self.waiting.pop(key, [])
        if img is None:
            return
        if len(self.mem) > 500:
            self.mem.clear()
        photo = ImageTk.PhotoImage(img)
        self.mem[key] = photo
        for cb in cbs:
            try:
                cb(photo)
            except tk.TclError:  # el widget ya no existe
                pass

    def _fetch(self, url):
        p = urllib.parse.urlparse(url)
        host = p.hostname or ""
        if p.scheme != "https" or not (host == "modrinth.com" or host.endswith(".modrinth.com")):
            raise RuntimeError("Origen de imagen no permitido.")
        path = os.path.join(ICON_CACHE, hashlib.sha1(url.encode("utf-8")).hexdigest())
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            pass
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
            data = r.read(self.MAX_BYTES + 1)
        if len(data) > self.MAX_BYTES:
            raise RuntimeError("Imagen demasiado grande.")
        try:
            with open(path + ".tmp", "wb") as f:
                f.write(data)
            os.replace(path + ".tmp", path)
        except OSError:
            pass
        return data

    def _load(self, url, size, radius):
        w, h = size
        data = self._fetch(url)
        try:
            img = Image.open(io.BytesIO(data), formats=("PNG", "JPEG", "WEBP", "GIF"))
        except TypeError:  # Pillow antiguo sin el parámetro formats
            img = Image.open(io.BytesIO(data))
        img = img.convert("RGBA")
        resample = getattr(Image, "Resampling", Image).LANCZOS
        k = max(w / img.width, h / img.height)  # "cover": llena el recuadro y recorta lo que sobre
        img = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), resample)
        left, top = (img.width - w) // 2, (img.height - h) // 2
        img = img.crop((left, top, left + w, top + h))
        mask = Image.new("L", (w * 4, h * 4), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * 4 - 1, h * 4 - 1), radius * 4, fill=255)
        mask = mask.resize((w, h), resample)
        img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
        return img


class IconBox(tk.Canvas):
    """Cuadro de imagen: muestra una inicial de color mientras llega (o si no hay) la imagen real."""

    def __init__(self, master, loader, url, title, size=56, bg=None, radius=12, height=None):
        h = height or size
        super().__init__(master, width=size, height=h, bg=bg or C["card"], highlightthickness=0, bd=0)
        self._photo = None
        color = PLACEHOLDER_COLORS[sum(map(ord, title or "?")) % len(PLACEHOLDER_COLORS)]
        round_rect(self, 1, 1, size - 1, h - 1, radius, fill=color, outline=color)
        if h == size:  # en cuadros pequeños se muestra la inicial mientras llega la imagen
            self.create_text(size / 2, size / 2, text=(title or "?")[:1].upper(), fill="#ffffff",
                             font=(FONT, max(10, size // 3), "bold"))
        if url:
            loader.get(url, (size, h), self._set, radius)

    def _set(self, photo):
        self.delete("all")
        self._photo = photo
        self.create_image(0, 0, anchor="nw", image=photo)


class ModsTab(tk.Frame):
    """Gestor de mods del perfil actual: buscar/instalar desde Modrinth y administrar los instalados."""
    PAGE = 25

    def __init__(self, master, app):
        super().__init__(master, bg=C["bg"])
        self.app = app
        self.ctx = None          # contexto actual: perfil, versión, loader y carpetas
        self.ctx_key = None
        self.hits = {}           # project_id -> resultado de búsqueda
        self.rows = {}           # archivo -> info del mod instalado
        self.installed_pids = set()
        self.offset = 0
        self.search_id = 0
        self.busy = False
        self.warn = ""
        self.sel_pid = None
        self.card_btns = {}
        self.card_frames = {}
        self.dyn_buttons = []
        self.detail_btn = None
        self.more_btn = None
        self.lock = threading.Lock()
        self.query = tk.StringVar()
        self.sort = tk.StringVar(value=SORTS[1][0])
        self.build()

    # ---------- interfaz ----------
    def build(self):
        cab = tk.Frame(self, bg=C["bg"])
        cab.pack(fill="x")
        tk.Label(cab, text="Mods", font=(FONT, 22, "bold"), bg=C["bg"], fg=C["text"]).pack(side="left")
        self.sub_var = tk.StringVar(value="explore")
        Segmented(cab, [("Explorar Modrinth", "explore"), ("Instalados", "installed")], self.sub_var,
                  command=self.switch_sub).pack(side="right")
        self.ctx_label = tk.Label(self, text="—", font=(FONT, 10), bg=C["bg"], fg=C["muted"], anchor="w",
                                  justify="left", wraplength=820)
        self.ctx_label.pack(fill="x", pady=(2, 0))
        self.warn_lbl = tk.Label(self, text="", font=(FONT, 10), bg=C["bg"], fg=C["warn"], anchor="w",
                                 justify="left", wraplength=820)
        self.warn_lbl.pack(fill="x", pady=(0, 6))
        if not HAS_PIL:
            tk.Label(self, text="Para ver las imágenes de los mods instala Pillow:   pip install pillow",
                     font=(FONT, 10), bg=C["bg"], fg=C["warn"], anchor="w").pack(fill="x", pady=(0, 6))

        pie = tk.Frame(self, bg=C["bg"])
        pie.pack(side="bottom", fill="x", pady=(8, 0))
        self.bar = ttk.Progressbar(pie, mode="determinate")
        self.bar.pack(fill="x")
        self.status = tk.Label(pie, text="", bg=C["bg"], fg=C["muted"], anchor="w")
        self.status.pack(fill="x", pady=(4, 0))

        self.body = tk.Frame(self, bg=C["bg"])
        self.body.pack(fill="both", expand=True)
        self.page_explore = self.build_explore(self.body)
        self.page_installed = self.build_installed(self.body)
        self.switch_sub()

    def switch_sub(self):
        self.page_explore.pack_forget()
        self.page_installed.pack_forget()
        (self.page_explore if self.sub_var.get() == "explore" else self.page_installed).pack(fill="both", expand=True)

    def build_explore(self, parent):
        p = tk.Frame(parent, bg=C["bg"])
        p.columnconfigure(0, weight=1)
        p.rowconfigure(1, weight=1)

        top = tk.Frame(p, bg=C["bg"])
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        top.columnconfigure(0, weight=1)
        entry = ttk.Entry(top, textvariable=self.query, font=(FONT, 11))
        entry.grid(row=0, column=0, sticky="ew", ipady=4)
        entry.bind("<Return>", lambda _e: self.search())
        cb = ttk.Combobox(top, textvariable=self.sort, state="readonly", width=12, values=[t for t, _ in SORTS])
        cb.grid(row=0, column=1, padx=8)
        cb.bind("<<ComboboxSelected>>", lambda _e: self.search())
        RoundButton(top, text="Buscar", command=self.search, width=96, height=34,
                    parent_bg=C["bg"]).grid(row=0, column=2)

        self.card_scroll = ScrollFrame(p, bg=C["bg"])
        self.card_scroll.grid(row=1, column=0, sticky="nsew")
        self.detail = tk.Frame(p, bg=C["panel"], width=320, highlightthickness=1, highlightbackground=C["border"])
        self.detail.grid(row=1, column=1, sticky="ns", padx=(14, 0))
        self.detail.pack_propagate(False)
        self.show_detail(None)
        return p

    def build_installed(self, parent):
        p = tk.Frame(parent, bg=C["bg"])
        barra = tk.Frame(p, bg=C["bg"])
        barra.pack(fill="x", pady=(0, 10))
        self.btn_check = RoundButton(barra, text="Buscar actualizaciones", width=180, height=34,
                                     command=self.check_updates, parent_bg=C["bg"])
        self.btn_check.pack(side="left")
        RoundButton(barra, text="↻ Actualizar lista", style="ghost", width=140, height=34, parent_bg=C["bg"],
                    command=lambda: self.refresh_installed(force=True)).pack(side="left", padx=8)
        RoundButton(barra, text="Abrir carpeta", style="ghost", width=120, height=34, parent_bg=C["bg"],
                    command=lambda: open_folder(self.ctx["dir"]) if self.ctx else None).pack(side="right")
        self.inst_scroll = ScrollFrame(p, bg=C["bg"])
        self.inst_scroll.pack(fill="both", expand=True)
        return p

    # ---------- utilidades ----------
    def say(self, text):
        self.app.log(f"[Mods] {text}")
        self.after(0, lambda: self.status.config(text=text))
        self.after(0, lambda: self.app.set_status(text))

    def progress(self, done, total):
        if self.app.cancel.is_set():  # el usuario canceló desde el botón principal
            raise InstallCancelled()

        def upd():
            self.bar.config(maximum=max(total, 1), value=done)
            self.app.bar.config(maximum=max(total, 1), value=done)

        self.after(0, upd)

    def can_search(self):
        return bool(self.ctx and self.ctx["ver"] and self.ctx["loader"] != "vanilla")

    def set_buttons(self):
        estado = "disabled" if self.busy else "normal"
        self.btn_check.config(state=estado)
        for b in self.dyn_buttons:
            try:
                b.config(state=estado)
            except tk.TclError:
                pass
        self.mark_installed()

    def guard(self):
        """True si se puede modificar la carpeta de mods ahora mismo."""
        if self.busy:
            return False
        if self.app.running:
            messagebox.showinfo("Juego en uso", "Espera a que termine el juego (o la instalación de la "
                                                "versión) antes de modificar los mods.")
            return False
        return True

    def update_ctx_label(self):
        c = self.ctx
        if not c["ver"]:
            text = "Selecciona una versión en la pestaña Jugar."
        elif c["loader"] == "vanilla":
            text = (f"Perfil '{c['profile']}': Vanilla {c['ver']}. Cambia el mod loader a Fabric o Forge "
                    "en la pestaña Jugar para buscar e instalar mods.")
        else:
            text = f"Perfil '{c['profile']}' — {LOADER_NAMES[c['loader']]} {c['ver']}"
        self.ctx_label.config(text=text)
        self.warn_lbl.config(text=self.warn)

    # ---------- entrada a la pestaña ----------
    def on_show(self):
        app = self.app
        ver = app.selected() or app.selected_ver
        loader = app.loader.get()
        root = app.profile_dir()
        self.ctx = {"profile": app.perfil.get(), "ver": ver, "loader": loader,
                    "root": root, "dir": os.path.join(root, "mods")}
        key = (self.ctx["profile"], ver, loader)
        self.refresh_installed()
        if key == self.ctx_key:
            return
        self.ctx_key = key
        self.update_ctx_label()
        self.clear_results()
        if self.can_search():
            self.search()
        else:
            self.show_empty("Elige Fabric o Forge (y una versión) en la pestaña Jugar\npara buscar e instalar mods.")

    def show_empty(self, text):
        tk.Label(self.card_scroll.inner, text=text, font=(FONT, 12), bg=C["bg"], fg=C["muted"],
                 justify="center").pack(pady=60)

    def clear_results(self):
        self.search_id += 1
        self.card_scroll.clear()
        self.hits.clear()
        self.card_btns.clear()
        self.card_frames.clear()
        self.more_btn = None
        self.sel_pid = None
        self.offset = 0
        self.show_detail(None)

    # ---------- búsqueda ----------
    def search(self, more=False):
        if not self.can_search():
            return
        ctx = dict(self.ctx)
        if not more:
            self.clear_results()
        self.search_id += 1
        sid = self.search_id
        offset = self.offset
        q = self.query.get().strip()
        index = dict(SORTS)[self.sort.get()]
        self.status.config(text="Buscando en Modrinth...")

        def work():
            facets = [["project_type:mod"], [f"categories:{ctx['loader']}"], [f"versions:{ctx['ver']}"]]
            try:
                data = modrinth("/search", {"query": q, "facets": json.dumps(facets), "index": index,
                                            "limit": self.PAGE, "offset": offset})
                err = None
            except Exception as e:
                data, err = None, str(e)
            self.after(0, lambda: self.show_results(sid, data, err))

        threading.Thread(target=work, daemon=True).start()

    def show_results(self, sid, data, err):
        if sid != self.search_id:  # llegó una respuesta vieja
            return
        if err:
            self.status.config(text=err)
            return
        if self.more_btn is not None:
            self.more_btn.destroy()
            self.more_btn = None
        for h in data["hits"]:
            pid = h["project_id"]
            if pid in self.hits:
                continue
            self.hits[pid] = h
            self.add_card(h)
        self.offset += len(data["hits"])
        total = data.get("total_hits", 0)
        if not self.hits:
            self.show_empty("Sin resultados para esta versión y loader.")
        elif self.offset < total:
            self.more_btn = RoundButton(self.card_scroll.inner, text="Cargar más resultados", style="ghost",
                                        width=210, height=36, parent_bg=C["bg"],
                                        command=lambda: self.search(more=True))
            self.more_btn.pack(pady=12)
        self.status.config(text=f"{total} resultados para {LOADER_NAMES[self.ctx['loader']]} {self.ctx['ver']}"
                           if total else "Sin resultados para esta versión/loader.")
        self.mark_installed()

    def add_card(self, h):
        pid = h["project_id"]
        bg = C["card"]
        card = tk.Frame(self.card_scroll.inner, bg=bg, highlightthickness=1, highlightbackground=C["border"],
                        cursor="hand2")
        card.pack(fill="x", padx=(0, 8), pady=4)
        card.columnconfigure(1, weight=1)
        icon = IconBox(card, self.app.images, h.get("icon_url"), h["title"], 64, bg, 14)
        icon.grid(row=0, column=0, rowspan=3, padx=14, pady=12)
        titulo = tk.Label(card, text=h["title"], font=(FONT, 12, "bold"), bg=bg, fg=C["text"], anchor="w")
        titulo.grid(row=0, column=1, sticky="sw", pady=(12, 0))
        meta = tk.Label(card, text=f"por {h.get('author', '?')}   ⬇ {fmt_num(h.get('downloads', 0))}   "
                                   f"♥ {fmt_num(h.get('follows', 0))}",
                        font=(FONT, 9), bg=bg, fg=C["muted"], anchor="w")
        meta.grid(row=1, column=1, sticky="w")
        desc = tk.Label(card, text=shorten(h.get("description", ""), 150), font=(FONT, 10), bg=bg, fg="#b8c0cc",
                        anchor="nw", justify="left", wraplength=420)
        desc.grid(row=2, column=1, sticky="nw", pady=(2, 12))
        btn = RoundButton(card, text="Instalar", width=100, height=32, parent_bg=bg,
                          command=lambda p=pid: self.install_hit(self.hits.get(p)))
        btn.grid(row=0, column=2, rowspan=3, padx=14)
        card.bind("<Configure>", lambda e, d=desc: d.config(wraplength=max(180, e.width - 64 - 28 - 100 - 36)))
        for w in (card, icon, titulo, meta, desc):
            w.bind("<Button-1>", lambda e, p=pid: self.select_hit(p))
        self.card_btns[pid] = btn
        self.card_frames[pid] = card

    def select_hit(self, pid):
        old = self.card_frames.get(self.sel_pid)
        if old is not None:
            old.config(highlightbackground=C["border"])
        self.sel_pid = pid
        card = self.card_frames.get(pid)
        if card is not None:
            card.config(highlightbackground=C["accent"])
        self.show_detail(self.hits.get(pid))

    def show_detail(self, h):
        for w in self.detail.winfo_children():
            w.destroy()
        self.detail_btn = None
        P = C["panel"]
        if not h:
            tk.Label(self.detail, text="Selecciona un mod\npara ver sus detalles", font=(FONT, 11), bg=P,
                     fg=C["muted"], justify="center").place(relx=0.5, rely=0.4, anchor="center")
            return
        pid = h["project_id"]
        botones = tk.Frame(self.detail, bg=P)
        botones.pack(side="bottom", fill="x", padx=18, pady=16)
        self.detail_btn = RoundButton(botones, text="Instalar", width=284, height=38, parent_bg=P,
                                      command=lambda: self.install_hit(self.hits.get(pid)))
        self.detail_btn.pack()
        RoundButton(botones, text="Abrir en Modrinth", style="ghost", width=284, height=34, parent_bg=P,
                    command=lambda: self.open_page(h)).pack(pady=(8, 0))

        IconBox(self.detail, self.app.images, h.get("icon_url"), h["title"], 88, P, 18).pack(
            anchor="w", padx=18, pady=(18, 10))
        tk.Label(self.detail, text=h["title"], font=(FONT, 15, "bold"), bg=P, fg=C["text"], anchor="w",
                 justify="left", wraplength=284).pack(anchor="w", padx=18)
        tk.Label(self.detail, text=f"por {h.get('author', '?')}", font=(FONT, 10), bg=P, fg=C["muted"]).pack(
            anchor="w", padx=18)
        tk.Label(self.detail, text=f"⬇ {fmt_num(h.get('downloads', 0))}      ♥ {fmt_num(h.get('follows', 0))}",
                 font=(FONT, 10, "bold"), bg=P, fg=C["accent"]).pack(anchor="w", padx=18, pady=(8, 0))
        tk.Label(self.detail, text=h.get("description", ""), font=(FONT, 10), bg=P, fg="#c3cad6", anchor="nw",
                 justify="left", wraplength=284).pack(anchor="w", padx=18, pady=(10, 0))
        cats = [c.capitalize() for c in (h.get("display_categories") or h.get("categories") or [])
                if c not in ("fabric", "forge", "neoforge", "quilt")][:5]
        if cats:
            tk.Label(self.detail, text="  ·  ".join(cats), font=(FONT, 9), bg=P, fg=C["muted"], anchor="w",
                     justify="left", wraplength=284).pack(anchor="w", padx=18, pady=(8, 0))
        cliente = SIDE_NAMES.get(h.get("client_side"), "?")
        servidor = SIDE_NAMES.get(h.get("server_side"), "?")
        tk.Label(self.detail, text=f"Cliente: {cliente}  ·  Servidor: {servidor}", font=(FONT, 9), bg=P,
                 fg=C["muted"]).pack(anchor="w", padx=18, pady=(4, 0))
        banner = h.get("featured_gallery") or next(iter(h.get("gallery") or []), None)
        if banner and HAS_PIL:
            IconBox(self.detail, self.app.images, banner, "", 284, P, 12, height=160).pack(
                anchor="w", padx=18, pady=(12, 0))
        self.mark_installed()

    def mark_installed(self):
        for pid, btn in list(self.card_btns.items()):
            self.style_install_btn(btn, pid)
        if self.detail_btn is not None and self.sel_pid:
            self.style_install_btn(self.detail_btn, self.sel_pid)

    def style_install_btn(self, btn, pid):
        try:
            if pid in self.installed_pids:
                btn.config(text="✔ Instalado", state="disabled")
            else:
                btn.config(text="Instalar", style="accent", state="disabled" if self.busy else "normal")
        except tk.TclError:
            pass

    def selected_hit(self):
        return self.hits.get(self.sel_pid)

    def open_page(self, h=None):
        h = h or self.selected_hit()
        if h:
            webbrowser.open(f"https://modrinth.com/mod/{h.get('slug') or h['project_id']}")

    # ---------- tareas en segundo plano ----------
    def run_task(self, fn):
        """Ejecuta fn() -> (mensaje, avisos) en un hilo, bloqueando los botones mientras tanto."""
        self.busy = True
        self.set_buttons()

        def work():
            try:
                msg, warns = fn()
                err = None
            except Exception as e:
                msg, warns, err = None, [], str(e)
            self.after(0, lambda: self.task_done(msg, warns, err))

        threading.Thread(target=work, daemon=True).start()

    def task_done(self, msg, warns, err):
        self.busy = False
        self.set_buttons()
        self.bar.config(value=0)
        if err:
            self.status.config(text="Error")
            self.app.log(f"[Mods] ERROR: {err}")
            messagebox.showerror("Error", err)
        else:
            self.status.config(text=msg)
            if warns:
                messagebox.showwarning("Aviso", "\n".join(warns))
        self.refresh_installed()

    # ---------- instalar ----------
    def install_hit(self, h):
        if not h or not self.can_search() or not self.guard():
            return
        if h["project_id"] in self.installed_pids:
            return
        ctx = dict(self.ctx)
        self.run_task(lambda: self.task_install(h, ctx))

    def task_install(self, hit, ctx):
        rows, _ = self.scan_local(ctx)  # para no duplicar dependencias que ya estén
        projects = {r["project_id"] for r in rows if r.get("project_id")}
        version = best_version(hit["project_id"], ctx["loader"], ctx["ver"])
        if not version:
            raise RuntimeError(f"'{hit['title']}' no tiene una versión para "
                               f"{LOADER_NAMES[ctx['loader']]} {ctx['ver']}.")
        files, warns = [], []
        self.fetch(version, ctx, projects, files, warns)
        extra = f" (+{len(files) - 1} dependencias)" if len(files) > 1 else ""
        return f"Instalado: {hit['title']}{extra}", warns

    def install_slugs(self, ctx, slugs):
        """Instala (sin duplicar) proyectos por slug, solo si tienen versión compatible. -> (instalados, avisos)"""
        rows, _ = self.scan_local(ctx)
        projects = {r["project_id"] for r in rows if r.get("project_id")}
        files, warns, nombres = [], [], []
        for slug in slugs:
            try:
                v = best_version(slug, ctx["loader"], ctx["ver"])
            except RuntimeError as e:
                if "404" in str(e):  # el proyecto ya no existe con ese nombre
                    continue
                raise
            if not v or v["project_id"] in projects:
                continue
            self.fetch(v, ctx, projects, files, warns)
            nombres.append(slug)
        return nombres, warns

    def fetch(self, version, ctx, projects, files, warns):
        """Descarga una versión y, recursivamente, sus dependencias obligatorias."""
        projects.add(version["project_id"])
        f = pick_file(version)
        name = safe_filename(f["filename"])
        os.makedirs(ctx["dir"], exist_ok=True)
        self.say(f"Descargando {name}...")
        download_file(f["url"], os.path.join(ctx["dir"], name), f.get("hashes", {}).get("sha1"), self.progress)
        files.append(name)

        for dep in version.get("dependencies") or []:
            if dep.get("dependency_type") != "required":
                continue
            dpid = dep.get("project_id")
            dver = None
            if dep.get("version_id"):
                dver = modrinth(f"/version/{dep['version_id']}")
                dpid = dver["project_id"]
            if not dpid or dpid in projects:
                continue
            if dver is None:
                dver = best_version(dpid, ctx["loader"], ctx["ver"])
            if dver is None:
                try:
                    nombre = modrinth(f"/project/{dpid}")["title"]
                except Exception:
                    nombre = dpid
                warns.append(f"Falta una dependencia obligatoria sin versión compatible: {nombre}")
                continue
            self.fetch(dver, ctx, projects, files, warns)

    # ---------- mods instalados ----------
    def scan_local(self, ctx, force=False):
        """Lista los .jar del perfil e identifica cuáles son de Modrinth por su hash SHA-1.
        Devuelve (filas, error). Los resultados se cachean en modrinth_index.json del perfil."""
        with self.lock:
            idx_path = os.path.join(ctx["root"], "modrinth_index.json")
            try:
                with open(idx_path, encoding="utf-8") as f:
                    idx = json.load(f)
            except Exception:
                idx = {}

            nombres = []
            if os.path.isdir(ctx["dir"]):
                nombres = sorted((n for n in os.listdir(ctx["dir"])
                                  if n.endswith(".jar") or n.endswith(".jar.disabled")), key=str.lower)
            new_idx, files = {}, {}
            for fn in nombres:
                enabled = fn.endswith(".jar")
                base = fn if enabled else fn[:-len(".disabled")]
                path = os.path.join(ctx["dir"], fn)
                st = os.stat(path)
                e = idx.get(base, {})
                if e.get("size") != st.st_size or e.get("mtime") != int(st.st_mtime):
                    e = {"size": st.st_size, "mtime": int(st.st_mtime), "sha1": sha1_file(path)}
                elif force:
                    e.pop("checked", None)
                new_idx[base] = e
                files[base] = (fn, enabled)

            error = None
            por_hash = {}
            for base, e in new_idx.items():
                if not e.get("checked") or (e.get("project_id") and ("game_versions" not in e or "icon_url" not in e)):
                    por_hash.setdefault(e["sha1"], []).append(base)
            if por_hash:
                try:
                    found = modrinth("/version_files", body={"hashes": list(por_hash), "algorithm": "sha1"})
                    pids = list({v["project_id"] for v in found.values()})
                    titles, icons = {}, {}
                    for i in range(0, len(pids), 100):
                        for p in modrinth("/projects", {"ids": json.dumps(pids[i:i + 100])}):
                            titles[p["id"]] = p["title"]
                            icons[p["id"]] = p.get("icon_url") or ""
                    for h, bases in por_hash.items():
                        v = found.get(h)
                        for b in bases:
                            if v:
                                new_idx[b].update(project_id=v["project_id"], version_id=v["id"],
                                                  version=v.get("version_number"),
                                                  title=titles.get(v["project_id"]),
                                                  game_versions=v.get("game_versions"), loaders=v.get("loaders"),
                                                  icon_url=icons.get(v["project_id"], ""))
                            new_idx[b]["checked"] = True
                except Exception as ex:
                    error = str(ex)

            try:
                os.makedirs(ctx["root"], exist_ok=True)
                tmp = idx_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(new_idx, f, indent=1)
                os.replace(tmp, idx_path)
            except OSError:
                pass

            rows = []
            for base, e in new_idx.items():
                fn, enabled = files[base]
                rows.append({"file": fn, "enabled": enabled, "sha1": e["sha1"],
                             "project_id": e.get("project_id"), "title": e.get("title"),
                             "version": e.get("version"), "game_versions": e.get("game_versions"),
                             "loaders": e.get("loaders"), "icon_url": e.get("icon_url")})
            return rows, error

    def refresh_installed(self, force=False):
        if not self.ctx:
            return
        ctx = dict(self.ctx)

        def work():
            try:
                rows, err = self.scan_local(ctx, force)
            except Exception as e:
                rows, err = [], str(e)
            self.after(0, lambda: self.show_installed(ctx, rows, err))

        threading.Thread(target=work, daemon=True).start()

    def show_installed(self, ctx, rows, err):
        if not self.ctx or ctx["dir"] != self.ctx["dir"]:  # cambió el perfil mientras tanto
            return
        self.inst_scroll.clear()
        self.dyn_buttons = []
        self.rows = {r["file"]: r for r in rows}
        malos = 0
        for r in rows:
            marca = self.compat_mark(r)
            malos += bool(marca == "⚠" and r["enabled"])
            self.add_installed_row(r, marca)
        if not rows:
            tk.Label(self.inst_scroll.inner, text="Este perfil todavía no tiene mods.\n"
                     "Ve a «Explorar Modrinth» para instalar alguno.", font=(FONT, 12), bg=C["bg"],
                     fg=C["muted"], justify="center").pack(pady=60)
        self.warn = (f"⚠ {malos} mod(s) activo(s) no declaran compatibilidad con esta versión/loader "
                     "(desactívalos o bórralos)." if malos else "")
        self.update_ctx_label()
        self.installed_pids = {r["project_id"] for r in rows if r.get("project_id")}
        self.mark_installed()
        if err:
            self.status.config(text=f"{len(rows)} mods (no se pudieron identificar en Modrinth: {err})")
        elif not self.busy:
            self.status.config(text=f"{len(rows)} mods en el perfil")

    def add_installed_row(self, r, marca):
        bg = C["card"]
        dim = not r["enabled"]
        row = tk.Frame(self.inst_scroll.inner, bg=bg, highlightthickness=1, highlightbackground=C["border"])
        row.pack(fill="x", padx=(0, 8), pady=3)
        row.columnconfigure(1, weight=1)
        IconBox(row, self.app.images, r.get("icon_url"), r["title"] or r["file"], 48, bg, 11).grid(
            row=0, column=0, rowspan=2, padx=12, pady=10)
        tk.Label(row, text=r["title"] or r["file"], font=(FONT, 11, "bold"), bg=bg,
                 fg=C["muted"] if dim else C["text"], anchor="w").grid(row=0, column=1, sticky="sw", pady=(10, 0))
        sub = f"{r['version']}  ·  {r['file']}" if r["version"] else r["file"]
        tk.Label(row, text=sub, font=(FONT, 9), bg=bg, fg=C["muted"], anchor="w").grid(
            row=1, column=1, sticky="nw", pady=(0, 10))
        if marca:
            tk.Label(row, text="✔ Compatible" if marca == "✔" else "⚠ Incompatible", font=(FONT, 9, "bold"),
                     bg=bg, fg=C["accent"] if marca == "✔" else C["warn"]).grid(row=0, column=2, rowspan=2, padx=8)
        f = r["file"]
        t = RoundButton(row, text="Activo" if r["enabled"] else "Desactivado",
                        style="accent" if r["enabled"] else "ghost", width=104, height=30, parent_bg=bg,
                        command=lambda: self.toggle_file(f))
        t.grid(row=0, column=3, rowspan=2, padx=4)
        d = RoundButton(row, text="Eliminar", style="danger", width=84, height=30, parent_bg=bg,
                        command=lambda: self.delete_file(f))
        d.grid(row=0, column=4, rowspan=2, padx=(4, 12))
        self.dyn_buttons += [t, d]

    def compat_mark(self, r):
        c = self.ctx
        gv = r.get("game_versions")
        if gv is None or not c or not c["ver"] or c["loader"] == "vanilla":
            return ""
        ld = r.get("loaders") or []
        return "✔" if c["ver"] in gv and (not ld or c["loader"] in ld) else "⚠"

    def toggle_file(self, fn):
        if not self.guard():
            return
        src = os.path.join(self.ctx["dir"], fn)
        dst = src[:-len(".disabled")] if fn.endswith(".disabled") else src + ".disabled"
        try:
            if os.path.exists(dst):
                raise OSError("ya existe el archivo destino")
            os.rename(src, dst)
        except OSError as e:
            messagebox.showerror("Error", f"{fn}: {e}")
        self.refresh_installed()

    def delete_file(self, fn):
        if not self.guard():
            return
        if not messagebox.askyesno("Eliminar mod", f"¿Eliminar este mod del perfil?\n\n{fn}"):
            return
        try:
            os.remove(os.path.join(self.ctx["dir"], fn))
        except OSError as e:
            messagebox.showerror("Error", f"{fn}: {e}")
        self.refresh_installed()
    # ---------- actualizaciones ----------
    def check_updates(self):
        if not self.can_search() or not self.guard():
            return
        ctx = dict(self.ctx)
        self.busy = True
        self.set_buttons()
        self.status.config(text="Buscando actualizaciones...")

        def work():
            try:
                rows, _ = self.scan_local(ctx)
                updates = []
                if rows:
                    res = modrinth("/version_files/update", body={
                        "hashes": [r["sha1"] for r in rows], "algorithm": "sha1",
                        "loaders": [ctx["loader"]], "game_versions": [ctx["ver"]]})
                    for r in rows:
                        v = res.get(r["sha1"])
                        if v and pick_file(v).get("hashes", {}).get("sha1") != r["sha1"]:
                            updates.append((r, v))
                err = None
            except Exception as e:
                updates, err = [], str(e)
            self.after(0, lambda: self.confirm_updates(ctx, updates, err))

        threading.Thread(target=work, daemon=True).start()

    def confirm_updates(self, ctx, updates, err):
        self.busy = False
        self.set_buttons()
        if err:
            self.status.config(text="Error")
            messagebox.showerror("Error", err)
            return
        if not updates:
            self.status.config(text="Todos los mods están al día.")
            return
        lineas = [f"• {r['title'] or r['file']}: {r['version'] or '?'} → {v.get('version_number')}"
                  for r, v in updates[:15]]
        if len(updates) > 15:
            lineas.append(f"... y {len(updates) - 15} más")
        if messagebox.askyesno("Actualizaciones disponibles",
                               f"Hay {len(updates)} actualización(es):\n\n" + "\n".join(lineas)
                               + "\n\n¿Actualizar ahora?"):
            self.run_task(lambda: self.task_update(ctx, updates))
        else:
            self.status.config(text=f"{len(updates)} actualización(es) disponible(s).")

    def task_update(self, ctx, updates):
        rows, _ = self.scan_local(ctx)
        projects = {r["project_id"] for r in rows if r.get("project_id")}
        warns, hechos = [], 0
        for r, v in updates:
            files = []
            self.fetch(v, ctx, projects, files, warns)
            nuevo = files[0]
            viejo = r["file"]
            try:
                if not r["enabled"]:  # conserva el estado "desactivado"
                    os.replace(os.path.join(ctx["dir"], nuevo), os.path.join(ctx["dir"], nuevo + ".disabled"))
                    nuevo += ".disabled"
                if viejo != nuevo:
                    os.remove(os.path.join(ctx["dir"], viejo))
            except OSError as e:
                warns.append(f"No se pudo reemplazar {viejo}: {e}")
            hechos += 1
        return f"{hechos} mod(s) actualizado(s).", warns


# ---------- aplicación ----------
class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Mini Launcher")
        self.geometry("1040x700")
        self.minsize(960, 640)
        apply_theme(self)
        self.images = ImageLoader(self)

        self.cfg = load_config()
        self.versions = []
        self.shown = []
        self.installed = set()
        self.fabric_vers = set()
        self.forge_vers = set()

        self.running = False
        self.stopped = False
        self.proc = None
        self.logq = queue.Queue()
        self.online = True
        self.phase = "idle"  # idle | installing | playing
        self.cancel = threading.Event()
        self.tail = collections.deque(maxlen=600)  # últimas líneas del juego, para el diagnóstico

        prof = self.cfg["profiles"][self.cfg["current"]]
        self.selected_ver = prof.get("version")
        self.filtro = tk.StringVar(value=self.cfg.get("filtro", "release"))
        self.loader = tk.StringVar(value=prof.get("loader", "vanilla"))
        self.perfil = tk.StringVar(value=self.cfg["current"])
        self.ram_auto = tk.BooleanVar(value=prof.get("ram", "auto") == "auto")
        cuenta = self.cfg.get("account", "offline")
        if cuenta == "microsoft" and not load_auth().get("refresh_token"):
            cuenta = "offline"
        self.account = tk.StringVar(value=cuenta)
        self.user_var = tk.StringVar(value=self.cfg.get("username", "Jugador"))
        self._acct_prev = "offline"
        self.busqueda = tk.StringVar()
        self.busqueda.trace_add("write", lambda *_: self.refresh_list())

        self.build_ui()
        self.apply_account_ui()
        self.user_var.trace_add("write", lambda *_: self.update_account_card())
        self.pump_log()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        threading.Thread(target=self.load_versions, daemon=True).start()

    # ---------- interfaz ----------
    def build_ui(self):
        self.current_page = "play"
        self.build_sidebar()
        contenido = tk.Frame(self, bg=C["bg"])
        contenido.pack(side="left", fill="both", expand=True, padx=28, pady=22)
        self.tab_play = tk.Frame(contenido, bg=C["bg"])
        self.tab_log = tk.Frame(contenido, bg=C["bg"])
        self.tab_mods = ModsTab(contenido, self)
        self.pages = {"play": self.tab_play, "mods": self.tab_mods, "log": self.tab_log}
        self.build_play_tab()
        self.build_log_tab()
        self.show_page("play")

    def build_sidebar(self):
        side = tk.Frame(self, bg=C["side"], width=210)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="⛏", font=("Segoe UI Emoji", 26), bg=C["side"], fg=C["accent"]).pack(
            anchor="w", padx=22, pady=(24, 0))
        tk.Label(side, text="Mini Launcher", font=(FONT, 15, "bold"), bg=C["side"], fg=C["text"]).pack(
            anchor="w", padx=22)
        tk.Label(side, text="Minecraft Java Edition", font=(FONT, 9), bg=C["side"], fg=C["muted"]).pack(
            anchor="w", padx=22, pady=(0, 24))

        self.nav = {}
        for key, icono, texto in (("play", "▶", "Jugar"), ("mods", "▣", "Mods"), ("log", "≣", "Consola")):
            fila = tk.Frame(side, bg=C["side"], cursor="hand2")
            fila.pack(fill="x", pady=2)
            barra = tk.Frame(fila, bg=C["side"], width=4)
            barra.pack(side="left", fill="y")
            lbl = tk.Label(fila, text=f"{icono}    {texto}", font=(FONT, 11, "bold"), bg=C["side"], fg=C["muted"],
                           anchor="w", padx=18, pady=11, cursor="hand2")
            lbl.pack(side="left", fill="x", expand=True)
            for w in (fila, lbl):
                w.bind("<Button-1>", lambda e, k=key: self.show_page(k))
                w.bind("<Enter>", lambda e, k=key: self.nav_hover(k, True))
                w.bind("<Leave>", lambda e, k=key: self.nav_hover(k, False))
            self.nav[key] = (fila, barra, lbl)

        # tarjeta de cuenta (abajo)
        acc = tk.Frame(side, bg=C["panel"], highlightthickness=1, highlightbackground=C["border"])
        acc.pack(side="bottom", fill="x", padx=14, pady=16)
        self.acc_avatar = tk.Canvas(acc, width=38, height=38, bg=C["panel"], highlightthickness=0)
        self.acc_avatar.pack(side="left", padx=(10, 8), pady=10)
        txt = tk.Frame(acc, bg=C["panel"])
        txt.pack(side="left", fill="x", expand=True)
        self.acc_name = tk.Label(txt, text="", font=(FONT, 10, "bold"), bg=C["panel"], fg=C["text"], anchor="w")
        self.acc_name.pack(fill="x")
        self.acc_sub = tk.Label(txt, text="", font=(FONT, 9), bg=C["panel"], fg=C["muted"], anchor="w")
        self.acc_sub.pack(fill="x")

    def nav_hover(self, key, on):
        if key == self.current_page:
            return
        fila, barra, lbl = self.nav[key]
        bg = C["card"] if on else C["side"]
        fila.config(bg=bg)
        lbl.config(bg=bg)
        barra.config(bg=bg)

    def show_page(self, key):
        for f in self.pages.values():
            f.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        self.current_page = key
        for k, (fila, barra, lbl) in self.nav.items():
            on = k == key
            bg = C["card"] if on else C["side"]
            fila.config(bg=bg)
            lbl.config(bg=bg, fg=C["text"] if on else C["muted"])
            barra.config(bg=C["accent"] if on else bg)
        if key == "mods":
            self.tab_mods.on_show()

    def update_account_card(self):
        if not hasattr(self, "acc_avatar"):
            return
        nombre = self.user_var.get().strip() or "Jugador"
        ms = self.account.get() == "microsoft"
        self.acc_name.config(text=nombre[:20])
        self.acc_sub.config(text="Cuenta Microsoft" if ms else "Modo offline")
        self.acc_avatar.delete("all")
        color = C["accent"] if ms else "#4a5565"
        self.acc_avatar.create_oval(1, 1, 37, 37, fill=color, outline=color)
        self.acc_avatar.create_text(19, 19, text=nombre[:1].upper(), fill=C["accent_fg"] if ms else "#ffffff",
                                    font=(FONT, 14, "bold"))

    def build_play_tab(self):
        root = self.tab_play
        root.columnconfigure(0, minsize=350)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)
        P, T, M, B = C["panel"], C["text"], C["muted"], C["border"]

        # --- izquierda: versiones ---
        izq = tk.Frame(root, bg=P, highlightthickness=1, highlightbackground=B)
        izq.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        izq.columnconfigure(0, weight=1)
        izq.rowconfigure(3, weight=1)
        tk.Label(izq, text="Versiones", font=(FONT, 15, "bold"), bg=P, fg=T).grid(
            row=0, column=0, sticky="w", padx=18, pady=(16, 8))
        ttk.Entry(izq, textvariable=self.busqueda, font=(FONT, 10)).grid(row=1, column=0, sticky="ew", padx=18, ipady=3)
        Segmented(izq, FILTROS, self.filtro, command=self.refresh_list, small=True).grid(
            row=2, column=0, sticky="w", padx=18, pady=10)
        marco = tk.Frame(izq, bg=P)
        marco.grid(row=3, column=0, sticky="nsew", padx=(18, 8))
        marco.rowconfigure(0, weight=1)
        marco.columnconfigure(0, weight=1)
        self.lista = tk.Listbox(marco, activestyle="none", exportselection=False, font=(FONT, 11), bg=P, fg=T,
                                selectbackground=C["accent"], selectforeground=C["accent_fg"], borderwidth=0,
                                highlightthickness=0, relief="flat")
        sb = ttk.Scrollbar(marco, orient="vertical", command=self.lista.yview)
        self.lista.config(yscrollcommand=sb.set)
        self.lista.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.lista.bind("<<ListboxSelect>>", self.on_user_select)
        self.lista.bind("<Double-Button-1>", lambda e: self.play())
        pie = tk.Frame(izq, bg=P)
        pie.grid(row=4, column=0, sticky="ew", padx=18, pady=12)
        tk.Label(pie, text="✔ = ya descargada", font=(FONT, 9), bg=P, fg=M).pack(side="left")
        RoundButton(pie, text="↻ Recargar", style="ghost", width=100, height=28, radius=8, font=(FONT, 9, "bold"),
                    parent_bg=P, command=self.reload_versions).pack(side="right")

        # --- derecha ---
        der = tk.Frame(root, bg=C["bg"])
        der.grid(row=0, column=1, sticky="nsew")
        der.columnconfigure(0, weight=1)
        der.rowconfigure(3, weight=1)

        hero = tk.Frame(der, bg=P, highlightthickness=1, highlightbackground=B)
        hero.grid(row=0, column=0, sticky="ew")
        hero.columnconfigure(1, weight=1)
        self.icon_lbl = tk.Label(hero, text="⛏", font=("Segoe UI Emoji", 28), bg=P, fg=C["accent"])
        self.icon_lbl.grid(row=0, column=0, rowspan=2, padx=(20, 14), pady=18)
        self.hero_name = tk.Label(hero, text="", font=(FONT, 19, "bold"), bg=P, fg=T, anchor="w")
        self.hero_name.grid(row=0, column=1, sticky="sw", pady=(18, 0))
        self.sel_label = tk.Label(hero, text="—", font=(FONT, 11, "bold"), bg=P, fg=C["accent"], anchor="w")
        self.sel_label.grid(row=1, column=1, sticky="nw", pady=(0, 18))
        acciones = tk.Frame(hero, bg=P)
        acciones.grid(row=0, column=2, rowspan=2, padx=16)
        self.combo = ttk.Combobox(acciones, textvariable=self.perfil, state="readonly", width=15,
                                  values=list(self.cfg["profiles"]))
        self.combo.pack(side="left")
        self.combo.bind("<<ComboboxSelected>>", self.on_profile_change)
        for texto, cmd, estilo in (("+", self.new_profile, "ghost"), ("⧉", self.duplicate_profile, "ghost"),
                                   ("⚙", self.profile_settings, "ghost"), ("✕", self.delete_profile, "danger")):
            RoundButton(acciones, text=texto, style=estilo, width=34, height=30, radius=8, parent_bg=P,
                        command=cmd).pack(side="left", padx=(5, 0))
        self.notes_lbl = tk.Label(hero, text="", font=(FONT, 10), bg=P, fg=M, anchor="w", justify="left",
                                  wraplength=520)
        self.notes_lbl.grid(row=2, column=0, columnspan=3, sticky="ew", padx=20, pady=(0, 14))

        ajustes = tk.Frame(der, bg=P, highlightthickness=1, highlightbackground=B)
        ajustes.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        ajustes.columnconfigure(1, weight=1)

        def fila(r, texto):
            tk.Label(ajustes, text=texto, font=(FONT, 10), bg=P, fg=M, width=11, anchor="w").grid(
                row=r, column=0, sticky="w", padx=(20, 6), pady=9)

        fila(0, "Mod loader")
        Segmented(ajustes, LOADERS, self.loader, command=self.on_loader_change).grid(row=0, column=1, sticky="w")
        fila(1, "Cuenta")
        cf = tk.Frame(ajustes, bg=P)
        cf.grid(row=1, column=1, sticky="w")
        Segmented(cf, (("Offline", "offline"), ("Microsoft", "microsoft")), self.account,
                  command=self.on_account_change).pack(side="left")
        RoundButton(cf, text="Cuenta…", style="ghost", width=84, height=30, radius=8, parent_bg=P,
                    font=(FONT, 9, "bold"), command=self.ms_login_dialog).pack(side="left", padx=10)
        fila(2, "Usuario")
        self.user = ttk.Entry(ajustes, textvariable=self.user_var, width=26)
        self.user.grid(row=2, column=1, sticky="w", ipady=2)
        fila(3, "RAM (GB)")
        fr = tk.Frame(ajustes, bg=P)
        fr.grid(row=3, column=1, sticky="w", pady=(0, 4))
        self.ram = ttk.Spinbox(fr, from_=1, to=64, width=6)
        self.ram.pack(side="left")
        ttk.Checkbutton(fr, text=f"Auto ({auto_ram_gb()} GB)", variable=self.ram_auto,
                        command=self.on_ram_auto).pack(side="left", padx=(12, 0))
        self.load_ram_widgets(self.cfg["profiles"][self.cfg["current"]])

        carpetas = tk.Frame(der, bg=C["bg"])
        carpetas.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        tk.Label(carpetas, text="Carpetas", font=(FONT, 10), bg=C["bg"], fg=M).pack(side="left", padx=(2, 10))
        accesos = [("Mods", "mods"), ("Capturas", "screenshots"), ("Mundos", "saves"), ("Crashes", "crash-reports"),
                   ("Carpeta", "")]
        for texto, sub in accesos:
            RoundButton(carpetas, text=texto, style="ghost", width=82, height=30, radius=8, parent_bg=C["bg"],
                        font=(FONT, 9, "bold"), command=lambda s=sub: self.open_profile_folder(s)).pack(
                side="left", padx=3)

        abajo = tk.Frame(der, bg=C["bg"])
        abajo.grid(row=4, column=0, sticky="ew")
        self.status = tk.Label(abajo, text="Cargando versiones...", font=(FONT, 10), bg=C["bg"], fg=M, anchor="w",
                               justify="left", wraplength=560)
        self.status.pack(fill="x")
        self.bar = ttk.Progressbar(abajo, mode="determinate")
        self.bar.pack(fill="x", pady=(6, 12))
        filab = tk.Frame(abajo, bg=C["bg"])
        filab.pack(fill="x")
        self.btn = RoundButton(filab, text="JUGAR", command=self.on_main_btn, state="disabled", height=58,
                               radius=14, font=(FONT, 16, "bold"), parent_bg=C["bg"])
        self.btn.pack(side="left", fill="x", expand=True)
        self.btn_stop = RoundButton(filab, text="Detener", style="danger", width=110, height=58, radius=14,
                                    state="disabled", command=self.stop_game, parent_bg=C["bg"])
        self.btn_stop.pack(side="left", padx=(12, 0))
        self.update_profile_badge()
        self.update_account_card()

    def build_log_tab(self):
        t = self.tab_log
        t.rowconfigure(1, weight=1)
        t.columnconfigure(0, weight=1)
        cab = tk.Frame(t, bg=C["bg"])
        cab.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        tk.Label(cab, text="Consola", font=(FONT, 22, "bold"), bg=C["bg"], fg=C["text"]).pack(side="left")
        RoundButton(cab, text="Copiar todo", style="ghost", width=110, height=32, parent_bg=C["bg"],
                    command=self.copy_log).pack(side="right")
        RoundButton(cab, text="Limpiar", style="ghost", width=90, height=32, parent_bg=C["bg"],
                    command=self.clear_log).pack(side="right", padx=8)

        self.console = tk.Text(t, wrap="none", state="disabled", font=("Consolas", 9), background="#0a0c10",
                               foreground="#cfd6e1", relief="flat", highlightthickness=1,
                               highlightbackground=C["border"], insertbackground="#cfd6e1", padx=10, pady=8)
        sy = ttk.Scrollbar(t, orient="vertical", command=self.console.yview)
        sx = ttk.Scrollbar(t, orient="horizontal", command=self.console.xview)
        self.console.config(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.console.grid(row=1, column=0, sticky="nsew")
        sy.grid(row=1, column=1, sticky="ns")
        sx.grid(row=2, column=0, sticky="ew")

    # ---------- consola ----------
    def log(self, text):
        self.logq.put(text)

    def pump_log(self):
        lines = []
        try:
            for _ in range(500):
                lines.append(self.logq.get_nowait())
        except queue.Empty:
            pass
        if lines:
            at_end = self.console.yview()[1] >= 0.999
            self.console.config(state="normal")
            self.console.insert("end", "".join(l if l.endswith("\n") else l + "\n" for l in lines))
            total = int(self.console.index("end-1c").split(".")[0])
            if total > MAX_LOG_LINES:
                self.console.delete("1.0", f"{total - MAX_LOG_LINES}.0")
            self.console.config(state="disabled")
            if at_end:
                self.console.see("end")
        self.after(100, self.pump_log)

    def clear_log(self):
        self.console.config(state="normal")
        self.console.delete("1.0", "end")
        self.console.config(state="disabled")

    def copy_log(self):
        self.clipboard_clear()
        self.clipboard_append(self.console.get("1.0", "end"))
        self.set_status("Consola copiada al portapapeles")

    # ---------- configuración y perfiles ----------
    def manual_ram(self):
        try:
            return max(1, min(64, int(self.ram.get())))
        except ValueError:
            return 4

    def get_ram(self):
        return auto_ram_gb() if self.ram_auto.get() else self.manual_ram()

    def save_current_profile(self):
        ver = self.selected() or self.selected_ver
        p = self.cfg["profiles"].setdefault(self.perfil.get(), {})
        p.update(version=ver, loader=self.loader.get(), ram="auto" if self.ram_auto.get() else self.manual_ram())
        self.cfg["current"] = self.perfil.get()
        if self.account.get() == "offline":
            self.cfg["username"] = self.user.get().strip() or "Jugador"
        self.cfg["account"] = self.account.get()
        self.cfg["ram"] = self.manual_ram()
        self.cfg["filtro"] = self.filtro.get()
        save_config(self.cfg)

    def profile_dir(self, name=None):
        safe = re.sub(r'[\\/:*?"<>|]', "_", (name or self.perfil.get())).strip() or "Default"
        return os.path.join(INSTANCES_DIR, safe)

    def open_profile_folder(self, sub):
        open_folder(os.path.join(self.profile_dir(), sub))

    def apply_profile(self, name):
        p = self.cfg["profiles"][name]
        self.perfil.set(name)
        self.loader.set(p.get("loader", "vanilla"))
        self.selected_ver = p.get("version")
        self.cfg["current"] = name
        self.load_ram_widgets(p)
        self.update_profile_badge()

    def ensure_visible(self):
        """Ajusta el filtro de tipo para que la versión del perfil aparezca en la lista."""
        t = next((v["type"] for v in self.versions if v["id"] == self.selected_ver), None)
        if t and self.filtro.get() not in ("all", t):
            self.filtro.set(t)

    def on_profile_change(self, _e=None):
        self.apply_profile(self.perfil.get())
        self.ensure_visible()
        self.refresh_list()
        save_config(self.cfg)

    def new_profile(self):
        name = simpledialog.askstring("Nuevo perfil", "Nombre del perfil:", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in self.cfg["profiles"]:
            messagebox.showwarning("Perfil existente", "Ya existe un perfil con ese nombre.")
            return
        # Se crea a partir de la selección actual (hereda RAM y ajustes de rendimiento)
        base = {k: v for k, v in self.cfg["profiles"].get(self.perfil.get(), {}).items()
                if k in ("ram", "jvm_args", "optimize", "perf_mods")}
        base.update(version=self.selected() or self.selected_ver, loader=self.loader.get())
        self.cfg["profiles"][name] = base
        self.perfil.set(name)
        self.combo["values"] = list(self.cfg["profiles"])
        os.makedirs(self.profile_dir(name), exist_ok=True)
        self.save_current_profile()

    def delete_profile(self):
        name = self.perfil.get()
        if len(self.cfg["profiles"]) <= 1:
            messagebox.showinfo("No se puede", "Debe quedar al menos un perfil.")
            return
        if not messagebox.askyesno(
                "Borrar perfil",
                f"¿Quitar el perfil '{name}'?\n\nSus archivos (mods, mundos) NO se borran:\n{self.profile_dir(name)}"):
            return
        del self.cfg["profiles"][name]
        self.combo["values"] = list(self.cfg["profiles"])
        self.apply_profile(next(iter(self.cfg["profiles"])))
        self.ensure_visible()
        self.refresh_list()
        save_config(self.cfg)

    def on_loader_change(self):
        self.refresh_list()
        self.save_current_profile()

    # ---------- versiones ----------
    def load_versions(self):
        try:
            self.versions = mll.utils.get_version_list()
            self.online = True
        except Exception as e:
            self.online = False
            self.versions = installed_mc_versions()  # modo sin conexión: solo lo ya descargado
            if not self.versions:
                msg = str(e)
                self.after(0, lambda: self.set_status(f"Sin conexión y sin versiones descargadas: {msg}"))
                return
            self.fabric_vers, self.forge_vers = set(), set()
            self.after(0, self.versions_ready)
            return
        try:
            self.fabric_vers = {v["version"] for v in mll.fabric.get_all_minecraft_versions()}
        except Exception:
            self.fabric_vers = set()
        try:
            self.forge_vers = {fv.split("-")[0] for fv in mll.forge.list_forge_versions()}
        except Exception:
            self.forge_vers = set()
        self.after(0, self.versions_ready)

    def versions_ready(self):
        if not self.selected_ver:
            self.selected_ver = next((v["id"] for v in self.versions if v["type"] == "release"), None)
        self.ensure_visible()
        self.refresh_list()
        self.set_status("Listo" if self.online else "Sin conexión: solo versiones ya descargadas (↻ para reintentar)")

    def refresh_installed(self):
        try:
            self.installed = {v["id"] for v in mll.utils.get_installed_versions(MC_DIR)}
        except Exception:
            self.installed = set()

    def is_installed(self, mc_ver, loader):
        if loader == "vanilla":
            return mc_ver in self.installed
        if loader == "fabric":
            return any(i.startswith("fabric-loader-") and i.endswith("-" + mc_ver) for i in self.installed)
        return any(i.startswith(mc_ver + "-forge") for i in self.installed)

    def refresh_list(self):
        self.refresh_installed()
        f = self.filtro.get()
        loader = self.loader.get()
        q = self.busqueda.get().strip().lower()

        soportadas = None
        if loader == "fabric" and self.fabric_vers:
            soportadas = self.fabric_vers
        elif loader == "forge" and self.forge_vers:
            soportadas = self.forge_vers

        self.shown = [
            v["id"] for v in self.versions
            if (f == "all" or v["type"] == f)
            and q in v["id"].lower()
            and (soportadas is None or v["id"] in soportadas)
        ]
        self.lista.delete(0, "end")
        for vid in self.shown:
            self.lista.insert("end", ("✔ " if self.is_installed(vid, loader) else "    ") + vid)

        if self.shown:
            idx = self.shown.index(self.selected_ver) if self.selected_ver in self.shown else 0
            self.lista.selection_set(idx)
            self.lista.see(idx)
        self.update_label()
        self.set_btn()

    def selected(self):
        sel = self.lista.curselection()
        return self.shown[sel[0]] if sel else None

    def on_user_select(self, _e=None):
        ver = self.selected()
        if ver:
            self.selected_ver = ver
            self.save_current_profile()
        self.update_label()
        self.set_btn()

    def update_label(self):
        ver = self.selected() or self.selected_ver
        self.sel_label.config(text=f"{LOADER_NAMES[self.loader.get()]} {ver}" if ver else "—")

    def set_btn(self):
        if self.phase == "installing":
            self.btn.config(text="CANCELAR", style="danger", state="normal")
        else:
            ok = bool(self.selected()) and not self.running
            self.btn.config(text="JUGAR", style="accent", state="normal" if ok else "disabled")

    # ---------- helpers de estado ----------
    def set_status(self, text):
        self.status.config(text=text)

    def check_cancel(self):
        if self.cancel.is_set():
            raise InstallCancelled()

    def callbacks(self):
        # Si el usuario cancela, lanzar la excepción desde el callback corta la descarga en el siguiente avance.
        def status(t):
            self.check_cancel()
            self.log(t)
            self.after(0, lambda: self.set_status(t))

        def progress(v):
            self.check_cancel()
            self.after(0, lambda: self.bar.config(value=v))

        def maximum(m):
            self.check_cancel()
            self.after(0, lambda: self.bar.config(maximum=max(m, 1)))

        return {"setStatus": status, "setProgress": progress, "setMax": maximum}

    def java_exec(self, ver):
        """Java que Minecraft descargó para esa versión (lo necesitan los instaladores de Fabric y Forge)."""
        try:
            path = os.path.join(MC_DIR, "versions", ver, f"{ver}.json")
            with open(path, encoding="utf-8") as f:
                comp = json.load(f).get("javaVersion", {}).get("component", "jre-legacy")
            return mll.runtime.get_executable_path(comp, MC_DIR)
        except Exception:
            return None

    def installed_loader(self, ver, loader):
        """ID del Fabric/Forge ya instalado para esa versión de Minecraft (el más reciente), o None."""
        def nums(s):
            return [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", s)]

        if loader == "fabric":
            pre, suf = "fabric-loader-", "-" + ver
            ids = [i for i in self.installed if i.startswith(pre) and i.endswith(suf)]
            return max(ids, key=lambda i: nums(i[len(pre):-len(suf)]), default=None)
        pre = ver + "-forge"
        ids = [i for i in self.installed if i.startswith(pre)]
        return max(ids, key=lambda i: nums(i[len(pre):]), default=None)

    # ---------- instalación ----------
    def install_version(self, ver, loader):
        """Instala todo lo necesario y devuelve el id de versión a lanzar. Funciona sin internet si ya está descargado."""
        cb = self.callbacks()

        tiene = ver in self.installed
        if not self.online and not tiene:
            raise RuntimeError(f"Sin conexión: la versión {ver} no está descargada.")
        if self.online or not tiene:
            try:
                mll.install.install_minecraft_version(ver, MC_DIR, callback=cb)
            except InstallCancelled:
                raise
            except Exception as e:
                if not tiene:
                    raise
                self.log(f"No se pudo verificar los archivos ({e}); se usa la instalación existente.")
        self.check_cancel()
        if loader == "vanilla":
            return ver

        # Si ese loader ya está instalado se usa directamente (más rápido y sin red)
        vid = self.installed_loader(ver, loader)
        if vid:
            return vid
        if not self.online:
            raise RuntimeError(f"Sin conexión: {LOADER_NAMES[loader]} no está instalado para {ver}.")

        if loader == "fabric":
            if not mll.fabric.is_minecraft_version_supported(ver):
                raise RuntimeError(f"Fabric no soporta la versión {ver}.")
            loader_ver = mll.fabric.get_latest_loader_version()
            vid = f"fabric-loader-{loader_ver}-{ver}"
            self.log("Instalando Fabric...")
            self.after(0, lambda: self.set_status("Instalando Fabric..."))
            java = self.java_exec(ver)  # Java descargado por el launcher (no depende del PATH)
            if java:
                mll.fabric.install_fabric(ver, MC_DIR, loader_version=loader_ver, callback=cb, java=java)
            else:
                mll.fabric.install_fabric(ver, MC_DIR, loader_version=loader_ver, callback=cb)
            return vid

        fv = mll.forge.find_forge_version(ver)
        if fv is None:
            raise RuntimeError(f"No hay Forge disponible para {ver}.")
        if not mll.forge.supports_automatic_install(fv):
            raise RuntimeError(f"Forge {fv} no permite instalación automática (versión muy antigua).")
        vid = mll.forge.forge_to_installed_version(fv)
        self.log("Instalando Forge (puede tardar)...")
        self.after(0, lambda: self.set_status("Instalando Forge (puede tardar)..."))
        java = self.java_exec(ver)
        if java:
            mll.forge.install_forge_version(fv, MC_DIR, callback=cb, java=java)
        else:
            mll.forge.install_forge_version(fv, MC_DIR, callback=cb)
        return vid

    def ensure_perf_mods(self, pname, ver, loader, root, prof):
        """Instala una sola vez (por versión/loader) los mods de rendimiento compatibles."""
        tag = f"{loader}:{ver}"
        if prof.get("perf_done") == tag:
            return
        if not self.online:
            self.log("Sin conexión: se omiten los mods de rendimiento.")
            return
        self.after(0, lambda: self.set_status("Instalando mods de rendimiento..."))
        ctx = {"profile": pname, "ver": ver, "loader": loader, "root": root, "dir": os.path.join(root, "mods")}
        try:
            nombres, avisos = self.tab_mods.install_slugs(ctx, PERF_MODS[loader])
        except InstallCancelled:
            raise
        except Exception as e:
            self.log(f"No se pudieron instalar los mods de rendimiento (se reintentará la próxima vez): {e}")
            return
        self.log("Mods de rendimiento: " + (", ".join(nombres) or "ya estaban o ninguno es compatible con esta versión"))
        for a in avisos:
            self.log(a)
        self.after(0, lambda: self.mark_perf_done(pname, tag))

    def mark_perf_done(self, pname, tag):
        if pname in self.cfg["profiles"]:
            self.cfg["profiles"][pname]["perf_done"] = tag
            save_config(self.cfg)

    # ---------- jugar ----------
    def set_phase(self, phase):
        self.phase = phase
        self.set_btn()

    def on_main_btn(self):
        if self.phase == "installing":
            self.cancel.set()
            self.btn.config(state="disabled")
            self.set_status("Cancelando...")
        else:
            self.play()

    def play(self):
        if self.running:
            return
        mode = self.account.get()
        name = self.user.get().strip()
        ver = self.selected()
        if not ver or (mode == "offline" and not name):
            messagebox.showwarning("Falta algo", "Pon un usuario y selecciona una versión.")
            return
        if mode == "microsoft" and not load_auth().get("refresh_token"):
            messagebox.showwarning("Cuenta Microsoft", "Primero inicia sesión con tu cuenta Microsoft (botón 'Cuenta…').")
            self.ms_login_dialog()
            return
        loader = self.loader.get()
        self.selected_ver = ver
        self.save_current_profile()
        pname = self.perfil.get()
        gamedir = self.profile_dir()

        if loader != "vanilla":
            malos = incompatible_mods(gamedir, os.path.join(gamedir, "mods"), ver, loader)
            if malos:
                lista = "\n".join("• " + m for m in malos[:12]) + ("\n..." if len(malos) > 12 else "")
                if not messagebox.askyesno(
                        "Mods posiblemente incompatibles",
                        f"Estos mods no declaran compatibilidad con {LOADER_NAMES[loader]} {ver} y podrían "
                        f"hacer fallar el juego:\n\n{lista}\n\n¿Iniciar de todas formas?"):
                    self.show_page("mods")
                    return

        prof = dict(self.cfg["profiles"][pname])
        self.running = True
        self.stopped = False
        self.cancel.clear()
        self.tail.clear()
        self.set_phase("installing")
        self.log(f"\n=== Perfil '{pname}': {LOADER_NAMES[loader]} {ver} ===")
        threading.Thread(
            target=self.run_game,
            args=(name, ver, loader, gamedir, self.get_ram(), prof, mode, pname),
            daemon=True,
        ).start()

    def run_game(self, name, ver, loader, gamedir, ram, prof, mode, pname):
        code = None
        try:
            self.refresh_installed()
            vid = self.install_version(ver, loader)
            os.makedirs(gamedir, exist_ok=True)

            optimize = prof.get("optimize", True)
            if optimize:
                write_default_options(gamedir)
            if loader in PERF_MODS and prof.get("perf_mods", True):
                self.ensure_perf_mods(pname, ver, loader, gamedir, prof)
            self.check_cancel()

            username, uid, token = name, offline_uuid(name), "0"
            if mode == "microsoft":
                self.after(0, lambda: self.set_status("Verificando cuenta Microsoft..."))
                username, uid, token = MicrosoftAuth(self.cfg.get("ms_client_id", "")).session()
                self.check_cancel()

            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
            for intento in (0, 1):
                options = {
                    "username": username,
                    "uuid": uid,
                    "token": token,
                    "gameDirectory": gamedir,
                    "jvmArguments": build_jvm_args(ram, optimize, prof.get("jvm_args", "")),
                }
                cmd = mll.command.get_minecraft_command(vid, MC_DIR, options)

                self.log(f"Iniciando juego en: {gamedir} (RAM {ram} GB{', optimizado' if optimize else ''})")
                self.after(0, lambda: self.set_status("Jugando..."))
                self.after(0, lambda: self.btn_stop.config(state="normal"))
                self.after(0, lambda: self.set_phase("playing"))

                started = time.time()
                self.tail.clear()
                self.proc = subprocess.Popen(
                    cmd, cwd=gamedir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags,
                )
                for line in self.proc.stdout:
                    self.logq.put(line)
                    self.tail.append(line)
                code = self.proc.wait()
                self.log(f"=== El juego terminó (código {code}) ===")

                # Si Java rechazó nuestros parámetros, se reintenta una vez con valores seguros
                salida = "".join(self.tail)
                if code != 0 and not self.stopped and intento == 0 and JVM_FAIL_RE.search(salida):
                    self.log("Java rechazó los parámetros; reintentando sin optimizaciones y con menos RAM...")
                    optimize = False
                    ram = max(1, ram // 2)
                    prof = dict(prof, jvm_args="")
                    continue
                break

            if code != 0 and not self.stopped:
                diag, _report = diagnose_crash(gamedir, started, list(self.tail), ram)
                self.log("\n=== Diagnóstico ===\n" + diag)
                self.after(0, lambda: self.show_crash(code, diag))
            else:
                self.after(0, lambda: self.set_status("Listo"))
        except InstallCancelled:
            self.log("=== Cancelado ===")
            self.after(0, lambda: self.set_status("Cancelado"))
        except Exception as e:
            msg = str(e)
            self.log(f"ERROR: {msg}")
            self.after(0, lambda: messagebox.showerror("Error", msg))
            self.after(0, lambda: self.set_status("Error"))
            self.after(0, lambda: self.show_page("log"))
        finally:
            self.proc = None
            self.running = False
            self.phase = "idle"
            self.cancel.clear()
            self.after(0, lambda: self.bar.config(value=0))
            self.after(0, lambda: self.btn_stop.config(state="disabled"))
            self.after(0, self.refresh_list)

    def show_crash(self, code, diag):
        self.set_status(f"El juego se cerró con errores (código {code})")
        self.show_page("log")
        messagebox.showwarning("El juego se cerró con errores", diag)

    def stop_game(self):
        if self.proc and self.proc.poll() is None:
            self.stopped = True
            self.log("Deteniendo el juego...")
            self.proc.terminate()

    # ---------- RAM por perfil ----------
    def _ram_state(self):
        self.ram.config(state="disabled" if self.ram_auto.get() else "normal")

    def on_ram_auto(self):
        self._ram_state()
        self.save_current_profile()

    def load_ram_widgets(self, p):
        r = p.get("ram", "auto")
        self.ram_auto.set(r == "auto")
        self.ram.config(state="normal")
        self.ram.set(r if r != "auto" else self.cfg.get("ram", 4))
        self._ram_state()

    # ---------- ajustes del perfil ----------
    def update_profile_badge(self):
        p = self.cfg["profiles"].get(self.perfil.get(), {})
        self.icon_lbl.config(text=p.get("icon") or "⛏")
        self.hero_name.config(text=self.perfil.get())
        notas = " ".join(p.get("notes", "").split())
        self.notes_lbl.config(text=(notas[:120] + "…") if len(notas) > 120 else notas)
        if notas:
            self.notes_lbl.grid()
        else:
            self.notes_lbl.grid_remove()

    def profile_settings(self):
        name = self.perfil.get()
        p = self.cfg["profiles"][name]
        win = tk.Toplevel(self)
        win.title(f"Ajustes de '{name}'")
        win.transient(self)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)

        icono = tk.StringVar(value=p.get("icon", ""))
        jvm = tk.StringVar(value=p.get("jvm_args", ""))
        opt = tk.BooleanVar(value=p.get("optimize", True))
        perf = tk.BooleanVar(value=p.get("perf_mods", True))

        ttk.Label(frm, text="Icono").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Combobox(frm, textvariable=icono, values=[""] + ICONS, width=6, state="readonly").grid(
            row=0, column=1, sticky="w", pady=2)
        ttk.Label(frm, text="Notas").grid(row=1, column=0, sticky="nw", pady=2)
        notas = tk.Text(frm, width=42, height=4, wrap="word", bg=C["input"], fg=C["text"], insertbackground=C["text"],
                        relief="flat", highlightthickness=1, highlightbackground=C["border"])
        notas.insert("1.0", p.get("notes", ""))
        notas.grid(row=1, column=1, pady=2)
        ttk.Label(frm, text="Argumentos JVM extra").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=jvm, width=44).grid(row=2, column=1, pady=2)
        ttk.Checkbutton(frm, text="Optimizar rendimiento (flags de Java + ajustes gráficos iniciales)",
                        variable=opt).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(frm, text="Instalar mods de rendimiento (Sodium, Lithium... con Fabric/Forge)",
                        variable=perf).grid(row=4, column=0, columnspan=2, sticky="w")

        def guardar():
            if perf.get() and not p.get("perf_mods", True):
                p["perf_done"] = ""  # se volvió a activar: que se reinstalen en el próximo inicio
            p.update(icon=icono.get(), notes=notas.get("1.0", "end").strip(), jvm_args=jvm.get().strip(),
                     optimize=opt.get(), perf_mods=perf.get())
            save_config(self.cfg)
            self.update_profile_badge()
            win.destroy()

        fila = ttk.Frame(frm)
        fila.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(fila, text="Cancelar", command=win.destroy).pack(side="right")
        ttk.Button(fila, text="Guardar", style="Accent.TButton", command=guardar).pack(side="right", padx=6)
        win.grab_set()

    def duplicate_profile(self):
        if self.running:
            messagebox.showinfo("Juego en uso", "Espera a que termine el juego antes de duplicar el perfil.")
            return
        src = self.perfil.get()
        name = simpledialog.askstring("Duplicar perfil", f"Nombre de la copia de '{src}':", parent=self,
                                      initialvalue=f"{src} (copia)")
        if not name or not name.strip():
            return
        name = name.strip()
        dst_dir = self.profile_dir(name)
        if name in self.cfg["profiles"] or os.path.exists(dst_dir):
            messagebox.showwarning("Perfil existente", "Ya existe un perfil (o carpeta) con ese nombre.")
            return
        con_mundos = messagebox.askyesno("Duplicar perfil", "¿Copiar también los mundos (carpeta saves)?\n"
                                                           "Los mods y ajustes siempre se copian.")
        self.save_current_profile()
        src_dir = self.profile_dir(src)
        datos = json.loads(json.dumps(self.cfg["profiles"][src]))
        self.set_status("Copiando perfil...")

        def work():
            try:
                if os.path.isdir(src_dir):
                    ignorar = ["crash-reports", "logs"] + ([] if con_mundos else ["saves"])
                    shutil.copytree(src_dir, dst_dir, ignore=shutil.ignore_patterns(*ignorar))
                else:
                    os.makedirs(dst_dir)
                err = None
            except Exception as e:
                err = str(e)
            self.after(0, lambda: listo(err))

        def listo(err):
            if err:
                self.set_status("Error al copiar")
                messagebox.showerror("Error", f"No se pudo duplicar el perfil:\n{err}")
                return
            self.cfg["profiles"][name] = datos
            self.combo["values"] = list(self.cfg["profiles"])
            self.apply_profile(name)
            self.ensure_visible()
            self.refresh_list()
            save_config(self.cfg)
            self.set_status(f"Perfil '{name}' creado")

        threading.Thread(target=work, daemon=True).start()

    # ---------- versiones ----------
    def reload_versions(self):
        self.set_status("Cargando versiones...")
        threading.Thread(target=self.load_versions, daemon=True).start()

    # ---------- cuenta ----------
    def apply_account_ui(self):
        modo = self.account.get()
        if modo == "microsoft":
            if self._acct_prev == "offline":  # guarda el nombre offline antes de mostrar el de Microsoft
                self.cfg["username"] = self.user.get().strip() or "Jugador"
            self.user.config(state="normal")
            self.user.delete(0, "end")
            self.user.insert(0, load_auth().get("name", ""))
            self.user.config(state="disabled")
        else:
            self.user.config(state="normal")
            self.user.delete(0, "end")
            self.user.insert(0, self.cfg.get("username", "Jugador"))
        self._acct_prev = modo
        self.update_account_card()

    def on_account_change(self):
        if self.account.get() == "microsoft" and not load_auth().get("refresh_token"):
            self.ms_login_dialog()
            if not load_auth().get("refresh_token"):
                self.account.set("offline")
        self.apply_account_ui()
        self.save_current_profile()

    def ms_login_dialog(self):
        win = tk.Toplevel(self)
        win.title("Cuenta Microsoft")
        win.transient(self)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=12)
        frm.pack()

        cid = tk.StringVar(value=self.cfg.get("ms_client_id", ""))
        guardada = load_auth()
        info = tk.StringVar(value=f"Sesión iniciada: {guardada['name']}" if guardada.get("name")
                            else "Sin sesión iniciada.")
        codigo = tk.StringVar()
        detener = threading.Event()

        ttk.Label(frm, textvariable=info, font=("Segoe UI", 10, "bold"), wraplength=400).pack(anchor="w")
        ttk.Label(frm, text="Client ID de tu app de Azure").pack(anchor="w", pady=(10, 0))
        ttk.Entry(frm, textvariable=cid, width=48).pack(fill="x")
        ttk.Label(frm, justify="left", foreground="gray", wraplength=400, text=(
            "Necesitas registrar una app gratuita en portal.azure.com → Registros de aplicaciones "
            "(cuentas 'solo personales de Microsoft', activa 'Permitir flujos de cliente público') y pegar aquí "
            "su ID de aplicación. Además Mojang exige aprobar la app para usar su API (aka.ms/AppRegInfo); "
            "sin eso el inicio de sesión falla con 'Invalid app registration'.")).pack(anchor="w", pady=(6, 6))
        ttk.Label(frm, textvariable=codigo, font=("Consolas", 16, "bold")).pack()

        def vivo():
            return win.winfo_exists()

        def ok(nombre):
            if not vivo():
                return
            info.set(f"Sesión iniciada: {nombre}")
            codigo.set("")
            btn.config(state="normal")
            self.account.set("microsoft")
            self.apply_account_ui()
            self.save_current_profile()

        def fallo(msg):
            if not vivo():
                return
            info.set("No se pudo iniciar sesión.")
            codigo.set("")
            btn.config(state="normal")
            messagebox.showerror("Cuenta Microsoft", msg, parent=win)

        def mostrar(d):
            if vivo():
                codigo.set(d["user_code"])
                info.set(f"Se abrió {d['verification_uri']} — escribe este código:")

        def entrar():
            client = cid.get().strip()
            if not client:
                messagebox.showwarning("Cuenta Microsoft", "Pega el Client ID de tu app de Azure.", parent=win)
                return
            self.cfg["ms_client_id"] = client
            save_config(self.cfg)
            btn.config(state="disabled")
            detener.clear()

            def work():
                try:
                    auth = MicrosoftAuth(client)
                    d = auth.start_device_flow()
                    self.after(0, lambda: mostrar(d))
                    webbrowser.open(d["verification_uri"])
                    t = auth.poll_device_flow(d, detener.is_set)
                    _tok, nombre, uid = auth.minecraft_login(t["access_token"])
                    save_auth({"refresh_token": t["refresh_token"], "name": nombre, "uuid": uid})
                    self.after(0, lambda: ok(nombre))
                except InstallCancelled:
                    return
                except Exception as e:
                    msg = str(e)
                    self.after(0, lambda: fallo(msg))

            threading.Thread(target=work, daemon=True).start()

        def salir_cuenta():
            clear_auth()
            info.set("Sin sesión iniciada.")
            self.account.set("offline")
            self.apply_account_ui()
            self.save_current_profile()

        def cerrar():
            detener.set()
            win.destroy()

        fila = ttk.Frame(frm)
        fila.pack(fill="x", pady=(8, 0))
        btn = ttk.Button(fila, text="Iniciar sesión", command=entrar)
        btn.pack(side="left")
        ttk.Button(fila, text="Cerrar sesión", command=salir_cuenta).pack(side="left", padx=6)
        ttk.Button(fila, text="Cerrar", command=cerrar).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", cerrar)
        win.grab_set()
        self.wait_window(win)

    # ---------- cierre ----------
    def on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askokcancel("Juego abierto",
                                          "El juego sigue abierto. Si cierras el launcher, también se cerrará."):
                return
            self.proc.terminate()
        self.save_current_profile()
        self.destroy()


if __name__ == "__main__":
    Launcher().mainloop()