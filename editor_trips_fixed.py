"""
Trips JSON Editor
==================

This module provides a graphical user interface (GUI) application for
creating and editing a simple travel calendar stored as a JSON file. Each
trip entry includes an identifier (id), date, direction ("ida" or
"volta"), title, passenger capacity, a list of stop cities, and an
optional list of bookings. The editor allows the user to add, modify
and delete trips, manage stops and bookings, and persist changes to
disk.  A calendar view summarises all trips by month and day.

The code is self‑contained and does not depend on any external modules
beyond the Python standard library.  It uses Tkinter for the GUI and
assumes that the underlying Python installation includes support for
Tkinter.  If Tkinter is unavailable the program will exit with a
friendly error.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except Exception as e:
    # Abort early if Tk cannot be imported.  Running without GUI
    # support is pointless for this application.
    import sys

    sys.stderr.write(
        "Erro: Tkinter (_tkinter) não está disponível nesta instalação do Python.\n\n"
        "Você está usando um Python que foi compilado/instalado sem suporte a Tk.\n"
        "Para corrigir no macOS (pyenv):\n"
        "  1) brew install tcl-tk\n"
        "  2) Reinstale o Python via pyenv com suporte a Tcl/Tk.\n\n"
        "Alternativas rápidas:\n"
        "  • Use o Python do instalador oficial (python.org), que já vem com Tkinter.\n"
        "  • Ou use /usr/bin/python3 (se tiver tkinter).\n\n"
        f"Detalhes do erro: {e}\n"
    )
    raise SystemExit(1)

# Regular expression to validate ISO date strings (YYYY‑MM‑DD)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Path to remember the last opened JSON file
LAST_FILE_PATH = os.path.join(os.path.expanduser("~"), ".trips_editor_last_json.txt")


def load_last_json_path() -> Optional[str]:
    """Return the last opened JSON path stored on disk, or None if not set."""
    try:
        if os.path.exists(LAST_FILE_PATH):
            with open(LAST_FILE_PATH, "r", encoding="utf-8") as f:
                path = f.read().strip()
                return path or None
    except Exception:
        pass
    return None


def save_last_json_path(path: str) -> None:
    """Persist the provided path to disk as the last opened JSON."""
    try:
        with open(LAST_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(path)
    except Exception:
        pass


def safe_load_json(path: str) -> Dict[str, Any]:
    """Load a JSON file and ensure the expected structure is present."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "trips" not in data or not isinstance(data["trips"], list):
        raise ValueError('JSON inválido. Esperado: { "trips": [ ... ] }')
    return data


