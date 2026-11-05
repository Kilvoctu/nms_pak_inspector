"""Pak inspector dialog: browse/search the pak index and pull entries into mods."""

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from .. import archivelib, mbincompiler, paklib
from ..verify_mods import _extract_and_decompile

_ROLE = Qt.ItemDataRole.UserRole  # ("pak", name) | ("dir", pak, prefix) | ("file", pak, stored)
_LOADED_ROLE = Qt.ItemDataRole.UserRole + 1
_RESULT_CAP = 2000


class PakInspectorDialog(QDialog):
    """Browse/search every indexed pak entry; pull vanilla files into existing or new mods."""

    def __init__(self, index: dict, pcbanks: str, mods: list, settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pak inspector - PCBANKS")
        self.resize(1000, 700)
        self._index = index
        self._pcbanks = pcbanks
        self._mods = mods
        self._settings = settings
        self._compiler = None
        self.pulled = False
        self._log_lines: list[str] = []

        layout = QVBoxLayout(self)
        self._search = QLineEdit(self)
        self._search.setPlaceholderText(
            "Search all pak entries (case-insensitive substring) - leave empty to browse"
        )
        self._search.textChanged.connect(self._on_search_changed)
        layout.addWidget(self._search)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(lambda: self._run_search(self._search.text()))

        self._tree = QTreeWidget(self)
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["Path", "Pak"])
        self._tree.setColumnWidth(0, 720)
        self._tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        layout.addWidget(self._tree, 1)

        self._status = QLabel(self)
        layout.addWidget(self._status)

        row = QHBoxLayout()
        self._mod_combo = QComboBox(self)
        self._fill_mods()
        row.addWidget(QLabel("Pull into:", self), 0)
        row.addWidget(self._mod_combo, 1)
        new_btn = QPushButton("New mod…", self)
        new_btn.clicked.connect(self._on_new_mod)
        row.addWidget(new_btn)
        self._pull_btn = QPushButton("Pull into mod", self)
        self._pull_btn.clicked.connect(self._on_pull)
        row.addWidget(self._pull_btn)
        close_btn = QPushButton("Close", self)
        close_btn.clicked.connect(self.reject)
        row.addWidget(close_btn)
        layout.addLayout(row)

        self._populate_roots()
        self._update_status()

    # --- data -----------------------------------------------------------------

    def _pak_files(self, pak: str) -> list[str]:
        return self._index["paks"][pak]["files"]

    def _fill_mods(self) -> None:
        """Writable mods only (rar archives cannot be written)."""
        self._mod_combo.clear()
        last = self._settings.value("pak_inspector_last_mod", "")
        writable = []
        for mod in self._mods:
            path = Path(mod.path)
            if path.is_dir() or path.suffix.lower() in (".zip", ".7z"):
                writable.append(mod.name)
        for name in sorted(writable, key=str.lower):
            self._mod_combo.addItem(name)
        if last:
            idx = self._mod_combo.findText(last)
            if idx >= 0:
                self._mod_combo.setCurrentIndex(idx)

    def _populate_roots(self) -> None:
        self._tree.clear()
        for pak in sorted(self._index["paks"], key=str.lower):
            n = len(self._pak_files(pak))
            item = QTreeWidgetItem([pak, f"{n} file(s)"])
            item.setData(0, _ROLE, ("pak", pak))
            item.addChild(QTreeWidgetItem(["…"]))  # expand marker
            self._tree.addTopLevelItem(item)

    def _expand_marker(self, item: QTreeWidgetItem) -> bool:
        return item.childCount() == 1 and item.child(0).text(0) == "…"

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if not self._expand_marker(item):
            return
        item.takeChildren()
        kind = item.data(0, _ROLE)
        if kind[0] == "pak":
            self._populate_pak(item, kind[1])
        else:
            _, pak, prefix = kind
            self._populate_dir(item, pak, prefix)

    def _populate_pak(self, item: QTreeWidgetItem, pak: str) -> None:
        """Children of a pak node: bare-stored files directly, or top-level dirs."""
        dirs: dict[str, dict] = {}
        files: list[str] = []
        for stored in self._pak_files(pak):
            name = stored.replace("\\", "/")
            if "/" in name:
                top = name.split("/", 1)[0]
                dirs.setdefault(top, None)
            else:
                files.append(name)
        for d in sorted(dirs, key=str.lower):
            child = QTreeWidgetItem([f"{d}/", ""])
            child.setData(0, _ROLE, ("dir", pak, d + "/"))
            child.addChild(QTreeWidgetItem(["…"]))
            item.addChild(child)
        for f in sorted(files, key=str.lower):
            item.addChild(self._file_item(f, pak, f))

    def _populate_dir(self, item: QTreeWidgetItem, pak: str, prefix: str) -> None:
        """Children of one directory: subdirs and files sharing this prefix."""
        dirs: set[str] = set()
        files: list[str] = []
        plen = len(prefix)
        for stored in self._pak_files(pak):
            name = stored.replace("\\", "/")
            if not name.lower().startswith(prefix.lower()):
                continue
            rest = name[plen:]
            if "/" in rest:
                dirs.add(rest.split("/", 1)[0])
            else:
                files.append(rest)
        for d in sorted(dirs, key=str.lower):
            child = QTreeWidgetItem([f"{d}/", ""])
            child.setData(0, _ROLE, ("dir", pak, prefix + d + "/"))
            child.addChild(QTreeWidgetItem(["…"]))
            item.addChild(child)
        for f in sorted(files, key=str.lower):
            item.addChild(self._file_item(f, pak, prefix + f))

    @staticmethod
    def _file_item(name: str, pak: str, stored: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem([name, pak])
        item.setData(0, _ROLE, ("file", pak, stored))
        return item

    # --- search -----------------------------------------------------------------

    def _on_search_changed(self, _text: str) -> None:
        self._search_timer.start()

    def _run_search(self, text: str) -> None:
        text = text.strip().lower()
        self._tree.clear()
        if not text:
            self._populate_roots()
            self._update_status()
            return
        matches, total = [], 0
        for pak, info in self._index["paks"].items():
            for stored in info["files"]:
                if text in stored.lower():
                    total += 1
                    if len(matches) < _RESULT_CAP:
                        matches.append((stored.replace("\\", "/"), pak))
        for name, pak in sorted(matches, key=lambda m: (m[0].lower(), m[1].lower())):
            self._tree.addTopLevelItem(self._file_item(name, pak, name))
        self._status.setText(self._result_text(total, len(matches)))
        self._pull_btn.setEnabled(bool(matches))

    @staticmethod
    def _result_text(total: int, shown: int) -> str:
        base = f"{total} match(es)"
        return base if shown >= total else f"{base} - showing first {shown}"

    def _update_status(self) -> None:
        paks = self._index.get("total_paks", len(self._index["paks"]))
        files = self._index.get("total_files", "?")
        self._status.setText(f"{paks} paks, {files} indexed file(s) - select and pull")

    # --- pull -------------------------------------------------------------------

    def _selected_entries(self) -> list[tuple[str, str]]:
        out = []
        for item in self._tree.selectedItems():
            kind = item.data(0, _ROLE)
            if kind and kind[0] == "file":
                out.append((kind[1], kind[2]))
        return out

    def _on_new_mod(self) -> None:
        mods_root = self._settings.value("mods_folder", "")
        if not mods_root:
            QMessageBox.warning(self, "Pak inspector", "Set the mods folder first.")
            return
        name, ok = QInputDialog.getText(self, "New mod", "Mod folder name:")
        if not ok or not name.strip():
            return
        name = "".join(c for c in name.strip() if c not in '\\/:*?"<>|').strip()
        if not name:
            return
        (Path(mods_root) / name).mkdir(parents=True, exist_ok=True)
        self._mods = self._mods + [_SimpleMod(name, str(Path(mods_root) / name))]
        self._fill_mods()
        self._mod_combo.setCurrentText(name)

    def _on_pull(self) -> None:
        entries = self._selected_entries()
        if not entries:
            QMessageBox.information(self, "Pak inspector", "Select file(s) first.")
            return
        target = self._mod_combo.currentText()
        mod = next((m for m in self._mods if m.name == target), None)
        if mod is None:
            QMessageBox.warning(self, "Pak inspector", "Pick a target mod first.")
            return
        self._compiler = self._compiler or mbincompiler.ensure_compiler()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        done = failed = 0
        try:
            for pak, stored in entries:
                try:
                    rel, data = self._pull_content(pak, stored)
                    self._write_into_mod(mod, rel, data)
                    done += 1
                except Exception as exc:  # noqa: BLE001 - report per-file, keep going
                    failed += 1
                    self._log_lines.append(f"Pak inspector: {stored} ({pak}): {exc}")
        finally:
            QApplication.restoreOverrideCursor()
        if done:
            self.pulled = True
            self._settings.setValue("pak_inspector_last_mod", target)
        msg = f"Pulled {done} file(s) into {target}."
        if failed:
            msg += f"\n{failed} failed - see the log."
        QMessageBox.information(self, "Pak inspector", msg)

    def _pull_content(self, pak: str, stored: str) -> tuple[str, bytes]:
        """Vanilla content for one entry: decompiled EXML for MBINs, raw bytes else."""
        if stored.lower().endswith(".mbin"):
            mxml = _extract_and_decompile(
                self._pcbanks, pak, stored, self._index, self._log, self._compiler
            )
            return stored[:-len(".mbin")] + ".EXML", Path(mxml).read_bytes()
        with paklib.Pak(Path(self._pcbanks) / pak) as pk:
            return stored, pk.read_file(stored)

    def _log(self, line: str) -> None:
        self._log_lines.append(f"Pak inspector: {line}")

    @staticmethod
    def _write_into_mod(mod, rel: str, data: bytes) -> None:
        """Write one file into a folder mod, or add/replace a member in a zip/7z."""
        path = Path(mod.path)
        if path.is_dir():
            dest = path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return
        if path.suffix.lower() == ".rar":
            raise ValueError("RAR archives cannot be written - use a folder/zip/7z mod")
        arc = archivelib.open_archive(path)
        entries = {n: arc.read(n) for n in arc.names()}
        entries[rel] = data
        tmp = path.with_suffix(path.suffix + ".editing")
        archivelib.write_archive(tmp, entries, fmt=path.suffix.lower())
        tmp.replace(path)


class _SimpleMod:
    """Minimal mod handle for a freshly created folder (until the next scan)."""

    def __init__(self, name: str, path: str) -> None:
        self.name = name
        self.path = path
