#!/usr/bin/python3
# <xbar.title>OpenCode Usage</xbar.title>
# <xbar.desc>Menu bar quota widget for OpenCode: shows the Go plan (5h / weekly / monthly) as a segmented meter, or detects a Zen pay-as-you-go account. Seven languages, multiple named keys, low-usage notification; everything is set from the menu.</xbar.desc>
# <xbar.author>luyi</xbar.author>
# <xbar.version>2.5.1</xbar.version>
# <xbar.dependencies>python3,Pillow</xbar.dependencies>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
#
# Refresh interval comes from the file name: *.5m.py -> every 5 minutes.
#
# Configuration lives in ~/.config/opencode-usage/config.json (0600):
#   {
#     "lang": "zh" | "en",
#     "active": 0,
#     "keys": [{"key": "oc_sk_...", "added": 1700000000}, ...]
#   }
# Everything in that file can be edited from the dropdown — the plugin re-invokes
# itself with a --flag argument (SwiftBar's `bash=` + `params=`) to run each action,
# so the whole thing stays a single file you can drop into SwiftBar.
#
# Why the title is a generated image instead of plain text:
#   SwiftBar renders a multi-line title by stacking the text block upward, and the
#   menu bar is only ~37pt tall, so a 3-line title gets clipped even at 9pt
#   (at 12pt only the last line survives). Drawing our own PNG gives us full
#   control over height; the status bar scales the whole image down to fit.
#   Measured ceiling for a status item image on macOS: 44px = 22pt. Beyond that
#   the system rescales it and the glyphs go soft, so TITLE_H is pinned to 44.
#
# Requirements: Pillow. If the ambient python3 lacks it, this script re-executes
# itself with another python3 that has it (see find_pil_python). If none is found
# it falls back to a plain single-line text title so the item never goes blank.

import base64
import calendar
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_ROOT = "https://opencode.ai"
GO_USAGE_URL = API_ROOT + "/zen/go/v1/usage"
ZEN_MODELS_URL = API_ROOT + "/zen/v1/models"
DASHBOARD_URL = API_ROOT + "/zen"
GO_URL = API_ROOT + "/go"
KEYS_URL = API_ROOT + "/zen"

KEY_ENV = "OPENCODE_API_KEY"
CONFIG_DIR = os.path.expanduser("~/.config/opencode-usage")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# Last good reading, so a dropped network keeps the numbers on screen instead of
# silently degrading the widget into its "unknown" state.
DATA_DIR = os.environ.get("SWIFTBAR_PLUGIN_DATA_PATH") or \
    os.path.expanduser("~/.cache/opencode-usage")
CACHE_PATH = os.path.join(DATA_DIR, "last-usage.json")
NOTIFY_PATH = os.path.join(DATA_DIR, "notified.json")

# Go plan quota windows, in the order they appear in the menu.
WINDOWS = ["rolling", "weekly", "monthly"]

MONO = "Menlo"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTHS_ES = ["ene", "feb", "mar", "abr", "may", "jun",
             "jul", "ago", "sep", "oct", "nov", "dic"]
MONTHS_FR = ["janv.", "févr.", "mars", "avr.", "mai", "juin",
             "juil.", "août", "sept.", "oct.", "nov.", "déc."]
# Rows that need no special colour leave `color` off: the system label colour is already
# correct in both appearances, and — measured on macOS 26 — setting `color` on a row makes
# SwiftBar drop that row's `sfimage`, so default-coloured rows are the ones that get icons.
# The catch is that a row with no action, href or submenu is auto-disabled by AppKit and
# painted grey, which made the plain heading rows look washed out. So such rows state the
# label colour explicitly. Nothing uses `md=true`: its attributed path renders lighter.
TEXT = "#000000,#ffffff"         # explicit label colour, for rows that would grey out
MUTED = "#6e6e73,#a1a1a6"        # secondary lines: hints, captions
ALERT_TEXT = "#b4342f,#e5605a"

# How far ahead of the window's own clock a window may run before we say so. 0.15 = 15
# percentage points of budget burned more than the elapsed share of the window.
PACE_TOLERANCE = 0.15
# One notification per window per reset cycle, once a window passes this.
NOTIFY_PCT = 85
WINDOW_ICON = {"rolling": "clock", "weekly": "calendar", "monthly": "calendar.badge.clock"}

# Progress bar is built from ANSI background-coloured cells, not an image: AppKit puts
# any image attached to a menu row into a ~16pt icon slot (measured: a 240pt-wide bar
# came out 16pt wide), so an image can never span the menu. Coloured cells have no
# glyph seams, and the background fills the whole line box, which is what makes the bar
# look solid and thick. Cell width is fixed by the font, so width = cells x advance.
#
# The bar row must NOT set a `size`. SwiftBar measures a row at the menu's default font but
# paints it at the row's own `size`, and the two disagree by a constant factor: at size=9
# the meter always rendered at 75% of the width it reserved (measured across 49/100/125-cell
# builds — the menu grew with the cell count but the ratio never moved). Without a `size`,
# measurement and paint use the same font, so the meter runs exactly edge to edge. The row
# is taller this way; that is the price of the width.
SEG_COUNT = 50
SEG_CELLS = 1
BAR_TRACK = 250                     # ANSI 256-colour index: light grey track
ESC = "\x1b"

# ── title image ──
TITLE_H = 44          # px at 2x; 44 = 22pt (status bar ceiling)
FS_RATIO = 1.10       # font size / line height; >1 squeezes the leading, fatter glyphs
LOGO_RATIO = 0.74     # badge edge as a share of image height
STROKE = 0            # extra outline on digits (0 = none)
FONT_PATH = "/System/Library/Fonts/Menlo.ttc"
FONT_INDEX = 1        # Menlo Bold

# ── monochrome logo (template image: alpha is treated as grey level) ──
SQ_RADIUS = 0.17
SQ_STROKE = 0.055
PAGE_BOX = (0.23, 0.17, 0.77, 0.83)     # white page
WIN_BOX = (0.367, 0.31, 0.633, 0.69)    # window inside the page
BLOCK_TOP = 0.45                        # upper part transparent, lower part grey
GREY_ALPHA = 150

