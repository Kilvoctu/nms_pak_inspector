"""Main window: frameless Housekeeper-style layout, TaskWorkers, persisted settings."""

import copy
import hashlib
import json
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

from PySide6.QtCore import (
    QByteArray,
    QEvent,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QDesktopServices,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QImage,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QShortcut,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import (
    index_paks,
    inject_mods,
    install_mods,
    mbincompiler,
    merge_mods,
    nexus,
    paklib,
    repo,
    verify_mods,
)
from ..conflicts import (
    K_AMBIGUOUS,
    K_CONFLICT,
    K_DUPLICATE,
    FieldConflict,
    compute_field_conflicts,
)
from .worker import (
    FileHit,
    ModScan,
    TaskWorker,
    compiler_status_worker,
    compiler_update_worker,
    index_worker,
    inject_all_worker,
    inject_group_worker,
    inject_mod_worker,
    install_mod_worker,
    merge_preview_worker,
    nexus_info_worker,
    revert_all_worker,
    revert_mod_worker,
    scan_mods_worker,
    stash_replaced_zip,
    uninstall_mod_worker,
    verify_worker,
)

# --- identity -----------------------------------------------------------------

COLOR_ACCENT = QColor("#ff9a3c")  # NMS amber: hover tint, busy wash, merge flag
COLOR_CLEAN = QColor(Qt.GlobalColor.darkGreen)
COLOR_MERGE = QColor("#c47f00")
COLOR_UNMATCHED = QColor("#808080")
COLOR_ERROR = QColor("#c0392b")
COLOR_DIM = QColor("#808080")

_TREE_COLUMNS = ["Mod", "Status", "Fields", "Target pak(s)", "Files", "Actions"]
_TREE_COLUMN_WIDTHS = (
    (0, 300), (1, 115), (2, 90), (3, 230), (4, 50), (5, 170),
)
_ACTION_ROLE = Qt.ItemDataRole.UserRole + 1  # per-mod action state (dict)
_SCANTIP_ROLE = Qt.ItemDataRole.UserRole + 2  # scan-derived part of a file tooltip

_RESIZE_MARGIN = 8  # resizable window border thickness
_PATH_LABEL_WIDTH = 78  # aligns the Mods folder / PCBANKS path rows
_PROJECT_ROOT = repo.project_root()
_DEFAULT_MODS_DIR = repo.mods_dir()
_INDEX_PATH = repo.index_file()
_LOCK_PATH = _PROJECT_ROOT / "nms_pak_injector.lock"

# Status keys shared by tree rows and tooltips.
ST_CLEAN = "clean"
ST_MERGE = "merge"
ST_CONFLICT = "conflict"
ST_UNMATCHED = "unmatched"

# Field-verification statuses (from verify_mods).
FV_VERIFIED = "verified"
FV_NO_EFFECT = "no_effect"
FV_FORMAT = "format"
FV_OUTDATED = "outdated"
FV_ERROR = "error"
_FIELDS_LABEL = {
    FV_VERIFIED: "Verified",
    FV_NO_EFFECT: "No effect",
    FV_FORMAT: "Format",
    FV_OUTDATED: "Outdated",
    FV_ERROR: "Error",
}
_FIELDS_COLOR = {
    FV_VERIFIED: COLOR_CLEAN,
    FV_NO_EFFECT: COLOR_MERGE,
    FV_FORMAT: COLOR_MERGE,
    FV_OUTDATED: COLOR_ERROR,
    FV_ERROR: COLOR_ERROR,
}
_FIELDS_WORST_FIRST = [FV_ERROR, FV_OUTDATED, FV_FORMAT, FV_NO_EFFECT, FV_VERIFIED]


# --- small helpers ------------------------------------------------------------


def _settings():
    from PySide6.QtCore import QSettings

    return QSettings(str(_PROJECT_ROOT / "state.ini"), QSettings.Format.IniFormat)


def _restored_geometry(raw: object) -> QByteArray | None:
    """Decode a stored geometry (hex of saveGeometry bytes); None when unusable."""
    if isinstance(raw, QByteArray):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return QByteArray.fromHex(raw.encode("ascii"))
        except UnicodeEncodeError:
            return None
    return None


def _status_color(status: str) -> QColor:
    return {
        ST_CLEAN: COLOR_CLEAN,
        ST_MERGE: COLOR_MERGE,
        ST_CONFLICT: COLOR_ERROR,
        ST_UNMATCHED: COLOR_UNMATCHED,
    }.get(status, COLOR_ERROR)


def _dimmed(palette: QPalette, amount: float = 0.4) -> QColor:
    """Palette text color blended toward the window background."""
    text = palette.color(QPalette.ColorGroup.Normal, QPalette.ColorRole.WindowText)
    back = palette.color(QPalette.ColorGroup.Normal, QPalette.ColorRole.Window)
    blend = lambda a, b: a + round((b - a) * amount)
    return QColor(
        blend(text.red(), back.red()),
        blend(text.green(), back.green()),
        blend(text.blue(), back.blue()),
    )


# --- custom title bar ---------------------------------------------------------


class _TitleBar(QWidget):
    """Frameless title bar: drag to move, hamburger menu, window buttons, busy wash."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self._window = window
        self.setObjectName("nmsTitleBar")
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFixedHeight(34)
        self._busy_count = 0
        self._pulse_phase = 0.0
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(33)  # ~30 fps pulse
        self._pulse_timer.timeout.connect(self._on_pulse)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 4, 0)
        layout.setSpacing(6)

        self._menu_button = QToolButton(self)
        self._menu_button.setObjectName("menuButton")
        self._menu_button.setText("☰")
        self._menu_button.setAutoRaise(True)
        self._menu_button.setFixedSize(28, 28)
        self._menu_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._menu_button.setToolTip("Menu")
        self.app_menu = QMenu(self)
        self._menu_button.clicked.connect(self._show_menu)
        layout.addWidget(self._menu_button)

        title_label = QLabel("NMS Pak Inspector", self)
        title_font = title_label.font()
        title_font.setPointSize(10)
        title_font.setBold(True)
        title_label.setFont(title_font)
        layout.addWidget(title_label)
        layout.addStretch(1)

        self._min_button = QToolButton(self)
        self._min_button.setObjectName("minButton")
        self._min_button.setAutoRaise(True)
        self._min_button.setFixedSize(28, 28)
        self._min_button.setToolTip("Minimize")
        self._min_button.clicked.connect(window.showMinimized)
        layout.addWidget(self._min_button)
        self._max_button = QToolButton(self)
        self._max_button.setObjectName("maxButton")
        self._max_button.setAutoRaise(True)
        self._max_button.setFixedSize(28, 28)
        self._max_button.setToolTip("Maximize")
        self._max_button.clicked.connect(self._toggle_maximized)
        layout.addWidget(self._max_button)
        self._close_button = QToolButton(self)
        self._close_button.setObjectName("closeButton")
        self._close_button.setAutoRaise(True)
        self._close_button.setFixedSize(28, 28)
        self._close_button.setToolTip("Close")
        self._close_button.clicked.connect(window.close)
        layout.addWidget(self._close_button)

        # Segoe Fluent Icons / Segoe MDL2 Assets: ChromeMinimize, ChromeClose,
        # ChromeRestore, ChromeMaximize.
        glyph_font = QFont()
        glyph_font.setFamilies(["Segoe Fluent Icons", "Segoe MDL2 Assets"])
        glyph_font.setPointSize(9)
        self._min_button.setFont(glyph_font)
        self._max_button.setFont(glyph_font)
        self._close_button.setFont(glyph_font)
        self._min_button.setText("\uE921")
        self._close_button.setText("\uE8BB")
        self._sync_max_glyph()

        accent = COLOR_ACCENT
        self.setStyleSheet(
            "QToolButton:hover {"
            f" background-color: rgba({accent.red()}, {accent.green()}, {accent.blue()}, 15%);"
            " }"
            "QToolButton:pressed {"
            f" background-color: rgba({accent.red()}, {accent.green()}, {accent.blue()}, 30%);"
            " }"
            "QToolButton#closeButton:hover {"
            " background-color: #e81123;"
            " color: white;"
            " }"
            "QToolButton#closeButton:pressed {"
            " background-color: #c50f1f;"
            " }"
        )
        window.installEventFilter(self)
        self._menu_closed_at = 0.0
        self.app_menu.aboutToHide.connect(self._remember_menu_close)

    def _remember_menu_close(self) -> None:
        import time

        self._menu_closed_at = time.monotonic()

    def _show_menu(self) -> None:
        import time

        if self.app_menu.isVisible():
            self.app_menu.close()
            return
        if time.monotonic() - self._menu_closed_at < 0.3:
            return  # the click that closed it also lands here - don't reopen
        self.app_menu.popup(
            self._menu_button.mapToGlobal(QPoint(0, self._menu_button.height()))
        )

    def eventFilter(self, obj, event) -> bool:
        if obj is self._window and event.type() == QEvent.Type.WindowStateChange:
            self._sync_max_glyph()
        return super().eventFilter(obj, event)

    def _toggle_maximized(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()

    def _sync_max_glyph(self) -> None:
        """Set the Fluent ChromeRestore (maximized) / ChromeMaximize glyph."""
        self._max_button.setText("\uE923" if self._window.isMaximized() else "\uE922")

    def set_busy(self, busy: bool) -> None:
        self._busy_count += 1 if busy else -1
        self._busy_count = max(0, self._busy_count)
        if self._busy_count > 0:
            if not self._pulse_timer.isActive():
                self._pulse_phase = 0.0
                self._pulse_timer.start()
        else:
            self._pulse_timer.stop()
        self.update()

    def _on_pulse(self) -> None:
        """Advance the busy-wash pulse and repaint the title bar."""
        self._pulse_phase += 0.21  # ~1.4 s per full cycle at 33 ms ticks
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Window))
        if self._busy_count > 0:
            # Breathing accent wash: alpha oscillates smoothly between a dim
            # base and a brighter peak so activity reads at a glance.
            import math

            wave = 0.5 + 0.5 * math.sin(self._pulse_phase)
            alpha = int(35 + 50 * wave)
            accent = QColor(
                COLOR_ACCENT.red(), COLOR_ACCENT.green(), COLOR_ACCENT.blue(), alpha
            )
            painter.fillRect(self.rect(), accent)
        painter.setPen(QPen(self.palette().color(QPalette.ColorRole.Mid), 1.0))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        handle = self._window.windowHandle()
        if handle is not None:
            handle.startSystemMove()
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


# --- collision detail dialog --------------------------------------------------


class _CollisionDialog(QDialog):
    """Show which mods assign the same pak path and how their edits relate."""

    _KIND_COLOR = {
        K_CONFLICT: COLOR_ERROR,
        K_AMBIGUOUS: COLOR_MERGE,
        K_DUPLICATE: COLOR_DIM,
    }

    def __init__(
        self,
        stored_path: str,
        entries: list[tuple[str, str]],
        conflicts: list[FieldConflict] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Collision detail")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        header = QLabel(f"Pak path assigned by {len(entries)} mods:")
        f = header.font()
        f.setBold(True)
        header.setFont(f)
        layout.addWidget(header)
        for mod_name, entry in sorted(entries):
            row = QLabel(f"• {mod_name}\n    {entry}")
            row.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(row)

        conflicts = conflicts or []
        if conflicts:
            shared = QLabel("Shared fields (edited by more than one mod):")
            sf = shared.font()
            sf.setBold(True)
            shared.setFont(sf)
            layout.addSpacing(6)
            layout.addWidget(shared)
            for c in conflicts:
                color = self._KIND_COLOR.get(c.kind, COLOR_DIM)
                kind_text = {
                    K_CONFLICT: "CONFLICT",
                    K_AMBIGUOUS: "possible conflict (repeated field)",
                    K_DUPLICATE: "duplicate (same value)",
                }[c.kind]
                title = QLabel(f"{c.path}  —  {kind_text}")
                title.setForeground(color)
                layout.addWidget(title)
                for mod_name, value in sorted(c.edits.items()):
                    row = QLabel(f"      {mod_name} = {value}")
                    row.setTextInteractionFlags(
                        Qt.TextInteractionFlag.TextSelectableByMouse
                    )
                    layout.addWidget(row)

        note = QLabel(
            "Only one MBIN per pak path can exist - these mods must be merged "
            "into a single MBIN before injection."
        )
        note.setWordWrap(True)
        note.setForegroundRole(QPalette.ColorRole.WindowText)
        layout.addSpacing(6)
        layout.addWidget(note)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)


class _RowActionDelegate(QStyledItemDelegate):
    """Paints the per-mod action buttons into the Actions column; clicks route to handlers."""

    def __init__(self, tree: QTreeWidget, on_action, parent=None) -> None:
        super().__init__(parent)
        self._tree = tree
        self._on_action = on_action
        self._mouse_pos: QPoint | None = None
        self._hover_row = -1
        self._pressed: tuple[int, str] | None = None
        tree.viewport().installEventFilter(self)
        tree.viewport().setMouseTracking(True)

    # --- geometry -----------------------------------------------------------

    def _button_rects(self, cell: QRect) -> tuple[QRect, QRect]:
        margins, gap = 5, 6
        area = cell.adjusted(margins, 3, -margins, -3)
        w = (area.width() - gap) // 2
        return (
            QRect(area.left(), area.top(), w, area.height()),
            QRect(area.left() + w + gap, area.top(), area.width() - w - gap, area.height()),
        )

    def sizeHint(self, option, index) -> QSize:
        """Rows must be tall enough for the painted action buttons."""
        hint = super().sizeHint(option, index)
        if index.column() == 5:
            hint.setHeight(max(hint.height(), 28))
        return hint

    def _hit_button(self, pos: QPoint) -> tuple[int, str] | None:
        index = self._tree.indexAt(pos)
        if not index.isValid() or index.column() != 5:
            return None
        state = index.sibling(index.row(), 0).data(_ACTION_ROLE)
        if not state or state.get("busy"):
            return None
        rect = self._tree.visualRect(index)
        primary, secondary = self._button_rects(rect)
        local = pos  # viewport coords == visualRect coords
        if primary.contains(local):
            return (index.row(), "primary")
        if secondary.contains(local):
            return (index.row(), "secondary")
        return None

    # --- event filter: hover + click -----------------------------------------

    def eventFilter(self, obj, event) -> bool:
        etype = event.type()
        if etype in (
            QEvent.Type.MouseMove, QEvent.Type.Enter, QEvent.Type.Wheel,
            QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
        ):
            pos = event.position().toPoint() if hasattr(event, "position") else QPoint(0, 0)
            self._mouse_pos = pos
            index = self._tree.indexAt(pos)
            new_hover = index.row() if index.isValid() else -1
            if new_hover != self._hover_row or etype == QEvent.Type.MouseMove:
                self._hover_row = new_hover
                self._tree.viewport().update()
            if etype == QEvent.Type.MouseButtonPress:
                self._pressed = self._hit_button(pos)
                self._tree.viewport().update()
            elif etype == QEvent.Type.MouseButtonRelease:
                hit = self._hit_button(pos)
                if hit is not None and self._pressed == hit:
                    index = self._tree.indexAt(pos)
                    name_item = self._tree.itemFromIndex(index.sibling(index.row(), 0))
                    if name_item is not None:
                        self._on_action(name_item.text(0), hit[1])
                self._pressed = None
                self._tree.viewport().update()
        elif etype == QEvent.Type.Leave:
            self._mouse_pos = None
            self._hover_row = -1
            self._pressed = None
            self._tree.viewport().update()
        return False

    # Tooltips: Qt's native item tooltips show the combined Actions-cell text
    # from MainWindow._update_action_states (no manual QToolTip calls here).

    # --- painting ------------------------------------------------------------

    def paint(self, painter, option, index) -> None:
        super().paint(painter, option, index)  # row background / selection
        if index.column() != 5:
            return
        state = index.sibling(index.row(), 0).data(_ACTION_ROLE)
        if not state:
            return
        painter.save()
        if state.get("busy"):
            painter.setPen(COLOR_DIM)
            painter.drawText(option.rect, Qt.AlignmentFlag.AlignCenter, state["busy"])
            painter.restore()
            return
        hover = index.row() == self._hover_row
        primary, secondary = self._button_rects(option.rect)
        self._paint_button(
            painter, primary, state["primary"][0], state["primary"][1],
            hover and primary.contains(self._mouse_pos or QPoint(-1, -1)),
            self._pressed == (index.row(), "primary"),
        )
        self._paint_button(
            painter, secondary, state["secondary"][0], state["secondary"][1],
            hover and secondary.contains(self._mouse_pos or QPoint(-1, -1)),
            self._pressed == (index.row(), "secondary"),
        )
        painter.restore()

    def _paint_button(self, painter, rect: QRect, label: str, enabled: bool,
                      hovered: bool, pressed: bool) -> None:
        if not label:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not enabled:
            fill, border, text = QColor("#2b2b2b"), QColor("#383838"), QColor("#6f6f6f")
        elif pressed:
            fill, border, text = QColor("#232323"), QColor("#5a5a5a"), QColor("#e6e6e6")
        elif hovered:
            fill, border, text = QColor("#45403a"), COLOR_MERGE, QColor("#f5f5f5")
        else:
            fill, border, text = QColor("#3c3c3c"), QColor("#565656"), QColor("#e6e6e6")
        painter.setPen(border)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect.adjusted(0, 0, -1, -1), 3, 3)
        painter.setPen(text)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
        painter.restore()


class _MergedPreview(QTreeWidget):
    """Merged-result tree with diff highlighting; shared by editor and merge dialog."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setColumnCount(1)
        self.setHeaderHidden(True)

    def set_data(self, mod_root, van_root) -> None:
        merged, _notes = merge_mods.merge_tree(mod_root, van_root)
        self.set_merged(merged, van_root)

    def set_merged(self, merged_root, van_root) -> None:
        self.clear()
        self._fill(
            list(merged_root.findall("Property")),
            list(van_root.findall("Property")),
            self.invisibleRootItem(),
        )
        self.expandAll()

    def _fill(self, merged_props, van_props, parent_item) -> None:
        van_by_name: dict[str | None, list] = {}
        for vp in van_props:
            van_by_name.setdefault(vp.get("name"), []).append(vp)
        van_by_id: dict[tuple[str | None, str], object] = {}
        van_by_index: dict[tuple[str | None, str], object] = {}
        for vp in van_props:
            vid, vidx = vp.get("_id"), vp.get("_index")
            if vid is not None:
                van_by_id[(vp.get("name"), vid)] = vp
            if vidx is not None:
                van_by_index[(vp.get("name"), vidx)] = vp
        consumed: dict[str | None, int] = {}

        def pick(name):
            cands = van_by_name.get(name, [])
            i = consumed.get(name, 0)
            consumed[name] = i + 1
            return cands[i] if i < len(cands) else None

        for mp in merged_props:
            name = mp.get("name")
            mid, mindex = mp.get("_id"), mp.get("_index")
            inserted = False
            if mid is not None:
                vp = van_by_id.get((name, mid))
            elif mindex is not None and van_by_index.get((name, mindex)) is not None:
                vp = van_by_index[(name, mindex)]
            elif mindex is not None:
                vp = None
                inserted = True
            else:
                vp = pick(name)
            label = name or "?"
            children = list(mp.findall("Property"))
            if children:
                item = QTreeWidgetItem(parent_item, [label])
                item.setForeground(0, COLOR_MERGE if inserted else QBrush())
                if inserted:
                    item.setText(0, f"{label}  (inserted)")
                self._fill(children, list(vp.findall("Property")) if vp is not None else [], item)
                continue
            value = mp.get("value")
            if value is None:
                item = QTreeWidgetItem(parent_item, [label])
                item.setForeground(0, QBrush())
                continue
            text = f"{label} = {value}"
            item = QTreeWidgetItem(parent_item, [text])
            if inserted:
                item.setText(0, f"{text}  (inserted)")
                item.setForeground(0, COLOR_CLEAN)
            elif vp is not None and not vp.findall("Property") and vp.get("value") != value:
                item.setForeground(0, COLOR_MERGE)
            else:
                item.setForeground(0, QBrush())


class _TreeValueDelegate(QStyledItemDelegate):
    """Inline editor for a mod-tree leaf: edits only the value part of
    'name = value', committing to the underlying element."""

    def __init__(self, tree, on_commit, parent=None) -> None:
        super().__init__(parent)
        self._tree = tree
        self._on_commit = on_commit

    def setEditorData(self, editor, index) -> None:
        item = self._tree.itemFromIndex(index)
        elem = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        text = elem.get("value") if elem is not None else (index.data() or "")
        editor.setText(text or "")

    def setModelData(self, editor, model, index) -> None:
        item = self._tree.itemFromIndex(index)
        elem = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if elem is None:
            return
        name = item.text(0).partition(" = ")[0]
        new_value = editor.text()
        item.setText(0, f"{name} = {new_value}")
        if new_value != (elem.get("value") or ""):
            self._on_commit(elem, new_value)


class _CompareTrees(QWidget):
    """Side-by-side mod/vanilla trees: linked nav, diff highlight, inline edits."""

    edited = Signal()

    def __init__(self, mod_root, van_root, editable: bool, parent=None) -> None:
        super().__init__(parent)
        self._mod_root = mod_root
        self._van_root = van_root
        self._editable = editable
        self._building = False
        self._syncing = False
        self._pairs: dict[int, QTreeWidgetItem] = {}  # left item -> right item
        self._pairs_r: dict[int, QTreeWidgetItem] = {}  # right item -> left item
        self._pair_vpid: dict[int, QTreeWidgetItem] = {}  # id(van elem) -> left item
        self._dups: dict[int, int] = {}  # id(elem) -> duplicate group size

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self._search = QLineEdit(self)
        self._search.setPlaceholderText("Search field name…")
        self._search.textChanged.connect(lambda _t: self.rebuild())
        top.addWidget(self._search, 1)
        self._changed_only = QCheckBox("Changed only", self)
        self._changed_only.toggled.connect(lambda _c: self.rebuild())
        top.addWidget(self._changed_only)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._left = QTreeWidget(self)
        self._left.setHeaderLabel("Mod file" + ("" if editable else " (read-only)"))
        self._right = QTreeWidget(self)
        self._right.setHeaderLabel("Vanilla - full tree (right-click to pull into mod)")
        for tree in (self._left, self._right):
            tree.setColumnCount(1)
            tree.setAlternatingRowColors(True)
            tree.setWordWrap(False)
        splitter.addWidget(self._left)
        splitter.addWidget(self._right)
        splitter.setSizes((1, 1))
        layout.addWidget(splitter, 1)

        self._left.itemExpanded.connect(lambda item: self._sync_expand(item, True))
        self._left.itemCollapsed.connect(lambda item: self._sync_expand(item, False))
        self._left.itemSelectionChanged.connect(
            lambda: self._align_to_selection(self._left)
        )
        self._right.itemSelectionChanged.connect(
            lambda: self._align_to_selection(self._right)
        )
        self._left.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._left.customContextMenuRequested.connect(self._on_left_menu)
        # Anchor-based scroll sync: align the panes on the paired item.
        self._left.verticalScrollBar().valueChanged.connect(
            lambda _v: self._sync_scroll_from(self._left)
        )
        self._right.verticalScrollBar().valueChanged.connect(
            lambda _v: self._sync_scroll_from(self._right)
        )
        self._last_anchor: tuple[int, int] | None = None
        if editable:
            self._left.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
            self._left.setItemDelegate(
                _TreeValueDelegate(self._left, self._commit_value, self)
            )
        self._right.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._right.customContextMenuRequested.connect(self._on_right_menu)

    # --- building -----------------------------------------------------------

    def rebuild(self) -> None:
        needle = self._search.text().strip().lower()
        changed_only = self._changed_only.isChecked()
        self._building = True
        try:
            self._left.clear()
            self._right.clear()
            self._pairs.clear()
            self._pairs_r.clear()
            self._pair_vpid.clear()
            self._dups = verify_mods._duplicate_groups(self._mod_root)
            van_props = list(self._van_root.findall("Property"))
            left_items, right_items = self._fill_mod(
                list(self._mod_root.findall("Property")), van_props,
                needle, changed_only,
            )
            for item in left_items:
                self._left.invisibleRootItem().addChild(item)
            if changed_only:
                for item in right_items:
                    self._right.invisibleRootItem().addChild(item)
            else:
                # Full vanilla tree for browsing (linked to paired mod nodes).
                for item in self._fill_van(van_props, needle):
                    self._right.invisibleRootItem().addChild(item)
            self._left.expandAll()
            self._right.expandAll()
        finally:
            self._building = False

    def _fill_mod(self, mod_props, van_props, needle: str,
                  changed_only: bool) -> tuple[list, list]:
        """Build the left (mod) tree items; filtered mode also returns vanilla mirrors."""
        van_by_name: dict[str | None, list] = {}
        for vp in van_props:
            van_by_name.setdefault(vp.get("name"), []).append(vp)
        van_by_id: dict[tuple[str | None, str], object] = {}
        van_by_index: dict[tuple[str | None, str], object] = {}
        for vp in van_props:
            vid, vidx = vp.get("_id"), vp.get("_index")
            if vid is not None:
                van_by_id[(vp.get("name"), vid)] = vp
            if vidx is not None:
                van_by_index[(vp.get("name"), vidx)] = vp
        consumed: dict[str | None, int] = {}

        def pick(name):
            cands = van_by_name.get(name, [])
            i = consumed.get(name, 0)
            consumed[name] = i + 1
            return cands[i] if i < len(cands) else None

        left_out: list[QTreeWidgetItem] = []
        right_out: list[QTreeWidgetItem] = []
        for mp in mod_props:
            name = mp.get("name") or "?"
            mid, mindex = mp.get("_id"), mp.get("_index")
            mremove = mp.get("_remove") is not None
            if mid is not None:
                vp = van_by_id.get((name, mid))
            elif mindex is not None and van_by_index.get((name, mindex)) is not None:
                vp = van_by_index[(name, mindex)]
            elif mindex is not None:
                vp = None
            else:
                vp = pick(name)
            children = list(mp.findall("Property"))
            van_children = list(vp.findall("Property")) if vp is not None else []
            value = mp.get("value")
            inserted = vp is None and not mremove
            child_left, child_right = ([], [])
            if children:
                child_left, child_right = self._fill_mod(
                    children, van_children, needle, changed_only
                )
            changed = (
                mremove or inserted
                or (
                    value is not None and not children and not van_children
                    and vp is not None and vp.get("value") != value
                )
                or bool(child_left)
            )
            if changed_only and not changed:
                continue
            if needle and not self._matches(name, value, needle) and not child_left:
                continue
            ltext = f"{name} = {value}" if value is not None and not children else name
            if mremove:
                ltext = f"{name}  [removed]"
            elif inserted and not children:
                ltext = (
                    f"{name} = {value}  [inserted]"
                    if value is not None else f"{name}  [inserted]"
                )
            dup_n = self._dups.get(id(mp))
            if dup_n:
                ltext += "  [duplicate]"
            litem = QTreeWidgetItem([ltext])
            litem.setData(0, Qt.ItemDataRole.UserRole, mp)
            if dup_n:
                litem.setToolTip(
                    0,
                    f"this field name appears {dup_n} times in the same table "
                    "- keep one copy (right-click to remove/move)",
                )
            if mremove:
                litem.setForeground(0, COLOR_ERROR)
            elif inserted:
                litem.setForeground(0, COLOR_CLEAN)
            elif changed:
                litem.setForeground(0, COLOR_MERGE)
            if needle and self._matches(name, value, needle):
                litem.setBackground(0, QColor(255, 154, 60, 60))
            if self._editable and not children and vp is not None and not mremove:
                litem.setFlags(litem.flags() | Qt.ItemFlag.ItemIsEditable)
            for child in child_left:
                litem.addChild(child)
            # Filtered mode builds the right counterpart here; full mode lets
            # _fill_van link via _pair_vpid.
            if vp is not None:
                self._pair_vpid[id(vp)] = litem
            left_out.append(litem)
            if vp is not None and changed_only:
                rtext = (
                    f"{name} = {vp.get('value')}"
                    if value is not None and not van_children else name
                )
                ritem = QTreeWidgetItem([rtext])
                self._pairs[id(litem)] = ritem
                self._pairs_r[id(ritem)] = litem
                for rc in child_right:
                    ritem.addChild(rc)
                right_out.append(ritem)
        return left_out, right_out

    @staticmethod
    def _matches(name: str, value, needle: str) -> bool:
        if needle in name.lower():
            return True
        return value is not None and needle in str(value).lower()

    def _fill_van(self, van_props, needle: str) -> list[QTreeWidgetItem]:
        """Right tree: the complete vanilla file; mod-touched leaves get highlight colors."""
        right_out: list[QTreeWidgetItem] = []
        for vp in van_props:
            name = vp.get("name") or "?"
            children = list(vp.findall("Property"))
            value = vp.get("value")
            child_items = self._fill_van(children, needle) if children else []
            if needle and not self._matches(name, value, needle) and not child_items:
                continue
            rtext = f"{name} = {value}" if value is not None and not children else name
            ritem = QTreeWidgetItem([rtext])
            ritem.setData(0, Qt.ItemDataRole.UserRole, vp)
            # Link to the left counterpart when the mod touches this node,
            # and mirror the mod-side change highlighting.
            litem = self._pair_vpid.get(id(vp))
            if litem is not None:
                self._pairs[id(litem)] = ritem
                self._pairs_r[id(ritem)] = litem
                mp = litem.data(0, Qt.ItemDataRole.UserRole)
                if (
                    value is not None and not children and mp is not None
                    and not mp.findall("Property")
                ):
                    if mp.get("_remove") is not None:
                        ritem.setForeground(0, COLOR_ERROR)
                        ritem.setToolTip(0, "removed by the mod")
                    elif mp.get("value") != value:
                        ritem.setForeground(0, COLOR_MERGE)
                        ritem.setToolTip(0, f"the mod changes this to: {mp.get('value')}")
            for child in child_items:
                ritem.addChild(child)
            # Mirror the left side's "region changed" cue: containers whose
            # descendant changed turn amber there, so they must here too.
            if value is None or children:
                if any(c.foreground(0).color() == COLOR_MERGE for c in child_items):
                    ritem.setForeground(0, COLOR_MERGE)
            right_out.append(ritem)
        return right_out

    # --- interaction ----------------------------------------------------------

    def _sync_expand(self, item: QTreeWidgetItem, expanded: bool) -> None:
        if self._building or self._syncing:
            return
        counterpart = self._pairs.get(id(item))
        if counterpart is None:
            return
        self._syncing = True
        try:
            counterpart.setExpanded(expanded)
        finally:
            self._syncing = False

    def _sync_scroll_from(self, source: QTreeWidget) -> None:
        """Align the other pane on the counterpart of the first visible item
        (unpaired items anchor to the nearest preceding paired one)."""
        if self._building or self._syncing:
            return
        item = source.itemAt(QPoint(0, 0))
        if item is None:
            return
        anchor = (id(source), id(item))
        if self._last_anchor == anchor:
            return  # same anchor as the last sync; nothing to do
        pairs = self._pairs if source is self._left else self._pairs_r
        target = self._nearest_pair_at_or_above(item, pairs)
        if target is None:
            return  # no relation at all: leave the other pane alone
        self._last_anchor = anchor
        self._syncing = True
        try:
            other = self._right if source is self._left else self._left
            other.scrollToItem(target, QAbstractItemView.ScrollHint.PositionAtTop)
        finally:
            self._syncing = False

    @staticmethod
    def _prev_sibling(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
        """The item's previous sibling (PySide6 has no previousSibling())."""
        parent = item.parent()
        if parent is None:
            tree = item.treeWidget()
            idx = tree.indexOfTopLevelItem(item) if tree is not None else 0
            return tree.topLevelItem(idx - 1) if tree is not None and idx > 0 else None
        idx = parent.indexOfChild(item)
        return parent.child(idx - 1) if idx > 0 else None

    def _nearest_pair_at_or_above(self, item: QTreeWidgetItem, pairs: dict):
        """The paired counterpart of this item, or of the closest item at or
        above it (previous siblings, then ancestors and their predecessors)."""
        node = item
        while node is not None:
            sib = node
            while sib is not None:
                pair = pairs.get(id(sib))
                if pair is not None:
                    return pair
                sib = self._prev_sibling(sib)
            node = node.parent()
        return None

    def _align_to_selection(self, tree: QTreeWidget) -> None:
        """Clicking/arrowing to a row in one pane aligns the other pane on
        its counterpart (same nearest-paired rule)."""
        item = tree.currentItem()
        if item is None or self._building or self._syncing:
            return
        pairs = self._pairs if tree is self._left else self._pairs_r
        target = self._nearest_pair_at_or_above(item, pairs)
        if target is None:
            return
        self._syncing = True
        try:
            other = self._right if tree is self._left else self._left
            other.scrollToItem(target, QAbstractItemView.ScrollHint.PositionAtTop)
        finally:
            self._syncing = False

    def _commit_value(self, elem, new_value: str) -> None:
        elem.set("value", new_value)
        self.rebuild()
        self.edited.emit()

    def _on_left_menu(self, pos) -> None:
        item = self._left.itemAt(pos)
        if item is None or not self._editable:
            return
        elem = item.data(0, Qt.ItemDataRole.UserRole)
        if elem is None:
            return
        menu = QMenu(self)
        if elem.get("_remove") is not None:
            action = menu.addAction("Undo removal")
            action.triggered.connect(lambda: self._apply_op("undo_removal", elem))
        elif self._paired_van(elem) is not None:
            heal = self._unpaired_same_name_sibling(elem) is not None
            action = menu.addAction(
                "Remove duplicate copy (sibling pairs with vanilla)"
                if heal else "Remove this entry"
            )
            action.triggered.connect(lambda: self._apply_op("remove", elem))
            action2 = menu.addAction("Insert duplicate as new list entry")
            action2.triggered.connect(lambda: self._apply_op("insert", elem))
            conformed = _format_drift(elem, self._paired_van(elem))
            if conformed is not None:
                action3 = menu.addAction(f"Fix formatting (write as {conformed})")
                action3.triggered.connect(lambda: self._apply_op("fix_format", elem))
        else:
            # Dead entry: vanilla doesn't have it, so a _remove directive
            # would be a no-op - the fix is deleting it from the mod file.
            action = menu.addAction("Delete from mod (dead entry)")
            action.triggered.connect(lambda: self._apply_op("delete", elem))
        self._add_move_actions(menu, elem)
        menu.exec(self._left.viewport().mapToGlobal(pos))

    def _add_move_actions(self, menu: QMenu, elem) -> None:
        """Move up/down within the same parent (keeps hand-edited files tidy)."""
        mod_parents = {c: p for p in self._mod_root.iter() for c in p}
        parent = mod_parents.get(elem)
        if parent is None:
            return
        sibs = list(parent)
        idx = sibs.index(elem)
        if idx > 0:
            menu.addAction("Move up").triggered.connect(
                lambda: self._apply_op("move_up", elem))
        if idx < len(sibs) - 1:
            menu.addAction("Move down").triggered.connect(
                lambda: self._apply_op("move_down", elem))

    def _apply_op(self, op: str, elem) -> None:
        if op == "remove" and self._unpaired_same_name_sibling(elem) is not None:
            # Duplicate consume: delete this copy so its unpaired same-name
            # sibling takes the vanilla pairing (a _remove would strand it).
            op = "delete"
        if op == "remove":
            elem.set("_remove", "")
        elif op == "fix_format":
            conformed = _format_drift(elem, self._paired_van(elem))
            if conformed is not None:
                elem.set("value", conformed)
        elif op == "undo_removal":
            elem.attrib.pop("_remove", None)
        elif op == "delete":
            mod_parents = {c: p for p in self._mod_root.iter() for c in p}
            parent = mod_parents.get(elem)
            if parent is not None:
                parent.remove(elem)
        elif op == "insert":
            mod_parents = {c: p for p in self._mod_root.iter() for c in p}
            parent = mod_parents.get(elem)
            if parent is None:
                return
            duplicate = copy.deepcopy(elem)
            duplicate.attrib.pop("_id", None)
            duplicate.attrib.pop("_remove", None)
            van_parent = self._find_van_parent(elem)
            if van_parent is not None:
                van_count = sum(
                    1 for c in van_parent if c.get("name") == elem.get("name")
                )
                duplicate.set("_index", str(van_count))
            parent.insert(list(parent).index(elem) + 1, duplicate)
        elif op in ("move_up", "move_down"):
            mod_parents = {c: p for p in self._mod_root.iter() for c in p}
            parent = mod_parents.get(elem)
            if parent is not None:
                sibs = list(parent)
                idx = sibs.index(elem)
                new = idx - 1 if op == "move_up" else idx + 1
                if 0 <= new < len(sibs):
                    parent.remove(elem)
                    parent.insert(new, elem)
        self.rebuild()
        self.edited.emit()

    # --- right tree menu (browse vanilla, pull into mod) ------------------------

    def _on_right_menu(self, pos) -> None:
        item = self._right.itemAt(pos)
        if item is None or not self._editable:
            return
        elem = item.data(0, Qt.ItemDataRole.UserRole)
        if elem is None:
            return
        menu = QMenu(self)
        if elem.findall("Property"):
            action = menu.addAction("Pull whole entry into mod")
        else:
            action = menu.addAction(f"Pull into mod (value: {elem.get('value')})")
        action.triggered.connect(lambda: self._pull_into_mod(elem))
        menu.exec(self._right.viewport().mapToGlobal(pos))

    def _pull_into_mod(self, van_elem) -> None:
        """Adopt a vanilla field into the mod (minimal container chain, value = vanilla's)."""
        van_parents = {c: p for p in self._van_root.iter() for c in p}
        chain = []
        node = van_elem
        while node is not None and node is not self._van_root:
            chain.append(node)
            node = van_parents.get(node)
        chain.reverse()  # top-level ... target

        mod_children = list(self._mod_root.findall("Property"))
        van_siblings = list(self._van_root.findall("Property"))
        cur_parent = self._mod_root
        consumed: set[int] = set()
        for van_node in chain[:-1]:
            counterpart = self._mod_counterpart(
                mod_children, van_node, van_siblings, consumed
            )
            if counterpart is None:
                # Structural copy: pairing attrs + type value, no children.
                counterpart = ET.Element("Property")
                for attr in ("name", "value", "_id", "_index"):
                    if van_node.get(attr) is not None:
                        counterpart.set(attr, van_node.get(attr))
                cur_parent.append(counterpart)
            cur_parent = counterpart
            mod_children = list(cur_parent.findall("Property"))
            van_siblings = list(van_node.findall("Property"))

        target = chain[-1]
        if target.findall("Property"):
            new_elem = copy.deepcopy(target)  # whole entry adoption
        else:
            new_elem = ET.Element("Property")
            for attr in ("name", "value", "_id", "_index"):
                if target.get(attr) is not None:
                    new_elem.set(attr, target.get(attr))
        cur_parent.append(new_elem)

        self.rebuild()
        self.edited.emit()
        # Select the new node and open its inline editor, ready to type.
        stack = [
            self._left.topLevelItem(i)
            for i in range(self._left.topLevelItemCount())
        ]
        while stack:
            item = stack.pop()
            if item.data(0, Qt.ItemDataRole.UserRole) is new_elem:
                self._left.setCurrentItem(item)
                self._left.scrollToItem(item)
                if self._editable and not item.childCount():
                    self._left.editItem(item, 0)
                return
            stack.extend(item.child(i) for i in range(item.childCount()))

    def _mod_counterpart(self, mod_children, van_node, van_siblings,
                         consumed: set[int]):
        """Find the mod-side counterpart of one vanilla node (keyed pairing),
        or None when the mod doesn't have it yet."""
        name = van_node.get("name")
        vid, vidx = van_node.get("_id"), van_node.get("_index")
        if vid is not None:
            return next(
                (c for c in mod_children if c.get("_id") == vid), None
            )
        if vidx is not None:
            return next(
                (c for c in mod_children if c.get("_index") == vidx), None
            )
        for c in mod_children:
            if c.get("name") == name and id(c) not in consumed:
                consumed.add(id(c))
                return c
        return None

    def _find_van_parent(self, mod_elem):
        """Vanilla parent of the mod element's pairing, if it has one."""
        van_elem = self._paired_van(mod_elem)
        if van_elem is None:
            return None
        for p in self._van_root.iter():
            if any(c is van_elem for c in p):
                return p
        return None

    def _paired_van(self, mod_elem):
        """Locate the vanilla counterpart of a mod element (keyed walk)."""
        def walk(mod_props, van_props):
            van_by_id = {
                (vp.get("name"), vp.get("_id")): vp
                for vp in van_props if vp.get("_id") is not None
            }
            van_by_index = {
                (vp.get("name"), vp.get("_index")): vp
                for vp in van_props if vp.get("_index") is not None
            }
            by_name: dict[str | None, list] = {}
            for vp in van_props:
                by_name.setdefault(vp.get("name"), []).append(vp)
            consumed: dict[str | None, int] = {}
            for mp in mod_props:
                name = mp.get("name")
                mid, mindex = mp.get("_id"), mp.get("_index")
                vp = None
                if mid is not None:
                    vp = van_by_id.get((name, mid))
                elif mindex is not None and van_by_index.get((name, mindex)) is not None:
                    vp = van_by_index[(name, mindex)]
                elif mindex is None:
                    cands = by_name.get(name, [])
                    i = consumed.get(name, 0)
                    consumed[name] = i + 1
                    vp = cands[i] if i < len(cands) else None
                if mp is mod_elem:
                    return mp, vp
                if mp.get("_remove") is not None or (
                    mindex is not None and vp is None
                ):
                    continue  # directive subtrees have no vanilla pairing
                result = walk(list(mp.findall("Property")),
                              list(vp.findall("Property")) if vp is not None else [])
                if result is not None:
                    return result
            return None

        result = walk(list(self._mod_root.findall("Property")),
                      list(self._van_root.findall("Property")))
        if result is None:
            return None
        _mp, vp = result
        return vp

    def _unpaired_same_name_sibling(self, elem):
        """A same-name sibling of elem in the mod tree that has no vanilla
        pairing (an inserted duplicate copy), or None."""
        mod_parents = {c: p for p in self._mod_root.iter() for c in p}
        parent = mod_parents.get(elem)
        if parent is None:
            return None
        for sib in parent:
            if sib is elem or sib.get("name") != elem.get("name"):
                continue
            if sib.get("_remove") is not None:
                continue
            if self._paired_van(sib) is None:
                return sib
        return None


class _AboutDialog(QDialog):
    """About box: feature summary, credits, license."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About NMS Pak Inspector")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        name_label = QLabel(
            '<span style="font-size:13pt; font-weight:bold;">NMS Pak Inspector</span>',
            self,
        )
        layout.addWidget(name_label)
        body_label = QLabel(
            "A simple tool that can verify, inspect, and install No Man's Sky mods",
            self,
        )
        body_label.setWordWrap(True)
        layout.addWidget(body_label)
        author_label = QLabel(
            'By Kilvoctu: <a href="https://github.com/Kilvoctu/nms_pak_inspector">Source</a>',
            self,
        )
        author_label.setOpenExternalLinks(True)
        layout.addWidget(author_label)
        credit_label = QLabel(
            'Uses <a href="https://github.com/monkeyman192/MBINCompiler">MBINCompiler</a> '
            "by monkeyman192.",
            self,
        )
        credit_label.setOpenExternalLinks(True)
        layout.addWidget(credit_label)
        license_label = QLabel("Released under the MIT License.", self)
        layout.addWidget(license_label)
        self.adjustSize()

    def showEvent(self, event: QShowEvent) -> None:
        """Lock the measured size once, after the real-font layout exists."""
        if self.minimumHeight() != self.maximumHeight():
            self.setFixedSize(self.sizeHint())
        super().showEvent(event)


class _NexusInfoDialog(QDialog):
    """Nexus metadata for one mod: versions, update verdict, page link."""

    def __init__(self, mod_name: str, result: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Nexus info")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        parsed = result.get("parsed")
        if parsed is None:
            note = QLabel(
                f"\"{mod_name}\" was not recognized as a Nexus download.<br>"
                "Nexus archives are named like:<br>"
                "<code>Name ModID Version YYYY-MM-DDTHH-MMZ Hash.zip</code>",
                self,
            )
            note.setWordWrap(True)
            layout.addWidget(note)
            self.setFixedWidth(480)
            self.adjustSize()
            return

        mod = result.get("mod")
        latest = result.get("latest")
        error = result.get("error")

        title = QLabel(
            f'<span style="font-size:13pt; font-weight:bold;">'
            f"{mod.get('name', parsed['name']) if mod else parsed['name']}</span>",
            self,
        )
        title.setWordWrap(True)
        layout.addWidget(title)

        def row(label: str, value: str, color: QColor | None = None) -> None:
            # No wordWrap: the dialog width adapts to the widest row instead.
            text = QLabel(f"<b>{label}:</b> {value}", self)
            if color is not None:
                text.setStyleSheet(
                    f"color: rgb({color.red()}, {color.green()}, {color.blue()});"
                )
            layout.addWidget(text)

        def stamp(epoch) -> str:
            if not epoch:
                return "?"
            from datetime import datetime, timezone

            return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )

        if mod:
            uploader = (mod.get("user") or {}).get("name") or "?"
            row(
                "Author",
                f"{uploader} · {mod.get('mod_downloads', 0):,} downloads",
            )
            if mod.get("summary"):
                summary = QLabel(mod["summary"], self)
                summary.setWordWrap(True)
                layout.addWidget(summary)
        row("Your version", f"{parsed['version']}  (uploaded {stamp(parsed['uploaded'])})")
        if latest:
            latest_stamp = stamp(latest.get("uploaded_timestamp"))
            row(
                "Latest on Nexus",
                f"{latest.get('version') or '?'}  (uploaded {latest_stamp})",
            )
        if error:
            row("Status", f"Nexus request failed - {error}", COLOR_ERROR)
        elif latest is None:
            row("Status", "no downloadable files listed on Nexus", COLOR_UNMATCHED)
        elif nexus.is_newer_version(parsed["version"], latest.get("version") or ""):
            row(
                "Status",
                f"update available: {parsed['version']} → {latest.get('version')}",
                COLOR_MERGE,
            )
        else:
            row("Status", "up to date", COLOR_CLEAN)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        page = QPushButton(f"Open Nexus page (mod {parsed['mod_id']})")
        page.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(nexus.page_url(parsed["mod_id"])))
        )
        buttons.addWidget(page)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.adjustSize()
        # Content-derived width: wide enough for the widest row, capped so a
        # long summary/error wraps instead of stretching the dialog.
        self.setFixedWidth(min(max(self.sizeHint().width(), 420), 800))
        self.adjustSize()

    def showEvent(self, event: QShowEvent) -> None:
        """Re-measure the height once real fonts exist; the width stays fixed."""
        self.setFixedHeight(self.sizeHint().height())
        super().showEvent(event)


class _NexusKeyDialog(QDialog):
    """Set the personal Nexus API key (paste, or import from SingularityMM)."""

    def __init__(self, current: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Nexus API key")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        info = QLabel(
            "Nexus metadata needs a <b>personal API key</b> (one-time setup):<br>"
            '1. Open <a href="https://www.nexusmods.com/users/myaccount?tab=api+access">'
            "nexusmods.com → Settings → API Access</a><br>"
            "2. Generate/copy a personal key and paste it below.",
            self,
        )
        info.setWordWrap(True)
        info.setOpenExternalLinks(True)
        layout.addWidget(info)
        self._edit = QLineEdit(self)
        self._edit.setPlaceholderText("Paste API key…")
        self._edit.setText(current)
        layout.addWidget(self._edit)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear)
        buttons.addWidget(clear)
        save = QPushButton("Save")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        self.adjustSize()

    def _clear(self) -> None:
        self._edit.clear()
        self.accept()

    def key(self) -> str:
        return self._edit.text().strip()

    def showEvent(self, event: QShowEvent) -> None:
        """Lock the measured size once, after the real-font layout exists."""
        if self.minimumHeight() != self.maximumHeight():
            self.setFixedSize(self.sizeHint())
        super().showEvent(event)


class _MergeDialog(QDialog):
    """Resolve cross-mod conflicts for a group injection and preview the
    merged result before anything is written."""

    def __init__(self, preview: dict, injected_names: list[str], parent=None) -> None:
        super().__init__(parent)
        self._preview = preview
        self.accepted_inject = False
        self.resolutions: dict[str, int] = {}
        names = preview["mod_names"]
        n_files = len(preview["files"])
        n_conflicts = sum(
            len(f["conflicts"]) for f in preview["files"].values()
        )
        self.setWindowTitle("Merge mods for injection")
        self.setMinimumSize(720, 480)
        layout = QVBoxLayout(self)

        header = QLabel(
            f"{len(names)} mod(s) share {n_files} pak file(s): "
            + ", ".join(names)
        )
        f = header.font()
        f.setBold(True)
        header.setFont(f)
        layout.addWidget(header)
        injected_note = [
            n for n in names if n in injected_names
        ]
        if injected_note:
            note = QLabel(
                "Already injected (will be updated in place): "
                + ", ".join(injected_note)
            )
            note.setWordWrap(True)
            layout.addWidget(note)

        if n_conflicts:
            info = QLabel(
                f"{n_conflicts} field(s) are claimed by more than one mod with "
                "different values - pick the winner for each. Everything else "
                "merges automatically."
            )
            info.setWordWrap(True)
            layout.addWidget(info)

            self._table = QTableWidget(n_conflicts, 3, self)
            self._table.setHorizontalHeaderLabels(
                ["Field", "Values", "Winner"]
            )
            self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            self._table.verticalHeader().setVisible(False)
            self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            row = 0
            for file in preview["files"].values():
                for conflict in file["conflicts"]:
                    participants = file["participants"]
                    field_item = QTableWidgetItem(
                        f"{conflict.path}\n({file['stored']})"
                    )
                    field_item.setFlags(field_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self._table.setItem(row, 0, field_item)
                    values_text = "\n".join(
                        f"{participants[idx]}: {value}"
                        for idx, value in conflict.choices
                    )
                    values_item = QTableWidgetItem(values_text)
                    values_item.setFlags(values_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self._table.setItem(row, 1, values_item)
                    combo = QComboBox(self)
                    for idx, value in conflict.choices:
                        combo.addItem(f"{participants[idx]}: {value}", idx)
                    combo.setCurrentIndex(0)
                    combo.currentIndexChanged.connect(
                        lambda _i, c=conflict, cb=combo: self._set_winner(c, cb)
                    )
                    self._table.setCellWidget(row, 2, combo)
                    row += 1
            layout.addWidget(self._table, 1)
        else:
            layout.addWidget(QLabel(
                "All fields merge automatically - no conflicts."
            ))

        if n_files:
            selector_row = QHBoxLayout()
            selector_row.addWidget(QLabel("Preview:", self))
            self._file_combo = QComboBox(self)
            for key, file in preview["files"].items():
                self._file_combo.addItem(file["stored"], key)
            self._file_combo.currentIndexChanged.connect(self._refresh_preview)
            selector_row.addWidget(self._file_combo, 1)
            layout.addLayout(selector_row)
            self._merge_preview = _MergedPreview(self)
            layout.addWidget(self._merge_preview, 2)
            self._refresh_preview(0)

        self._always_first = QCheckBox(
            "Always let the first mod win conflicts (don't ask again)", self
        )
        self._always_first.setChecked(
            _settings().value("merge_first_wins", False) in (True, "true")
        )
        if n_conflicts:
            layout.addWidget(self._always_first)

        buttons = QHBoxLayout()
        inject_btn = QPushButton("Merge & inject")
        inject_btn.clicked.connect(self._on_inject)
        buttons.addWidget(inject_btn)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        buttons.addStretch(1)
        layout.addLayout(buttons)

    def _set_winner(self, conflict, combo: QComboBox) -> None:
        idx = combo.currentData()
        if idx is not None:
            self.resolutions[conflict.path] = int(idx)
            self._refresh_preview(self._file_combo.currentIndex())

    def _refresh_preview(self, index: int) -> None:
        key = self._file_combo.itemData(index)
        if key is None:
            return
        file = self._preview["files"][key]
        merged, _notes, _conflicts = merge_mods.merge_many(
            file["mod_roots"], file["van_root"], self.resolutions or None
        )
        self._merge_preview.set_merged(merged, file["van_root"])

    def _on_inject(self) -> None:
        if self._always_first.isChecked():
            _settings().setValue("merge_first_wins", True)
        self.accepted_inject = True
        self.accept()


class _RawAssetDialog(QDialog):
    """Read-only inspector for non-MBIN mod files: sizes, diff verdict, image preview."""

    def __init__(self, ver, mod, hit, pcbanks: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Raw asset — {ver.mod_name}")
        self.setMinimumSize(520, 420)
        layout = QVBoxLayout(self)

        header = QLabel(
            f"{ver.rel}\n{ver.pak} : {ver.stored} — {_FIELDS_LABEL[ver.status]}"
        )
        f = header.font()
        f.setBold(True)
        header.setFont(f)
        layout.addWidget(header)

        mod_bytes = verify_mods._read_mod_data(mod, hit.entry)
        try:
            with paklib.Pak(Path(pcbanks) / hit.pak) as pk:
                van_bytes = pk.read_file(hit.stored)
            same = mod_bytes == van_bytes
            size_line = (
                f"Mod file: {len(mod_bytes):,} bytes · "
                f"Vanilla: {len(van_bytes):,} bytes · "
                + ("contents identical" if same else "contents differ")
            )
        except Exception as exc:  # noqa: BLE001 - shown, not fatal
            size_line = f"Mod file: {len(mod_bytes):,} bytes · vanilla read failed: {exc}"
        info = QLabel(size_line)
        info.setWordWrap(True)
        layout.addWidget(info)

        suffix = hit.entry.lower().rsplit(".", 1)[-1]
        preview: QLabel | None = None
        if suffix == "dds":
            preview = self._dds_preview(mod_bytes)
        if preview is None:
            note = QLabel(
                "No preview available for this format."
                if suffix != "dds"
                else "DDS preview unavailable (Pillow could not decode this variant)."
            )
            note.setForegroundRole(QPalette.ColorRole.WindowText)
            layout.addWidget(note)
            layout.addStretch(1)
        else:
            layout.addWidget(preview, 1)

        close = QPushButton("Close", self)
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)

    def _dds_preview(self, data: bytes) -> QLabel | None:
        """Decode a DDS texture with Pillow (best effort) into a label."""
        try:
            import io

            from PIL import Image

            img = Image.open(io.BytesIO(data))
            width, height = img.size
            img = img.convert("RGBA")
            data_bytes = img.tobytes("raw", "RGBA")
            qimg = QImage(
                data_bytes, width, height, width * 4, QImage.Format.Format_RGBA8888
            )
            if qimg.isNull():
                return None
            label = QLabel()
            pixmap = QPixmap.fromImage(qimg)
            scaled = pixmap.scaled(
                480, 400,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            label.setPixmap(scaled)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setToolTip(f"{width} x {height} px")
            label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            return label
        except Exception:  # noqa: BLE001 - undecodable variant: no preview
            return None


class _FieldDialog(QDialog):
    """Review/edit one mod file's fields (changed / all / merged views); Save writes the source."""

    _VERDICT_COLOR = {
        "OK": COLOR_CLEAN,
        "SAME": COLOR_DIM,
        "NO-OP": COLOR_MERGE,
        "INSERT": COLOR_MERGE,
        "REMOVE": COLOR_MERGE,
        "FORMAT": COLOR_MERGE,
        "MISSING": COLOR_ERROR,
        "TEMPLATE": COLOR_ERROR,
    }

    def __init__(self, ver, mod, hit, van_root, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Mod file editor — {ver.mod_name}")
        self.setMinimumSize(780, 540)
        self._ver = ver
        self._mod = mod
        self._hit = hit
        self._van_root = van_root
        self._dirty = False
        self.saved = False
        self._edit_work = repo.cache_dir() / "_editwork"
        self._table_rows: dict[int, list] = {}
        self._dups: dict[int, int] = {}  # id(elem) -> duplicate group size

        # In-memory mod tree; shipped MBINs are decompiled for editing and
        # recompiled through the managed compiler on Save.
        data = verify_mods._read_mod_data(mod, hit.entry)
        self._is_exml = hit.entry.lower().endswith((".exml", ".mxml"))
        self._mod_root = verify_mods._parse_mod_tree(
            hit.entry, data, self._edit_work, lambda m: None
        )
        self._refresh_rows()

        layout = QVBoxLayout(self)
        header = QLabel(f"{ver.rel}\n{ver.pak} : {ver.stored} — {_FIELDS_LABEL[ver.status]}")
        f = header.font()
        f.setBold(True)
        header.setFont(f)
        layout.addWidget(header)
        if ver.message:
            note = QLabel(ver.message)
            note.setWordWrap(True)
            note.setForegroundRole(QPalette.ColorRole.WindowText)
            layout.addWidget(note)
        self._fix_fmt_btn = QPushButton("Fix all formatting", self)
        self._fix_fmt_btn.setToolTip(
            "Rewrites every value whose number format drifts from vanilla "
            "(e.g. 10000 -> 10000.000000). Saves with the normal Save button."
        )
        self._fix_fmt_btn.clicked.connect(self._fix_all_formatting)
        self._fix_fmt_btn.hide()
        layout.addWidget(self._fix_fmt_btn)

        self._tabs = QTabWidget(self)
        self._changed_table = self._make_table()
        self._all_table = self._make_table()
        self._tabs.addTab(self._changed_table, "Changed fields")
        self._tabs.addTab(self._all_table, "All fields")
        self._compare = _CompareTrees(self._mod_root, van_root, True, self)
        self._compare.edited.connect(self._on_compare_edited)
        self._tabs.addTab(self._compare, "Compare trees")
        self._preview_tree = _MergedPreview(self)
        self._tabs.addTab(self._preview_tree, "Merged preview")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self._tabs, 1)

        self._rebuild_tables()

        # Verdict color legend.
        legend = QLabel(
            f'<span style="color:{COLOR_CLEAN.name()};">●</span> applies '
            f'&nbsp; <span style="color:{COLOR_MERGE.name()};">●</span> attention '
            f'(format drift, directives) &nbsp; '
            f'<span style="color:{COLOR_ERROR.name()};">●</span> missing '
            f'&nbsp; <span style="color:{COLOR_DIM.name()};">●</span> unchanged',
            self,
        )
        layout.addWidget(legend)

        self._note_label = QLabel(
            "Double-click a Mod value to edit it. Select a row marked with * for its explanation."
            if self._is_exml else
            "Shipped MBIN: edits are saved by recompiling with the managed "
            "MBINCompiler. Double-click a Mod value to edit it."
        )
        self._note_label.setWordWrap(True)
        self._note_label.setForegroundRole(QPalette.ColorRole.WindowText)
        layout.addWidget(self._note_label)

        # Restore the last editor geometry for a familiar window.
        geometry = _restored_geometry(_settings().value("editor_geometry"))
        if geometry is not None:
            self.restoreGeometry(geometry)

        buttons = QHBoxLayout()
        self._save_btn = QPushButton("Save")
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._save)
        buttons.addWidget(self._save_btn)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        buttons.addStretch(1)
        layout.addLayout(buttons)

    # --- table construction / filling ------------------------------------------

    def _make_table(self) -> QTableWidget:
        table = QTableWidget(0, 4, self)
        table.setHorizontalHeaderLabels(["Field", "Mod value", "Vanilla value", "Verdict"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(
            lambda pos, t=table: self._on_table_menu(t, pos)
        )
        table.itemChanged.connect(self._on_item_changed)
        table.itemSelectionChanged.connect(
            lambda t=table: self._on_selection_changed(t)
        )
        table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        return table

    def _refresh_rows(self) -> None:
        """Re-enumerate the (possibly edited) mod tree against vanilla."""
        rows = verify_mods.iter_field_rows(self._mod_root, self._van_root)
        self._all_rows = rows
        self._changed_rows = [r for r in rows if r.verdict != verify_mods.V_SAME]

    def _fill_table(self, table: QTableWidget, rows: list) -> None:
        self._dups = verify_mods._duplicate_groups(self._mod_root)
        self._table_rows[id(table)] = rows
        table.blockSignals(True)
        table.setRowCount(len(rows))
        editable = Qt.ItemFlag.ItemIsEditable
        for row, r in enumerate(rows):
            dup_n = self._dups.get(id(getattr(r, "mod_elem", None)))
            texts = (
                r.path + ("  [duplicate]" if dup_n else ""),
                r.mod_value if r.mod_value is not None else "—",
                r.vanilla_value if r.vanilla_value is not None else "—",
                r.verdict,
            )
            for col, text in enumerate(texts):
                item = QTableWidgetItem(text)
                if col == 1 and r.can_edit:
                    item.setFlags(item.flags() | editable)
                else:
                    item.setFlags(item.flags() & ~editable)
                tip = r.note
                if col == 0 and dup_n:
                    tip = (f"{tip}\n" if tip else "") + (
                        f"this field name appears {dup_n} times in the same "
                        "table - keep one copy (right-click to remove/move)"
                    )
                if col == 1 and r.verdict == verify_mods.V_FORMAT:
                    conformed = merge_mods._conform_value(r.mod_value, r.vanilla_value)
                    tip = f"{r.note}\nthe merge will write it as: {conformed}"
                    item.setForeground(COLOR_MERGE)
                if col == 1 and r.verdict == verify_mods.V_MISSING:
                    tip = (
                        f"{r.note}\ndead entry: delete it in Compare trees "
                        "(left pane menu), then find the replacement in the "
                        "vanilla pane and 'Pull into mod'"
                    )
                if tip:
                    item.setToolTip(tip)
                table.setItem(row, col, item)
            verdict_item = table.item(row, 3)
            verdict_item.setForeground(self._VERDICT_COLOR.get(r.verdict, COLOR_DIM))
            if r.note:
                verdict_item.setText(r.verdict + " *")
        table.blockSignals(False)

    def _rebuild_tables(self) -> None:
        self._refresh_rows()
        has_format = any(r.verdict == verify_mods.V_FORMAT for r in self._all_rows)
        self._fix_fmt_btn.setVisible(has_format)
        self._fill_table(self._changed_table, self._changed_rows)
        self._fill_table(self._all_table, self._all_rows)
        if self._tabs.currentWidget() is self._preview_tree:
            self._preview_tree.set_data(self._mod_root, self._van_root)

    # --- editing -----------------------------------------------------------------

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 1:
            return
        rows = self._table_rows.get(id(item.tableWidget()))
        if rows is None or not (0 <= item.row() < len(rows)):
            return
        r = rows[item.row()]
        if not r.can_edit or r.mod_elem is None:
            return
        new_value = item.text()
        r.mod_elem.set("value", new_value)
        self._set_dirty(True)
        # Live verdict: unchanged / type-compatible / format drift.
        if new_value == r.vanilla_value:
            r.verdict, r.note = verify_mods.V_SAME, "unchanged - equals vanilla"
        else:
            same_class = verify_mods._value_class(new_value) == verify_mods._value_class(r.vanilla_value)
            r.verdict = verify_mods.V_OK if same_class else verify_mods.V_FORMAT
            r.note = "" if same_class else (
                f"mod writes {verify_mods._value_class(new_value)} format, "
                f"vanilla field is {verify_mods._value_class(r.vanilla_value)}"
            )
        table = item.tableWidget()
        table.blockSignals(True)
        verdict_item = table.item(item.row(), 3)
        verdict_item.setText(r.verdict + (" *" if r.note else ""))
        verdict_item.setForeground(self._VERDICT_COLOR.get(r.verdict, COLOR_DIM))
        if r.verdict == verify_mods.V_FORMAT:
            conformed = merge_mods._conform_value(new_value, r.vanilla_value)
            item.setToolTip(f"{r.note}\nthe merge will write it as: {conformed}")
            item.setForeground(COLOR_MERGE)
        else:
            item.setToolTip(r.note)
            item.setForeground(QBrush())
        table.blockSignals(False)

    def _on_table_menu(self, table: QTableWidget, pos) -> None:
        rows = self._table_rows.get(id(table))
        row = table.rowAt(pos.y())
        if rows is None or not (0 <= row < len(rows)):
            return
        r = rows[row]
        menu = QMenu(self)
        if r.verdict == verify_mods.V_REMOVE:
            action = menu.addAction("Undo removal")
            action.triggered.connect(lambda: self._undo_removal(r))
        elif r.van_elem is not None and not r.van_elem.findall("Property"):
            heal = self._compare._unpaired_same_name_sibling(r.mod_elem) is not None
            action = menu.addAction(
                "Remove duplicate copy (sibling pairs with vanilla)"
                if heal else "Remove this entry"
            )
            action.triggered.connect(lambda: self._remove_entry(r))
        if (
            r.van_elem is not None
            and not r.mod_elem.findall("Property")
        ):
            action = menu.addAction("Insert duplicate as new list entry")
            action.triggered.connect(lambda: self._insert_duplicate(r))
        conformed = _format_drift(r.mod_elem, r.van_elem)
        if conformed is not None:
            action = menu.addAction(f"Fix formatting (write as {conformed})")
            action.triggered.connect(lambda: self._fix_formatting(r))
        self._add_move_actions(menu, r.mod_elem)
        if menu.isEmpty():
            return
        menu.exec(table.viewport().mapToGlobal(pos))

    def _remove_entry(self, r) -> None:
        if self._compare._unpaired_same_name_sibling(r.mod_elem) is not None:
            # Duplicate consume: delete this copy so the sibling takes the
            # vanilla pairing instead of stranding it as an insertion.
            mod_parents = {c: p for p in self._mod_root.iter() for c in p}
            parent = mod_parents.get(r.mod_elem)
            if parent is not None:
                parent.remove(r.mod_elem)
        else:
            r.mod_elem.set("_remove", "")
        self._set_dirty(True)
        self._rebuild_tables()

    def _undo_removal(self, r) -> None:
        if "_remove" in r.mod_elem.attrib:
            del r.mod_elem.attrib["_remove"]
            self._set_dirty(True)
            self._rebuild_tables()

    def _fix_formatting(self, r) -> None:
        """Write one field's value in its vanilla counterpart's format."""
        conformed = _format_drift(r.mod_elem, r.van_elem)
        if conformed is None:
            return
        r.mod_elem.set("value", conformed)
        self._set_dirty(True)
        self._rebuild_tables()

    def _fix_all_formatting(self) -> None:
        """Conform every format-drifted value in this file at once."""
        fixed = 0
        for r in self._all_rows:
            conformed = _format_drift(r.mod_elem, r.van_elem)
            if conformed is not None:
                r.mod_elem.set("value", conformed)
                fixed += 1
        if fixed:
            self._set_dirty(True)
            self._rebuild_tables()

    def _insert_duplicate(self, r) -> None:
        mod_parents = {c: p for p in self._mod_root.iter() for c in p}
        van_parents = {c: p for p in self._van_root.iter() for c in p}
        parent = mod_parents.get(r.mod_elem)
        if parent is None:
            return
        duplicate = copy.deepcopy(r.mod_elem)
        duplicate.attrib.pop("_id", None)
        duplicate.attrib.pop("_remove", None)
        van_parent = van_parents.get(r.van_elem)
        if van_parent is not None:
            van_count = sum(
                1 for c in van_parent if c.get("name") == r.mod_elem.get("name")
            )
            duplicate.set("_index", str(van_count))
        parent.insert(list(parent).index(r.mod_elem) + 1, duplicate)
        self._set_dirty(True)
        self._rebuild_tables()

    def _move_entry(self, elem, delta: int) -> None:
        """Move a field up/down within its parent (order is cosmetic)."""
        mod_parents = {c: p for p in self._mod_root.iter() for c in p}
        parent = mod_parents.get(elem)
        if parent is None:
            return
        sibs = list(parent)
        idx = sibs.index(elem)
        new = idx + delta
        if 0 <= new < len(sibs):
            parent.remove(elem)
            parent.insert(new, elem)
            self._set_dirty(True)
            self._rebuild_tables()

    def _add_move_actions(self, menu: QMenu, elem) -> None:
        mod_parents = {c: p for p in self._mod_root.iter() for c in p}
        parent = mod_parents.get(elem)
        if parent is None:
            return
        sibs = list(parent)
        idx = sibs.index(elem)
        if idx > 0:
            menu.addAction("Move up").triggered.connect(
                lambda: self._move_entry(elem, -1))
        if idx < len(sibs) - 1:
            menu.addAction("Move down").triggered.connect(
                lambda: self._move_entry(elem, 1))

    # --- merged preview ------------------------------------------------------------

    def _on_tab_changed(self, _index: int) -> None:
        if self._tabs.currentWidget() is self._preview_tree:
            self._preview_tree.set_data(self._mod_root, self._van_root)
        elif self._tabs.currentWidget() is self._compare:
            self._compare.rebuild()

    def _on_compare_edited(self) -> None:
        """The compare tree mutated the in-memory mod tree: refresh tables."""
        self._set_dirty(True)
        self._rebuild_tables()

    # --- save / lifecycle ------------------------------------------------------

    def _on_selection_changed(self, table: QTableWidget) -> None:
        rows = self._table_rows.get(id(table), [])
        row = table.currentRow()
        if 0 <= row < len(rows) and rows[row].note:
            self._note_label.setText(f"{rows[row].path}: {rows[row].note}")
        elif self._is_exml:
            self._note_label.setText(
                "Double-click a Mod value to edit it. "
                "Select a row marked with * for its explanation."
            )
        else:
            self._note_label.setText(
                "Shipped MBIN: edits are saved by recompiling with the "
                "managed MBINCompiler."
            )

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        self._save_btn.setEnabled(dirty)

    def _save(self) -> None:
        source = Path(self._mod.path)
        try:
            if self._is_exml:
                text = (
                    '<?xml version="1.0" encoding="utf-8"?>\n'
                    + ET.tostring(self._mod_root, encoding="unicode")
                )
                # The mod convention is a valueless `_remove` flag attribute.
                text = re.sub(r'_remove=""', "_remove", text)
                data = text.encode("utf-8")
            else:
                # Shipped MBIN: recompile the edited tree with the managed
                # compiler; verify the mod's source against the result.
                data = merge_mods.merged_to_mbin(
                    self._mod_root, self._edit_work,
                    hashlib.md5(self._hit.entry.encode()).hexdigest()[:12],
                )
            if source.is_dir():
                (source / self._hit.entry).write_bytes(data)
            else:
                self._rewrite_zip_entry(source, self._hit.entry, data)
        except Exception as exc:  # noqa: BLE001 - surfaced in the dialog
            QMessageBox.critical(self, "Save failed", f"{type(exc).__name__}: {exc}")
            return
        self.saved = True
        self.accept()

    @staticmethod
    def _rewrite_zip_entry(zip_path: Path, entry: str, data: bytes) -> None:
        tmp = zip_path.with_suffix(".zip.editing")
        with ZipFile(zip_path) as zin, ZipFile(tmp, "w") as zout:
            for info in zin.infolist():
                payload = data if info.filename == entry else zin.read(info.filename)
                zout.writestr(info, payload)
        os.replace(tmp, zip_path)

    def reject(self) -> None:
        if self._dirty:
            answer = QMessageBox.question(
                self, "Discard changes?",
                "Discard the unsaved edits to this mod file?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.done(QDialog.DialogCode.Rejected)

    def done(self, result: int) -> None:
        _settings().setValue("editor_geometry", self.saveGeometry().data().hex())
        shutil.rmtree(self._edit_work, ignore_errors=True)
        super().done(result)


def _verdict_tooltip(ver) -> str:
    """Short tooltip text summarizing one file's verification."""
    changed = [i for i in ver.fields if i.verdict != "OK"]
    lines = [f"Fields: {_FIELDS_LABEL[ver.status]}"]
    if ver.message:
        lines.append(ver.message)
    for issue in changed[:8]:
        lines.append(f"[{issue.verdict}] {issue.path}: {issue.mod_value} (vanilla {issue.vanilla_value})")
    if len(changed) > 8:
        lines.append(f"… and {len(changed) - 8} more (right-click → Inspect fields…)")
    return _wrap_tooltip("\n".join(lines))


def _format_drift(mp, vp) -> str | None:
    """The conformed value string when a mod leaf's value class (int/float/
    text) differs from its paired vanilla leaf's, else None."""
    if mp is None or vp is None:
        return None
    if mp.findall("Property") or vp.findall("Property"):
        return None
    mv, vv = mp.get("value"), vp.get("value")
    if mv is None or vv is None:
        return None
    if verify_mods._value_class(mv) == verify_mods._value_class(vv):
        return None
    return merge_mods._conform_value(mv, vv)


# --- main window --------------------------------------------------------------


class MainWindow(QMainWindow):
    """Pick the mods folder, keep the pak index fresh, see where mods land."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle("NMS Pak Inspector")
        self._settings = _settings()
        self._worker: TaskWorker | None = None
        self._active_workers: set[TaskWorker] = set()
        self._index: dict | None = None
        self._index_stale = False
        self._scan: dict | None = None  # {"mods": [...], "collisions": {...}}
        self._verifications: dict | None = None  # (mod, entry) -> FileVerification
        self._verify_counts: dict | None = None
        self._conflicts: list | None = None  # FieldConflict list from conflicts.py
        self._compiler_release: dict | None = None  # remote release pending download
        self._inject_state: dict = {}  # inject_mods state: mod name -> files
        self._injecting_mod: str | None = None  # mod whose inject/revert runs
        self._installed_mods: set[str] = set()  # folder names in GAMEDATA\MODS
        self._installing_mod: str | None = None  # mod whose install/remove runs
        self._user_col_widths: dict[int, int] = {}  # widths the user chose
        self._fitting_columns = False  # guard: programmatic width changes
        # Deferred re-flex + live persistence: touching the header mid-drag
        # makes the drag snap back and lose the chosen width.
        self._col_fit_timer = QTimer(self)
        self._col_fit_timer.setSingleShot(True)
        self._col_fit_timer.setInterval(200)
        self._col_fit_timer.timeout.connect(self._fit_columns)
        self._col_save_timer = QTimer(self)
        self._col_save_timer.setSingleShot(True)
        self._col_save_timer.setInterval(1000)
        self._col_save_timer.timeout.connect(self._save_col_widths)
        self._mod_items: dict[str, QTreeWidgetItem] = {}
        self._resize_drag_edges: Qt.Edge | None = None
        self._resize_press_global: QPoint | None = None
        self._resize_geometry: QRect | None = None
        self._edge_cursor: Qt.CursorShape | None = None

        self._build_ui()
        self._build_menu()

        if sys.platform == "win32":
            _apply_rounded_corners(self)

        rescan = QShortcut(QKeySequence(Qt.Key.Key_F5), self)
        rescan.activated.connect(self._scan_mods)

        self.resize(880, 620)
        self.setMinimumSize(720, 460)
        geometry = _restored_geometry(self._settings.value("window_geometry"))
        if geometry is not None:
            self.restoreGeometry(geometry)
        if self._settings.value("window_maximized") in (True, "true"):
            self.showMaximized()
        for column, default in _TREE_COLUMN_WIDTHS:
            try:
                width = int(self._settings.value(f"col_width_{column}", default))
            except (TypeError, ValueError):
                width = default
            if width > 0:
                self._tree.setColumnWidth(column, width)
            self._user_col_widths[column] = self._tree.columnWidth(column)
        # The Actions column must stay wide enough for its painted buttons.
        actions_floor = _TREE_COLUMN_WIDTHS[-1][1]
        if self._tree.columnWidth(5) < actions_floor:
            self._tree.setColumnWidth(5, actions_floor)
            self._user_col_widths[5] = actions_floor
        self._mods_edit.setText(
            self._settings.value("mods_folder", str(_DEFAULT_MODS_DIR))
        )
        # Game folder: migrate from older settings / the pak index, then
        # resolve PCBANKS from it (re-validated on every startup).
        game_folder = self._migrate_game_folder()
        self._game_edit.setText(game_folder or "")
        self._pcbanks: str | None = None
        self._resolve_pcbanks()
        # Tree expansion: None = first run (expand all); otherwise the set of
        # expanded mod names saved on close.
        raw_expanded = self._settings.value("expanded_mods")
        self._expanded_mods: set[str] | None = (
            None if raw_expanded is None else set(json.loads(raw_expanded or "[]"))
        )

        self.installEventFilter(self)
        self.setMouseTracking(True)
        for widget in self.findChildren(QWidget):
            widget.installEventFilter(self)
            widget.setMouseTracking(True)

        # Drag & drop: archives/folders dropped anywhere on the window are
        # copied into the mods folder, then a rescan picks them up.
        self.setAcceptDrops(True)
        self._drop_overlay = QWidget(self)
        self._drop_overlay.setStyleSheet(
            f"background-color: rgba({COLOR_ACCENT.red()}, {COLOR_ACCENT.green()}, "
            f"{COLOR_ACCENT.blue()}, 36);"
            f"border: 2px dashed {COLOR_ACCENT.name()};"
        )
        self._drop_overlay.hide()
        self._rescan_pending = False

        QTimer.singleShot(0, self._startup)

    # --- UI construction ------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)
        margins = layout.contentsMargins()
        layout.setContentsMargins(margins.left(), 0, margins.right(), margins.bottom())

        self._title_bar = _TitleBar(self)
        layout.addWidget(self._title_bar)

        mods_row = QHBoxLayout()
        mods_row.setContentsMargins(8, 6, 8, 0)
        mods_label = QLabel("Mods folder", central)
        mods_label.setForegroundRole(QPalette.ColorRole.WindowText)
        mods_label.setFixedWidth(_PATH_LABEL_WIDTH)
        mods_row.addWidget(mods_label)
        self._mods_edit = QLineEdit(central)
        self._mods_edit.setReadOnly(True)
        self._mods_edit.setPlaceholderText("Select your mods folder (loose-mod zips)…")
        self._mods_edit.textChanged.connect(self._refresh_actions)
        mods_row.addWidget(self._mods_edit, 1)
        self._browse_btn = QPushButton("Browse…", central)
        mods_row.addWidget(self._browse_btn)
        layout.addLayout(mods_row)

        game_row = QHBoxLayout()
        game_row.setContentsMargins(8, 4, 8, 0)
        game_label = QLabel("Game folder", central)
        game_label.setForegroundRole(QPalette.ColorRole.WindowText)
        game_label.setFixedWidth(_PATH_LABEL_WIDTH)
        game_row.addWidget(game_label)
        self._game_edit = QLineEdit(central)
        self._game_edit.setReadOnly(True)
        self._game_edit.setPlaceholderText("Select the game install folder (e.g. G:\\XboxGames\\No Man's Sky)…")
        game_row.addWidget(self._game_edit, 1)
        self._game_browse_btn = QPushButton("Browse…", central)
        game_row.addWidget(self._game_browse_btn)
        game_row.addSpacing(12)
        self._inject_all_btn = QPushButton("Inject all", central)
        self._inject_all_btn.setEnabled(False)
        self._inject_all_btn.clicked.connect(self._on_inject_all)
        game_row.addWidget(self._inject_all_btn)
        self._restore_btn = QPushButton("Restore vanilla", central)
        self._restore_btn.setEnabled(False)
        self._restore_btn.clicked.connect(self._on_restore_vanilla)
        game_row.addWidget(self._restore_btn)
        layout.addLayout(game_row)

        self._index_status = QLabel("Index: loading…", central)
        self._mods_status = QLabel("", central)
        self._version_status = QLabel("", central)
        self._version_status.setTextFormat(Qt.TextFormat.RichText)

        self._tree = QTreeWidget(central)
        self._tree.setColumnCount(len(_TREE_COLUMNS))
        self._tree.setHeaderLabels(_TREE_COLUMNS)
        for column, width in _TREE_COLUMN_WIDTHS:
            self._tree.setColumnWidth(column, width)
        self._tree.header().setStretchLastSection(False)
        self._tree.header().setMinimumSectionSize(48)
        # Every width is the user's choice (persisted live); Target pak(s)
        # only flexes when forced, keeping the Inject column pinned visible.
        self._tree.header().sectionResized.connect(self._on_col_resized)
        self._tree.setAlternatingRowColors(True)
        self._tree.setWordWrap(False)
        self._tree.setIndentation(14)
        self._tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self._tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        self._tree.itemSelectionChanged.connect(self._refresh_actions)
        accent = COLOR_ACCENT
        self._tree.setStyleSheet(
            "QTreeWidget::item:hover {"
            f" background-color: rgba({accent.red()}, {accent.green()}, {accent.blue()}, 40%);"
            " }"
        )
        self._tree.setItemDelegate(
            _RowActionDelegate(self._tree, self._on_row_action)
        )
        layout.addWidget(self._tree, 3)
        layout.addWidget(self._tree, 3)

        self._log_view = QPlainTextEdit(central)
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(5000)
        self._log_view.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        layout.addWidget(self._log_view, 1)
        self._log_view.setVisible(
            self._settings.value("show_console", True) in (True, "true")
        )

        self.setCentralWidget(central)

        # Bottom status bar: index, versions (left) · mods summary (right).
        status_bar = self.statusBar()
        status_bar.setSizeGripEnabled(False)  # custom frameless resize edges
        status_bar.addWidget(self._index_status)
        status_bar.addWidget(self._version_status)
        status_bar.addPermanentWidget(self._mods_status)

        self._browse_btn.clicked.connect(self._on_browse)
        self._game_browse_btn.clicked.connect(self._on_browse_game)

        # Compiler folder is a cheap read; version + update check run in the
        # background at the end of the startup chain.
        self._set_version_label(game_span=None, compiler_tag=self._local_compiler_tag())

    def _build_menu(self) -> None:
        menu = self._title_bar.app_menu
        rescan = menu.addAction("Rescan mods")
        rescan.setShortcut(QKeySequence(Qt.Key.Key_F5))
        rescan.triggered.connect(self._scan_mods)
        reindex = menu.addAction("Rebuild pak index")
        reindex.triggered.connect(self._on_rebuild_index)
        inject_all = menu.addAction("Inject all mods")
        inject_all.triggered.connect(self._on_inject_all)
        menu.addSeparator()
        expand_all = menu.addAction("Expand all")
        expand_all.triggered.connect(self._tree.expandAll)
        collapse_all = menu.addAction("Collapse all")
        collapse_all.triggered.connect(self._tree.collapseAll)
        open_mods = menu.addAction("Open mods folder")
        open_mods.triggered.connect(self._on_open_mods_folder)
        open_game = menu.addAction("Open game folder")
        open_game.triggered.connect(self._on_open_game_folder)
        menu.addSeparator()
        console = menu.addAction("Show console")
        console.setCheckable(True)
        # isVisible() is always False before the window is first shown, so
        # the checkbox must reflect the persisted setting, not the widget.
        console.setChecked(
            self._settings.value("show_console", True) in (True, "true")
        )
        console.toggled.connect(self._on_console_toggled)
        menu.addSeparator()
        self._compiler_action = menu.addAction("Check for compiler updates")
        self._compiler_action.triggered.connect(self._on_compiler_action)
        nexus_login = menu.addAction("Nexus API key…")
        nexus_login.triggered.connect(self._on_nexus_key_menu)
        menu.addSeparator()
        about = menu.addAction("About")
        about.triggered.connect(self._show_about)

    # --- startup / workers ----------------------------------------------------

    def _startup(self) -> None:
        if self._pcbanks is None:
            self.append_log("No game folder set - pick it to enable the index")
            self._update_index_status()
            self._check_compiler_updates()
            return
        if _INDEX_PATH.exists():
            self._index = index_paks.load_index(str(_INDEX_PATH))
            self._load_inject_state()
            if self._index.get("pcbanks") != self._pcbanks:
                self.append_log(
                    "Pak index was built for a different PCBANKS folder - rebuilding…"
                )
                self._on_rebuild_index()
                return
            self._index_stale = index_paks.is_stale(self._index)
            if self._index_stale:
                self.append_log(
                    "Pak index is stale (game paks changed) - rebuilding…"
                )
                self._on_rebuild_index()
                return
            self._update_index_status()
            self.append_log(
                f"Loaded pak index: {self._index['total_paks']} paks, "
                f"{self._index['total_files']} files"
            )
            self._scan_mods()
        else:
            self.append_log("No pak index found - building one…")
            self._on_rebuild_index()

    def _load_inject_state(self) -> None:
        """Load injection state; void it on external pak changes (game update)."""
        state = inject_mods.load_state()
        if state and self._index is not None:
            externally_changed = (
                self._index.get("pcbanks") != self._pcbanks
                or index_paks.is_stale(self._index)
            )
            if externally_changed:
                inject_mods.clear_state()
                state = {}
                self.append_log(
                    "Game paks changed outside the app - cleared injection state "
                    "and pre-injection backups (the game likely updated)",
                    color=COLOR_MERGE,
                )
        self._inject_state = state

    def _start_task(self, fn, *args, on_done) -> None:
        if self._worker is not None:
            self.append_log("A task is already running - ignoring request")
            return
        worker = TaskWorker(fn, *args)
        self._worker = worker
        self._active_workers.add(worker)
        self._title_bar.set_busy(True)
        self._refresh_actions()
        worker.log.connect(self.append_log)
        worker.error.connect(self._on_task_error)
        worker.done.connect(on_done)
        worker.finished.connect(lambda w=worker: self._on_task_finished(w))
        worker.start()

    def _on_task_finished(self, worker: TaskWorker) -> None:
        self._active_workers.discard(worker)
        if self._worker is worker:
            self._worker = None
        self._title_bar.set_busy(False)
        self._refresh_actions()
        self._maybe_rescan_for_drop()

    def _on_task_error(self, message: str) -> None:
        self.append_log(f"ERROR: {message}", color=COLOR_ERROR)
        if self._injecting_mod is not None or self._installing_mod is not None:
            # A failed action must not leave its button stuck mid-state.
            self._injecting_mod = None
            self._installing_mod = None
        if self._scan is not None:
            self._inject_state = inject_mods.load_state()
            self._fill_tree()

    def _migrate_game_folder(self) -> str | None:
        """Best-effort game folder: saved setting, then old PCBANKS setting,
        then the pak index's recorded PCBANKS, then the Store package."""
        game = self._settings.value("game_folder") or None
        if game:
            return game
        for old_pcbanks in (
            self._settings.value("pcbanks_folder") or None,
            self._index_pcbanks(),
        ):
            if not old_pcbanks:
                continue
            pcb = Path(old_pcbanks)
            # <game>\Content\GAMEDATA\PCBANKS or <game>\GAMEDATA\PCBANKS
            for candidate in (pcb.parents[2], pcb.parents[1]):
                if repo.find_pcbanks(candidate) == old_pcbanks:
                    return str(candidate)
        return repo.appx_install_location()

    def _index_pcbanks(self) -> str | None:
        """The PCBANKS folder recorded in the saved pak index, if any."""
        if not _INDEX_PATH.exists():
            return None
        try:
            index = json.loads(_INDEX_PATH.read_text(encoding="utf-8"))
            return index.get("pcbanks")
        except Exception:  # noqa: BLE001 - corrupt index, ignore
            return None

    def _resolve_pcbanks(self) -> None:
        """Resolve PCBANKS from the game folder and remember both."""
        game = self._game_edit.text().strip()
        self._pcbanks = repo.find_pcbanks(game) if game else None
        self._game_edit.setToolTip(
            f"PCBANKS: {self._pcbanks}" if self._pcbanks else "PCBANKS not found"
        )
        if game:
            self._settings.setValue("game_folder", game)
            self._settings.setValue("pcbanks_folder", self._pcbanks or "")

    def _current_pcbanks(self) -> str:
        return self._pcbanks or ""

    def _on_rebuild_index(self) -> None:
        if not self._pcbanks:
            self.append_log("Cannot rebuild the index: no PCBANKS folder")
            self._update_index_status()
            return
        self._index_status.setText("Index: rebuilding…")
        self._start_task(
            index_worker, self._current_pcbanks(), on_done=self._on_index_built
        )

    def _on_index_built(self, index: dict) -> None:
        self._index = index
        self._index_stale = False
        with open(_INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump(index, f, separators=(",", ":"))
        self._update_index_status()
        self.append_log(f"Pak index saved to {_INDEX_PATH.name}")
        # Chain the scan once the index worker has fully finished.
        QTimer.singleShot(0, self._scan_mods)

    def _scan_mods(self) -> None:
        if self._index is None:
            self.append_log("No pak index yet - cannot resolve mods")
            return
        self._start_task(
            scan_mods_worker,
            self._mods_edit.text().strip(),
            self._index,
            self._settings.value("zip_keep_both"),
            on_done=self._on_mods_scanned,
        )

    def _on_mods_scanned(self, result: dict) -> None:
        self._scan = result
        self._verifications = None
        self._verify_counts = None
        self._fill_tree()
        pending = [
            (m, m.superseded_zips)
            for m in result["mods"]
            if m.superseded_zips
        ]
        if pending and self._handle_superseded(pending):
            # Defer: this handler runs before the worker's `finished` clears
            # _worker, so an immediate rescan would be refused.
            QTimer.singleShot(0, self._scan_mods)
            return  # fresh rescan supersedes verification of the old layout
        # Chain verification once the scan worker has fully finished.
        QTimer.singleShot(0, self._start_verify)

    def _handle_superseded(self, pending: list) -> bool:
        """Prompt for newer-archive mods; True when an old archive was replaced."""
        replaced = False
        for mod, old_zips in pending:
            old_names = ", ".join(os.path.basename(z) for z in old_zips)
            self.append_log(
                f"Update found for {mod.name}: superseding {old_names}",
                color=COLOR_MERGE,
            )
            box = QMessageBox(self)
            box.setWindowTitle("Mod update")
            box.setText(
                f"Two archives provide \"{mod.name}\".\n\n"
                f"New: {os.path.basename(mod.path)}\n"
                f"Old: {old_names}\n\n"
                "Replace the old archive with the new one?"
            )
            replace_btn = box.addButton(
                "Replace old archive", QMessageBox.ButtonRole.AcceptRole
            )
            keep_btn = box.addButton(
                "Keep both", QMessageBox.ButtonRole.RejectRole
            )
            box.addButton("Decide later", QMessageBox.ButtonRole.ActionRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is replace_btn:
                mods_dir = self._mods_edit.text().strip()
                for old in old_zips:
                    try:
                        dest = stash_replaced_zip(old, mods_dir)
                        self.append_log(f"Moved old archive to {dest}")
                    except OSError as exc:
                        self.append_log(
                            f"Could not move {old}: {exc}", color=COLOR_ERROR
                        )
                replaced = True
            elif clicked is keep_btn:
                pair = sorted([os.path.basename(mod.path), *map(os.path.basename, old_zips)])
                existing = self._settings.value("zip_keep_both")
                pairs = json.loads(existing) if existing else []
                if pair not in pairs:
                    pairs.append(pair)
                    self._settings.setValue("zip_keep_both", json.dumps(pairs))
        return replaced

    def _start_verify(self) -> None:
        if self._scan is None or self._index is None:
            return
        self._start_task(
            verify_worker, self._scan, self._index, on_done=self._on_verified
        )

    def _on_verified(self, result: dict) -> None:
        self._verifications = result["verifications"]
        self._verify_counts = result["counts"]
        self._conflicts = compute_field_conflicts(self._scan, self._verifications)
        n_conflicts = sum(1 for c in self._conflicts if c.kind == K_CONFLICT)
        if n_conflicts:
            self.append_log(
                f"{n_conflicts} value conflict(s) detected between mods", color=COLOR_ERROR
            )
        self._fill_tree()
        # Chain the version/update check once verification has fully finished.
        QTimer.singleShot(0, self._check_compiler_updates)

    # --- targeted re-verify (post-edit save) ------------------------------------

    def _reverify_mod(self, mod_name: str) -> None:
        """Re-verify just one mod's files and refresh its rows in place."""
        if self._scan is None or self._index is None:
            return
        self._start_task(
            verify_worker, self._scan, self._index, mod_name,
            on_done=lambda result, name=mod_name: self._on_mod_reverified(result, name),
        )

    def _on_mod_reverified(self, result: dict, mod_name: str) -> None:
        if not result["verifications"]:
            # Nothing resolved (mod layout changed on disk): full rescan.
            self._scan_mods()
            return
        if self._verifications is None:
            return  # a full verification is still running and will cover it
        self._verifications.update(result["verifications"])
        self._verify_counts = self._recompute_verify_counts()
        self._conflicts = compute_field_conflicts(self._scan, self._verifications)
        self._update_mod_rows(mod_name)
        self._update_mods_status()
        self._refresh_actions()

    def _recompute_verify_counts(self) -> dict:
        counts = {s: 0 for s in _FIELDS_WORST_FIRST}
        for ver in self._verifications.values():
            counts[ver.status] += 1
        return counts

    def _update_mod_rows(self, mod_name: str) -> None:
        """In-place refresh of one mod's verification-derived rows after a targeted re-verify."""
        mod = next((m for m in self._scan["mods"] if m.name == mod_name), None)
        item = self._mod_items.get(mod_name)
        if mod is None or item is None or self._verifications is None:
            return
        worst_rank = {s: i for i, s in enumerate(_FIELDS_WORST_FIRST)}
        ver_statuses = [
            self._verifications[(mod.name, h.entry)].status
            for h in mod.game_files
            if (mod.name, h.entry) in self._verifications
        ]
        if ver_statuses:
            worst = min(ver_statuses, key=lambda s: worst_rank[s])
            item.setText(2, _FIELDS_LABEL[worst])
            item.setForeground(2, _FIELDS_COLOR[worst])
        conflict_map = {
            c.stored: c for c in (self._conflicts or []) if c.kind == K_CONFLICT
        }
        ambiguous_map = {
            c.stored: c for c in (self._conflicts or []) if c.kind == K_AMBIGUOUS
        }
        collisions: dict[str, list[str]] = self._scan["collisions"]
        for i in range(item.childCount()):
            child = item.child(i)
            data = child.data(0, Qt.ItemDataRole.UserRole)
            if not data or data[0] != "file":
                continue
            hit = next(
                (h for h in mod.game_files if h.entry == data[2]), None
            )
            if hit is None:
                continue
            # Resolution status can shift if conflicts changed with the edit.
            if hit.stored is not None:
                if hit.stored in conflict_map:
                    child_status = ST_CONFLICT
                elif hit.stored in ambiguous_map or hit.stored in collisions:
                    child_status = ST_MERGE
                else:
                    child_status = ST_CLEAN
                child.setText(1, _STATUS_LABEL[child_status])
                child.setForeground(1, _status_color(child_status))
            ver = self._verifications.get((mod.name, hit.entry))
            if ver is not None:
                child.setText(2, _FIELDS_LABEL[ver.status])
                child.setForeground(2, _FIELDS_COLOR[ver.status])
                scantip = child.data(0, _SCANTIP_ROLE) or ""
                tip = _verdict_tooltip(ver)
                child.setToolTip(
                    0, _wrap_tooltip(f"{scantip}\n\n{tip}" if scantip else tip)
                )

    # --- versions / compiler updates -------------------------------------------

    def _local_compiler_tag(self) -> str | None:
        """Newest compiler tag in the managed folder, if any."""
        local = mbincompiler.local_compilers()
        return local[0]["tag"] if local else None

    def _set_version_label(
        self, *, game_span: str | None, compiler_tag: str | None,
        extra_spans: list[str] | None = None,
    ) -> None:
        """Render the versions row: dim info, amber for attention items."""
        dim = COLOR_DIM.name()
        if game_span is None:
            game_span = f'<span style="color:{dim};">Game: detecting…</span>'
        if compiler_tag:
            compiler_span = f'<span style="color:{dim};">Compiler {compiler_tag}</span>'
        else:
            compiler_span = (
                f'<span style="color:{COLOR_MERGE.name()};">no compiler downloaded</span>'
            )
        spans = [game_span, compiler_span] + (extra_spans or [])
        self._version_status.setText(" · ".join(spans))

    def _check_compiler_updates(self, force: bool = False) -> None:
        """Background check: game version, local compiler, newest release."""
        self._start_task(
            compiler_status_worker, force, on_done=self._on_compiler_status
        )

    def _on_compiler_status(self, result: dict) -> None:
        game = result["game"]
        local = result["local"]
        if game:
            game_span = (
                f'<span style="color:{COLOR_DIM.name()};">'
                f'Game {".".join(map(str, game))}</span>'
            )
        else:
            game_span = (
                f'<span style="color:{COLOR_MERGE.name()};">'
                "Game version not detected</span>"
            )
        compiler_tag = local["tag"] if local else None
        extra: list[str] = []
        remote = result.get("remote")
        release = None
        if remote and (
            local is None
            or mbincompiler.parse_tag(remote["tag"]) > mbincompiler.parse_tag(local["tag"])
        ):
            release = remote
            extra.append(
                f'<span style="color:{COLOR_MERGE.name()};">'
                f'update available: {remote["tag"]}</span>'
            )
        self._compiler_release = release
        if result.get("remote_error"):
            extra.append(
                f'<span style="color:{COLOR_DIM.name()};">update check failed</span>'
            )
        self._set_version_label(
            game_span=game_span, compiler_tag=compiler_tag, extra_spans=extra
        )
        # The check should report its outcome, not just announce itself.
        game_txt = (
            f"game {'.'.join(map(str, game))}" if game else "game version not detected"
        )
        if result.get("remote_error"):
            outcome = (
                f"release lookup failed ({result['remote_error']})"
                " - using local/cached info"
            )
            color = COLOR_MERGE
        elif local is None:
            if remote is not None:
                outcome = (
                    f"no local compiler - newest release {remote['tag']}, "
                    "download from the menu"
                )
            else:
                outcome = "no local compiler and no release info"
            color = COLOR_MERGE
        elif release is not None:
            outcome = f"update available: {release['tag']} (local {compiler_tag})"
            color = COLOR_MERGE
        else:
            outcome = f"{compiler_tag} is up to date" + (
                " (release info from cache)" if result.get("cached_check") else ""
            )
            color = COLOR_DIM
        self.append_log(f"Compiler check: {game_txt} - {outcome}", color=color)
        tooltip = (
            "Game version: the installed NMS Store package version.\n"
            "Compiler: newest MBINCompiler in the managed compilers folder."
        )
        if result["locals"]:
            tooltip += "\nDownloaded: " + ", ".join(result["locals"])
        if result.get("remote_error"):
            tooltip += f"\nUpdate check failed: {result['remote_error']}"
        elif result.get("cached_check"):
            tooltip += (
                "\nRelease info from cache (max 6 h old) - "
                "'Check again' forces a fresh GitHub fetch"
            )
        self._version_status.setToolTip(tooltip)
        if release:
            self._compiler_action.setText(f"Download MBINCompiler {release['tag']}")
            self._compiler_action.setToolTip(
                f"Downloads MBINCompiler {release['tag']} into the managed "
                "compilers folder (newest release compatible with the game)"
            )
        else:
            self._compiler_action.setText("Check for compiler updates")
            self._compiler_action.setToolTip(
                "Check GitHub for a newer MBINCompiler release"
            )

    def _on_compiler_action(self) -> None:
        if self._compiler_release is not None:
            self._start_task(
                compiler_update_worker,
                self._compiler_release,
                on_done=self._on_compiler_updated,
            )
        else:
            self._check_compiler_updates(force=True)  # manual: fresh fetch

    def _on_compiler_updated(self, entry: dict) -> None:
        self.append_log(
            f"Downloaded MBINCompiler {entry['tag']} - it will be used from now on"
        )
        # Refresh the versions row from the now-updated managed folder.
        QTimer.singleShot(0, self._check_compiler_updates)

    # --- tree filling ---------------------------------------------------------

    def _fill_tree(self) -> None:
        if self._scan is None:
            return
        # Snapshot live expansion so refills (post-verification) keep it.
        if self._tree.topLevelItemCount() > 0:
            self._expanded_mods = {
                self._tree.topLevelItem(i).text(0)
                for i in range(self._tree.topLevelItemCount())
                if self._tree.topLevelItem(i).isExpanded()
            }
        self._tree.blockSignals(True)
        self._tree.clear()
        self._mod_items.clear()
        self._installed_mods = (
            install_mods.installed_names(self._pcbanks) if self._pcbanks else set()
        )
        collisions: dict[str, list[str]] = self._scan["collisions"]
        mods: list[ModScan] = self._scan["mods"]
        conflict_map = {
            c.stored: c for c in (self._conflicts or []) if c.kind == K_CONFLICT
        }
        ambiguous_map = {
            c.stored: c for c in (self._conflicts or []) if c.kind == K_AMBIGUOUS
        }

        for mod in mods:
            game_files = mod.game_files
            mod_conflicts = {h.stored for h in game_files if h.stored in conflict_map}
            mod_shared = {h.stored for h in game_files if h.stored in collisions}
            if mod_conflicts:
                status = ST_CONFLICT
            elif mod_shared or (
                any(h.stored in ambiguous_map for h in game_files)
            ):
                status = ST_MERGE
            elif mod.has_unmatched:
                status = ST_UNMATCHED
            else:
                status = ST_CLEAN

            # Fields verdict: worst verification status among this mod's files.
            worst_rank = {s: i for i, s in enumerate(_FIELDS_WORST_FIRST)}
            ver_statuses = [
                self._verifications[(mod.name, h.entry)].status
                for h in game_files
                if self._verifications and (mod.name, h.entry) in self._verifications
            ]
            fields_text = "…"
            fields_color = COLOR_DIM
            if ver_statuses:
                worst = min(ver_statuses, key=lambda s: worst_rank[s])
                fields_text = _FIELDS_LABEL[worst]
                fields_color = _FIELDS_COLOR[worst]

            item = QTreeWidgetItem(self._tree)
            item.setText(0, mod.name)
            item.setText(1, _STATUS_LABEL[status])
            item.setForeground(1, _status_color(status))
            item.setText(2, fields_text)
            item.setForeground(2, fields_color)
            item.setText(3, ", ".join(sorted(mod.paks)))
            item.setForeground(3, COLOR_DIM)
            item.setText(4, str(len(game_files)))
            item.setData(0, Qt.ItemDataRole.UserRole, ("mod", mod.name))
            item.setToolTip(
                0,
                _wrap_tooltip(
                    f"{len(game_files)} game file(s) in {len(mod.paks)} pak(s).\n"
                    + {
                        ST_CLEAN: "All files resolve to exactly one pak location.",
                        ST_MERGE: "Shares pak path(s) with other mods - merge before injection: "
                        + ", ".join(sorted(mod_shared)),
                        ST_CONFLICT: "True field conflict(s) with other mods: "
                        + ", ".join(
                            f"{c.path} ({c.stored})" for c in (self._conflicts or [])
                            if c.kind == K_CONFLICT and c.stored in mod_conflicts
                        ),
                        ST_UNMATCHED: "Some file(s) matched no pak entry - they would be added as new files.",
                    }[status]
                ),
            )
            self._mod_items[mod.name] = item

            for hit in sorted(game_files, key=lambda h: h.entry):
                child = QTreeWidgetItem(item)
                child.setText(0, hit.rel)
                child.setForeground(0, COLOR_DIM)
                ver = (
                    self._verifications.get((mod.name, hit.entry))
                    if self._verifications
                    else None
                )
                if hit.stored is not None:
                    if hit.stored in conflict_map:
                        child_status = ST_CONFLICT
                    elif hit.stored in ambiguous_map or hit.stored in collisions:
                        child_status = ST_MERGE
                    else:
                        child_status = ST_CLEAN
                    child.setText(1, _STATUS_LABEL[child_status])
                    child.setForeground(1, _status_color(child_status))
                    child.setText(3, hit.stored)
                    child.setForeground(3, COLOR_DIM)
                    tip = f"Matched as: {hit.match}\nStored in pak as: {hit.stored}"
                    if hit.stored in collisions:
                        tip += (
                            "\nCollision with: "
                            + ", ".join(
                                n for n in collisions[hit.stored] if n != mod.name
                            )
                        )
                    if hit.stored in conflict_map:
                        c = conflict_map[hit.stored]
                        tip += "\nCONFLICT on " + c.path + ": " + "; ".join(
                            f"{m} = {v}" for m, v in sorted(c.edits.items())
                        )
                else:
                    child.setText(1, _STATUS_LABEL[ST_UNMATCHED])
                    child.setForeground(1, _status_color(ST_UNMATCHED))
                    tip = "No pak entry matched any candidate path form."
                scantip = tip
                if ver is not None:
                    child.setText(2, _FIELDS_LABEL[ver.status])
                    child.setForeground(2, _FIELDS_COLOR[ver.status])
                    vtip = _verdict_tooltip(ver)
                    tip = (tip + "\n\n" + vtip) if tip else vtip
                child.setData(0, _SCANTIP_ROLE, scantip)
                child.setToolTip(0, _wrap_tooltip(tip))
                child.setData(0, Qt.ItemDataRole.UserRole, ("file", mod.name, hit.entry))

        if self._expanded_mods is None:
            self._tree.expandAll()
        else:
            for i in range(self._tree.topLevelItemCount()):
                item = self._tree.topLevelItem(i)
                item.setExpanded(item.text(0) in self._expanded_mods)
        self._tree.blockSignals(False)
        self._update_mods_status()
        self._fit_columns()
        self._refresh_actions()

    # --- status labels --------------------------------------------------------

    def _update_index_status(self) -> None:
        if self._pcbanks is None:
            self._index_status.setText("Index: no game folder")
            self._index_status.setStyleSheet(f"color: {COLOR_ERROR.name()};")
            return
        if self._index is None:
            self._index_status.setText("Index: missing")
            self._index_status.setStyleSheet(f"color: {COLOR_ERROR.name()};")
            return
        text = (
            f"Index: {self._index['total_paks']} paks · "
            f"{self._index['total_files']:,} files · "
            + ("stale" if self._index_stale else "fresh")
        )
        self._index_status.setText(text)
        color = COLOR_MERGE if self._index_stale else COLOR_CLEAN
        self._index_status.setStyleSheet(f"color: {color.name()};")

    def _update_mods_status(self) -> None:
        if not self._scan:
            return
        mods = self._scan["mods"]
        collisions = self._scan["collisions"]
        game_files = sum(len(m.game_files) for m in mods)
        parts = [f"{len(mods)} mods", f"{game_files} game files"]
        worst = 0  # 0 green, 1 amber, 2 red
        if collisions:
            parts.append(f"{len(collisions)} collision(s)")
            worst = max(worst, 1)
        conflicts = self._conflicts or []
        n_conflicts = sum(1 for c in conflicts if c.kind == K_CONFLICT)
        n_ambiguous = sum(1 for c in conflicts if c.kind == K_AMBIGUOUS)
        if n_conflicts:
            parts.append(f"{n_conflicts} value conflict(s)")
            worst = 2
        elif n_ambiguous:
            parts.append(f"{n_ambiguous} possible conflict(s)")
            worst = max(worst, 1)
        if self._verify_counts:
            total = sum(self._verify_counts.values())
            clean = self._verify_counts[FV_VERIFIED]
            parts.append(f"{clean}/{total} verified")
            issues = []
            if self._verify_counts[FV_OUTDATED]:
                issues.append(f"{self._verify_counts[FV_OUTDATED]} outdated")
            if self._verify_counts[FV_FORMAT]:
                issues.append(f"{self._verify_counts[FV_FORMAT]} format")
            if self._verify_counts[FV_NO_EFFECT]:
                issues.append(f"{self._verify_counts[FV_NO_EFFECT]} no effect")
            if self._verify_counts[FV_ERROR]:
                issues.append(f"{self._verify_counts[FV_ERROR]} error(s)")
            if issues:
                parts.append(" · ".join(issues))
                if self._verify_counts[FV_OUTDATED] or self._verify_counts[FV_ERROR]:
                    worst = 2
                else:
                    worst = max(worst, 1)
        self._mods_status.setText(" · ".join(parts))
        color = (COLOR_CLEAN, COLOR_MERGE, COLOR_ERROR)[worst]
        self._mods_status.setStyleSheet(f"color: {color.name()};")

    # --- actions / context menu ----------------------------------------------

    def _on_browse(self) -> None:
        start = self._mods_edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select mods folder", start)
        if not chosen:
            return
        self._mods_edit.setText(chosen)
        self._settings.setValue("mods_folder", chosen)
        self.append_log(f"Mods folder: {chosen}")
        self._scan_mods()

    def _on_browse_game(self) -> None:
        start = self._game_edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self, "Select the game install folder", start
        )
        if not chosen or chosen == self._game_edit.text():
            return
        pcbanks = repo.find_pcbanks(chosen)
        if not pcbanks:
            QMessageBox.warning(
                self,
                "NMS Pak Inspector",
                "No PCBANKS folder with .pak files found under the selected "
                "folder.\nExpected the game's install directory.",
            )
            return
        self._game_edit.setText(chosen)
        self._resolve_pcbanks()
        self.append_log(f"Game folder: {chosen} (PCBANKS: {self._pcbanks})")
        if self._index is not None and self._index.get("pcbanks") == self._pcbanks:
            self._scan_mods()
            return  # index already matches - nothing to rebuild
        self.append_log("Rebuilding pak index for the new PCBANKS folder…")
        self._on_rebuild_index()

    def _on_open_mods_folder(self) -> None:
        self._open_folder(self._mods_edit.text().strip(), "Mods folder")

    def _on_open_game_folder(self) -> None:
        self._open_folder(self._game_edit.text().strip(), "Game folder")

    def _open_folder(self, folder: str, label: str) -> None:
        if Path(folder).is_dir():
            _open_in_explorer(folder)
        else:
            self.append_log(f"{label} does not exist: {folder}", color=COLOR_MERGE)

    def _on_console_toggled(self, shown: bool) -> None:
        self._log_view.setVisible(shown)
        self._settings.setValue("show_console", shown)

    # --- nexus ------------------------------------------------------------------

    def _nexus_key(self) -> str:
        return self._settings.value("nexus_api_key") or ""

    def _on_nexus_key_menu(self) -> None:
        self._show_nexus_key_dialog()

    def _show_nexus_key_dialog(self) -> bool:
        """The API-key dialog; True when a key is set afterwards."""
        dialog = _NexusKeyDialog(self._nexus_key(), self)
        dialog.exec()
        key = dialog.key()
        self._settings.setValue("nexus_api_key", key)
        if key and key != self._nexus_key():
            user = nexus.validate(key)
            self.append_log(
                f"Nexus API key set (account: {user})" if user
                else "Nexus API key set (account check failed - offline?)"
            )
        elif not key:
            self.append_log("Nexus API key cleared")
        return bool(key)

    def _on_nexus_info(self, mod_name: str) -> None:
        mod = next((m for m in self._scan["mods"] if m.name == mod_name), None)
        if mod is None:
            return
        # No key yet (or the user just cleared it): offer the one-time setup.
        if not self._nexus_key() and not self._show_nexus_key_dialog():
            return

        def done(result: dict) -> None:
            _NexusInfoDialog(mod_name, result, self).exec()

        self._start_task(nexus_info_worker, self._nexus_key(), mod.path, on_done=done)

    def _toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _show_about(self) -> None:
        _AboutDialog(self).exec()

    def _selected_item(self) -> QTreeWidgetItem | None:
        items = self._tree.selectedItems()
        return items[0] if items else None

    def _on_tree_context_menu(self, pos) -> None:
        item = self._tree.itemAt(pos)
        menu = QMenu(self)
        if item is not None:
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data and data[0] == "file":
                hit = self._find_hit(data[1], data[2])
                if hit and hit.stored and hit.stored in self._scan["collisions"]:
                    stored = hit.stored
                    action = menu.addAction("Inspect collision…")
                    action.triggered.connect(
                        lambda: self._show_collision(stored)
                    )
                if self._verifications and (data[1], data[2]) in self._verifications:
                    key = (data[1], data[2])
                    action = menu.addAction("Review / edit fields…")
                    action.triggered.connect(
                        lambda: self._show_fields(self._verifications[key])
                    )
            if data and data[0] == "mod":
                mod_path = next(
                    (m.path for m in self._scan["mods"] if m.name == data[1]), None
                )
                if mod_path:
                    info = menu.addAction("Info…")
                    info.triggered.connect(
                        lambda checked=False, n=data[1]: self._on_nexus_info(n)
                    )
                    action = menu.addAction("Open in Explorer")
                    action.triggered.connect(
                        lambda: _open_in_explorer(mod_path)
                    )
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _find_hit(self, mod_name: str, entry: str) -> FileHit | None:
        for mod in self._scan["mods"]:
            if mod.name == mod_name:
                for hit in mod.hits:
                    if hit.entry == entry:
                        return hit
        return None

    def _show_collision(self, stored: str) -> None:
        entries: list[tuple[str, str]] = []
        for mod in self._scan["mods"]:
            for hit in mod.game_files:
                if hit.stored == stored:
                    entries.append((mod.name, hit.entry))
        related = (
            [c for c in (self._conflicts or []) if c.stored == stored]
            if self._conflicts
            else []
        )
        dlg = _CollisionDialog(stored, entries, related, self)
        dlg.exec()

    def _show_fields(self, ver) -> None:
        mod = next((m for m in self._scan["mods"] if m.name == ver.mod_name), None)
        hit = self._find_hit(ver.mod_name, ver.entry)
        if mod is None or hit is None or ver.pak is None or self._pcbanks is None:
            return
        if verify_mods.is_raw_asset(hit.entry):
            dlg = _RawAssetDialog(ver, mod, hit, self._pcbanks, self)
            dlg.exec()
            return
        van_mxml = verify_mods._extract_and_decompile(
            self._pcbanks, ver.pak, ver.stored, self._index,
            self.append_log, mbincompiler.ensure_compiler(),
        )
        van_root = ET.parse(van_mxml).getroot()
        dlg = _FieldDialog(ver, mod, hit, van_root, self)
        dlg.exec()
        if dlg.saved:
            self.append_log(f"Saved edits to {ver.rel} - re-verifying…")
            self._reverify_mod(ver.mod_name)

    def _on_tree_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data or data[0] != "file" or self._verifications is None:
            return
        ver = self._verifications.get((data[1], data[2]))
        if ver is not None:
            self._show_fields(ver)

    def _refresh_actions(self) -> None:
        busy = self._worker is not None
        if self._inject_state:
            self._restore_btn.setEnabled(not busy)
            self._restore_btn.setToolTip(
                f"Reverts all {len(self._inject_state)} injected mod(s), restoring "
                "the vanilla pak bytes from the managed pre-injection backups"
            )
        else:
            self._restore_btn.setEnabled(False)
            self._restore_btn.setToolTip("Nothing is injected into the paks")
        eligible = self._eligible_mods()
        self._inject_all_btn.setEnabled(not busy and bool(eligible))
        self._inject_all_btn.setToolTip(
            f"Injects {len(eligible)} eligible mod(s), merging mods that share pak files"
            if eligible
            else "No injectable mods (all injected, installed, or blocked by verification)"
        )
        self._update_action_states()

    def _is_installed(self, mod_name: str) -> bool:
        return install_mods.safe_name(mod_name) in self._installed_mods

    def _on_col_resized(self, idx: int, _old: int, new: int) -> None:
        """Record user column sizing; the re-fit is deferred (mid-drag re-layout
        makes Qt abandon the drag and lose the width)."""
        if self._fitting_columns:
            return
        self._user_col_widths[idx] = new
        self._col_fit_timer.start()
        self._col_save_timer.start()

    def _save_col_widths(self) -> None:
        """Persist chosen widths (debounced); closeEvent saves them too."""
        for column in range(len(_TREE_COLUMNS)):
            width = self._user_col_widths.get(column)
            if width:
                self._settings.setValue(f"col_width_{column}", width)

    def _fit_columns(self) -> None:
        """Fit columns to the viewport; Target pak(s) absorbs the difference (drag-deferred)."""
        if QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
            self._col_fit_timer.start()  # mid-drag: try again once it ends
            return
        viewport = self._tree.viewport().width()
        if viewport <= 0:
            return
        others = sum(
            self._tree.columnWidth(c)
            for c in range(len(_TREE_COLUMNS))
            if c != 3
        )
        cap = self._user_col_widths.get(3, dict(_TREE_COLUMN_WIDTHS)[3])
        target = min(max(viewport - others, 48), cap)
        self._fitting_columns = True
        try:
            self._tree.setColumnWidth(3, target)
        finally:
            self._fitting_columns = False

    def _update_action_states(self) -> None:
        """Push each mod's action-button state into its tree row for the delegate."""
        busy = self._worker is not None
        for name, item in self._mod_items.items():
            state = self._action_state(name, busy)
            item.setData(0, _ACTION_ROLE, state)
            # Combined tooltip on the Actions cell, shown by Qt's native
            # item-tooltip machinery (per-button QToolTip calls flickered).
            tips = state.get("tips") or ("", "")
            item.setToolTip(
                5,
                f"Pak inject / Revert: {tips[0]}\nInstall / Remove: {tips[1]}",
            )
        self._tree.viewport().update()

    def _action_state(self, name: str, busy: bool) -> dict:
        """Per-mod action labels, enabled flags and tooltips for the Actions column."""
        if name == self._injecting_mod:
            return {"busy": "Injecting…", "tips": ("", "")}
        if name == self._installing_mod:
            return {"busy": "Installing…", "tips": ("", "")}
        injected = name in self._inject_state
        installed = self._is_installed(name)
        allowed = self._inject_allowed(name)
        if injected:
            primary = ("Revert", not busy)
            primary_tip = (
                "Restores this mod's pre-injection bytes from the managed backups"
            )
        else:
            primary = ("Pak inject", not busy and allowed and not installed)
            if installed:
                primary_tip = (
                    "Blocked: this mod is installed in GAMEDATA\\MODS - "
                    "remove that install first to avoid double-applying"
                )
            elif busy:
                primary_tip = "Blocked while another task is running"
            elif not allowed:
                primary_tip = "Blocked: verification has outdated/error files"
            else:
                primary_tip = (
                    "Bakes this mod's files into the vanilla paks "
                    "(requires verification without blocking issues)"
                )
        if installed:
            secondary = ("Remove", not busy)
            secondary_tip = "Deletes this mod's folder from GAMEDATA\\MODS"
        else:
            secondary = ("Install", not busy and allowed and not injected)
            if injected:
                secondary_tip = (
                    "Blocked: this mod is injected into the paks - "
                    "revert that first to avoid double-applying"
                )
            elif busy:
                secondary_tip = "Blocked while another task is running"
            elif not allowed:
                secondary_tip = "Blocked: verification has outdated/error files"
            else:
                secondary_tip = (
                    "Copies this mod's files into GAMEDATA\\MODS "
                    "(the game's loose-mod loader)"
                )
        return {
            "primary": primary,
            "secondary": secondary,
            "tips": (primary_tip, secondary_tip),
        }

    def _inject_allowed(self, mod_name: str) -> bool:
        """Red mods (outdated/error files) may not be injected for now."""
        if self._verifications is None or self._scan is None:
            return False
        mod = next((m for m in self._scan["mods"] if m.name == mod_name), None)
        if mod is None or not mod.game_files:
            return False
        statuses = [
            v.status for (n, _e), v in self._verifications.items() if n == mod_name
        ]
        if not statuses:
            return False
        worst = min(statuses, key=lambda s: _FIELDS_WORST_FIRST.index(s))
        return worst not in (FV_OUTDATED, FV_ERROR)

    # --- inject / revert --------------------------------------------------------

    def _on_row_action(self, mod_name: str, which: str) -> None:
        """Delegate-painted Actions column clicked."""
        if which == "primary":
            self._on_inject_button(mod_name)
        else:
            self._on_install_button(mod_name)

    def _on_inject_button(self, mod_name: str) -> None:
        if self._worker is not None:
            return
        if mod_name not in self._inject_state:
            if self._is_installed(mod_name):
                self.append_log(
                    f"Cannot inject {mod_name}: it is installed in GAMEDATA\\MODS - "
                    "remove that install first (double-application)",
                    color=COLOR_ERROR,
                )
                return
            if not self._inject_allowed(mod_name):
                self.append_log(
                    f"Cannot inject {mod_name}: fix the outdated/error files first "
                    "(or update the mod for the current game version)",
                    color=COLOR_ERROR,
                )
                return
            partners = self._injected_collision_partners(mod_name)
            if partners:
                # Merged group injection with the already-injected mods.
                self._start_merge_preview([mod_name] + partners)
                return
        self._injecting_mod = mod_name
        self._refresh_actions()
        if mod_name in self._inject_state:
            self._start_task(
                revert_mod_worker, mod_name, self._scan, self._index,
                self._current_pcbanks(), on_done=self._on_inject_done,
            )
        else:
            self._start_task(
                inject_mod_worker, mod_name, self._scan, self._index,
                self._current_pcbanks(), on_done=self._on_inject_done,
            )

    def _injected_collision_partners(self, mod_name: str) -> list[str]:
        """Injected mods that share pak paths with the given mod."""
        collisions = self._scan["collisions"] if self._scan else {}
        partners = set()
        for stored, owners in collisions.items():
            if mod_name in owners:
                partners.update(o for o in owners if o in self._inject_state)
        return sorted(partners)

    def _start_merge_preview(self, names: list[str]) -> None:
        self._start_task(
            merge_preview_worker, names, self._scan, self._index,
            self._current_pcbanks(), on_done=self._on_merge_preview,
        )

    def _on_merge_preview(self, preview: dict) -> None:
        has_conflicts = any(
            f["conflicts"] for f in preview["files"].values()
        )
        injected_names = list(self._inject_state)
        if has_conflicts and _settings().value("merge_first_wins", False) in (True, "true"):
            dlg = None  # policy: first mod wins without asking
        else:
            dlg = _MergeDialog(preview, injected_names, self)
            dlg.exec()
            if not dlg.accepted_inject:
                self.append_log("Merged injection cancelled")
                return
        self._start_task(
            inject_group_worker, preview["mod_names"], self._scan, self._index,
            self._current_pcbanks(),
            dlg.resolutions if dlg else None,
            on_done=self._on_inject_done,
        )

    def _eligible_mods(self) -> list[str]:
        """Mods that pass the red gate and are neither injected nor installed."""
        if self._scan is None or self._verifications is None:
            return []
        out = []
        for m in self._scan["mods"]:
            if m.name in self._inject_state or self._is_installed(m.name):
                continue
            if self._inject_allowed(m.name):
                out.append(m.name)
        return out

    def _on_inject_all(self) -> None:
        if self._worker is not None:
            return
        eligible = self._eligible_mods()
        if not eligible:
            self.append_log("No injectable mods (all injected, installed, or blocked)")
            return
        self._start_task(
            merge_preview_worker, eligible, self._scan, self._index,
            self._current_pcbanks(),
            on_done=lambda result: self._on_inject_all_preview(eligible, result),
        )

    def _on_inject_all_preview(self, eligible: list[str], preview: dict) -> None:
        has_conflicts = any(
            f["conflicts"] for f in preview["files"].values()
        )
        if has_conflicts and _settings().value("merge_first_wins", False) in (True, "true"):
            dlg = None
        else:
            dlg = _MergeDialog(preview, list(self._inject_state), self)
            dlg.exec()
            if not dlg.accepted_inject:
                self.append_log("Inject all cancelled")
                return
        self._start_task(
            inject_all_worker, eligible, self._scan, self._index,
            self._current_pcbanks(),
            dlg.resolutions if dlg else None,
            on_done=self._on_inject_done,
        )

    def _on_inject_done(self, result: dict) -> None:
        self._injecting_mod = None
        self._inject_state = inject_mods.load_state()
        self._fill_tree()  # rebuilds the buttons in their new state

    def _on_install_button(self, mod_name: str) -> None:
        if self._worker is not None:
            return
        installed = self._is_installed(mod_name)
        if not installed:
            if mod_name in self._inject_state:
                self.append_log(
                    f"Cannot install {mod_name}: it is injected into the paks - "
                    "revert that first (double-application)",
                    color=COLOR_ERROR,
                )
                return
            if not self._inject_allowed(mod_name):
                self.append_log(
                    f"Cannot install {mod_name}: fix the outdated/error files first "
                    "(or update the mod for the current game version)",
                    color=COLOR_ERROR,
                )
                return
        self._installing_mod = mod_name
        self._refresh_actions()
        if installed:
            self._start_task(
                uninstall_mod_worker, mod_name, self._scan,
                self._current_pcbanks(), on_done=self._on_install_done,
            )
        else:
            self._start_task(
                install_mod_worker, mod_name, self._scan,
                self._current_pcbanks(), on_done=self._on_install_done,
            )

    def _on_install_done(self, result: dict) -> None:
        self._installing_mod = None
        self._fill_tree()  # re-reads the loose-mod folder state from disk

    def _on_restore_vanilla(self) -> None:
        if self._worker is not None:
            return
        if not self._inject_state:
            self.append_log("Nothing is injected - the paks are already vanilla")
            return
        n_mods = len(self._inject_state)
        n_files = sum(len(s.get("files", [])) for s in self._inject_state.values())
        n_loose = len(self._installed_mods)
        text = (
            f"Revert {n_mods} injected mod(s) ({n_files} baked file(s)) back to "
            f"the vanilla pak bytes?"
        )
        if n_loose:
            text += (
                f"\n\n{n_loose} loose mod folder(s) in GAMEDATA\\MODS are not "
                "touched (remove them individually if wanted)."
            )
        answer = QMessageBox.question(
            self, "Restore vanilla", text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_task(
            revert_all_worker, self._scan, self._index,
            self._current_pcbanks(), on_done=self._on_restore_done,
        )

    def _on_restore_done(self, result: dict) -> None:
        self._inject_state = inject_mods.load_state()
        self._fill_tree()  # refreshes button states (nothing injected now)

    # --- console ---------------------------------------------------------------

    def append_log(self, message: str, color: QColor | None = None) -> None:
        from datetime import datetime

        stamp = datetime.now().strftime("%H:%M:%S")
        self._log_view.appendPlainText(f"[{stamp}] {message}")
        try:
            print(f"[{stamp}] {message}", flush=True)
        except OSError:
            pass

    # --- frameless edge resize (ported from ZZZ Mod Housekeeper) ---------------

    def eventFilter(self, obj, event) -> bool:
        """Track the mouse for manual edge resizing, mirror the hover cursor,
        and refit the tree columns when their viewport changes size."""
        if obj is self._tree.viewport() and event.type() == QEvent.Type.Resize:
            self._fit_columns()
            return False
        if not isinstance(event, QMouseEvent):
            return False
        pos = self.mapFromGlobal(event.globalPosition().toPoint())
        if event.type() == QEvent.Type.MouseButtonPress:
            if (
                event.button() == Qt.MouseButton.LeftButton
                and not self.isMaximized()
            ):
                edges = self._resize_edges(pos)
                if edges is not None:
                    self._resize_drag_edges = edges
                    self._resize_press_global = event.globalPosition().toPoint()
                    self._resize_geometry = self.geometry()
                    self._restore_edge_cursor()
                    self.grabMouse(self._resize_cursor(pos))
                    return True
            return False
        if event.type() == QEvent.Type.MouseMove:
            if (
                self._resize_drag_edges is not None
                and self._resize_press_global is not None
                and self._resize_geometry is not None
            ):
                self._apply_resize_drag(event.globalPosition().toPoint())
                return True
            if self.isMaximized():
                self._restore_edge_cursor()
            else:
                self._update_edge_cursor(self._resize_cursor(pos))
            return False
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and self._resize_drag_edges is not None
        ):
            self._finish_resize_drag()
            return True
        return False

    def _apply_resize_drag(self, global_pos: QPoint) -> None:
        drag_edges = self._resize_drag_edges
        press_global = self._resize_press_global
        base = self._resize_geometry
        if drag_edges is None or press_global is None or base is None:
            return
        delta = global_pos - press_global
        min_width = self.minimumWidth()
        min_height = self.minimumHeight()
        x = base.x()
        y = base.y()
        width = base.width()
        height = base.height()
        if drag_edges & Qt.Edge.LeftEdge:
            x = min(base.x() + delta.x(), base.right() - min_width + 1)
            width = base.right() - x + 1
        if drag_edges & Qt.Edge.TopEdge:
            y = min(base.y() + delta.y(), base.bottom() - min_height + 1)
            height = base.bottom() - y + 1
        if drag_edges & Qt.Edge.RightEdge:
            width = max(base.width() + delta.x(), min_width)
        if drag_edges & Qt.Edge.BottomEdge:
            height = max(base.height() + delta.y(), min_height)
        self.setGeometry(x, y, max(width, min_width), max(height, min_height))

    def _finish_resize_drag(self) -> None:
        self._restore_edge_cursor()
        if self.mouseGrabber() is self:
            self.releaseMouse()
        self._resize_drag_edges = None
        self._resize_press_global = None
        self._resize_geometry = None

    def _resize_edges(self, pos: QPoint) -> Qt.Edge | None:
        rect = self.rect()
        left = pos.x() <= rect.left() + _RESIZE_MARGIN
        right = pos.x() >= rect.right() - _RESIZE_MARGIN
        top = pos.y() <= rect.top() + _RESIZE_MARGIN
        bottom = pos.y() >= rect.bottom() - _RESIZE_MARGIN
        if not (left or right or top or bottom):
            return None
        edges = Qt.Edge(0)
        if left:
            edges |= Qt.Edge.LeftEdge
        if right:
            edges |= Qt.Edge.RightEdge
        if top:
            edges |= Qt.Edge.TopEdge
        if bottom:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _resize_cursor(self, pos: QPoint) -> Qt.CursorShape:
        rect = self.rect()
        left = pos.x() <= rect.left() + _RESIZE_MARGIN
        right = pos.x() >= rect.right() - _RESIZE_MARGIN
        top = pos.y() <= rect.top() + _RESIZE_MARGIN
        bottom = pos.y() >= rect.bottom() - _RESIZE_MARGIN
        if (left and top) or (right and bottom):
            return Qt.CursorShape.SizeFDiagCursor
        if (left and bottom) or (right and top):
            return Qt.CursorShape.SizeBDiagCursor
        if left or right:
            return Qt.CursorShape.SizeHorCursor
        if top or bottom:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def _update_edge_cursor(self, shape: Qt.CursorShape) -> None:
        """Show the OS resize cursor while hovering a window edge."""
        if shape is Qt.CursorShape.ArrowCursor:
            self._restore_edge_cursor()
            return
        if self._edge_cursor is shape:
            return
        self._restore_edge_cursor()
        QGuiApplication.setOverrideCursor(QCursor(shape))
        self._edge_cursor = shape

    def _restore_edge_cursor(self) -> None:
        """Drop the edge-resize cursor override if one is active."""
        if self._edge_cursor is None:
            return
        QGuiApplication.restoreOverrideCursor()
        self._edge_cursor = None

    # --- persistence -----------------------------------------------------------

    # --- drag & drop ------------------------------------------------------------

    def _drop_paths(self, mime) -> list[Path]:
        """Local files/folders from a drag, filtered to mod-shaped entries."""
        out: list[Path] = []
        if mime is None or not mime.hasUrls():
            return out
        for url in mime.urls():
            p = Path(url.toLocalFile()) if url.isLocalFile() else None
            if p is None or not p.exists():
                continue
            if (p.is_file() and p.suffix.lower() == ".zip") or p.is_dir():
                out.append(p)
        return out

    def _set_drop_active(self, active: bool) -> None:
        """Amber overlay while a drag hovers anywhere over the window."""
        if active:
            self._drop_overlay.setGeometry(self.rect())
            self._drop_overlay.raise_()
            self._drop_overlay.show()
        else:
            self._drop_overlay.hide()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._drop_paths(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self._set_drop_active(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self._drop_paths(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._set_drop_active(False)
        event.accept()

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_drop_active(False)
        paths = self._drop_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self._import_dropped(paths)

    def _import_dropped(self, paths: list[Path]) -> None:
        """Copy dropped archives/folders into the mods folder, then rescan."""
        mods_dir = Path(self._mods_edit.text().strip())
        if not mods_dir.is_dir():
            self.append_log(
                f"Cannot copy drops: mods folder does not exist ({mods_dir})",
                color=COLOR_ERROR,
            )
            return
        copied = 0
        for src in paths:
            target = mods_dir / src.name
            if target.exists():
                answer = QMessageBox.question(
                    self,
                    "Replace mod?",
                    f"{src.name} already exists in the mods folder.\n"
                    "Replace it with the dropped version?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    self.append_log(f"Skipped {src.name} (kept existing)")
                    continue
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            try:
                if src.is_dir():
                    shutil.copytree(src, target)
                else:
                    shutil.copyfile(src, target)
            except OSError as exc:
                self.append_log(
                    f"Could not copy {src.name}: {exc}", color=COLOR_ERROR
                )
                continue
            copied += 1
            self.append_log(f"Copied {src.name} into the mods folder")
        if not copied:
            return
        if self._worker is None:
            self._scan_mods()
        else:
            # A task is running: queue one rescan when it finishes.
            self._rescan_pending = True
            self.append_log("Rescan queued until the current task finishes")

    def _maybe_rescan_for_drop(self) -> None:
        """Consume a queued drop-rescan once the task chain is idle."""
        if self._rescan_pending and self._worker is None:
            self._rescan_pending = False
            self._scan_mods()

    def closeEvent(self, event) -> None:
        for worker in list(self._active_workers):
            worker.wait(5000)
        self._restore_edge_cursor()
        self._settings.setValue("window_geometry", self.saveGeometry().data().hex())
        self._settings.setValue(
            "window_maximized", self.isMaximized()
        )
        # Remember which mods were expanded/collapsed.
        expanded = [
            self._tree.topLevelItem(i).text(0)
            for i in range(self._tree.topLevelItemCount())
            if self._tree.topLevelItem(i).isExpanded()
        ]
        self._settings.setValue("expanded_mods", json.dumps(expanded))
        for column in range(len(_TREE_COLUMNS)):
            # Persist the width the user chose, not a flexed value.
            width = self._user_col_widths.get(column) or self._tree.columnWidth(column)
            self._settings.setValue(f"col_width_{column}", width)
        self._settings.sync()
        super().closeEvent(event)


# --- module-level Qt shorthands used above ---------------------------------------

_STATUS_LABEL = {
    ST_CLEAN: "Clean",
    ST_MERGE: "Merge needed",
    ST_CONFLICT: "Conflict",
    ST_UNMATCHED: "New/unmatched",
}


def _open_in_explorer(path: str) -> None:
    """Open a mod folder in Explorer; for zips, select the file in its folder."""
    import os

    if Path(path).is_dir():
        os.startfile(path)
        return
    # Select the file via the Shell API: command-line forms ('explorer /select,…')
    # break on paths containing spaces unless quoted exactly right.
    import ctypes

    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32
    ole32.CoInitialize(None)
    try:
        pidl = ctypes.c_void_p()
        if shell32.SHParseDisplayName(
            ctypes.c_wchar_p(path), None, ctypes.byref(pidl), 0, None
        ) == 0:
            shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
            ole32.CoTaskMemFree(pidl)
            return
    finally:
        ole32.CoUninitialize()
    # Fallback: quoted single-string command line.
    import subprocess

    subprocess.Popen(f'explorer /select,"{path}"')


def _wrap_tooltip(text: str, width: int = 70) -> str:
    """Word-wrap text to ``width`` columns, preserving existing breaks."""
    wrapped: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            wrapped.append("")
            continue
        line = ""
        for word in paragraph.split(" "):
            if line and len(line) + 1 + len(word) > width:
                wrapped.append(line)
                line = word
            elif line:
                line += " " + word
            else:
                line = word
        wrapped.append(line)
    return "\n".join(wrapped)


def _apply_rounded_corners(window: QMainWindow) -> None:
    import ctypes

    try:
        dwmapi = ctypes.windll.dwmapi
        preference = ctypes.c_int(2)  # DWMWCP_ROUND
        dwmapi.DwmSetWindowAttribute(
            int(window.winId()),
            33,  # DWMWA_WINDOW_CORNER_PREFERENCE
            ctypes.byref(preference),
            ctypes.sizeof(preference),
        )
    except OSError:
        pass

