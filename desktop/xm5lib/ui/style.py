"""XM5 skin tokens.

Two layers, like the player itself:
- the *case*: sand-blasted aluminium (light or dark anodised), keys, lamps, wheel -- after ScopeWatch;
- the *screen*: the XM5 firmware look, black glass with gold line icons.

Module-level colour names (BG, TEXT, ACCENT, ...) describe the screen and are kept for older callers.
"""
from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"

# ---- screen (firmware) palette
BG = "#060606"
PANEL = "#0e0e0e"
SIDEBAR = "#0a0a0a"
LINE = "#1f1f1f"
TEXT = "#f1f1f1"
MUTED = "#8c8c8c"
ACCENT = "#f0cd81"        # firmware icon gold
ACCENT_HI = "#ffe5ae"
ACCENT_LO = "#7d6f51"     # firmware *_down icon tone
SELECT = "#221d13"        # gold-tinted selection
HOVER = "#151515"
DANGER = "#e07a6a"
OK = "#8fbf8a"
WARN = "#e0bd59"

# ---- case finishes (the two XM5 editions)
# base = --popup-underlay, wheel = --active-wheel, light = panel light intensity, seam = bevel light intensity
# (ScopeWatch 2.0.2 values: #c9c9c9 / #707070, wheel #4f4f4f, light .36, seam .64, texture .53)
METAL = {
    # face colours sampled from the XM5 Ti / black XM5 renders; the renders are evenly lit, hence the low light
    "titanium": dict(label="TITANIUM", base="#d3cac0", wheel="#4f4f4f", light=.07, seam=.64, texture=.30,
                     edge="#6f685c", etch="#b99212", etch_lip=110, notch=.42, notch_lip=150),
    "aluminum": dict(label="ALUMINUM", base="#2c2c2e", wheel="#4f4f4f", light=.10, seam=.64, texture=.30,
                     edge="#0c0c0d", etch="#c49a1c", etch_lip=28, notch=.62, notch_lip=60),
    # the ScopeWatch originals, kept for side-by-side checks
    "sw_light": dict(label="SCOPEWATCH", base="#c9c9c9", wheel="#4f4f4f", light=.36, seam=.64, texture=.53,
                     edge="#74716e", etch="#5d6267", etch_lip=110, notch=.42, notch_lip=150),
    "sw_dark": dict(label="SCOPEWATCH", base="#707070", wheel="#4f4f4f", light=.36, seam=.64, texture=.53,
                    edge="#25292d", etch="#9ca8b4", etch_lip=30, notch=.6, notch_lip=70),
}
LEGACY_THEME = {"light": "titanium", "dark": "aluminum"}


def icon_path(name: str) -> str:
    return str(ASSETS / "xm5" / f"{name}.png")