# The menu bar image stays a template image at all times, so macOS keeps control of
# its tint. Anything with real colour loses that automatic light/dark handling, and the
# backdrop is decided by the wallpaper rather than the system appearance (a Light-mode
# system can sit under a black menu bar), which no API exposes — so the warning lives
# in the dropdown, where colours are drawn by SwiftBar instead of by the system.


# ─────────────────────────── interpreter ───────────────────────────

def find_pil_python():
    """Another python3 on this machine that can actually import Pillow."""
    cands = [os.environ.get("OPENCODE_USAGE_PYTHON", "")]
    cands += ["/opt/homebrew/bin/python3", "/usr/local/bin/python3"]
    for home in ("~/anaconda3", "~/miniconda3", "~/miniforge3", "~/mambaforge"):
        cands.append(os.path.expanduser(home + "/bin/python3"))
    cands += sorted(glob.glob("/Library/Frameworks/Python.framework/Versions/*/bin/python3"))
    which = shutil.which("python3")
    if which:
        cands.append(which)
    seen = set()
    for c in cands:
        if not c or not os.path.exists(c):
            continue
        real = os.path.realpath(c)
        # Presence on disk is not enough: a python3 with no Pillow in it would just
        # move the failure one process further along, so ask it directly.
        if real in seen or real == os.path.realpath(sys.executable):
            continue
        seen.add(real)
        try:
            ok = subprocess.run([c, "-c", "import PIL"], capture_output=True,
                                timeout=10).returncode == 0
        except Exception:
            ok = False
        if ok:
            return c
    return None


def ensure_pil():
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        other = find_pil_python()
        if other and not os.environ.get("OPENCODE_USAGE_REEXEC"):
            os.environ["OPENCODE_USAGE_REEXEC"] = "1"
            os.execv(other, [other, os.path.abspath(__file__)])
        return False


# ─────────────────────────── text / language ───────────────────────────

