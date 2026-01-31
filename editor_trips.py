import json
import os
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def safe_load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "trips" not in data or not isinstance(data["trips"], list):
        raise ValueError('JSON inválido. Esperado: { "trips": [ ... ] }')
    return data


def safe_save_json(path: str, data: dict) -> None:
    # Salva “bonitinho” e estável
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def make_trip_label(t: dict) -> str:
    date = t.get("date", "????-??-??")
    direction = t.get("direction", "?")
    title = t.get("title", "")
    short = "IDA" if direction == "ida" else ("VOLTA" if direction == "volta" else direction)
    return f"{date} • {short} • {title}".strip(" •")


class TripsEditorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Editor de trips.json")
        self.geometry("1100x680")
        self.minsize(980, 600)

        self.file_path: str | None = None
        self.data: dict = {"trips": []}
        self.current_index: int | None = None
        self.dirty = False

        self._build_ui()
        self._bind_shortcuts()
        self.refresh_ui()

    # ---------- UI ----------
    def _build_ui(self):
        # Topbar
        top = ttk.Frame(self, padding=8)
        top.pack(side=tk.TOP, fill=tk.X)

        self.lbl_file = ttk.Label(top, text="Arquivo: (nenhum)")
        self.lbl_file.pack(side=tk.LEFT)

        btns = ttk.Frame(top)
        btns.pack(side=tk.RIGHT)

        ttk.Button(btns, text="Abrir…", command=self.open_file).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Salvar", command=self.save_file).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Salvar como…", command=self.save_file_as).pack(side=tk.LEFT, padx=4)

        # Main split
        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        # Left panel (list)
        left = ttk.Frame(main, padding=8)
        main.add(left, weight=1)

        ttk.Label(left, text="Viagens").pack(anchor="w")
        self.listbox = tk.Listbox(left, height=20)
        self.listbox.pack(fill=tk.BOTH, expand=True, pady=(6, 8))
        self.listbox.bind("<<ListboxSelect>>", self.on_select_trip)

        left_btns = ttk.Frame(left)
        left_btns.pack(fill=tk.X)

        ttk.Button(left_btns, text="Nova viagem", command=self.new_trip).pack(side=tk.LEFT, padx=3)
        ttk.Button(left_btns, text="Duplicar", command=self.duplicate_trip).pack(side=tk.LEFT, padx=3)
        ttk.Button(left_btns, text="Remover", command=self.delete_trip).pack(side=tk.LEFT, padx=3)

        ttk.Separator(left).pack(fill=tk.X, pady=10)

        ttk.Button(left, text="Ordenar por data", command=self.sort_trips).pack(fill=tk.X)

        # Right panel (editor)
        right = ttk.Frame(main, padding=8)
        main.add(right, weight=3)

        # Form grid
        form = ttk.Frame(right)
        form.pack(fill=tk.X)

        def add_row(r, label, widget):
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=4)
            widget.grid(row=r, column=1, sticky="we", pady=4)
            form.grid_columnconfigure(1, weight=1)

        self.var_id = tk.StringVar()
        self.var_date = tk.StringVar()
        self.var_direction = tk.StringVar()
        self.var_title = tk.StringVar()
        self.var_capacity = tk.StringVar()
        self.var_stops = tk.StringVar()

        add_row(0, "id", ttk.Entry(form, textvariable=self.var_id))
        add_row(1, "date (YYYY-MM-DD)", ttk.Entry(form, textvariable=self.var_date))

        self.cmb_direction = ttk.Combobox(form, textvariable=self.var_direction, values=["ida", "volta"], state="readonly")
        add_row(2, "direction", self.cmb_direction)

        add_row(3, "title", ttk.Entry(form, textvariable=self.var_title))
        add_row(4, "capacity", ttk.Entry(form, textvariable=self.var_capacity))
        add_row(5, "stops (separar por ;)", ttk.Entry(form, textvariable=self.var_stops))

        # Bookings section
        ttk.Separator(right).pack(fill=tk.X, pady=10)
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

        # Booking editor controls
        booking_edit = ttk.Frame(right)
        booking_edit.pack(fill=tk.X, pady=8)

        self.var_b_name = tk.StringVar()
        self.var_b_from = tk.StringVar()
        self.var_b_to = tk.StringVar()

        ttk.Label(booking_edit, text="Nome").grid(row=0, column=0, sticky="w")
        ttk.Entry(booking_edit, textvariable=self.var_b_name).grid(row=0, column=1, sticky="we", padx=6)

        ttk.Label(booking_edit, text="De").grid(row=0, column=2, sticky="w")
        ttk.Entry(booking_edit, textvariable=self.var_b_from).grid(row=0, column=3, sticky="we", padx=6)

        ttk.Label(booking_edit, text="Para").grid(row=0, column=4, sticky="w")
        ttk.Entry(booking_edit, textvariable=self.var_b_to).grid(row=0, column=5, sticky="we", padx=6)

        booking_edit.grid_columnconfigure(1, weight=1)
        booking_edit.grid_columnconfigure(3, weight=1)
        booking_edit.grid_columnconfigure(5, weight=1)

        bbtns = ttk.Frame(right)
        bbtns.pack(fill=tk.X)

        ttk.Button(bbtns, text="Adicionar reserva", command=self.add_booking).pack(side=tk.LEFT, padx=3)
        ttk.Button(bbtns, text="Atualizar seleção", command=self.update_booking).pack(side=tk.LEFT, padx=3)
        ttk.Button(bbtns, text="Remover seleção", command=self.remove_booking).pack(side=tk.LEFT, padx=3)

        self.bookings.bind("<<TreeviewSelect>>", self.on_select_booking)

        ttk.Separator(right).pack(fill=tk.X, pady=10)
        ttk.Button(right, text="Aplicar alterações desta viagem", command=self.apply_trip_changes).pack(anchor="e")

    def _bind_shortcuts(self):
        self.bind("<Control-s>", lambda e: self.save_file())
        self.bind("<Control-o>", lambda e: self.open_file())

    # ---------- File ----------
    def open_file(self):
        if not self.confirm_discard_if_dirty():
            return

        path = filedialog.askopenfilename(
            title="Abrir trips.json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")]
        )
        if not path:
            return

        try:
            self.data = safe_load_json(path)
        except Exception as e:
            messagebox.showerror("Erro ao abrir", str(e))
            return

        self.file_path = path
        self.lbl_file.configure(text=f"Arquivo: {path}")
        self.current_index = None
        self.dirty = False
        self.refresh_ui()

    def save_file(self):
        if self.file_path is None:
            return self.save_file_as()

        try:
            safe_save_json(self.file_path, self.data)
            self.dirty = False
            messagebox.showinfo("Salvo", "Arquivo salvo com sucesso.")
        except Exception as e:
            messagebox.showerror("Erro ao salvar", str(e))

    def save_file_as(self):
        path = filedialog.asksaveasfilename(
            title="Salvar como",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")]
        )
        if not path:
            return
        self.file_path = path
        self.lbl_file.configure(text=f"Arquivo: {path}")
        self.save_file()

    def confirm_discard_if_dirty(self) -> bool:
        if not self.dirty:
            return True
        return messagebox.askyesno("Alterações não salvas", "Você tem alterações não salvas. Deseja descartá-las?")

    # ---------- Trips CRUD ----------
    def refresh_ui(self):
        # Listbox
        self.listbox.delete(0, tk.END)
        for t in self.data.get("trips", []):
            self.listbox.insert(tk.END, make_trip_label(t))

        # Clear editor
        if self.current_index is None:
            self._load_trip_into_form(None)

    def sort_trips(self):
        self.data["trips"].sort(key=lambda t: (t.get("date", ""), t.get("direction", ""), t.get("id", "")))
        self.dirty = True
        self.refresh_ui()

    def on_select_trip(self, _evt=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        self.current_index = idx
        trip = self.data["trips"][idx]
        self._load_trip_into_form(trip)

    def new_trip(self):
        trip = {
            "id": "",
            "date": "",
            "direction": "ida",
            "title": "",
            "capacity": 3,
            "stops": [],
            "bookings": []
        }
        self.data["trips"].append(trip)
        self.dirty = True
        self.refresh_ui()
        self.current_index = len(self.data["trips"]) - 1
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(self.current_index)
        self.listbox.see(self.current_index)
        self._load_trip_into_form(trip)

    def duplicate_trip(self):
        if self.current_index is None:
            messagebox.showwarning("Selecione", "Selecione uma viagem para duplicar.")
            return
        src = self.data["trips"][self.current_index]
        dup = json.loads(json.dumps(src))  # deep copy
        dup["id"] = (dup.get("id", "") + "-copy").strip("-")
        self.data["trips"].append(dup)
        self.dirty = True
        self.refresh_ui()

    def delete_trip(self):
        if self.current_index is None:
            messagebox.showwarning("Selecione", "Selecione uma viagem para remover.")
            return
        trip = self.data["trips"][self.current_index]
        if not messagebox.askyesno("Confirmar", f"Remover a viagem:\n{make_trip_label(trip)} ?"):
            return
        del self.data["trips"][self.current_index]
        self.current_index = None
        self.dirty = True
        self.refresh_ui()

    def apply_trip_changes(self):
        if self.current_index is None:
            messagebox.showwarning("Selecione", "Selecione (ou crie) uma viagem para editar.")
            return

        # Validate and apply
        tid = self.var_id.get().strip()
        date = self.var_date.get().strip()
        direction = self.var_direction.get().strip()
        title = self.var_title.get().strip()
        cap_str = self.var_capacity.get().strip()
        stops_str = self.var_stops.get().strip()

        if not tid:
            messagebox.showerror("Validação", "O campo id é obrigatório.")
            return
        if not DATE_RE.match(date):
            messagebox.showerror("Validação", "Data inválida. Use YYYY-MM-DD (ex.: 2026-02-03).")
            return
        if direction not in ("ida", "volta"):
            messagebox.showerror("Validação", 'Direction deve ser "ida" ou "volta".')
            return
        try:
            capacity = int(cap_str)
            if capacity <= 0:
                raise ValueError()
        except Exception:
            messagebox.showerror("Validação", "Capacity deve ser um inteiro positivo (ex.: 3).")
            return

        stops = [s.strip() for s in stops_str.split(";") if s.strip()]
        if len(stops) < 2:
            messagebox.showerror("Validação", "Stops deve ter pelo menos 2 cidades (separe por ;).")
            return

        trip = self.data["trips"][self.current_index]

        # Ensure unique id (except current)
        for i, t in enumerate(self.data["trips"]):
            if i != self.current_index and t.get("id") == tid:
                messagebox.showerror("Validação", f'Já existe outra viagem com id="{tid}".')
                return

        trip["id"] = tid
        trip["date"] = date
        trip["direction"] = direction
        trip["title"] = title
        trip["capacity"] = capacity
        trip["stops"] = stops

        # Bookings are edited via table; we keep as is
        self.dirty = True
        self.refresh_ui()
        # keep selection
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(self.current_index)
        self.listbox.see(self.current_index)

    def _load_trip_into_form(self, trip: dict | None):
        if not trip:
            self.var_id.set("")
            self.var_date.set("")
            self.var_direction.set("ida")
            self.var_title.set("")
            self.var_capacity.set("3")
            self.var_stops.set("")
            self._reload_bookings([])
            return

        self.var_id.set(trip.get("id", ""))
        self.var_date.set(trip.get("date", ""))
        self.var_direction.set(trip.get("direction", "ida"))
        self.var_title.set(trip.get("title", ""))
        self.var_capacity.set(str(trip.get("capacity", 3)))

        stops = trip.get("stops", [])
        if isinstance(stops, list):
            self.var_stops.set("; ".join(stops))
        else:
            self.var_stops.set("")

        self._reload_bookings(trip.get("bookings", []))

    # ---------- Bookings CRUD ----------
    def _reload_bookings(self, bookings):
        self.bookings.delete(*self.bookings.get_children())
        if not isinstance(bookings, list):
            bookings = []
        for i, b in enumerate(bookings):
            self.bookings.insert("", tk.END, iid=str(i), values=(b.get("name",""), b.get("from",""), b.get("to","")))

        self.var_b_name.set("")
        self.var_b_from.set("")
        self.var_b_to.set("")

    def on_select_booking(self, _evt=None):
        sel = self.bookings.selection()
        if not sel:
            return
        iid = sel[0]
        vals = self.bookings.item(iid, "values")
        if len(vals) >= 3:
            self.var_b_name.set(vals[0])
            self.var_b_from.set(vals[1])
            self.var_b_to.set(vals[2])

    def add_booking(self):
        if self.current_index is None:
            messagebox.showwarning("Selecione", "Selecione uma viagem primeiro.")
            return
        name = self.var_b_name.get().strip()
        frm = self.var_b_from.get().strip()
        to = self.var_b_to.get().strip()
        if not (name and frm and to):
            messagebox.showerror("Validação", "Preencha Nome, De e Para.")
            return

        trip = self.data["trips"][self.current_index]
        if "bookings" not in trip or not isinstance(trip["bookings"], list):
            trip["bookings"] = []
        trip["bookings"].append({"name": name, "from": frm, "to": to})
        self.dirty = True
        self._reload_bookings(trip["bookings"])

    def update_booking(self):
        if self.current_index is None:
            messagebox.showwarning("Selecione", "Selecione uma viagem primeiro.")
            return
        sel = self.bookings.selection()
        if not sel:
            messagebox.showwarning("Selecione", "Selecione uma reserva na tabela.")
            return
        iid = sel[0]
        idx = int(iid)

        name = self.var_b_name.get().strip()
        frm = self.var_b_from.get().strip()
        to = self.var_b_to.get().strip()
        if not (name and frm and to):
            messagebox.showerror("Validação", "Preencha Nome, De e Para.")
            return

        trip = self.data["trips"][self.current_index]
        trip["bookings"][idx] = {"name": name, "from": frm, "to": to}
        self.dirty = True
        self._reload_bookings(trip["bookings"])

    def remove_booking(self):
        if self.current_index is None:
            messagebox.showwarning("Selecione", "Selecione uma viagem primeiro.")
            return
        sel = self.bookings.selection()
        if not sel:
            messagebox.showwarning("Selecione", "Selecione uma reserva na tabela.")
            return
        iid = sel[0]
        idx = int(iid)

        trip = self.data["trips"][self.current_index]
        if not messagebox.askyesno("Confirmar", "Remover esta reserva?"):
            return
        del trip["bookings"][idx]
        self.dirty = True
        self._reload_bookings(trip["bookings"])


if __name__ == "__main__":
    # Windows: melhora um pouco o visual com tema padrão
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    app = TripsEditorApp()
    app.mainloop()