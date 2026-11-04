"""NMS Pak Inspector entry point."""
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox, QProxyStyle, QStyle

from tools import repo
from tools.ui.main_window import MainWindow

_LOCK_NAME = "nms_pak_inspector.lock"
_TOOLTIP_DELAY_MS = 450


class _TooltipDelayStyle(QProxyStyle):
    """App-wide style that shows tooltips only after a fixed hover delay."""

    def styleHint(self, hint, *args, **kwargs):
        if hint == QStyle.StyleHint.SH_ToolTip_WakeUpDelay:
            return _TOOLTIP_DELAY_MS
        return super().styleHint(hint, *args, **kwargs)


def _lock_dir() -> Path:
    directory = repo.project_root()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        directory = Path.home()
    return directory


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("NMS Pak Inspector")
    app.setStyle("Fusion")
    app.setStyle(_TooltipDelayStyle(app.style()))

    lock = QLockFile(str(_lock_dir() / _LOCK_NAME))
    if not lock.tryLock(0):
        QMessageBox.warning(
            None, "NMS Pak Inspector", "NMS Pak Inspector is already running."
        )
        sys.exit(0)

    window = MainWindow()
    window.show()
    if "--selftest" in sys.argv:
        from PySide6.QtCore import QTimer

        # Let startup workers finish (index load/scan/verify can take a while
        # on the first run), then dump a summary and quit.
        def _finish() -> None:
            mods = window._tree.topLevelItemCount()
            fields_done = window._verify_counts is not None
            print(f"SELFTEST: tree rows={mods}")
            print(f"SELFTEST: index={'loaded' if window._index else 'missing'}")
            print(f"SELFTEST: verification={'done' if fields_done else 'pending'}")
            if window._verify_counts:
                print(f"SELFTEST: counts={window._verify_counts}")
            shot = repo.project_root() / "ui_selftest.png"
            window.grab().save(str(shot))
            print(f"SELFTEST: screenshot saved to {shot.name}")
            sys.exit(0 if window._index is not None and mods > 0 else 1)

        QTimer.singleShot(30000, _finish)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