# Every string, grouped by language. A new language is one new block here plus a line in
# LANGS, its units in T_UNITS and its date form in fmt_date. Missing entries fall back to
# English rather than raising, so a half-finished translation still renders.
STRINGS = {
    "zh": {
        "win_rolling": "5 小时", "win_weekly": "本周", "win_monthly": "本月",
        "renews": "续费 {date}",
        "resets": "{eta}后重置", "pace_ahead": "进度偏快",
        "stale": "读取失败，下面是 {age}前的数据", "stale_generic": "读取失败，下面是上次的数据",
        "notify_title": "OpenCode · {window}", "notify_body": "已用 {pct}%，{eta}后重置",
        "notify_body_short": "已用 {pct}%",
        "zen_payg": "按量付费 · {n} 个模型",
        "no_reading": "还没读到用量", "reason": "原因：{why}",
        "http_auth": "API key 无效或无权访问", "net_down": "网络不通",
        "bad_shape": "接口返回的结构变了", "no_key": "还没有配置 Key",
        "no_key_hint": "从下面导入，或去后台复制一个",
        "open_go": "打开控制台", "open_zen": "打开 Zen 控制台", "refresh": "刷新",
        "retry": "重试", "get_key": "去拿一个 Key",
        "key_menu": "API Key", "key_add": "添加 Key", "key_import": "从剪贴板导入…",
        "key_prompt": "手动输入…", "key_rename": "重命名…", "key_delete": "删除这把 Key",
        "lang_menu": "语言", "reveal": "打开配置文件",
        "dlg_title": "OpenCode Usage", "dlg_prompt": "粘贴 OpenCode API Key：",
        "dlg_rename": "给这把 Key 起个名字（留空则清除）：",
        "dlg_ok": "确定", "dlg_cancel": "取消",
        "err_bad_key": "这不像是 OpenCode 的 Key。",
        "err_hint": "需要一段没有空格、长度足够的字符。",
        "plugin_error": "插件出错：{name}",
    },
    "zh-Hant": {
        "win_rolling": "5 小時", "win_weekly": "本週", "win_monthly": "本月",
        "renews": "續費 {date}",
        "resets": "{eta}後重置", "pace_ahead": "進度偏快",
        "stale": "讀取失敗，下面是 {age}前的資料", "stale_generic": "讀取失敗，下面是上次的資料",
        "notify_title": "OpenCode · {window}", "notify_body": "已用 {pct}%，{eta}後重置",
        "notify_body_short": "已用 {pct}%",
        "zen_payg": "按量付費 · {n} 個模型",
        "no_reading": "還沒讀到用量", "reason": "原因：{why}",
        "http_auth": "API key 無效或無權存取", "net_down": "網路不通",
        "bad_shape": "介面回傳的結構變了", "no_key": "還沒有設定 Key",
        "no_key_hint": "從下面匯入，或去後台複製一個",
        "open_go": "開啟控制台", "open_zen": "開啟 Zen 控制台", "refresh": "重新整理",
        "retry": "重試", "get_key": "去拿一個 Key",
        "key_menu": "API Key", "key_add": "新增 Key", "key_import": "從剪貼簿匯入…",
        "key_prompt": "手動輸入…", "key_rename": "重新命名…", "key_delete": "刪除這把 Key",
        "lang_menu": "語言", "reveal": "開啟設定檔",
        "dlg_title": "OpenCode Usage", "dlg_prompt": "貼上 OpenCode API Key：",
        "dlg_rename": "給這把 Key 起個名字（留空則清除）：",
        "dlg_ok": "確定", "dlg_cancel": "取消",
        "err_bad_key": "這不像是 OpenCode 的 Key。",
        "err_hint": "需要一段沒有空格、長度足夠的字元。",
        "plugin_error": "外掛出錯：{name}",
    },
    "en": {
        "win_rolling": "Rolling", "win_weekly": "Weekly", "win_monthly": "Monthly",
        "renews": "Renews {date}",
        "resets": "resets in {eta}", "pace_ahead": "ahead of pace",
        "stale": "Read failed — showing data from {age} ago",
        "stale_generic": "Read failed — showing the last reading",
        "notify_title": "OpenCode · {window}", "notify_body": "At {pct}%, resets in {eta}",
        "notify_body_short": "At {pct}%",
        "zen_payg": "Pay as you go · {n} models",
        "no_reading": "No usage read yet", "reason": "Reason: {why}",
        "http_auth": "API key rejected", "net_down": "Network unreachable",
        "bad_shape": "Unexpected response shape", "no_key": "No API key yet",
        "no_key_hint": "Import one below, or copy it from the console",
        "open_go": "Open dashboard", "open_zen": "Open Zen console", "refresh": "Refresh",
        "retry": "Retry", "get_key": "Get an API key",
        "key_menu": "API Key", "key_add": "Add key", "key_import": "From clipboard…",
        "key_prompt": "Type it…", "key_rename": "Rename…", "key_delete": "Remove this key",
        "lang_menu": "Language", "reveal": "Reveal config file",
        "dlg_title": "OpenCode Usage", "dlg_prompt": "Paste your OpenCode API key:",
        "dlg_rename": "Name this key (leave empty to clear):",
        "dlg_ok": "OK", "dlg_cancel": "Cancel",
        "err_bad_key": "That doesn't look like an OpenCode key.",
        "err_hint": "It should be a single token with no spaces.",
        "plugin_error": "Plugin error: {name}",
    },
    "ja": {
        "win_rolling": "5時間", "win_weekly": "今週", "win_monthly": "今月",
        "renews": "更新 {date}",
        "resets": "{eta}後にリセット", "pace_ahead": "ペース超過",
        "stale": "取得失敗 — {age}前のデータ", "stale_generic": "取得失敗 — 前回のデータ",
        "notify_title": "OpenCode · {window}", "notify_body": "{pct}% 使用、{eta}後にリセット",
        "notify_body_short": "{pct}% 使用",
        "zen_payg": "従量課金 · モデル {n} 個",
        "no_reading": "使用量をまだ取得できていません", "reason": "理由：{why}",
        "http_auth": "API キーが拒否されました", "net_down": "ネットワークに接続できません",
        "bad_shape": "レスポンスの形式が変わりました", "no_key": "API キーが未設定です",
        "no_key_hint": "下から取り込むか、コンソールでコピーしてください",
        "open_go": "ダッシュボードを開く", "open_zen": "Zen コンソールを開く",
        "refresh": "更新", "retry": "再試行", "get_key": "API キーを取得",
        "key_menu": "API キー", "key_add": "キーを追加", "key_import": "クリップボードから…",
        "key_prompt": "手入力…", "key_rename": "名前を変更…", "key_delete": "このキーを削除",
        "lang_menu": "言語", "reveal": "設定ファイルを開く",
        "dlg_title": "OpenCode Usage", "dlg_prompt": "OpenCode の API キーを貼り付けてください：",
        "dlg_rename": "このキーの名前（空欄で解除）：",
        "dlg_ok": "OK", "dlg_cancel": "キャンセル",
        "err_bad_key": "OpenCode のキーではないようです。",
        "err_hint": "空白を含まない、十分な長さの文字列が必要です。",
        "plugin_error": "プラグインエラー：{name}",
    },
    "ko": {
        "win_rolling": "5시간", "win_weekly": "이번 주", "win_monthly": "이번 달",
        "renews": "갱신 {date}",
        "resets": "{eta} 후 초기화", "pace_ahead": "예상보다 빠름",
        "stale": "읽기 실패 — {age} 전 데이터", "stale_generic": "읽기 실패 — 마지막 데이터",
        "notify_title": "OpenCode · {window}", "notify_body": "{pct}% 사용, {eta} 후 초기화",
        "notify_body_short": "{pct}% 사용",
        "zen_payg": "종량제 · 모델 {n}개",
        "no_reading": "아직 사용량을 읽지 못했습니다", "reason": "이유: {why}",
        "http_auth": "API 키가 거부되었습니다", "net_down": "네트워크에 연결할 수 없습니다",
        "bad_shape": "응답 형식이 바뀌었습니다", "no_key": "API 키가 없습니다",
        "no_key_hint": "아래에서 가져오거나 콘솔에서 복사하세요",
        "open_go": "대시보드 열기", "open_zen": "Zen 콘솔 열기",
        "refresh": "새로 고침", "retry": "다시 시도", "get_key": "API 키 받기",
        "key_menu": "API 키", "key_add": "키 추가", "key_import": "클립보드에서…",
        "key_prompt": "직접 입력…", "key_rename": "이름 변경…", "key_delete": "이 키 삭제",
        "lang_menu": "언어", "reveal": "설정 파일 열기",
        "dlg_title": "OpenCode Usage", "dlg_prompt": "OpenCode API 키를 붙여넣으세요:",
        "dlg_rename": "이 키의 이름 (비우면 지움):",
        "dlg_ok": "확인", "dlg_cancel": "취소",
        "err_bad_key": "OpenCode 키가 아닌 것 같습니다.",
        "err_hint": "공백 없는 충분히 긴 문자열이어야 합니다.",
        "plugin_error": "플러그인 오류: {name}",
    },
    "es": {
        "win_rolling": "5 horas", "win_weekly": "Semana", "win_monthly": "Mes",
        "renews": "Renueva {date}",
        "resets": "se reinicia en {eta}", "pace_ahead": "ritmo adelantado",
        "stale": "Fallo de lectura — datos de hace {age}",
        "stale_generic": "Fallo de lectura — última lectura",
        "notify_title": "OpenCode · {window}", "notify_body": "Al {pct}%, se reinicia en {eta}",
        "notify_body_short": "Al {pct}%",
        "zen_payg": "Pago por uso · {n} modelos",
        "no_reading": "Aún sin datos de uso", "reason": "Motivo: {why}",
        "http_auth": "Clave API rechazada", "net_down": "Sin conexión de red",
        "bad_shape": "La respuesta cambió de formato", "no_key": "Sin clave API",
        "no_key_hint": "Impórtala abajo o cópiala de la consola",
        "open_go": "Abrir panel", "open_zen": "Abrir consola Zen",
        "refresh": "Actualizar", "retry": "Reintentar", "get_key": "Obtener una clave",
        "key_menu": "Clave API", "key_add": "Añadir clave", "key_import": "Desde el portapapeles…",
        "key_prompt": "Escribirla…", "key_rename": "Renombrar…", "key_delete": "Eliminar esta clave",
        "lang_menu": "Idioma", "reveal": "Ver archivo de configuración",
        "dlg_title": "OpenCode Usage", "dlg_prompt": "Pega tu clave API de OpenCode:",
        "dlg_rename": "Nombre para esta clave (vacío para borrar):",
        "dlg_ok": "Aceptar", "dlg_cancel": "Cancelar",
        "err_bad_key": "Esto no parece una clave de OpenCode.",
        "err_hint": "Debe ser un solo token sin espacios.",
        "plugin_error": "Error del plugin: {name}",
    },
    "fr": {
        "win_rolling": "5 heures", "win_weekly": "Semaine", "win_monthly": "Mois",
        "renews": "Renouvellement {date}",
        "resets": "réinit. dans {eta}", "pace_ahead": "rythme en avance",
        "stale": "Échec de lecture — données d'il y a {age}",
        "stale_generic": "Échec de lecture — dernière lecture",
        "notify_title": "OpenCode · {window}", "notify_body": "{pct}% utilisé, réinit. dans {eta}",
        "notify_body_short": "{pct}% utilisé",
        "zen_payg": "Paiement à l'usage · {n} modèles",
        "no_reading": "Aucune donnée de consommation", "reason": "Raison : {why}",
        "http_auth": "Clé API refusée", "net_down": "Réseau injoignable",
        "bad_shape": "La réponse a changé de format", "no_key": "Aucune clé API",
        "no_key_hint": "Importez-la ci-dessous ou copiez-la depuis la console",
        "open_go": "Ouvrir le tableau de bord", "open_zen": "Ouvrir la console Zen",
        "refresh": "Actualiser", "retry": "Réessayer", "get_key": "Obtenir une clé",
        "key_menu": "Clé API", "key_add": "Ajouter une clé",
        "key_import": "Depuis le presse-papiers…", "key_prompt": "La saisir…",
        "key_rename": "Renommer…", "key_delete": "Supprimer cette clé",
        "lang_menu": "Langue", "reveal": "Afficher le fichier de config",
        "dlg_title": "OpenCode Usage", "dlg_prompt": "Collez votre clé API OpenCode :",
        "dlg_rename": "Nom de cette clé (vide pour effacer) :",
        "dlg_ok": "OK", "dlg_cancel": "Annuler",
        "err_bad_key": "Cela ne ressemble pas à une clé OpenCode.",
        "err_hint": "Il faut un seul jeton, sans espaces.",
        "plugin_error": "Erreur du plugin : {name}",
    },
}