def safe_save_json(path: str, data: Dict[str, Any]) -> None:
    """Write the JSON data to disk in a stable, human‑readable format."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def make_backup(path: str, max_backups: int = 10) -> None:
    """
    Create a timestamped backup of the JSON file in a sibling directory called
    'backups_trips'.  Only the last ``max_backups`` backups are kept.
    """
    try:
        if not path or not os.path.exists(path):
            return
        folder = os.path.join(os.path.dirname(path), "backups_trips")
        os.makedirs(folder, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.basename(path)
        dst = os.path.join(folder, f"{base}.{ts}.bak")
        shutil.copy2(path, dst)
        backups = sorted([
            p for p in os.listdir(folder)
            if p.startswith(base + ".") and p.endswith(".bak")
        ])
        if len(backups) > max_backups:
            for old in backups[: len(backups) - max_backups]:
                try:
                    os.remove(os.path.join(folder, old))
                except Exception:
                    pass
    except Exception:
        pass


def _slugify(text: str) -> str:
    """
    Convert an arbitrary string into a slug suitable for file or identifier
    components: lowercase, ASCII only, separated by hyphens.  This helper
    removes accents and collapses repeated non‑alphanumeric characters.
    """
    normalized = unicodedata.normalize("NFKD", text)
    no_accents = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    clean = no_accents.lower().strip()
    clean = re.sub(r"[^a-z0-9]+", "-", clean)
    clean = re.sub(r"-+", "-", clean).strip("-")
    return clean


class TripsEditorApp(tk.Tk):
    """
    Main application window.  Provides methods for loading, editing and saving
    trip data, as well as a simple calendar view and booking management.
    """

    def __init__(self) -> None:
        super().__init__()
        self.title("Editor de trips.json")

        # Data model: a dictionary containing a list of trips.  Each trip is a
        # dictionary with keys id, date, direction, title, capacity, stops and
        # bookings.  Bookings is a list of dicts with keys name, from and to.
        self.data: Dict[str, Any] = {"trips": []}
        self.file_path: Optional[str] = None
        self.current_index: Optional[int] = None
        self.dirty: bool = False
        self._undo_stack: List[Dict[str, Any]] = []
        self._redo_stack: List[Dict[str, Any]] = []
        self._undo_max: int = 50
        self._publishing: bool = False
        self._log_lines_max: int = 400

        # UI state variables
        self.var_id = tk.StringVar()
        self.var_date = tk.StringVar()
        self.var_direction = tk.StringVar()
        self.var_title = tk.StringVar()
        self.var_capacity = tk.StringVar()
        self.var_stop_new = tk.StringVar()
        self.var_year = tk.StringVar()
        self.var_month = tk.StringVar()
        self.var_b_name = tk.StringVar()
        self.var_b_from = tk.StringVar()
        self.var_b_to = tk.StringVar()

        # Build UI
        self._setup_theme()
        self._build_ui()
        self._bind_shortcuts()

        # Try to auto‑open the last JSON file used (if present)
        self._auto_open_last_json()
        self.refresh_ui()
        self._update_dirty_ui()

    # -------------------------------------------------------------------------
    # Helper methods

    def _snapshot(self) -> Dict[str, Any]:
        """Return a deep copy of the current data for undo/redo."""
        return json.loads(json.dumps(self.data, ensure_ascii=False))

    def _push_undo(self) -> None:
        """Push the current state onto the undo stack and clear redo."""
        try:
            self._undo_stack.append(self._snapshot())
            if len(self._undo_stack) > self._undo_max:
                self._undo_stack = self._undo_stack[-self._undo_max:]
            self._redo_stack.clear()
        except Exception:
            pass

    def _set_status(self, msg: str) -> None:
        """Update the status label with a short message."""
        try:
            self.lbl_status.configure(text=msg)
        except Exception:
            pass

    def _clear_validation(self) -> None:
        """Clear any validation error message from the UI."""
        try:
            self.lbl_validation.configure(text="")
        except Exception:
            pass

    def _validation_error(self, message: str, focus_widget: Optional[tk.Widget] = None) -> None:
        """Display a validation error message and optionally focus a widget."""
        self._clear_validation()
        try:
            self.lbl_validation.configure(text=message)
        except Exception:
            pass
        if focus_widget is not None:
            try:
                focus_widget.focus_set()
            except Exception:
                pass

    def _append_log(self, text: str) -> None:
        """
        Append a line to the log text box with a timestamp.  Keeps only the
        last ``_log_lines_max`` lines.
        """
        if not hasattr(self, "txt_log"):
            return
        try:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            msg = f"[{ts}] {text}\n"
            self.txt_log.configure(state=tk.NORMAL)
            self.txt_log.insert(tk.END, msg)
            lines = int(self.txt_log.index('end-1c').split('.')[0])
            if lines > self._log_lines_max:
                self.txt_log.delete('1.0', f"{lines - self._log_lines_max}.0")
            self.txt_log.see(tk.END)
            self.txt_log.configure(state=tk.DISABLED)
        except Exception:
            pass

    def _clear_log(self) -> None:
        """Clear all messages from the log text box."""
        if not hasattr(self, "txt_log"):
            return
        try:
            self.txt_log.configure(state=tk.NORMAL)
            self.txt_log.delete('1.0', tk.END)
            self.txt_log.configure(state=tk.DISABLED)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Theme and UI setup

    def _setup_theme(self) -> None:
        """Configure Tk styles and default fonts."""
        style = ttk.Style(self)
        try:
            if "clam" in style.theme_names():
                style.theme_use("clam")
        except Exception:
            pass
        # Define some compact fonts
        try:
            default_font = ("Helvetica", 10)
            small_font = ("Helvetica", 9)
            bold_font = ("Helvetica", 10, "bold")
            self.option_add("*Font", default_font)
            style.configure("TLabel", font=default_font)
            style.configure("TButton", font=default_font, padding=(6, 3))
            style.configure("TEntry", font=default_font, padding=(4, 2))
            style.configure("TCombobox", font=default_font, padding=(4, 2))
            style.configure("Treeview", font=small_font, rowheight=22)
            style.configure("Treeview.Heading", font=bold_font)
            style.configure("TLabelframe.Label", font=bold_font)
        except Exception:
            pass

    def _build_ui(self) -> None:
        """Create and arrange all widgets in the application."""
        # Top bar with file info and status
        top = ttk.Frame(self, padding=10)
        top.pack(side=tk.TOP, fill=tk.X)
        self.lbl_file = ttk.Label(top, text="Arquivo: (nenhum)")
        self.lbl_file.pack(side=tk.LEFT)
        self.lbl_status = ttk.Label(top, text="", foreground="#555")
        self.lbl_status.pack(side=tk.LEFT, padx=(12, 0))
        btns = ttk.Frame(top)
        btns.pack(side=tk.RIGHT)
        ttk.Button(btns, text="Abrir…", command=self.open_file).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Abrir último", command=self._auto_open_last_json).pack(side=tk.LEFT, padx=4)
        self.btn_save = ttk.Button(btns, text="Salvar", command=self.save_file)
        self.btn_save.pack(side=tk.LEFT, padx=4)
        self.btn_save_as = ttk.Button(btns, text="Salvar como…", command=self.save_file_as)
        self.btn_save_as.pack(side=tk.LEFT, padx=4)
        self.btn_publish = ttk.Button(btns, text="Publicar no GitHub", command=self.publish_to_github)
        self.btn_publish.pack(side=tk.LEFT, padx=4)

        # Main pane with left and right sections
        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        # Left panel: list and calendar
        left = ttk.Frame(main, padding=10)
        main.add(left, weight=1)
        ttk.Label(left, text="Viagens").pack(anchor="w")
        self.nb_left = ttk.Notebook(left)
        self.nb_left.pack(fill=tk.BOTH, expand=True, pady=(6, 8))
        tab_list = ttk.Frame(self.nb_left)
        tab_cal = ttk.Frame(self.nb_left)
        self.nb_left.add(tab_list, text="Lista")
        self.nb_left.add(tab_cal, text="Calendário")
        # List tab
        self.listbox = tk.Listbox(tab_list, height=20)
        self.listbox.pack(fill=tk.BOTH, expand=True)
        self.listbox.bind("<<ListboxSelect>>", self.on_select_trip)
        # Calendar tab: year selector and month buttons
        cal_top = ttk.Frame(tab_cal, padding=(0, 0, 0, 6))
        cal_top.pack(fill=tk.X)
        ttk.Label(cal_top, text="Ano:").pack(side=tk.LEFT)
        self.cmb_year = ttk.Combobox(cal_top, textvariable=self.var_year, values=[], state="readonly", width=6)
        self.cmb_year.pack(side=tk.LEFT, padx=(6, 12))
        self.cmb_year.bind("<<ComboboxSelected>>", self.on_select_month)
        months_frame = ttk.Frame(tab_cal)
        months_frame.pack(fill=tk.X, pady=(0, 6))
        self._month_btns: Dict[int, ttk.Button] = {}
        month_labels = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
        for i, lab in enumerate(month_labels, start=1):
            r = (i - 1) // 3
            c = (i - 1) % 3
            btn = ttk.Button(months_frame, text=lab, width=4, command=lambda m=i: self._select_month_button(m))
            btn.grid(row=r, column=c, padx=1, pady=1, sticky="we")
            self._month_btns[i] = btn
        for c in range(3):
            months_frame.grid_columnconfigure(c, weight=1)
        self.cal_tree = ttk.Treeview(tab_cal, columns=("date", "trips"), show="headings", height=18)
        self.cal_tree.heading("date", text="Data")
        self.cal_tree.heading("trips", text="Viagens")
        self.cal_tree.column("date", width=110, anchor="w")
        self.cal_tree.column("trips", width=360, anchor="w")
        self.cal_tree.pack(fill=tk.BOTH, expand=True)
        self.cal_tree.bind("<<TreeviewSelect>>", self.on_select_calendar_row)
        # Left action buttons
        left_btns = ttk.Frame(left)
        left_btns.pack(fill=tk.X)
        self.btn_new = ttk.Button(left_btns, text="Nova viagem", command=self.new_trip)
        self.btn_new.pack(side=tk.LEFT, padx=3)
        self.btn_new_tpl = ttk.Button(left_btns, text="Nova (template)", command=self.new_trip_template)
        self.btn_new_tpl.pack(side=tk.LEFT, padx=3)
        self.btn_dup = ttk.Button(left_btns, text="Duplicar", command=self.duplicate_trip)
        self.btn_dup.pack(side=tk.LEFT, padx=3)
        self.btn_del = ttk.Button(left_btns, text="Remover", command=self.delete_trip)
        self.btn_del.pack(side=tk.LEFT, padx=3)

        # Right panel: trip editor
        right = ttk.Frame(main, padding=10)
        main.add(right, weight=3)
        form = ttk.Frame(right)
        form.pack(fill=tk.X)
        # Helper to place labelled widgets in two columns
        def add_row(r: int, label: str, widget: tk.Widget) -> None:
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", padx=(0, 10), pady=6)
            widget.grid(row=r, column=1, sticky="we", pady=6)
        id_row = ttk.Frame(form)
        self.ent_id = ttk.Entry(id_row, textvariable=self.var_id)
        self.ent_id.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(id_row, text="Gerar", command=self.generate_id).pack(side=tk.LEFT, padx=(6, 0))
        add_row(0, "id", id_row)
        self.ent_date = ttk.Entry(form, textvariable=self.var_date)
        add_row(1, "date (YYYY-MM-DD)", self.ent_date)
        self.cmb_direction = ttk.Combobox(form, textvariable=self.var_direction, values=["ida", "volta"], state="readonly")
        add_row(2, "direction", self.cmb_direction)
        add_row(3, "title", ttk.Entry(form, textvariable=self.var_title))
        self.ent_capacity = ttk.Entry(form, textvariable=self.var_capacity)
        add_row(4, "capacity", self.ent_capacity)
        self.lbl_validation = ttk.Label(right, text="", foreground="#b00020")
        self.lbl_validation.pack(anchor="w", pady=(2, 6))
        # Stops editor
        stops_container = ttk.Frame(form)
        stops_top = ttk.Frame(stops_container)
        stops_top.pack(fill=tk.X)
        self.cmb_stop_new = ttk.Combobox(stops_top, textvariable=self.var_stop_new, values=[
            "Paulo Afonso-BA",
            "Petrolândia-PE",
            "Floresta-PE",
            "Cabrobó-PE",
            "Salgueiro-PE",
            "Juazeiro do Norte-CE",
            "Brejo Santo-CE",
        ])
        self.cmb_stop_new.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(stops_top, text="Adicionar", command=self.add_stop).pack(side=tk.LEFT, padx=6)
        stops_mid = ttk.Frame(stops_container)
        stops_mid.pack(fill=tk.X, pady=(6, 0))
        self.stops_listbox = tk.Listbox(stops_mid, height=4)
        self.stops_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        stops_scroll = ttk.Scrollbar(stops_mid, orient="vertical", command=self.stops_listbox.yview)
        self.stops_listbox.configure(yscrollcommand=stops_scroll.set)
        stops_scroll.pack(side=tk.LEFT, fill=tk.Y, padx=(6, 0))
        stops_btns = ttk.Frame(stops_mid)
        stops_btns.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(stops_btns, text="↑", width=3, command=self.move_stop_up).pack(fill=tk.X, pady=1)
        ttk.Button(stops_btns, text="↓", width=3, command=self.move_stop_down).pack(fill=tk.X, pady=1)
        ttk.Button(stops_btns, text="Remover", command=self.remove_stop).pack(fill=tk.X, pady=1)
        add_row(5, "Paradas (ordem)", stops_container)
        # Segment occupancy view
        ttk.Label(right, text="Vagas por trecho").pack(anchor="w", pady=(8, 0))
        seg_frame = ttk.Frame(right)
        seg_frame.pack(fill=tk.BOTH, expand=False, pady=(6, 0))
        self.seg_tree = ttk.Treeview(seg_frame, columns=("from", "to", "used", "free"), show="headings", height=6)
        self.seg_tree.heading("from", text="De")
        self.seg_tree.heading("to", text="Para")
        self.seg_tree.heading("used", text="Ocupadas")
        self.seg_tree.heading("free", text="Livres")
        self.seg_tree.column("from", width=180, anchor="w")
        self.seg_tree.column("to", width=180, anchor="w")
        self.seg_tree.column("used", width=80, anchor="center")
        self.seg_tree.column("free", width=70, anchor="center")
        self.seg_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        seg_scroll = ttk.Scrollbar(seg_frame, orient="vertical", command=self.seg_tree.yview)
        self.seg_tree.configure(yscrollcommand=seg_scroll.set)
        seg_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        # Bookings section
        ttk.Separator(right).pack(fill=tk.X, pady=12)
        ttk.Label(right, text="Reservas (bookings)").pack(anchor="w")
        bookings_frame = ttk.Frame(right)
        bookings_frame.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.bookings = ttk.Treeview(bookings_frame, columns=("name", "from", "to"), show="headings", height=10)
        self.bookings.heading("name", text="Nome")
        self.bookings.heading("from", text="De")
        self.bookings.heading("to", text="Para")
        self.bookings.column("name", width=160, anchor="w")
        self.bookings.column("from", width=180, anchor="w")
        self.bookings.column("to", width=180, anchor="w")
        self.bookings.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(bookings_frame, orient="vertical", command=self.bookings.yview)
        self.bookings.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        # Booking editor
        booking_edit = ttk.Frame(right)
        booking_edit.pack(fill=tk.X, pady=8)
        ttk.Label(booking_edit, text="Nome").grid(row=0, column=0, sticky="w")
        ttk.Entry(booking_edit, textvariable=self.var_b_name).grid(row=0, column=1, sticky="we", padx=6)
        ttk.Label(booking_edit, text="De").grid(row=0, column=2, sticky="w")
        ttk.Combobox(booking_edit, textvariable=self.var_b_from, values=[
            "Paulo Afonso-BA",
            "Petrolândia-PE",
            "Floresta-PE",
            "Cabrobó-PE",
            "Salgueiro-PE",
            "Juazeiro do Norte-CE",
            "Brejo Santo-CE",
        ]).grid(row=0, column=3, sticky="we", padx=6)
        ttk.Label(booking_edit, text="Para").grid(row=0, column=4, sticky="w")
        ttk.Combobox(booking_edit, textvariable=self.var_b_to, values=[
            "Paulo Afonso-BA",
            "Petrolândia-PE",
            "Floresta-PE",
            "Cabrobó-PE",
            "Salgueiro-PE",
            "Juazeiro do Norte-CE",
            "Brejo Santo-CE",
        ]).grid(row=0, column=5, sticky="we", padx=6)
        booking_edit.grid_columnconfigure(1, weight=1)
        booking_edit.grid_columnconfigure(3, weight=1)
        booking_edit.grid_columnconfigure(5, weight=1)
        bbtns = ttk.Frame(right)
        bbtns.pack(fill=tk.X)
        self.btn_b_add = ttk.Button(bbtns, text="Adicionar reserva", command=self.add_booking)
        self.btn_b_add.pack(side=tk.LEFT, padx=3)
        self.btn_b_upd = ttk.Button(bbtns, text="Atualizar seleção", command=self.update_booking)
        self.btn_b_upd.pack(side=tk.LEFT, padx=3)
        self.btn_b_del = ttk.Button(bbtns, text="Remover seleção", command=self.remove_booking)
        self.btn_b_del.pack(side=tk.LEFT, padx=3)
        self.bookings.bind("<<TreeviewSelect>>", self.on_select_booking)
        ttk.Separator(right).pack(fill=tk.X, pady=10)
        self.btn_apply = ttk.Button(right, text="Aplicar alterações desta viagem", command=self.apply_trip_changes)
        self.btn_apply.pack(anchor="e")
        # Log area
        log_frame = ttk.Labelframe(self, text="Log", padding=8)
        log_frame.pack(side=tk.BOTTOM, fill=tk.BOTH, padx=8, pady=(0, 8))
        log_btns = ttk.Frame(log_frame)
        log_btns.pack(fill=tk.X)
        ttk.Button(log_btns, text="Limpar log", command=self._clear_log).pack(side=tk.RIGHT)
        self.txt_log = tk.Text(log_frame, height=6, wrap="word", state=tk.DISABLED)
        self.txt_log.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

    def _bind_shortcuts(self) -> None:
        """Bind useful keyboard shortcuts to application actions."""
        self.bind("<Control-s>", lambda e: self.save_file())
        self.bind("<Control-o>", lambda e: self.open_file())
        self.bind("<Control-n>", lambda e: self.new_trip())
        self.bind("<Control-Shift-N>", lambda e: self.new_trip_template())
        self.bind("<Control-d>", lambda e: self.duplicate_trip())
        self.bind("<Control-Return>", lambda e: self.apply_trip_changes())
        self.bind("<Control-z>", lambda e: self.undo())
        self.bind("<Control-y>", lambda e: self.redo())
        self.bind("<Control-Shift-Z>", lambda e: self.redo())
        self.bind("<F11>", lambda e: self.attributes("-fullscreen", not bool(self.attributes("-fullscreen"))))
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))
        # Autocomplete stops: fill with the first matching common city
        try:
            self.cmb_stop_new.bind("<KeyRelease>", self._on_stop_autocomplete)
        except Exception:
            pass
        # Deleting selected trip or booking with Delete/Backspace
        try:
            self.listbox.bind("<Delete>", lambda e: self.delete_trip())
            self.listbox.bind("<BackSpace>", lambda e: self.delete_trip())
        except Exception:
            pass
        try:
            self.bookings.bind("<Delete>", lambda e: self.remove_booking())
            self.bookings.bind("<BackSpace>", lambda e: self.remove_booking())
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Trip and stop management

    def undo(self) -> None:
        """Restore the previous state from the undo stack."""
        if not self._undo_stack:
            self._set_status("Nada para desfazer")
            return
        self._redo_stack.append(self._snapshot())
        self.data = self._undo_stack.pop()
        self.dirty = True
        self.refresh_ui()
        self._set_status("Desfazer ✅")

    def redo(self) -> None:
        """Reapply a state from the redo stack."""
        if not self._redo_stack:
            self._set_status("Nada para refazer")
            return
        self._undo_stack.append(self._snapshot())
        self.data = self._redo_stack.pop()
        self.dirty = True
        self.refresh_ui()
        self._set_status("Refazer ✅")

    def new_trip(self) -> None:
        """Append an empty trip to the list and select it for editing."""
        self._push_undo()
        trip = {
            "id": "",
            "date": "",
            "direction": "ida",
            "title": "",
            "capacity": 3,
            "stops": [],
            "bookings": [],
        }
        self.data["trips"].append(trip)
        self.dirty = True
        self._update_dirty_ui()
        self.refresh_ui()
        self.current_index = len(self.data["trips"]) - 1
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(self.current_index)
        self.listbox.see(self.current_index)
        self._load_trip_into_form(trip)

    def new_trip_template(self) -> None:
        """
        Create a new trip based on a template (standard routes).  The user
        chooses between "ida" (outbound) and "volta" (return) and whether
        optional cities should be included.
        """
        self._push_undo()
        choice = messagebox.askyesnocancel(
            "Template",
            "Criar viagem a partir de template?\n\n"
            "SIM → Template de IDA\n"
            "NÃO → Template de VOLTA\n"
            "CANCELAR → Não criar",
        )
        if choice is None:
            return
        direction = "ida" if choice else "volta"
        extra = messagebox.askyesno(
            "Template",
            "Incluir cidades opcionais na rota?\n\n"
            "• Juazeiro do Norte-CE\n"
            "• Brejo Santo-CE",
        )
        base_route = [
            "Paulo Afonso-BA",
            "Petrolândia-PE",
            "Floresta-PE",
            "Cabrobó-PE",
            "Salgueiro-PE",
        ]
        optional = ["Juazeiro do Norte-CE", "Brejo Santo-CE"] if extra else []
        if direction == "ida":
            stops = base_route + optional
        else:
            stops = list(reversed(base_route + optional))
        trip = {
            "id": "",
            "date": "",
            "direction": direction,
            "title": "",
            "capacity": 3,
            "stops": stops,
            "bookings": [],
        }
        self.data["trips"].append(trip)
        self.dirty = True
        self._update_dirty_ui()
        self.refresh_ui()
        self.current_index = len(self.data["trips"]) - 1
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(self.current_index)
        self.listbox.see(self.current_index)
        self._load_trip_into_form(trip)

    def duplicate_trip(self) -> None:
        """Duplicate the currently selected trip, if any."""
        self._push_undo()
        if self.current_index is None:
            messagebox.showwarning("Selecione", "Selecione uma viagem para duplicar.")
            return
        src = self.data["trips"][self.current_index]
        dup = json.loads(json.dumps(src))
        dup_id = (dup.get("id", "") + "-copy").strip("-")
        dup["id"] = dup_id
        self.data["trips"].append(dup)
        self.dirty = True
        self._update_dirty_ui()
        self.refresh_ui()

    def delete_trip(self) -> None:
        """Remove the currently selected trip."""
        self._push_undo()
        if self.current_index is None:
            messagebox.showwarning("Selecione", "Selecione uma viagem para remover.")
            return
        trip = self.data["trips"][self.current_index]
        if not messagebox.askyesno("Confirmar", f"Remover a viagem:\n{make_trip_label(trip)} ?"):
            return
        del self.data["trips"][self.current_index]
        self.current_index = None
        self.dirty = True
        self._update_dirty_ui()
        self.refresh_ui()

    def generate_id(self) -> None:
        """
        Generate a unique trip identifier based on the date, direction and the
        first and last stops.  Updates the id entry field and marks the
        document as dirty.
        """
        date = (self.var_date.get() or "").strip()
        direction = (self.var_direction.get() or "").strip()
        stops = [self.stops_listbox.get(i).strip() for i in range(self.stops_listbox.size()) if self.stops_listbox.get(i).strip()]
        if not DATE_RE.match(date):
            self._validation_error("Preencha a data válida (YYYY-MM-DD) para gerar o id.", self.ent_date)
            return
        if direction not in ("ida", "volta"):
            self._validation_error("Selecione direção (ida/volta) para gerar o id.", self.cmb_direction)
            return
        if len(stops) < 2:
            self._validation_error("Adicione pelo menos 2 paradas para gerar o id.")
            return
        a = _slugify(stops[0])
        b = _slugify(stops[-1])
        base = f"{date}_{direction}_{a}-{b}"
        if len(base) > 60:
            base = base[:60].rstrip("-")
        new_id = self._generate_unique_id(base)
        self.var_id.set(new_id)
        self.dirty = True
        self._update_dirty_ui()
        self._set_status("ID gerado")

    def _generate_unique_id(self, base_id: str) -> str:
        """Return a unique id by appending a suffix if necessary."""
        base_id = base_id.strip()
        existing = {str(t.get("id", "")) for t in self.data.get("trips", []) if isinstance(t, dict)}
        if base_id and base_id not in existing:
            return base_id
        n = 2
        while True:
            candidate = f"{base_id}_{n:02d}"
            if candidate not in existing:
                return candidate
            n += 1

    def _load_trip_into_form(self, trip: Optional[Dict[str, Any]]) -> None:
        """
        Populate the form fields with the contents of ``trip``.  If
        ``trip`` is None, clear all fields.  This method also refreshes
        bookings and segment occupancy.
        """
        if not trip:
            self.var_id.set("")
            self.var_date.set("")
            self.var_direction.set("ida")
            self.var_title.set("")
            self.var_capacity.set("3")
            self.stops_listbox.delete(0, tk.END)
            self._reload_bookings([])
            self._update_controls_state()
            self._refresh_segments_view(None)
            return
        # Set basic fields
        self.var_id.set(trip.get("id", ""))
        self.var_date.set(trip.get("date", ""))
        self.var_direction.set(trip.get("direction", "ida"))
        self.var_title.set(trip.get("title", ""))
        self.var_capacity.set(str(trip.get("capacity", 3)))
        # Populate stops listbox
        self.stops_listbox.delete(0, tk.END)
        stops = trip.get("stops", [])
        if isinstance(stops, list):
            for s in stops:
                s = str(s).strip()
                if s:
                    self.stops_listbox.insert(tk.END, s)
        # Reload bookings and segments
        self._reload_bookings(trip.get("bookings", []))
        self._refresh_segments_view(trip)
        self._update_controls_state()

    def _refresh_segments_view(self, trip: Optional[Dict[str, Any]]) -> None:
        """
        Recompute the number of occupied and free seats for each leg of the
        trip and populate the segment tree view accordingly.  The capacity is
        taken from the trip; if not defined or invalid, zero is assumed.  A
        segment is defined by two consecutive stops.
        """
        try:
            # Clear existing rows
            self.seg_tree.delete(*self.seg_tree.get_children())
        except Exception:
            return
        if not trip:
            return
        # Determine capacity
        try:
            capacity = int(trip.get("capacity", 0) or 0)
        except Exception:
            capacity = 0
        stops = trip.get("stops", [])
        if not isinstance(stops, list) or len(stops) < 2:
            return
        idx_map = {str(s): i for i, s in enumerate(stops)}
        bookings = trip.get("bookings", [])
        if not isinstance(bookings, list):
            bookings = []
        ranges: List[Tuple[int, int]] = []
        for b in bookings:
            try:
                frm = str(b.get("from", ""))
                to = str(b.get("to", ""))
                if frm not in idx_map or to not in idx_map:
                    continue
                i = idx_map[frm]
                j = idx_map[to]
                if i == j:
                    continue
                a, c = (i, j) if i < j else (j, i)
                ranges.append((a, c))
            except Exception:
                continue
        for k in range(len(stops) - 1):
            used = sum(1 for a, c in ranges if a <= k < c)
            free = max(0, capacity - used) if capacity else 0
            self.seg_tree.insert("", tk.END, values=(stops[k], stops[k + 1], used, free))

    def _reload_bookings(self, bookings: List[Dict[str, Any]]) -> None:
        """Populate the bookings tree view from a list of booking dicts."""
        try:
            for iid in self.bookings.get_children():
                self.bookings.delete(iid)
        except Exception:
            return
        if not isinstance(bookings, list):
            return
        for b in bookings:
            try:
                name = str(b.get("name", "")).strip()
                frm = str(b.get("from", "")).strip()
                to = str(b.get("to", "")).strip()
                self.bookings.insert("", tk.END, values=(name, frm, to))
            except Exception:
                continue

    # -------------------------------------------------------------------------
    # Bookings operations

    def on_select_booking(self, _evt: Optional[Any] = None) -> None:
        """
        When the user selects a booking row, populate the booking editor fields.
        If nothing is selected, clear the booking editor fields.
        """
        try:
            sel = self.bookings.selection()
            if not sel:
                self.var_b_name.set("")
                self.var_b_from.set("")
                self.var_b_to.set("")
                self._update_controls_state()
                return
            iid = sel[0]
            vals = self.bookings.item(iid, "values")
            if len(vals) >= 3:
                self.var_b_name.set(vals[0])
                self.var_b_from.set(vals[1])
                self.var_b_to.set(vals[2])
        except Exception:
            pass
        self._update_controls_state()

    def add_booking(self) -> None:
        """Add a new booking to the current trip."""
        self._push_undo()
        if self.current_index is None:
            self._validation_error("Selecione uma viagem para adicionar reserva.")
            return
        name = (self.var_b_name.get() or "").strip()
        frm = (self.var_b_from.get() or "").strip()
        to = (self.var_b_to.get() or "").strip()
        if not name or not frm or not to:
            self._validation_error("Preencha Nome, De e Para para adicionar a reserva.")
            return
        if frm == to:
            self._validation_error("O trecho 'De' e 'Para' não pode ser igual.")
            return
        trip = self.data["trips"][self.current_index]
        trip.setdefault("bookings", [])
        if not isinstance(trip["bookings"], list):
            trip["bookings"] = []
        trip["bookings"].append({"name": name, "from": frm, "to": to})
        self._reload_bookings(trip["bookings"])
        self._refresh_segments_view(trip)
        self.dirty = True
        self._update_dirty_ui()
        self._set_status("Reserva adicionada")

    def update_booking(self) -> None:
        """Update the selected booking with values from the editor fields."""
        self._push_undo()
        if self.current_index is None:
            self._validation_error("Selecione uma viagem para atualizar reserva.")
            return
        sel = self.bookings.selection()
        if not sel:
            self._validation_error("Selecione uma reserva para atualizar.")
            return
        name = (self.var_b_name.get() or "").strip()
        frm = (self.var_b_from.get() or "").strip()
        to = (self.var_b_to.get() or "").strip()
        if not name or not frm or not to:
            self._validation_error("Preencha Nome, De e Para para atualizar a reserva.")
            return
        if frm == to:
            self._validation_error("O trecho 'De' e 'Para' não pode ser igual.")
            return
        trip = self.data["trips"][self.current_index]
        bookings = trip.get("bookings", [])
        if not isinstance(bookings, list):
            bookings = []
            trip["bookings"] = bookings
        iid = sel[0]
        try:
            row_index = self.bookings.index(iid)
        except Exception:
            row_index = -1
        if row_index < 0 or row_index >= len(bookings):
            self._validation_error("Não consegui localizar esta reserva na lista.")
            return
        bookings[row_index] = {"name": name, "from": frm, "to": to}
        self._reload_bookings(bookings)
        self._refresh_segments_view(trip)
        self.dirty = True
        self._update_dirty_ui()
        self._set_status("Reserva atualizada")

    def remove_booking(self) -> None:
        """Remove the selected booking from the current trip."""
        self._push_undo()
        if self.current_index is None:
            self._validation_error("Selecione uma viagem para remover reserva.")
            return
        sel = self.bookings.selection()
        if not sel:
            self._validation_error("Selecione uma reserva para remover.")
            return
        trip = self.data["trips"][self.current_index]
        bookings = trip.get("bookings", [])
        if not isinstance(bookings, list):
            bookings = []
            trip["bookings"] = bookings
        iid = sel[0]
        try:
            row_index = self.bookings.index(iid)
        except Exception:
            row_index = -1
        if row_index < 0 or row_index >= len(bookings):
            self._validation_error("Não consegui localizar esta reserva na lista.")
            return
        del bookings[row_index]
        self._reload_bookings(bookings)
        self._refresh_segments_view(trip)
        self.dirty = True
        self._update_dirty_ui()
        self._set_status("Reserva removida")

    # -------------------------------------------------------------------------
    # Stops operations

    def add_stop(self) -> None:
        """Append a stop from the entry to the list of stops."""
        self._push_undo()
        s = (self.var_stop_new.get() or "").strip()
        if not s:
            return
        self.stops_listbox.insert(tk.END, s)
        self.var_stop_new.set("")
        self.dirty = True
        self._update_dirty_ui()

    def remove_stop(self) -> None:
        """Remove the currently selected stop."""
        self._push_undo()
        sel = self.stops_listbox.curselection()
        if not sel:
            return
        self.stops_listbox.delete(sel[0])
        self.dirty = True
        self._update_dirty_ui()

    def move_stop_up(self) -> None:
        """Move the selected stop up in the list."""
        self._push_undo()
        sel = self.stops_listbox.curselection()
        if not sel or sel[0] == 0:
            return
        i = sel[0]
        val = self.stops_listbox.get(i)
        self.stops_listbox.delete(i)
        self.stops_listbox.insert(i - 1, val)
        self.stops_listbox.selection_set(i - 1)
        self.dirty = True
        self._update_dirty_ui()

    def move_stop_down(self) -> None:
        """Move the selected stop down in the list."""
        self._push_undo()
        sel = self.stops_listbox.curselection()
        if not sel:
            return
        i = sel[0]
        if i >= self.stops_listbox.size() - 1:
            return
        val = self.stops_listbox.get(i)
        self.stops_listbox.delete(i)
        self.stops_listbox.insert(i + 1, val)
        self.stops_listbox.selection_set(i + 1)
        self.dirty = True
        self._update_dirty_ui()

    # -------------------------------------------------------------------------
    # Calendar and list view updates

    def sort_trips(self) -> None:
        """Sort trips by date, direction and id."""
        self.data["trips"].sort(key=lambda t: (t.get("date", ""), t.get("direction", ""), t.get("id", "")))
        self.dirty = True
        self._update_dirty_ui()
        self.refresh_ui()

    def refresh_ui(self) -> None:
        """
        Refresh the listbox and calendar view from the current data.  Also
        updates the year and month selectors and repopulates the calendar tree.
        """
        # Populate listbox
        self.listbox.delete(0, tk.END)
        for trip in self.data.get("trips", []):
            self.listbox.insert(tk.END, make_trip_label(trip))
        # Clear editor if no selection
        if self.current_index is None:
            self._load_trip_into_form(None)
        # Update year and month options
        try:
            months = self._month_options()
            years = sorted({m[:4] for m in months if len(m) >= 7})
            # Update year combobox values
            self.cmb_year.configure(values=years)
            # Determine selected month
            if months:
                cur = (self.var_month.get() or "").strip()
                if cur not in months:
                    cur = months[-1]
                    self.var_month.set(cur)
                sel_year = cur[:4]
                # Set year if invalid
                if not self.var_year.get() or self.var_year.get() not in years:
                    self.var_year.set(sel_year)
                self._populate_calendar(self.var_month.get())
            else:
                self.var_month.set("")
                self.var_year.set("")
                for iid in self.cal_tree.get_children():
                    self.cal_tree.delete(iid)
            self._update_month_buttons_state()
        except Exception:
            pass
        self._update_controls_state()

    def _month_options(self) -> List[str]:
        """Return a sorted list of months (YYYY-MM) that contain trips."""
        months: set[str] = set()
        for t in self.data.get("trips", []):
            d = str(t.get("date", "")).strip()
            if DATE_RE.match(d):
                months.add(d[:7])
        return sorted(months)

    def _populate_calendar(self, month: str) -> None:
        """Populate the calendar view for the given month (YYYY-MM)."""
        for iid in self.cal_tree.get_children():
            self.cal_tree.delete(iid)
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for idx, t in enumerate(self.data.get("trips", [])):
            d = str(t.get("date", "")).strip()
            if DATE_RE.match(d) and d.startswith(month):
                grouped.setdefault(d, []).append({"idx": idx, "trip": t})
        for d in sorted(grouped.keys()):
            labels = []
            for item in grouped[d]:
                trip = item["trip"]
                direction = trip.get("direction", "")
                short = "IDA" if direction == "ida" else ("VOLTA" if direction == "volta" else str(direction))
                title = (trip.get("title", "") or "").strip()
                labels.append(f"{short} {title}".strip())
            iids = ",".join(str(item["idx"]) for item in grouped[d])
            self.cal_tree.insert("", tk.END, iid=iids, values=(d, " | ".join(labels)))

    def on_select_month(self, _evt: Optional[Any] = None) -> None:
        """Update the month selection when the year combobox changes."""
        year = (self.var_year.get() or "").strip()
        cur = (self.var_month.get() or "").strip()
        if year and not DATE_RE.match(cur + "-01"):
            self.var_month.set(f"{year}-01")
        elif year and len(cur) >= 7 and cur[:4].isdigit():
            mm = cur[5:7] if len(cur) >= 7 else "01"
            self.var_month.set(f"{year}-{mm}")
        month = (self.var_month.get() or "").strip()
        if not month:
            return
        self._populate_calendar(month)
        self._update_month_buttons_state()

    def on_select_calendar_row(self, _evt: Optional[Any] = None) -> None:
        """When a calendar row is selected, load the first trip index found."""
        sel = self.cal_tree.selection()
        if not sel:
            return
        iid = sel[0]
        try:
            first = int(str(iid).split(",")[0])
        except Exception:
            return
        if 0 <= first < len(self.data.get("trips", [])):
            self.current_index = first
            self._load_trip_into_form(self.data["trips"][first])
            try:
                self.listbox.selection_clear(0, tk.END)
                self.listbox.selection_set(first)
                self.listbox.see(first)
            except Exception:
                pass
        self._update_controls_state()

    def _update_month_buttons_state(self) -> None:
        """Enable or disable month buttons based on available months."""
        months = self._month_options()
        available = set()
        for m in months:
            if len(m) >= 7 and m[5:7].isdigit():
                available.add((m[:4], int(m[5:7])))
        sel = (self.var_month.get() or "").strip()
        sel_year = sel[:4] if len(sel) >= 7 else ""
        sel_m = int(sel[5:7]) if len(sel) >= 7 and sel[5:7].isdigit() else None
        cur_year = (self.var_year.get() or "").strip() or sel_year
        for mnum, btn in self._month_btns.items():
            state = tk.NORMAL if not cur_year or (cur_year, mnum) in available else tk.DISABLED
            try:
                btn.configure(state=state)
            except Exception:
                pass
            try:
                base = btn.cget("text").strip("[]")
                if sel_m == mnum and sel_year == cur_year and cur_year:
                    btn.configure(text=f"[{base}]")
                else:
                    btn.configure(text=base)
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # File operations

    def open_file(self) -> None:
        """Prompt the user to select a JSON file and load it."""
        if not self.confirm_discard_if_dirty():
            return
        path = filedialog.askopenfilename(title="Abrir trips.json", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            self.data = safe_load_json(path)
        except Exception as e:
            messagebox.showerror("Erro ao abrir", str(e))
            return
        self.file_path = path
        save_last_json_path(path)
        self.lbl_file.configure(text=f"Arquivo: {path}")
        self.current_index = None
        self.dirty = False
        self._update_dirty_ui()
        self._update_controls_state()
        self.refresh_ui()

    def save_file(self) -> None:
        """Save the current JSON file.  If no file is open, ask for a path."""
        if self.file_path is None:
            self.save_file_as()
            return
        try:
            make_backup(self.file_path, max_backups=10)
            safe_save_json(self.file_path, self.data)
            self.dirty = False
            self._update_dirty_ui()
            self._append_log("Arquivo salvo ✅")
            self._set_status("Salvo")
        except Exception as e:
            messagebox.showerror("Erro ao salvar", str(e))

    def save_file_as(self) -> None:
        """Ask for a filename and save the JSON to that location."""
        path = filedialog.asksaveasfilename(
            title="Salvar como", defaultextension=".json", filetypes=[("JSON", "*.json"), ("All files", "*.*")]
        )
        if not path:
            return
        self.file_path = path
        save_last_json_path(path)
        self.lbl_file.configure(text=f"Arquivo: {path}")
        self.save_file()

    def confirm_discard_if_dirty(self) -> bool:
        """Ask the user to discard unsaved changes if the document is dirty."""
        if not self.dirty:
            return True
        return messagebox.askyesno("Alterações não salvas", "Você tem alterações não salvas. Deseja descartá-las?")

    def _auto_open_last_json(self) -> None:
        """Attempt to automatically open the last used JSON file."""
        try:
            last = load_last_json_path()
            if not last or not os.path.exists(last) or self.file_path:
                return
            self.data = safe_load_json(last)
            self.file_path = last
            self.current_index = None
            self.dirty = False
            self._update_dirty_ui()
            self._append_log(f"Arquivo aberto automaticamente: {last}")
        except Exception as e:
            try:
                self._append_log(f"Auto-abrir falhou: {e}")
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # Trip changes application

    def apply_trip_changes(self) -> None:
        """
        Commit changes from the form into the selected trip and validate
        inputs.  If any validation fails a message is displayed and the
        method returns without modifying the trip.
        """
        self._push_undo()
        if self.current_index is None:
            self._validation_error("Selecione uma viagem na lista para editar.")
            return
        self._clear_validation()
        tid = self.var_id.get().strip()
        date = self.var_date.get().strip()
        # Normalize date: allow users to enter e.g. 2026-2-3
        if date and '-' in date and not DATE_RE.match(date):
            parts = date.split('-')
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                y, m, d = parts
                if len(y) == 4:
                    date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
                    self.var_date.set(date)
        direction = self.var_direction.get().strip()
        title = self.var_title.get().strip()
        cap_str = self.var_capacity.get().strip()
        if not tid:
            self._validation_error("O campo id é obrigatório.", self.ent_id)
            return
        if not DATE_RE.match(date):
            self._validation_error("Data inválida. Use YYYY-MM-DD (ex.: 2026-02-03).", self.ent_date)
            return
        try:
            datetime.date.fromisoformat(date)
        except Exception:
            self._validation_error("Data inexistente no calendário. Verifique dia/mês/ano.", self.ent_date)
            return
        if direction not in ("ida", "volta"):
            self._validation_error('Direction deve ser "ida" ou "volta".', self.cmb_direction)
            return
        try:
            capacity = int(cap_str)
            if capacity <= 0 or capacity > 10:
                raise ValueError()
        except Exception:
            self._validation_error("Capacity deve ser um inteiro entre 1 e 10 (ex.: 3).", self.ent_capacity)
            return
        stops = [self.stops_listbox.get(i).strip() for i in range(self.stops_listbox.size())]
        stops = [s for s in stops if s]
        if len(stops) < 2:
            self._validation_error("Stops deve ter pelo menos 2 cidades.", self.cmb_stop_new)
            return
        trip = self.data["trips"][self.current_index]
        # Check for duplicate ID
        for i, t in enumerate(self.data["trips"]):
            if i != self.current_index and t.get("id") == tid:
                self._validation_error(f'Já existe outra viagem com id="{tid}".', self.ent_id)
                return
        # Apply
        trip["id"] = tid
        trip["date"] = date
        trip["direction"] = direction
        trip["title"] = title
        trip["capacity"] = capacity
        trip["stops"] = stops
        self.dirty = True
        self._update_dirty_ui()
        self.refresh_ui()
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(self.current_index)
        self.listbox.see(self.current_index)
        self._set_status("Alterações aplicadas")

    # -------------------------------------------------------------------------
    # Month button selection

    def _select_month_button(self, month_num: int) -> None:
        """Handler for month button presses (1‑12)."""
        year = (self.var_year.get() or "").strip()
        if not year:
            cur = (self.var_month.get() or "").strip()
            if len(cur) >= 4 and cur[:4].isdigit():
                year = cur[:4]
            else:
                year = str(datetime.date.today().year)
                self.var_year.set(year)
        self.var_month.set(f"{year}-{month_num:02d}")
        self._populate_calendar(self.var_month.get())
        self._update_month_buttons_state()

    # -------------------------------------------------------------------------
    # Calendar update helper

    def _on_stop_autocomplete(self, _evt: Optional[Any] = None) -> None:
        """Basic autocomplete: complete the city name based on prefix."""
        try:
            typed = (self.var_stop_new.get() or "").strip()
            if not typed:
                return
            typed_low = typed.lower()
            common = [
                "Paulo Afonso-BA",
                "Petrolândia-PE",
                "Floresta-PE",
                "Cabrobó-PE",
                "Salgueiro-PE",
                "Juazeiro do Norte-CE",
                "Brejo Santo-CE",
            ]
            for city in common:
                if city.lower().startswith(typed_low) and city != typed:
                    self.var_stop_new.set(city)
                    try:
                        self.cmb_stop_new.icursor(len(typed))
                        self.cmb_stop_new.selection_range(len(typed), tk.END)
                    except Exception:
                        pass
                    return
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Publishing to GitHub (unchanged from original, simplified for reliability)

    def publish_to_github(self) -> None:
        """
        Publish the current repository to GitHub.  This stub logs a message
        noting that the feature is not implemented in this simplified
        version.  It can be extended with actual git and SSH commands.
        """
        messagebox.showinfo(
            "GitHub",
            "A funcionalidade de publicação para GitHub não está implementada nesta versão.\n"
            "Salve o arquivo manualmente e use git no terminal para publicar.",
        )
        self._append_log("Publicação não implementada nesta versão simplificada.")

    # -------------------------------------------------------------------------
    # Controls update

    def _update_controls_state(self) -> None:
        """Enable or disable buttons based on the current selection and state."""
        has_file = self.file_path is not None
        has_sel = self.current_index is not None
        try:
            self.btn_save.configure(state=(tk.NORMAL if has_file else tk.DISABLED))
        except Exception:
            pass
        try:
            self.btn_save_as.configure(state=tk.NORMAL)
        except Exception:
            pass
        try:
            self.btn_publish.configure(state=(tk.NORMAL if has_file else tk.DISABLED))
        except Exception:
            pass
        try:
            self.btn_dup.configure(state=(tk.NORMAL if has_sel else tk.DISABLED))
        except Exception:
            pass
        try:
            self.btn_del.configure(state=(tk.NORMAL if has_sel else tk.DISABLED))
        except Exception:
            pass
        try:
            self.btn_apply.configure(state=(tk.NORMAL if has_sel else tk.DISABLED))
        except Exception:
            pass
        bsel = False
        try:
            bsel = bool(self.bookings.selection())
        except Exception:
            bsel = False
        try:
            self.btn_b_add.configure(state=(tk.NORMAL if has_sel else tk.DISABLED))
        except Exception:
            pass
        try:
            self.btn_b_upd.configure(state=(tk.NORMAL if (has_sel and bsel) else tk.DISABLED))
        except Exception:
            pass
        try:
            self.btn_b_del.configure(state=(tk.NORMAL if (has_sel and bsel) else tk.DISABLED))
        except Exception:
            pass


def make_trip_label(t: Dict[str, Any]) -> str:
    """Return a human‑readable label summarising a trip."""
    date = t.get("date", "????-??-??")
    direction = t.get("direction", "?")
    title = t.get("title", "")
    short = "IDA" if direction == "ida" else ("VOLTA" if direction == "volta" else direction)
    return f"{date} • {short} • {title}".strip(" •")


if __name__ == "__main__":
    app = TripsEditorApp()
    app.mainloop()