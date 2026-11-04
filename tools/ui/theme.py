"""NMS-style visual theme: color tokens, app font, and the global stylesheet."""

import sys

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QWidget

# --- tokens (NMS in-game palette, tuned for dark desktop use) --------------------

BG_BASE = "#12111a"   # window background (top of gradient)
BG_DEEP = "#1e1a28"   # window background (bottom of gradient, purple tint)
BG_PANEL = "#14121a"  # inset panels: console, headers, combos
BG_LIFT = "#1d1a24"   # raised surfaces: buttons, inputs
BORDER = "#2a2833"    # 1px hairlines everywhere (sharp corners, no shadows)
TEXT = "#e3e1e9"
DIM = "#8e8c96"

ACCENT = "#3fd8d8"    # cyan: structure, labels, hover, busy wash
YELLOW = "#f0c93c"    # attention: merge needed, format warnings
MAGENTA = "#ff4d6b"   # conflict / error text
SEL_BLOCK = "#8f1f4a" # deep magenta selection block behind rows
TEAL = "#4fd8c4"      # verified / clean

COLOR_ACCENT = QColor(ACCENT)
COLOR_CLEAN = QColor(TEAL)
COLOR_MERGE = QColor(YELLOW)
COLOR_ERROR = QColor(MAGENTA)
COLOR_UNMATCHED = QColor(DIM)
COLOR_DIM = QColor(DIM)

FONT_FAMILIES = ["Bahnschrift", "Segoe UI"]  # DIN-like geometric, Windows built-in


def app_font() -> QFont:
    """The theme font: Bahnschrift (DIN-like) with a Segoe UI fallback."""
    font = QFont()
    font.setFamilies(FONT_FAMILIES)
    return font


def apply_theme(app) -> None:
    """Apply the theme font, stylesheet, and window-chrome filter to the app."""
    app.setFont(app_font())
    app.setStyleSheet(APP_QSS)
    global _CHROME_FILTER
    _CHROME_FILTER = _ChromeFilter()
    app.installEventFilter(_CHROME_FILTER)


_CHROME_FILTER = None  # module-level ref: Qt keeps only a weak link to filters


class _ChromeFilter(QObject):
    """Applies dark DWM title-bar chrome to top-level windows after they show."""

    def eventFilter(self, obj, event) -> bool:
        # Deferred a tick (DWM inside Show re-enters the style machinery);
        # isinstance-guarded: app-level filters see every QObject, not only widgets.
        if (
            isinstance(obj, QWidget)
            and obj.isWindow()
            and event.type() == QEvent.Type.Show
        ):
            QTimer.singleShot(0, lambda w=obj: apply_window_chrome(w))
        return False


def _colorref(hex_color: str) -> int:
    """COLORREF for DWM: 0x00BBGGRR (note the channel order)."""
    c = QColor(hex_color)
    return c.blue() << 16 | c.green() << 8 | c.red()


def apply_window_chrome(widget) -> None:
    """Win32/DWM: rounded corners, dark caption/border/text colored to the theme."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        dwm = ctypes.windll.dwmapi
        hwnd = int(widget.winId())
        dark = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark)
        )  # DWMWA_USE_IMMERSIVE_DARK_MODE
        for attr, value in (
            (33, 2),                      # DWMWA_WINDOW_CORNER_PREFERENCE (round)
            (34, _colorref(BORDER)),      # DWMWA_BORDER_COLOR
            (35, _colorref(BG_BASE)),     # DWMWA_CAPTION_COLOR
            (36, _colorref(TEXT)),        # DWMWA_TEXT_COLOR
        ):
            v = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
    except OSError:
        pass


APP_QSS = f"""
QMainWindow, QDialog {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {BG_BASE}, stop:1 {BG_DEEP});
}}
QLabel {{ color: {TEXT}; background: transparent; }}
QTreeWidget, QTreeView, QListWidget, QTableWidget {{
    background: transparent;
    alternate-background-color: rgba(255, 255, 255, 6);
    border: 1px solid {BORDER};
    color: {TEXT};
}}
QHeaderView::section {{
    background-color: {BG_PANEL};
    color: {ACCENT};
    border: none;
    border-bottom: 1px solid rgba(63, 216, 216, 45%);
    padding: 3px 6px;
    font-weight: 600;
}}
QPlainTextEdit, QLineEdit, QComboBox {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER};
    color: {TEXT};
    selection-background-color: {SEL_BLOCK};
    padding: 2px 4px;
}}
QLineEdit:focus, QComboBox:focus {{ border-color: rgba(63, 216, 216, 60%); }}
QPushButton {{
    background-color: {BG_LIFT};
    border: 1px solid {BORDER};
    color: {TEXT};
    padding: 4px 14px;
}}
QPushButton:hover {{ border-color: rgba(63, 216, 216, 70%); color: white; }}
QPushButton:pressed {{ background-color: #242030; }}
QPushButton:disabled {{ color: {DIM}; border-color: {BORDER}; }}
QCheckBox {{ color: {TEXT}; background: transparent; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; background: transparent; top: -1px; }}
QTabBar::tab {{
    background: {BG_PANEL};
    color: {DIM};
    border: 1px solid {BORDER};
    border-bottom: none;
    padding: 4px 14px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {YELLOW};
    color: {BG_PANEL};
    font-weight: 600;
    border-color: {YELLOW};
}}
QTabBar::tab:hover:!selected {{ color: {TEXT}; }}
QComboBox QAbstractItemView {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER};
    selection-background-color: {SEL_BLOCK};
}}
QMenu {{ background-color: {BG_PANEL}; border: 1px solid {BORDER}; color: {TEXT}; }}
QMenu::item {{ padding: 4px 22px; }}
QMenu::item:selected {{ background-color: {SEL_BLOCK}; }}
QStatusBar {{
    background: {BG_PANEL};
    border-top: 1px solid rgba(63, 216, 216, 40%);
    color: {DIM};
}}
QStatusBar QLabel {{ color: {DIM}; font-size: 11px; }}
QToolTip {{
    background-color: {BG_PANEL};
    color: {TEXT};
    border: 1px solid rgba(63, 216, 216, 50%);
    padding: 3px;
}}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: rgba(63, 216, 216, 55%); }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; min-width: 24px; }}
"""