# Menu languages, each written in itself; this list is also the submenu order.
LANGS = [("zh", "简体中文"), ("zh-Hant", "繁體中文"), ("en", "English"),
         ("ja", "日本語"), ("ko", "한국어"), ("es", "Español"), ("fr", "Français")]
LANG_CODES = [code for code, _ in LANGS]

# Time units per language. English and the Romance languages build their own compact form
# in human(); the CJK ones are joined without spaces, which is how they are written.
T_UNITS = {
    "zh": ("天", "小时", "分钟", "秒"),
    "zh-Hant": ("天", "小時", "分鐘", "秒"),
    "ja": ("日", "時間", "分", "秒"),
    "ko": ("일", "시간", "분", "초"),
    "es": ("d", "h", "min", "s"),
    "fr": ("j", "h", "min", "s"),
}
T_TIGHT = ("ja", "ko")     # no space between number and unit; Chinese keeps them

LANG = "en"


def t(key, **kw):
    # Falls back to English so a partially translated language still renders.
    table = STRINGS.get(LANG) or STRINGS["en"]
    return (table.get(key) or STRINGS["en"][key]).format(**kw)


def system_lang():
    """The closest supported language to the system's preferred one."""
    try:
        out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"],
                             capture_output=True, text=True, timeout=3).stdout.lower()
    except Exception:
        return "en"
    if any(tag in out for tag in ("zh-hant", "zh-tw", "zh-hk", "zh-mo")):
        return "zh-Hant"
    if "zh" in out:
        return "zh"
    for code in ("ja", "ko", "es", "fr"):
        if f'"{code}' in out:
            return code
    return "en"


# ─────────────────────────── config ───────────────────────────

def load_config():
    raw = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            raw = loaded
    except Exception:
        pass
    if raw.get("apiKey") and not raw.get("keys"):      # pre-2.0 single-key config
        raw = {"keys": [{"key": raw["apiKey"]}]}
    keys = []
    for k in raw.get("keys") or []:
        if isinstance(k, dict) and k.get("key"):
            label = k.get("label")
            keys.append({"key": k["key"], "added": k.get("added", 0),
                         "label": label if isinstance(label, str) else ""})
    cfg = {"keys": keys,
           "lang": raw.get("lang") if raw.get("lang") in LANG_CODES else system_lang(),
           "active": raw.get("active")}
    if not isinstance(cfg.get("active"), int) or cfg["active"] >= len(cfg["keys"]):
        cfg["active"] = 0
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


def mask(key):
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "…"


def key_title(item):
    """'个人 · oc_sk_…sSbv', or just the mask when the key has no label.

    | is swapped out everywhere: SwiftBar splits a line at the first pipe, so a label
    containing one would tear the menu line apart.
    """
    m = mask(item["key"]).replace("|", "/")
    label = item.get("label", "").replace("|", "/")
    return f"{label} · {m}" if label else m


def find_key(cfg):
    """Saved key first, then $OPENCODE_API_KEY as an override."""
    keys = cfg.get("keys") or []
    if keys:
        return keys[min(cfg.get("active", 0), len(keys) - 1)]["key"]
    return (os.environ.get(KEY_ENV) or "").strip() or None


# ─────────────────────────── formatting helpers ───────────────────────────

def bar_color(pct):
    """ANSI 256-colour index for the bar fill; mid-tones so they read on light and dark."""
    if pct >= 85:
        return 167      # #d75f5f
    if pct >= 60:
        return 172      # #d78700
    return 71           # #5faf5f