def qss(theme: str = "titanium") -> str:
    m = METAL.get(LEGACY_THEME.get(theme, theme), METAL["titanium"])
    return f"""
* {{ font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "Noto Sans CJK SC", sans-serif; font-size: 10pt; color: {TEXT}; }}
QMainWindow {{ background: transparent; }}
QToolTip {{ background: #17212c; color: #eff7ff; border: 1px solid #000; padding: 6px 8px; }}

/* ---------------- in-screen search (status bar) */
QLineEdit#search {{ background: #111; color: #e8e8e8; border: 1px solid #2a2a2a; border-radius: 12px;
    padding: 2px 12px; selection-background-color: {ACCENT_LO}; }}
QLineEdit#search:focus {{ border-color: {ACCENT}; }}
QPushButton#searchIcon {{ background: transparent; border: none; color: {MUTED}; font-size: 15pt; padding: 0 4px; }}
QPushButton#searchIcon:hover {{ color: {ACCENT}; }}
QPushButton#winButton, QPushButton#winClose, QPushButton#winPlayerMode {{ background: transparent; border: none; border-radius: 3px; color: #cfcfcf;
    font-size: 10pt; padding: 0; }}
QPushButton#winButton:hover, QPushButton#winPlayerMode:hover {{ background: #1c1c1c; color: {ACCENT}; }}
QPushButton#winClose:hover {{ background: #c42b1c; color: #fff; }}

/* ---------------- screen */
QWidget#screen, QWidget#page, QStackedWidget#pages, QScrollArea {{ background: {BG}; border: none; }}
QWidget#rail {{ background: {SIDEBAR}; border-right: 1px solid {LINE}; }}
QWidget#inspector {{ background: {PANEL}; border-left: 1px solid {LINE}; }}
QWidget#inspector QLabel {{ color: #bdbdbd; }}
QWidget#statusStrip {{ background: #000; border-bottom: 1px solid {LINE}; }}
QWidget#statusStrip QLabel {{ color: {TEXT}; font-size: 10.5pt; }}
QWidget#nowBar {{ background: #000; border-top: 1px solid {LINE}; }}
QLabel#nowTitle {{ font-size: 12.5pt; color: {TEXT}; }}
QLabel#nowSub {{ color: {MUTED}; }}
QLabel#sectionTitle {{ color: {ACCENT_LO}; font-size: 8pt; font-weight: 600; letter-spacing: 2px; padding: 10px 18px 2px 18px; }}
QLabel#pageTitle {{ font-size: 19pt; font-weight: 600; color: {TEXT}; }}
QLabel#subtitle, QLabel[muted="true"] {{ color: {MUTED}; }}
QLabel#banner {{ background: #2a1512; color: #ffd9cf; border: 1px solid {DANGER}; border-radius: 4px; padding: 8px 10px; }}
QLabel#okBanner {{ background: #13200f; color: #d8f0d4; border: 1px solid {OK}; border-radius: 4px; padding: 8px 10px; }}

QListWidget#nav, QListWidget#groupList {{ background: transparent; border: none; outline: none; }}
QListWidget#nav::item {{ padding: 0 8px; height: 40px; border-left: 3px solid transparent; color: #d9d9d9; }}
QListWidget#nav::item:selected {{ background: {SELECT}; color: {ACCENT}; border-left: 3px solid {ACCENT}; }}
QListWidget#nav::item:hover:!selected {{ background: {HOVER}; }}
QListWidget#groupList::item {{ padding: 0 14px; height: 44px; border-bottom: 1px solid {LINE}; color: #e6e6e6; }}
QListWidget#groupList::item:selected {{ background: {SELECT}; color: {ACCENT}; }}
QListWidget#groupList::item:hover:!selected {{ background: {HOVER}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: #121212; border: 1px solid #2a2a2a; border-radius: 4px; padding: 4px 6px;
    selection-background-color: {ACCENT_LO}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: #121212; border: 1px solid #2a2a2a; selection-background-color: {SELECT}; selection-color: {ACCENT}; }}

QPushButton {{ background: #161616; border: 1px solid #2c2c2c; border-radius: 4px; padding: 6px 14px; color: #e6e6e6; }}
QPushButton:hover {{ border-color: {ACCENT_LO}; color: {ACCENT_HI}; }}
QPushButton:pressed {{ background: {SELECT}; }}
QPushButton:disabled {{ color: #555; border-color: #222; }}
QPushButton:checked {{ background: {SELECT}; border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton#primary {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {ACCENT_HI}, stop:1 #d9ad5f);
    color: #1a1408; border: none; font-weight: 600; }}
QPushButton#primary:hover {{ background: {ACCENT_HI}; color: #1a1408; }}
QPushButton#primary:disabled {{ background: #3a3324; color: #6d6450; }}
QPushButton#danger {{ color: {DANGER}; }}
QPushButton#danger:hover {{ border-color: {DANGER}; }}
QPushButton#backButton, QPushButton#newPlaylist {{ background: transparent; border: none; text-align: left; }}
QPushButton#backButton {{ color: {ACCENT}; padding: 0; font-size: 10.5pt; }}
QPushButton#newPlaylist {{ color: {MUTED}; padding: 8px 20px; }}
QPushButton#newPlaylist:hover {{ color: {ACCENT}; }}
QPushButton#crumb {{ background: transparent; border: none; color: {MUTED}; padding: 2px 4px; }}
QPushButton#crumb:hover {{ color: {ACCENT}; }}
QPushButton#crumbCurrent {{ background: transparent; border: none; color: {TEXT}; padding: 2px 4px; font-weight: 600; }}

QTableView, QListView, QTreeView, QListWidget {{ background: {BG}; border: none; alternate-background-color: {BG};
    selection-background-color: {SELECT}; selection-color: {ACCENT}; gridline-color: {LINE}; outline: none; }}
QTableView::item {{ padding: 0 6px; border-bottom: 1px solid #141414; }}
QTableView::item:selected {{ background: {SELECT}; color: {ACCENT}; }}
QTableView::item:hover:!selected {{ background: {HOVER}; }}
QTreeView::item {{ height: 30px; }}
QTreeView::item:selected {{ background: {SELECT}; color: {ACCENT}; }}
QHeaderView::section {{ background: {BG}; color: {ACCENT_LO}; border: none; border-bottom: 1px solid {LINE};
    padding: 8px 6px; font-size: 8.5pt; font-weight: 600; }}
QTableCornerButton::section {{ background: {BG}; border: none; }}
QHeaderView {{ background: {BG}; border: none; }}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 4px 0; }}
QScrollBar::handle:vertical {{ background: #2e2a22; border-radius: 4px; min-height: 36px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT_LO}; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; }}
QScrollBar::handle:horizontal {{ background: #2e2a22; border-radius: 4px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QProgressBar {{ background: #121212; border: 1px solid #2a2a2a; border-radius: 3px; text-align: center; height: 12px; color: {TEXT}; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}
QGroupBox {{ border: 1px solid {LINE}; border-radius: 6px; margin-top: 16px; padding: 12px 10px 10px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {ACCENT}; font-weight: 600; }}
QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; border: 1px solid #444; background: #101010; }}
QCheckBox::indicator {{ border-radius: 3px; }}
QRadioButton::indicator {{ border-radius: 7px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QMenu {{ background: #111; border: 1px solid #2a2a2a; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; }}
QMenu::item:selected {{ background: {SELECT}; color: {ACCENT}; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 4px 8px; }}
QStatusBar {{ background: #000; color: {MUTED}; }}
QSplitter::handle {{ background: {LINE}; }}
QTabWidget::pane {{ border: 1px solid {LINE}; border-radius: 4px; }}
QTabBar::tab {{ background: transparent; padding: 6px 14px; color: {MUTED}; }}
QTabBar::tab:selected {{ color: {ACCENT}; border-bottom: 2px solid {ACCENT}; }}
QMessageBox, QInputDialog, QFileDialog, QDialog {{ background: #101010; }}
"""


QSS = qss("titanium")
