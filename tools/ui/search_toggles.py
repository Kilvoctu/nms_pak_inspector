"""Checkable "Aa" (match case) and ".*" (regex) toggles embedded in a QLineEdit."""

import re
from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLineEdit, QToolButton

from . import theme

_INVALID_STYLE = (
    f"QLineEdit {{ background-color: {theme.BG_PANEL}; color: {theme.TEXT};"
    f" border: 1px solid {theme.MAGENTA}; selection-background-color:"
    f" {theme.SEL_BLOCK}; padding: 2px 4px; }}"
)

_BTN_STYLE = (
    "QToolButton { border: none; background: transparent; color: #b9b7c2;"
    " font-weight: bold; padding: 0 2px; }"
    f"QToolButton:hover {{ color: {theme.TEXT}; }}"
    f"QToolButton:checked {{ color: {theme.ACCENT}; }}"
    f"QToolButton:checked:hover {{ color: {theme.ACCENT}; }}"
)


def build_matcher(text: str, case_on: bool,
                  regex_on: bool) -> tuple[bool, Callable[[str], bool]]:
    """(valid, predicate) for a query; an invalid regex yields (False, all-False)."""
    text = text.strip()
    if not text:
        return True, lambda _s: True
    if regex_on:
        try:
            rx = re.compile(text, flags=0 if case_on else re.IGNORECASE)
        except re.error:
            return False, lambda _s: False
        return True, lambda s: rx.search(s) is not None
    if case_on:
        return True, lambda s: text in s
    low = text.lower()
    return True, lambda s: low in s.lower()


class SearchToggles(QObject):
    """Toggle buttons overlaid at a QLineEdit's right edge; `changed` on any flip."""

    changed = Signal()

    def __init__(self, edit: QLineEdit, case: bool = True, regex: bool = True) -> None:
        super().__init__(edit)
        self._edit = edit
        self._buttons: list[QToolButton] = []
        labels: list[tuple[str, str]] = []
        if case:
            labels.append(("Aa", "Match case"))
        if regex:
            labels.append((".*", "Treat query as a regular expression"))
        f = QFont()
        f.setBold(True)
        f.setPixelSize(13)
        for label, tip in labels:
            b = QToolButton(edit)
            b.setText(label)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setFont(f)
            b.setStyleSheet(_BTN_STYLE)
            b.setFixedSize(28, 20)
            b.toggled.connect(lambda _on: self.changed.emit())
            self._buttons.append(b)
        edit.installEventFilter(self)
        self._reposition()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        # hidden widgets get no resize events; Show covers first layout,
        # and the clear button only becomes visible after show().
        if obj is self._edit and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._reposition()
        return False

    def _builtin_icon_left(self) -> int | None:
        """Left edge of Qt's own icon buttons (clear button etc.), if any."""
        xs = [c.x() for c in self._edit.children()
              if c.metaObject().className() == "QLineEditIconButton" and c.isVisible()]
        return min(xs) if xs else None

    def _reposition(self) -> None:
        edit = self._edit
        builtin_x = self._builtin_icon_left()
        if builtin_x is None:
            builtin_x = edit.width() - 2  # no built-in icon buttons: right edge
        x = min(builtin_x - 2, edit.width() - 2)
        for b in reversed(self._buttons):
            x -= b.width()
            b.move(x, max(0, (edit.height() - b.height()) // 2))
            b.show()
        edit.setTextMargins(0, 0, max(0, edit.width() - x - 1), 0)

    @property
    def case_on(self) -> bool:
        return bool(self._buttons and self._buttons[0].isChecked())

    @property
    def regex_on(self) -> bool:
        return bool(len(self._buttons) > 1 and self._buttons[1].isChecked())

    def matcher(self, text: str) -> tuple[bool, Callable[[str], bool]]:
        """(valid, predicate) for this query; invalid regex flags the field red."""
        ok, fn = build_matcher(text, self.case_on, self.regex_on)
        self._mark(not ok)
        return ok, fn

    def _mark(self, invalid: bool) -> None:
        self._edit.setStyleSheet(_INVALID_STYLE if invalid else "")