def bar_line(pct):
    """Ten-segment meter built from ANSI background-coloured cells.

    Each segment is a fixed run of cells, separated by a plain cell with no background —
    which is why every segment resets first: a background colour would otherwise bleed
    across the gap and the segments would merge into one bar. The first cell of the first
    segment is a no-break space so the leading cell can't be trimmed away as whitespace.
    A 0% reading lights no segment at all; anything above zero lights at least one.
    """
    pct = min(100.0, max(0.0, pct))
    lit = 0 if pct <= 0 else max(1, int(round(SEG_COUNT * pct / 100)))
    color = bar_color(pct)
    parts = []
    for i in range(SEG_COUNT):
        parts.append(f"{ESC}[48;5;{color if i < lit else BAR_TRACK}m{' ' * SEG_CELLS}{ESC}[0m")
    # One uncoloured no-break space in front: it shifts the meter right so its left edge
    # lines up with the icons above it (which sit one cell in), and being a no-break space
    # it is not trimmed away as leading whitespace.
    return "\u00a0" + " ".join(parts)


def human(seconds):
    if seconds <= 0:
        return ""
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if LANG == "en":
        if d:
            return f"{d}d {h}h" if h else f"{d}d"
        if h:
            return f"{h}h {m}m" if m else f"{h}h"
        return f"{m}m" if m else f"{max(1, int(seconds))}s"
    day, hour, minute, second = T_UNITS.get(LANG) or ("d", "h", "min", "s")
    sp = "" if LANG in T_TIGHT else " "
    if d:
        return f"{d}{sp}{day}{sp}{h}{sp}{hour}" if h else f"{d}{sp}{day}"
    if h:
        return f"{h}{sp}{hour}{sp}{m}{sp}{minute}" if m else f"{h}{sp}{hour}"
    return f"{m}{sp}{minute}" if m else f"{max(1, int(seconds))}{sp}{second}"


def fmt_date(dt):
    local = dt.astimezone()
    if LANG == "en":
        return f"{MONTHS[local.month - 1]} {local.day}"
    if LANG == "es":
        return f"{local.day} {MONTHS_ES[local.month - 1]}"
    if LANG == "fr":
        return f"{local.day} {MONTHS_FR[local.month - 1]}"
    if LANG == "ko":
        return f"{local.month}월 {local.day}일"
    return f"{local.month} 月 {local.day} 日"


def one_month_before(dt):
    """Same day-of-month, one calendar month earlier (clamped to that month's end)."""
    y, m = dt.year, dt.month - 1
    if m == 0:
        y, m = y - 1, 12
    return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))


def window_span(kind, reset_dt):
    """Length of one quota window in seconds, derived from its reset time.

    Monthly has no fixed length, so it is measured as "same day last month" instead of
    assuming 30 days — a 31-day period would otherwise read as ~3% ahead of pace.
    """
    if kind == "rolling":
        return 5 * 3600
    if kind == "weekly":
        return 7 * 86400
    return (reset_dt - one_month_before(reset_dt)).total_seconds()


# ─────────────────────────── image rendering ───────────────────────────

