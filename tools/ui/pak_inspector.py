"""Pak inspector dialog: browse/search the pak index and pull entries or fields into mods."""

import json
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import archivelib, mbincompiler, merge_mods, paklib, repo
from ..verify_mods import _extract_and_decompile, _parse_mod_tree, _read_mod_data
from .search_toggles import SearchToggles, build_matcher

_ROLE = Qt.ItemDataRole.UserRole  # main tree: ("pak",..)/("dir",..)/("file",..)
_ELEM_ROLE = Qt.ItemDataRole.UserRole  # preview tree: the row's ET element
_RESULT_CAP = 2000
_COVERED_COLOR = QColor("#7fbf7f")
_BIG_PULL = 300  # ask before pulling this many files at once
_DEEP_WORKERS = 8  # parallel MBINCompiler processes for deep search
_DEEP_CONFIRM = 2000  # ask before deep-searching more than this many mbin files


class PakInspectorDialog(QDialog):
    """Browse/search every indexed pak entry; pull vanilla files or fields into mods."""

    _deep_row = Signal(int, str, str, str)  # gen, stored, pak, match detail
    _deep_status = Signal(int, str)  # gen, progress text
    _deep_finished = Signal(int, int, int)  # gen, files matched, templates matched

    def __init__(self, index: dict, pcbanks: str, mods: list, settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pak Inspector - PCBANKS")
        self.resize(1100, 720)
        self._index = index
        self._pcbanks = pcbanks
        self._mods = mods
        self._settings = settings
        self._compiler = None
        self.pulled = False
        self._log_lines: list[str] = []
        self._preview_ctx = None  # (pak, stored, van_root, template) while previewing
        self._preview_rows: dict[int, QTreeWidgetItem] = {}
        self._adopted: dict[int, str] = {}  # preview elem id -> "added"/"updated"
        self._deep_stop = threading.Event()
        self._deep_gen = 0
        self._key_locks: dict[str, threading.Lock] = {}
        self._key_locks_master = threading.Lock()
        self._build_coverage()

        layout = QVBoxLayout(self)
        prow = QHBoxLayout()
        prow.addWidget(QLabel("Paks:", self))
        self._pak_filter = QLineEdit(self)
        self._pak_filter.setPlaceholderText("filter pak list (e.g. globals, EntityScene)")
        self._pak_filter.textChanged.connect(self._on_pak_filter)
        self._pak_toggles = SearchToggles(self._pak_filter, regex=False)
        self._pak_toggles.changed.connect(
            lambda: self._on_pak_filter(self._pak_filter.text()))
        prow.addWidget(self._pak_filter, 1)
        layout.addLayout(prow)

        srow = QHBoxLayout()
        srow.addWidget(QLabel("Find:", self))
        self._search = QLineEdit(self)
        self._search.setPlaceholderText(
            "search entry paths in the filtered paks - leave empty to browse the tree"
        )
        self._search.textChanged.connect(self._on_search_changed)
        self._search_toggles = SearchToggles(self._search)
        self._search_toggles.changed.connect(lambda: self._on_search_changed(""))
        srow.addWidget(self._search, 1)
        self._deep_check = QCheckBox("Deep search", self)
        self._deep_check.setToolTip(
            "Also match template and field names inside .mbin files.\n"
            "Uses a template index (built once per game version): header scan\n"
            "plus one decompile per distinct template - no per-file decompiling."
        )
        self._deep_check.toggled.connect(self._on_search_changed)
        srow.addWidget(self._deep_check)
        layout.addLayout(srow)
        self._deep_row.connect(self._on_deep_row)
        self._deep_status.connect(self._on_deep_status)
        self._deep_finished.connect(self._on_deep_finished)
        self._deep_confirmed_scope: str | None = None  # last user-confirmed sweep scope
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(lambda: self._run_search(self._search.text()))

        split = QSplitter(Qt.Orientation.Horizontal, self)
        self._tree = QTreeWidget(self)
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["Path", "Pak", "Covered by"])
        self._tree.setColumnWidth(0, 500)
        self._tree.setColumnWidth(1, 175)
        self._tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_context)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        split.addWidget(self._tree)

        preview_box = QWidget(self)
        pv_layout = QVBoxLayout(preview_box)
        pv_layout.setContentsMargins(0, 0, 0, 0)
        self._preview_title = QLabel("Preview", self)
        pv_layout.addWidget(self._preview_title)
        pf_row = QHBoxLayout()
        self._preview_find_label = QLabel("Find:", self)
        pf_row.addWidget(self._preview_find_label)
        self._preview_filter = QLineEdit(self)
        self._preview_filter.setPlaceholderText("filter fields in preview")
        self._preview_filter.textChanged.connect(self._apply_preview_filter)
        self._preview_toggles = SearchToggles(self._preview_filter)
        self._preview_toggles.changed.connect(
            lambda: self._apply_preview_filter(self._preview_filter.text()))
        pf_row.addWidget(self._preview_filter, 1)
        pv_layout.addLayout(pf_row)
        self._preview_tree = QTreeWidget(self)
        self._preview_tree.setColumnCount(3)
        self._preview_tree.setHeaderLabels(["Field", "Value", "Mod value"])
        self._preview_tree.setColumnWidth(0, 240)
        self._preview_tree.setColumnWidth(1, 130)
        self._preview_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._preview_tree.customContextMenuRequested.connect(self._on_preview_context)
        pv_layout.addWidget(self._preview_tree, 1)
        self._preview_image = QLabel(self)
        self._preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_image.setWordWrap(True)
        self._preview_image.hide()
        pv_layout.addWidget(self._preview_image, 1)
        pv_hint = QLabel("Right-click field(s) to pull them into the target mod.", self)
        pv_layout.addWidget(pv_hint)
        split.addWidget(preview_box)
        split.setSizes([640, 460])
        layout.addWidget(split, 1)

        self._status = QLabel(self)
        layout.addWidget(self._status)

        row = QHBoxLayout()
        self._mod_combo = QComboBox(self)
        self._fill_mods()
        self._mod_combo.currentTextChanged.connect(lambda _t: self._mark_mod_fields())
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

    def done(self, r) -> None:
        self._stop_deep()  # no background sweep outlives the dialog
        super().done(r)

    # --- data -------------------------------------------------------------------

    def _build_coverage(self) -> None:
        """Map (pak, stored) -> mod names already shipping that file."""
        cov: dict[tuple[str, str], list[str]] = {}
        for mod in self._mods:
            for hit in getattr(mod, "game_files", None) or []:
                pak = getattr(hit, "pak", None)
                stored = getattr(hit, "stored", None)
                if pak and stored:
                    cov.setdefault((pak.lower(), stored.lower()), []).append(mod.name)
        self._coverage = cov

    def _covered_label(self, pak: str, stored: str) -> str:
        names = self._coverage.get((pak.lower(), stored.lower()))
        if not names:
            return ""
        return names[0] + (f" (+{len(names) - 1} more)" if len(names) > 1 else "")

    def _pak_files(self, pak: str) -> list[str]:
        return self._index["paks"][pak]["files"]

    def _filtered_paks(self) -> list[str]:
        _ok, match = self._pak_toggles.matcher(self._pak_filter.text())
        paks = sorted(self._index["paks"], key=str.lower)
        return [p for p in paks if match(p)]

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

    # --- browse tree ---------------------------------------------------------------

    def _populate_roots(self) -> None:
        self._tree.clear()
        for pak in self._filtered_paks():
            n = len(self._pak_files(pak))
            item = QTreeWidgetItem([pak, "", f"{n} file(s)"])
            item.setData(0, _ROLE, ("pak", pak))
            item.addChild(QTreeWidgetItem(["…"]))  # lazy-expand marker
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
        dirs: dict[str, None] = {}
        files: list[str] = []
        for stored in self._pak_files(pak):
            name = stored.replace("\\", "/")
            if "/" in name:
                dirs.setdefault(name.split("/", 1)[0], None)
            else:
                files.append(name)
        self._add_dir_children(item, pak, "", dirs, files)

    def _populate_dir(self, item: QTreeWidgetItem, pak: str, prefix: str) -> None:
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
        self._add_dir_children(item, pak, prefix, dirs, files)

    def _add_dir_children(self, item: QTreeWidgetItem, pak: str, prefix: str,
                          dirs, files) -> None:
        for d in sorted(dirs, key=str.lower):
            child = QTreeWidgetItem([f"{d}/", pak, ""])
            child.setData(0, _ROLE, ("dir", pak, prefix + d + "/"))
            child.addChild(QTreeWidgetItem(["…"]))
            item.addChild(child)
        for f in sorted(files, key=str.lower):
            item.addChild(self._file_item(f, pak, prefix + f))

    def _file_item(self, name: str, pak: str, stored: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem([name, pak, self._covered_label(pak, stored)])
        if item.text(2):
            item.setForeground(2, _COVERED_COLOR)
        item.setData(0, _ROLE, ("file", pak, stored))
        return item

    def _refresh_coverage(self, pak: str, stored: str) -> None:
        """Update the 'Covered by' badge of every materialized item for one entry."""
        label = self._covered_label(pak, stored)

        def walk(item: QTreeWidgetItem) -> None:
            for i in range(item.childCount()):
                child = item.child(i)
                kind = child.data(0, _ROLE)
                if kind and kind[0] == "file" and kind[2].lower() == stored.lower():
                    child.setText(2, label)
                    if label:
                        child.setForeground(2, _COVERED_COLOR)
                walk(child)

        for i in range(self._tree.topLevelItemCount()):
            walk(self._tree.topLevelItem(i))
        # rebuild coverage cache entry so future items show it too
        names = self._coverage.setdefault((pak.lower(), stored.lower()), [])
        target = self._mod_combo.currentText()
        if target and target not in names:
            names.append(target)

    # --- search -------------------------------------------------------------------

    def _on_pak_filter(self, _text: str) -> None:
        if self._search.text().strip():
            self._run_search(self._search.text())
        else:
            self._populate_roots()
            self._update_status()

    def _on_search_changed(self, _text: str) -> None:
        # Deep sweeps are expensive to start: give typing a longer pause first.
        self._search_timer.setInterval(750 if self._deep_check.isChecked() else 250)
        self._search_timer.start()

    def _run_search(self, text: str) -> None:
        self._stop_deep()
        self._tree.clear()
        ok, match = self._search_toggles.matcher(text)
        if not text.strip() or not ok:
            self._populate_roots()
            self._update_status()
            return
        matches, total = [], 0
        for pak in self._filtered_paks():
            for stored in self._pak_files(pak):
                if match(stored):
                    total += 1
                    if len(matches) < _RESULT_CAP:
                        matches.append((stored.replace("\\", "/"), pak))
        for name, pak in sorted(matches, key=lambda m: (m[0].lower(), m[1].lower())):
            self._tree.addTopLevelItem(self._file_item(name, pak, name))
        base = f"{total} path match(es)"
        self._status.setText(base if len(matches) >= total
                             else f"{base} - showing first {len(matches)}")
        self._pull_btn.setEnabled(bool(matches))
        if self._deep_check.isChecked():
            self._start_deep(text)

    # --- deep search (template-index lookup: names, no per-file decompiling) ------

    def _stop_deep(self) -> None:
        self._deep_gen += 1
        self._deep_stop.set()

    def _start_deep(self, text: str) -> None:
        scope = self._pak_filter.text().strip().lower()
        paks = self._filtered_paks()
        if not paks:
            return
        tidx = self._load_tidx()
        to_scan = [p for p in paks if p not in tidx["paks"]]
        to_decompile = sum(
            1 for p in to_scan for s in self._pak_files(p) if s.lower().endswith(".mbin")
        )
        if (len(to_scan) > 10 or to_decompile > _DEEP_CONFIRM) \
                and scope != self._deep_confirmed_scope:
            answer = QMessageBox.question(
                self, "Deep search",
                f"Deep search builds a one-time template index first: header-scanning "
                f"{len(to_scan)} pak(s) (~{to_decompile} files) and decompiling one "
                "representative per template. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._deep_confirmed_scope = scope
        self._compiler = self._compiler or mbincompiler.ensure_compiler()
        self._deep_stop = threading.Event()
        self._deep_gen += 1
        gen = self._deep_gen
        stop = self._deep_stop
        thread = threading.Thread(
            target=self._deep_job,
            args=(text, paks, stop, gen,
                  self._search_toggles.case_on, self._search_toggles.regex_on),
            daemon=True,
        )
        thread.start()

    def _key_lock(self, key: str) -> threading.Lock:
        """Per-cache-entry lock: concurrent decompiles of one file would corrupt it."""
        with self._key_locks_master:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def _decompile_van(self, pak: str, stored: str) -> tuple[Path, ET.Element]:
        """Vanilla mbin -> (mxml path, parsed root); per-key locked, cache-backed."""
        key = f"{pak}|{stored}|{self._index.get('generated')}"
        with self._key_lock(key):
            mxml = _extract_and_decompile(
                self._pcbanks, pak, stored, self._index, self._log,
                self._compiler or mbincompiler.ensure_compiler(),
            )
            root = ET.fromstring(Path(mxml).read_text(encoding="utf-8", errors="replace"))
        return Path(mxml), root

    def _load_tidx(self) -> dict:
        """The per-index-build template index: file->hash map + hash->fields dictionary."""
        f = repo.cache_dir() / "templates.json"
        if f.exists():
            try:
                t = json.loads(f.read_text(encoding="utf-8"))
                if t.get("index_generated") == self._index.get("generated"):
                    return t
            except (OSError, ValueError):
                pass
        return {"index_generated": self._index.get("generated"),
                "paks": {}, "templates": {}}

    def _save_tidx(self, tidx: dict) -> None:
        f = repo.cache_dir() / "templates.json"
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(tidx), encoding="utf-8")
        tmp.replace(f)

    def _ensure_template_index(self, paks: list[str], stop: threading.Event,
                               gen: int) -> dict | None:
        """Build the template index incrementally: header scan + representative
        decompiles. Returns the index, or None when cancelled."""
        tidx = self._load_tidx()
        to_scan = [p for p in paks if p not in tidx["paks"]]
        for i, pak in enumerate(to_scan):
            fmap: dict[str, str] = {}
            with paklib.Pak(Path(self._pcbanks) / pak) as pk:
                for stored in self._pak_files(pak):
                    if stop.is_set() or gen != self._deep_gen:
                        return None
                    if not stored.lower().endswith(".mbin"):
                        continue
                    head = pk.read_file(stored)[:16]
                    if len(head) >= 16:
                        fmap[stored] = head[12:16].hex()
            tidx["paks"][pak] = fmap
            self._deep_status.emit(gen, f"template index: scanned pak {i + 1}/{len(to_scan)}")
        rep: dict[str, tuple[str, str]] = {}
        for pak, fmap in tidx["paks"].items():
            for stored, hh in fmap.items():
                if hh not in tidx["templates"] and hh not in rep:
                    rep[hh] = (pak, stored)
        if rep:
            self._deep_status.emit(
                gen, f"template index: decompiling {len(rep)} template(s)…"
            )
        with ThreadPoolExecutor(max_workers=_DEEP_WORKERS) as pool:
            futs = {pool.submit(self._decompile_van, *r): hh for hh, r in rep.items()}
            for fut in as_completed(futs):
                if stop.is_set() or gen != self._deep_gen:
                    for f in futs:
                        f.cancel()
                    return None
                hh = futs[fut]
                try:
                    _mxml, root = fut.result()
                except Exception as exc:  # noqa: BLE001 - skip unreadable entries
                    self._log(f"template index: {exc}")
                    continue
                fields = sorted({c.get("name") or "?" for c in root.iter()
                                 if c.get("name")})
                tidx["templates"][hh] = {
                    "name": root.get("template") or "?", "fields": fields,
                }
                self._deep_status.emit(
                    gen, f"template index: {len(tidx['templates'])} template(s) known"
                )
        self._save_tidx(tidx)
        return tidx

    def _deep_job(self, text: str, paks: list[str], stop: threading.Event,
                  gen: int, case_on: bool, regex_on: bool) -> None:
        tidx = self._ensure_template_index(paks, stop, gen)
        if tidx is None:
            return
        ok, match = build_matcher(text, case_on, regex_on)
        if not text.strip() or not ok:
            self._deep_finished.emit(gen, 0, 0)
            return
        matched: dict[str, tuple[str, str]] = {}
        for hh, t in tidx["templates"].items():
            hit_fields = [f for f in t["fields"] if match(f)]
            if match(t["name"]):
                matched[hh] = (t["name"], f"template {t['name']} matches")
            elif hit_fields:
                show = ", ".join(hit_fields[:3])
                if len(hit_fields) > 3:
                    show += f" (+{len(hit_fields) - 3} more)"
                matched[hh] = (t["name"], f"{t['name']}: {show}")
        rows = []
        for pak in paks:
            for stored, hh in tidx["paks"].get(pak, {}).items():
                if hh in matched:
                    _name, detail = matched[hh]
                    rows.append((stored.replace("\\", "/"), pak, stored, detail))
        rows.sort()
        for i, (_disp, pak, stored, detail) in enumerate(rows[:_RESULT_CAP]):
            if stop.is_set() or gen != self._deep_gen:
                return
            self._deep_row.emit(gen, stored, pak, detail)
        self._deep_finished.emit(gen, len(rows), len(matched))

    def _on_deep_row(self, gen: int, stored: str, pak: str, detail: str) -> None:
        if gen != self._deep_gen:
            return  # stale sweep - its results were superseded
        item = self._file_item(stored.replace("\\", "/"), pak, stored)
        item.setToolTip(0, f"deep match: {detail}")
        self._tree.addTopLevelItem(item)

    def _on_deep_status(self, gen: int, text: str) -> None:
        if gen == self._deep_gen:
            self._status.setText(text)

    def _on_deep_finished(self, gen: int, files: int, templates: int) -> None:
        if gen != self._deep_gen or self._deep_stop.is_set():
            return
        self._status.setText(
            f"deep search done: {files} file(s) matched via {templates} template(s)"
        )

    def _apply_preview_filter(self, text: str) -> None:
        """Hide preview rows that don't match (ancestors of matches stay visible)."""
        _ok, match = self._preview_toggles.matcher(text)
        top = self._preview_tree.topLevelItem(0)
        if top is not None:
            self._filter_row(top, match)

    def _filter_row(self, item: QTreeWidgetItem, match: Callable[[str], bool]) -> bool:
        """Return True if this row or any descendant matches; set visibility."""
        self_match = match(item.text(0)) or match(item.text(1))
        # no any() short-circuit: every child must be visited to clear its state
        results = [self._filter_row(item.child(i), match)
                   for i in range(item.childCount())]
        child_visible = any(results)
        item.setHidden(not (self_match or child_visible))
        if child_visible:
            item.setExpanded(True)
        return self_match or child_visible

    def _update_status(self) -> None:
        paks = self._index.get("total_paks", len(self._index["paks"]))
        files = self._index.get("total_files", "?")
        self._status.setText(f"{paks} paks, {files} indexed file(s) - select and pull")

    # --- preview ------------------------------------------------------------------

    def _on_selection_changed(self) -> None:
        selected = self._tree.selectedItems()
        self._preview_tree.clear()
        self._preview_ctx = None
        self._preview_rows = {}
        if len(selected) != 1:
            self._preview_title.setText("Preview")
            return
        kind = selected[0].data(0, _ROLE)
        if not kind or kind[0] != "file":
            self._preview_title.setText("Preview")
            return
        _, pak, stored = kind
        self._preview_title.setText(f"Preview: {stored}")
        if stored.lower().endswith(".dds"):
            self._show_dds_preview(pak, stored)
            return
        self._preview_mode(tree=True)
        if not stored.lower().endswith(".mbin"):
            self._preview_tree.addTopLevelItem(
                QTreeWidgetItem(["(raw file - no preview; pull it to edit externally)", ""])
            )
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._compiler = self._compiler or mbincompiler.ensure_compiler()
            _mxml, van_root = self._decompile_van(pak, stored)
        except Exception as exc:  # noqa: BLE001 - preview is best-effort
            self._preview_tree.addTopLevelItem(
                QTreeWidgetItem([f"Decompile failed: {exc}", ""])
            )
            return
        finally:
            QApplication.restoreOverrideCursor()
        template = van_root.get("template", "?")
        top = QTreeWidgetItem([f"<{template}>", ""])
        top.setData(0, _ELEM_ROLE, van_root)
        self._preview_tree.addTopLevelItem(top)
        self._fill_preview(top, van_root)
        top.setExpanded(True)
        self._preview_ctx = (pak, stored, van_root, template)
        self._adopted = {}
        self._mark_mod_fields()
        self._apply_preview_filter(self._preview_filter.text())

    def _fill_preview(self, parent_item: QTreeWidgetItem, elem: ET.Element) -> None:
        for child in elem.findall("Property"):
            name = child.get("name") or "?"
            kids = child.findall("Property")
            value = child.get("value") or ""
            row = QTreeWidgetItem([name, "" if kids else value])
            row.setData(0, _ELEM_ROLE, child)
            parent_item.addChild(row)
            self._preview_rows[id(child)] = row
            if kids:
                self._fill_preview(row, child)

    def _on_preview_context(self, pos) -> None:
        if self._preview_ctx is None:
            return
        elems = self._selected_preview_elems()
        if not elems:
            return
        pak, stored, _van_root, _template = self._preview_ctx
        target = self._mod_combo.currentText()
        mod = next((m for m in self._mods if m.name == target), None)
        if mod is None:
            return
        hit = self._mod_hit_for(mod, pak, stored)
        menu = QMenu(self)
        if hit is not None:
            if len(elems) == 1:
                e = elems[0]
                label = ("Pull whole entry into " + target if e.findall("Property")
                         else f"Pull into {target} (value: {e.get('value')})")
            else:
                label = f"Pull {len(elems)} fields into {target}"
            act = menu.addAction(label)
        else:
            rel = self._exml_rel(stored)
            noun = "this field" if len(elems) == 1 else f"{len(elems)} fields"
            act = menu.addAction(f"Create {rel} in {target} with {noun}")
        chosen = menu.exec(self._preview_tree.viewport().mapToGlobal(pos))
        if chosen is act:
            self._adopt_entries(mod, pak, stored, elems, hit)

    def _mark_mod_fields(self) -> None:
        """Tint preview rows present in the target mod and show their mod value."""
        if self._preview_ctx is None or not self._preview_tree.topLevelItemCount():
            return
        pak, stored, van_root, _template = self._preview_ctx
        mod = next((m for m in self._mods if m.name == self._mod_combo.currentText()), None)
        hit = self._mod_hit_for(mod, pak, stored) if mod is not None else None

        def reset(item: QTreeWidgetItem) -> None:
            for i in range(item.childCount()):
                child = item.child(i)
                child.setForeground(0, QColor("#e8e6ee"))
                child.setForeground(1, QColor("#e8e6ee"))
                child.setText(2, "")
                child.setToolTip(0, "")
                reset(child)

        top = self._preview_tree.topLevelItem(0)
        if top is not None:
            reset(top)
        if hit is None or mod is None:
            return
        data = _read_mod_data(mod, hit.entry)
        work = repo.cache_dir() / "_inspectwork"
        work.mkdir(parents=True, exist_ok=True)
        mod_root = _parse_mod_tree(hit.entry, data, work, self._log)

        def walk(van_elem: ET.Element, mod_elem: ET.Element) -> None:
            mod_children = list(mod_elem.findall("Property"))
            consumed: set[int] = set()
            by_id = {c.get("_id"): c for c in mod_children if c.get("_id") is not None}
            by_index = {c.get("_index"): c for c in mod_children if c.get("_index") is not None}
            for vc in van_elem.findall("Property"):
                mc = None
                if vc.get("_id") is not None:
                    mc = by_id.get(vc.get("_id"))
                elif vc.get("_index") is not None:
                    mc = by_index.get(vc.get("_index"))
                else:
                    for c in mod_children:
                        if c.get("name") == vc.get("name") and id(c) not in consumed:
                            consumed.add(id(c))
                            mc = c
                            break
                if mc is not None:
                    row = self._preview_rows.get(id(vc))
                    if row is not None:
                        row.setForeground(0, _COVERED_COLOR)
                        row.setForeground(1, _COVERED_COLOR)
                        row.setText(2, mc.get("value") or "")
                        row.setForeground(2, _COVERED_COLOR)
                        row.setToolTip(0, f"Present in {mod.name}")
                    walk(vc, mc)

        walk(van_root, mod_root)
        # session adoptions override the generic mark
        for eid, action in self._adopted.items():
            row = self._preview_rows.get(eid)
            if row is not None:
                row.setForeground(0, _COVERED_COLOR)
                row.setForeground(1, _COVERED_COLOR)
                row.setToolTip(0, f"Adopted into {mod.name} ({action})" if mod else "")

    def _preview_mode(self, tree: bool) -> None:
        """Toggle the preview pane between the field tree and the image label."""
        self._preview_tree.setVisible(tree)
        self._preview_filter.setVisible(tree)
        self._preview_find_label.setVisible(tree)
        self._preview_image.setVisible(not tree)

    def _show_dds_preview(self, pak: str, stored: str) -> None:
        """Render a DDS entry as a scaled thumbnail (Pillow decodes BC formats)."""
        self._preview_mode(tree=False)
        self._preview_tree.clear()
        self._preview_ctx = None
        try:
            with paklib.Pak(Path(self._pcbanks) / pak) as pk:
                data = pk.read_file(stored)
            import io

            from PIL import Image
            img = Image.open(io.BytesIO(data))
            w, h = img.size
            img.thumbnail((460, 520))
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            qimg = QImage(img.tobytes("raw", "RGBA"), img.width, img.height,
                          4 * img.width, QImage.Format.Format_RGBA8888)
            self._preview_image.setPixmap(QPixmap.fromImage(qimg))
            self._preview_title.setText(f"Preview: {stored}  ({w}x{h})")
        except Exception as exc:  # noqa: BLE001 - thumbnails are best-effort
            self._preview_image.setText(
                f"(no thumbnail for this DDS format: {exc})\n"
                "Pull it to edit externally."
            )
            self._preview_title.setText(f"Preview: {stored}")

    def _selected_preview_elems(self) -> list[ET.Element]:
        """Selected preview elements, topmost-only (children of a selected ancestor drop)."""
        elems = [it.data(0, _ELEM_ROLE) for it in self._preview_tree.selectedItems()]
        elems = [e for e in elems if e is not None]
        out = []
        for e in elems:
            ancestors = {id(c) for c in e.iter() if c is not e}
            if not any(id(other) in ancestors for other in elems if other is not e):
                out.append(e)
        return out

    @staticmethod
    def _exml_rel(stored: str) -> str:
        """Mod-side rel path for a new EXML created from a pak entry."""
        return stored[:-len(".mbin")] + ".EXML" if stored.lower().endswith(".mbin") else stored

    def _mod_hit_for(self, mod, pak: str, stored: str):
        """The mod's game-file hit for a pak entry, or None when the mod lacks it."""
        for hit in getattr(mod, "game_files", None) or []:
            if (getattr(hit, "pak", None) == pak
                    and (getattr(hit, "stored", "") or "").lower() == stored.lower()):
                return hit
        return None

    # --- pull ---------------------------------------------------------------------

    def _adopt_entries(self, mod, pak: str, stored: str, elems: list, hit) -> None:
        """Adopt preview-selected vanilla fields into the mod's file (or create it)."""
        van_root = self._preview_ctx[2]
        template = self._preview_ctx[3]
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        fail = None
        adopted = updated = 0
        try:
            work = repo.cache_dir() / "_inspectwork"
            work.mkdir(parents=True, exist_ok=True)
            if hit is not None:
                data = _read_mod_data(mod, hit.entry)
                mod_root = _parse_mod_tree(hit.entry, data, work, self._log)
                rel = hit.entry
            else:
                mod_root = ET.Element("Data", {"template": template})
                rel = self._exml_rel(stored)
            for e in elems:
                _elem, action = merge_mods.adopt_vanilla_field(
                    mod_root, van_root, e, update=hit is not None
                )
                if action == "updated":
                    updated += 1
                else:
                    adopted += 1
                row = self._preview_rows.get(id(e))
                if row is not None:
                    row.setForeground(0, _COVERED_COLOR)
                    row.setForeground(1, _COVERED_COLOR)
                    row.setToolTip(0, f"Adopted into {mod.name} ({action})")
                self._adopted[id(e)] = action
            if rel.lower().endswith(".mbin"):
                # Existing mod file is a compiled MBIN: write back in its format.
                data = merge_mods.merged_to_mbin(mod_root, work, "inspectwork")
            else:
                data = merge_mods.merged_mxml_bytes(mod_root)
            self._write_into_mod(mod, rel, data)
        except Exception as exc:  # noqa: BLE001 - report and keep the dialog alive
            fail = exc
        finally:
            QApplication.restoreOverrideCursor()
        if fail is not None:
            QMessageBox.warning(self, "Pak inspector", f"Pull failed: {fail}")
            return
        self.pulled = True
        self._settings.setValue("pak_inspector_last_mod", mod.name)
        receipt = f"Adopted {adopted + updated} field(s) into {mod.name} ({rel})"
        if updated:
            receipt += f" - {adopted} added, {updated} updated to vanilla"
        self._status.setText(receipt)
        self._log(f"adopted {adopted + updated} field(s) into {mod.name}: {rel}")
        self._refresh_coverage(pak, stored)

    def _selected_entries(self) -> list[tuple[str, str]]:
        out = []
        for item in self._tree.selectedItems():
            kind = item.data(0, _ROLE)
            if kind and kind[0] == "file":
                out.append((kind[1], kind[2]))
        return out

    def _files_under(self, item: QTreeWidgetItem) -> list[tuple[str, str]]:
        """Every file under one node: itself, or the whole prefix for dir/pak nodes."""
        kind = item.data(0, _ROLE)
        if not kind:
            return []
        if kind[0] == "file":
            return [(kind[1], kind[2])]
        pak = kind[1]
        prefix = "" if kind[0] == "pak" else kind[2]
        plen = len(prefix)
        return [
            (pak, s) for s in self._pak_files(pak)
            if s.lower().startswith(prefix.lower()) and len(s) > plen
        ]

    def _on_tree_context(self, pos) -> None:
        item = self._tree.itemAt(pos)
        if item is None:
            return
        entries = []
        seen = set()
        for sel in self._tree.selectedItems():
            for pak, stored in self._files_under(sel):
                key = (pak, stored)
                if key not in seen:
                    seen.add(key)
                    entries.append(key)
        if not entries:
            return
        target = self._mod_combo.currentText() or "(no mod selected)"
        menu = QMenu(self)
        act_pull = menu.addAction(f"Pull {len(entries)} file(s) into {target}")
        menu.addSeparator()
        act_copy = menu.addAction(
            f"Copy {len(entries)} path(s) to clipboard"
            if len(entries) > 1 else "Copy path to clipboard"
        )
        chosen = menu.exec(self._tree.viewport().mapToGlobal(pos))
        if chosen is act_pull:
            self._pull_entries(entries, target)
        elif chosen is act_copy:
            paths = "\n".join(sorted(stored.replace("\\", "/") for _pak, stored in entries))
            QApplication.clipboard().setText(paths)

    def _on_pull(self) -> None:
        entries = self._selected_entries()
        if not entries:
            QMessageBox.information(self, "Pak inspector", "Select file(s) first.")
            return
        self._pull_entries(entries, self._mod_combo.currentText())

    def _pull_entries(self, entries: list[tuple[str, str]], target: str) -> None:
        mod = next((m for m in self._mods if m.name == target), None)
        if mod is None:
            QMessageBox.warning(self, "Pak inspector", "Pick a target mod first.")
            return
        if len(entries) > _BIG_PULL:
            answer = QMessageBox.question(
                self, "Pak inspector",
                f"Pull {len(entries)} file(s) into {target}? This may take a while.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._compiler = self._compiler or mbincompiler.ensure_compiler()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        done = failed = 0
        try:
            for i, (pak, stored) in enumerate(entries, 1):
                self._status.setText(f"Pulling {i}/{len(entries)}: {stored}")
                QApplication.processEvents()
                try:
                    rel, data = self._pull_content(pak, stored)
                    self._write_into_mod(mod, rel, data)
                    done += 1
                except Exception as exc:  # noqa: BLE001 - report per-file, keep going
                    failed += 1
                    self._log_lines.append(f"Pak inspector: {stored} ({pak}): {exc}")
        finally:
            QApplication.restoreOverrideCursor()
            self._update_status()
        if done:
            self.pulled = True
            self._settings.setValue("pak_inspector_last_mod", target)
        msg = f"Pulled {done} file(s) into {target}."
        if failed:
            msg += f"\n{failed} failed - see the log."
        QMessageBox.information(self, "Pak inspector", msg)

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

    def _pull_content(self, pak: str, stored: str) -> tuple[str, bytes]:
        """Vanilla content for one entry: decompiled EXML for MBINs, raw bytes else."""
        if stored.lower().endswith(".mbin"):
            mxml_path, _root = self._decompile_van(pak, stored)
            return self._exml_rel(stored), Path(mxml_path).read_bytes()
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