def logo_mask(w, h, ss=8):
    """Monochrome OpenCode mark: rounded badge outline + page + inner window."""
    from PIL import Image, ImageDraw
    W, H = w * ss, h * ss
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    st = max(2, round(W * SQ_STROKE))
    d.rounded_rectangle([st // 2, st // 2, W - 1 - st // 2, H - 1 - st // 2],
                        radius=round(W * SQ_RADIUS), outline=255, width=st)

    px0, py0, px1, py1 = PAGE_BOX
    d.rectangle([round(px0 * W), round(py0 * H), round(px1 * W) - 1, round(py1 * H) - 1], fill=255)

    wx0, wy0, wx1, wy1 = WIN_BOX
    d.rectangle([round(wx0 * W), round(wy0 * H), round(wx1 * W) - 1, round(BLOCK_TOP * H) - 1], fill=0)
    d.rectangle([round(wx0 * W), round(BLOCK_TOP * H), round(wx1 * W) - 1, round(wy1 * H) - 1],
                fill=GREY_ALPHA)
    return m.resize((w, h), Image.LANCZOS)


def _draw(values, h, text=None, gap_ratio=0.16, pad_ratio=0.04, dots=False):
    from PIL import Image, ImageDraw, ImageFont

    fs = max(8, int(h / 3 * FS_RATIO))
    font = ImageFont.truetype(FONT_PATH, fs, index=FONT_INDEX)
    texts = [str(v) for v in values] if values else ([text] if text else [])

    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    if dots:      # pending marker: three drawn dots, not a glyph (glyph baselines sit low)
        tw = int(h * 0.50)
    else:
        tw = max(int(probe.textlength(t, font=font)) for t in texts) if texts else 0

    lh = int(h * LOGO_RATIO)
    gap = max(3, int(h * gap_ratio))
    pad = max(2, int(h * pad_ratio))
    W = pad + lh + (gap + tw + pad if (texts or dots) else pad)
    step = h / max(1, len(texts))

    # Alpha only: rendered as a template image, the system paints it black or white to
    # suit whatever sits behind the menu bar.
    ink = Image.new("L", (W, h), 0)
    ink.paste(logo_mask(lh, lh), (pad, (h - lh) // 2))
    d = ImageDraw.Draw(ink)
    if dots:
        r = max(2, int(h * 0.045))
        cy = h / 2
        cx = W - pad
        for i in range(3):
            x = cx - r - i * round(r * 4.0)
            d.ellipse([x - r, cy - r, x + r, cy + r], fill=255)
    for i, t in enumerate(texts):
        d.text((W - pad, step * (i + 0.5)), t, font=font, anchor="rm", fill=255,
               stroke_width=STROKE, stroke_fill=0)

    img = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    img.putalpha(ink)
    return img


def title_image(values):
    """Status bar icon: logo + one number per line."""
    return _draw(values, TITLE_H)


def pending_image():
    """Status bar icon before the first successful read: logo + three dots."""
    return _draw(None, TITLE_H, dots=True)


def b64(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(144, 144))
    return base64.b64encode(buf.getvalue()).decode()


# ─────────────────────────── network ───────────────────────────

def save_cache(kind, rows=None, renews="", models=None):
    """kind is "go" or "zen": a failed refresh must not change the card it shows."""
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "kind": kind, "renews": renews,
                       "rows": rows, "models": models}, f)
    except Exception:
        pass


def load_cache():
    """Returns (kind, rows, renews, models, timestamp) of the last good reading."""
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        rows, models, ts = d.get("rows"), d.get("models"), float(d.get("ts") or 0)
        # Pre-2.1 cache had no "kind" field: a reading with rows must have been Go.
        kind = d.get("kind") or ("go" if rows else "")
        if not ts or (not rows and models is None):
            return "", None, "", None, 0
        return kind, rows or None, d.get("renews", ""), models, ts
    except Exception:
        return "", None, "", None, 0


def stale_note(ts):
    """How old the cached reading is, for the "read failed" line."""
    if not ts:
        return ""
    age = human(time.time() - ts)
    return t("stale", age=age) if age else t("stale_generic")


def _get(url, key):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        # Cloudflare rejects python's default UA with error 1010
        "User-Agent": "curl/8.7.1",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_go(key):
    """Returns (rows, renews, error).

    An empty row list with `error is None` means the request succeeded and the key simply
    has no Go quota windows -> a Zen key. `error` is set only when the request itself
    failed, so a dropped network never gets mistaken for a Zen account.
    """
    try:
        payload = _get(GO_USAGE_URL, key)
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            return None, "", None          # endpoint doesn't know this key as a Go plan
        if e.code in (401, 403):
            return None, "", t("http_auth")
        return None, "", f"HTTP {e.code}"
    except Exception as e:
        name = type(e).__name__
        if "URLError" in name or "timeout" in name.lower():
            return None, "", t("net_down")
        return None, "", name
    usage = payload.get("usage") or {}
    rows, renews = [], ""
    for k in WINDOWS:
        w = usage.get(k) or {}
        pct = w.get("percent")
        if pct is None:
            continue
        eta, reset_iso, ahead = "", "", 0.0
        if w.get("resetsAt"):
            try:
                dt = datetime.fromisoformat(w["resetsAt"].replace("Z", "+00:00"))
                remaining = (dt - datetime.now(timezone.utc)).total_seconds()
                eta, reset_iso = human(remaining), dt.isoformat()
                span = window_span(k, dt)
                if span > 0:
                    # Budget burned minus clock burned. Positive means the window is
                    # going faster than the period it covers, which is the only thing
                    # worth flagging — a bare "83%" says nothing on its own.
                    elapsed = min(1.0, max(0.0, 1.0 - remaining / span))
                    ahead = float(pct) / 100.0 - elapsed
                if k == "monthly":
                    renews = fmt_date(dt)
            except Exception:
                pass
        rows.append((k, float(pct), None, eta, ahead, reset_iso))
    if not rows and usage:
        # 200 OK, a usage object came back, but none of the windows we know about are in
        # it: the endpoint's shape moved. That is an error, not an empty Go account —
        # treating it as Zen would silently relabel every Go user.
        return None, "", t("bad_shape")
    return (rows or None), renews, None


def fetch_zen_model_count(key):
    try:
        return len(_get(ZEN_MODELS_URL, key).get("data") or [])
    except Exception:
        return None


# ─────────────────────────── menu items ───────────────────────────

def self_path():
    return os.path.abspath(os.environ.get("SWIFTBAR_PLUGIN_PATH") or __file__)


def plugin_name():
    """SwiftBar's plugin name: the file name minus ".{interval}.{ext}"."""
    base = os.path.basename(self_path())
    m = re.match(r"^(.+?)\.\d+[smhd]\.\w+$", base)
    return m.group(1) if m else os.path.splitext(base)[0]


def action(flag, *params):
    """Parameters that re-invoke this very script with a flag, so it stays one file."""
    out = f"bash={self_path()} terminal=false"
    for i, p in enumerate((flag,) + params):
        out += f" param{i}={p}"
    return out


def key_block(cfg):
    """The key submenu: which key is loaded, plus add / rename / remove.

    Kept as one submenu so the top level stays about usage. The parent row carries the
    active key's name, so "which key am I on" is answered without opening it; the list
    itself shows every saved key with a tick, which is also the only place a key can be
    seen without opening the config file. Rows have actions only where clicking does
    something — a key row in a one-key setup is a status line.
    """
    keys = cfg["keys"]
    active = keys[cfg["active"]] if keys else None
    head = t("key_menu") + (f" · {active['label']}" if active and active["label"] else "")
    print(f"{head} | size=12 color={TEXT}")
    for i, item in enumerate(keys):
        tick = "checked=true " if i == cfg["active"] else ""
        click = " " + action("--use", i) if len(keys) > 1 else ""
        print(f"--{key_title(item)} | {tick}size=12 color={TEXT}{click}")
    print(f"--{t('key_add')} | size=12")
    print(f"----{t('key_import')} | size=12 sfimage=doc.on.clipboard {action('--import')}")
    print(f"----{t('key_prompt')} | size=12 sfimage=keyboard {action('--prompt')}")
    if keys:
        print(f"--{t('key_rename')} | size=12 sfimage=pencil {action('--rename')}")
        print(f"--{t('key_delete')} | size=12 sfimage=trash {action('--delete')}")


def lang_block():
    """Language submenu. Each language is written in itself, so it is readable whichever
    one is active; the parent row states the current one."""
    current = dict(LANGS).get(LANG, LANG)
    print(f"{t('lang_menu')} · {current} | size=12 color={TEXT}")
    for code, name in LANGS:
        tick = "checked=true " if code == LANG else ""
        print(f"--{name} | {tick}size=12 {action('--lang', code)}")
    print(f"{t('reveal')} | size=12 sfimage=doc.text {action('--reveal')}")


# ─────────────────────────── output ───────────────────────────

def header(img, fallback):
    if img is not None:
        print(f"\u200b | templateImage={b64(img)} dropdown=false")
    else:
        print(f"{fallback} | font={MONO} size=12 sfimage=gauge dropdown=false")
    # SwiftBar cycles everything before the FIRST "---" in the menu bar, so the header
    # has to be closed right here or the rows below start rotating.
    print("---")


def render_go(rows, renews, has_pil, cfg, stale=""):
    values = [int(round(r[1])) for r in rows]

    header(title_image(values) if has_pil else None, "\u2502".join(str(v) for v in values))

    print(f"OpenCode Go | size=13 color={TEXT}")
    if renews:
        print(f"{t('renews', date=renews)} | size=11 color={TEXT}")
    if stale:
        print(f"{stale} | size=11 color={ALERT_TEXT}")
    print("---")

    for row in rows:
        label, pct, eta = row[0], float(row[1]), row[3]
        ahead = row[4] if len(row) > 4 else 0.0
        parts = [t("win_" + label)]
        if eta:
            parts.append(t("resets", eta=eta))
        late = ahead > PACE_TOLERANCE
        if late:
            parts.append(t("pace_ahead"))
        # The badge is back (right-aligned pill): with the meter now the widest row it sits
        # level with the meter's right end instead of stretching the row past it.
        # The `href` is what keeps this row out of AppKit's auto-disabled grey: a row with
        # no action gets painted as a disabled item, and setting `color` to fix that would
        # drop the `sfimage`. Clicking the label opens the dashboard, same as the bar below.
        tint = f"color={ALERT_TEXT} " if late else ""
        print(f"{' · '.join(parts)} | size=13 {tint}sfimage={WINDOW_ICON.get(label, 'gauge')} "
              f"badge={pct:.0f}% href={GO_URL}")
        # ansi=true keeps the two-tone bar's colours; href makes the row selectable
        # No `size` here on purpose — see the note above SEG_COUNT.
        print(f"{bar_line(pct)} | ansi=true href={GO_URL}")

    print("---")
    print(f"{t('open_go')} | size=13 sfimage=arrow.up.right.square href={GO_URL}")
    print(f"{t('refresh')} | size=13 sfimage=arrow.clockwise refresh=true")
    print("---")
    key_block(cfg)
    print("---")
    lang_block()


def render_zen(model_count, has_pil, cfg, stale=""):
    header(_draw(None, TITLE_H, text="Zen") if has_pil else None, "Zen")

    print("OpenCode Zen | size=13")
    if model_count:
        print(f"{t('zen_payg', n=model_count)} | size=11 color={MUTED}")
    if stale:
        print(f"{stale} | size=11 color={ALERT_TEXT}")
    print("---")
    print(f"{t('open_zen')} | size=13 sfimage=arrow.up.right.square href={DASHBOARD_URL}")
    print(f"{t('refresh')} | size=13 sfimage=arrow.clockwise refresh=true")
    print("---")
    key_block(cfg)
    print("---")
    lang_block()


def render_unknown(reason, has_pil, cfg):
    """Read failed and nothing cached: show a placeholder, never a guess at the plan."""
    # None (not a "…" string) when Pillow is missing: header() treats a non-None image as
    # a real one and would try to encode it.
    header(pending_image() if has_pil else None, "…")

    print("OpenCode | size=13")
    print(f"{t('no_reading')} | size=11 color={MUTED}")
    print("---")
    print(f"{t('reason', why=reason)} | size=11 color={ALERT_TEXT}")
    print("---")
    print(f"{t('retry')} | size=13 sfimage=arrow.clockwise refresh=true")
    print(f"{t('open_go')} | size=13 sfimage=arrow.up.right.square href={DASHBOARD_URL}")
    print("---")
    key_block(cfg)
    print("---")
    lang_block()


def render_no_key(has_pil, cfg):
    header(pending_image() if has_pil else None, "…")

    print("OpenCode Usage | size=13")
    print(f"{t('no_key')} | size=11 color={MUTED}")
    print(f"{t('no_key_hint')} | size=11 color={MUTED}")
    print("---")
    print(f"{t('get_key')} | size=13 sfimage=key.fill href={KEYS_URL}")
    print("---")
    key_block(cfg)
    print("---")
    lang_block()


def fail(msg):
    print(f"! OpenCode | color={ALERT_TEXT} size=12")
    print("---")
    print(f"{msg} | color={ALERT_TEXT} size=12")
    print("---")
    print(f"{t('refresh')} | size=13 sfimage=arrow.clockwise refresh=true")


# ─────────────────────────── actions (re-invoked with a flag) ───────────────────────────

def valid_key(raw):
    """A key is one token: long enough, no spaces, no ellipsis from a masked display.

    Deliberately not tied to a prefix: OpenCode's key format is theirs to change, so
    anything that could plausibly be a key is accepted rather than rejecting a valid
    key whose shape we didn't anticipate.
    """
    s = (raw or "").strip().strip('"').strip("'").replace("\n", "")
    if len(s) < 16 or " " in s or "\u2026" in s:
        return None
    return s


def alert(msg):
    script = (f'display dialog "{msg}" buttons {{"OK"}} default button 1 '
              f'with title "{t("dlg_title")}" giving up after 6')
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)
    except Exception:
        pass


def refresh_self():
    """Ask SwiftBar to re-run this plugin after an action changed the config.

    Two details matter. The endpoint matches on the plugin *name* (file name minus
    ".{interval}.{ext}"), not on the file name — passing the file name is accepted
    silently and refreshes nothing. And the request is sent from a detached process
    after a short delay, so it arrives once SwiftBar is done handling the click instead
    of being dropped. Both together are why switching language needed a second refresh.
    """
    url = "swiftbar://refreshplugin?name=" + urllib.parse.quote(plugin_name())
    try:
        subprocess.Popen(["/bin/sh", "-c", f"sleep 0.3; open -g '{url}'"],
                         start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def notify(title, body):
    script = f'display notification "{body}" with title "{title}"'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        pass


def maybe_notify(rows):
    """One notification per window per reset cycle, once a window passes NOTIFY_PCT.

    Keyed by the window's resetsAt, so a window sitting at 90% for the rest of its cycle
    posts once rather than every five minutes.
    """
    state = {}
    try:
        with open(NOTIFY_PATH, encoding="utf-8") as f:
            state = json.load(f) or {}
    except Exception:
        pass
    dirty = False
    for row in rows:
        label, pct, eta = row[0], float(row[1]), row[3]
        reset_iso = row[5] if len(row) > 5 else ""
        if pct < NOTIFY_PCT or not reset_iso or state.get(label) == reset_iso:
            continue
        state[label] = reset_iso
        dirty = True
        body = t("notify_body", pct=f"{pct:.0f}", eta=eta) if eta else \
            t("notify_body_short", pct=f"{pct:.0f}")
        notify(t("notify_title", window=t("win_" + label)), body)
    if dirty:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(NOTIFY_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f)
        except Exception:
            pass


def store_key(key):
    cfg = load_config()
    for i, item in enumerate(cfg["keys"]):
        if item["key"] == key:
            cfg["active"] = i
            break
    else:
        cfg["keys"].append({"key": key, "added": int(time.time()), "label": ""})
        cfg["active"] = len(cfg["keys"]) - 1
    save_config(cfg)


def ask(prompt, default="", hidden=False):
    """A one-line system dialog. Returns the text, or "" on cancel."""
    safe = prompt.replace("\\", "").replace('"', "\\\"")
    cur = (default or "").replace("\\", "").replace('"', "")
    mask = " with hidden answer" if hidden else ""
    script = (
        "try\n"
        f'set r to display dialog "{safe}" default answer "{cur}"{mask} '
        f'buttons {{"{t("dlg_cancel")}", "{t("dlg_ok")}"}} default button 2 '
        f'with title "{t("dlg_title")}"\n'
        "return text returned of r\n"
        "on error number -128\n"
        'return ""\n'
        "end try\n"
    )
    try:
        return subprocess.run(["osascript", "-e", script], capture_output=True,
                              text=True, timeout=180).stdout
    except Exception:
        return ""


def act_import():
    try:
        raw = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        raw = ""
    key = valid_key(raw)
    if key:
        store_key(key)
    else:
        alert(t("err_bad_key") + " " + t("err_hint"))
    refresh_self()


def act_prompt():
    raw = ask(t("dlg_prompt"), hidden=True)
    key = valid_key(raw)
    if key:
        store_key(key)
    elif raw.strip():
        alert(t("err_bad_key") + " " + t("err_hint"))
    refresh_self()


def act_rename():
    """Label the active key. An empty answer clears the label back to the masked key."""
    cfg = load_config()
    if not cfg["keys"]:
        refresh_self()
        return
    item = cfg["keys"][cfg["active"]]
    label = ask(t("dlg_rename"), default=item.get("label", "")).strip()
    # | would split the menu line, leading dashes would turn a submenu row into a deeper
    # nesting level — both are silent layout breakage, so normalise instead of trusting.
    label = re.sub(r"\s+", " ", label.replace("|", "/")).lstrip("-").strip()[:24]
    item["label"] = label
    save_config(cfg)
    refresh_self()


def act_delete():
    cfg = load_config()
    if cfg["keys"]:
        del cfg["keys"][min(cfg["active"], len(cfg["keys"]) - 1)]
        cfg["active"] = 0
        save_config(cfg)
    refresh_self()


def act_use(index):
    cfg = load_config()
    if 0 <= index < len(cfg["keys"]):
        cfg["active"] = index
        save_config(cfg)
    refresh_self()


def act_lang(lang):
    cfg = load_config()
    if lang in LANG_CODES:
        cfg["lang"] = lang
        save_config(cfg)
    refresh_self()


def act_reveal():
    try:
        if not os.path.exists(CONFIG_PATH):
            save_config(load_config())
        subprocess.run(["open", "-R", CONFIG_PATH], timeout=5)
    except Exception:
        pass


def run_action(args):
    """args: values of SwiftBar's param0, param1... in order."""
    flag = args[0]
    if flag == "--import":
        act_import()
    elif flag == "--prompt":
        act_prompt()
    elif flag == "--rename":
        act_rename()
    elif flag == "--delete":
        act_delete()
    elif flag == "--use":
        act_use(int(args[1]) if len(args) > 1 and args[1].isdigit() else 0)
    elif flag == "--lang":
        act_lang(args[1] if len(args) > 1 else "en")
    elif flag == "--reveal":
        act_reveal()
    else:
        return False
    return True


def normalize_args(argv):
    """SwiftBar may hand us `--flag` or `param0=--flag`, depending on version."""
    out = []
    for a in argv:
        head = a.split("=", 1)[0]
        out.append(a.split("=", 1)[1] if head.startswith("param") and "=" in a else a)
    return out


# ─────────────────────────── entry point ───────────────────────────

def main():
    global LANG
    has_pil = ensure_pil()

    cfg = load_config()
    LANG = cfg["lang"]

    key = find_key(cfg)
    if not key:
        render_no_key(has_pil, cfg)
        return

    rows, renews, err = fetch_go(key)
    if rows:
        save_cache("go", rows=rows, renews=renews)
        maybe_notify(rows)
        render_go(rows, renews, has_pil, cfg)
        return

    if err is None:
        # The request succeeded and the key has no Go quota windows: a Zen key, proven
        # by the response rather than inferred from another endpoint.
        models = fetch_zen_model_count(key)
        save_cache("zen", models=models)
        render_zen(models, has_pil, cfg)
        return

    # The request itself failed, so the account type is unknowable from here. Show the
    # last good reading of whatever kind it was instead of switching cards: probing the
    # Zen endpoint would prove nothing, since it answers 200 for a Go key as well.
    kind, cached_rows, cached_renews, cached_models, ts = load_cache()
    if kind == "go" and cached_rows:
        render_go(cached_rows, cached_renews, has_pil, cfg, stale=stale_note(ts))
        return
    if kind == "zen":
        render_zen(cached_models, has_pil, cfg, stale=stale_note(ts))
        return

    render_unknown(err, has_pil, cfg)


if __name__ == "__main__":
    args = normalize_args(sys.argv[1:])
    if args:
        LANG = load_config()["lang"]
        if run_action(args):
            sys.exit(0)
    try:
        main()
    except Exception as e:  # never leave the menu bar blank
        fail(t("plugin_error", name=type(e).__name__))
