import json
import os
import re
import sys
import subprocess
import datetime
import tempfile
import shutil
import unicodedata

# Tkinter is optional depending on how Python was installed (Homebrew Python often lacks _tkinter).
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except Exception as e:
    tk = None  # type: ignore
    _TK_IMPORT_ERROR = e
else:
    _TK_IMPORT_ERROR = None

# If Tkinter failed to import, abort early with a friendly message.
if tk is None:
    sys.stderr.write(
        "Erro: Tkinter (_tkinter) não está disponível nesta instalação do Python.\n\n"
        "Você está usando um Python que foi compilado/instalado sem suporte a Tk.\n"
        "Como corrigir no macOS (pyenv):\n"
        "  1) brew install tcl-tk\n"
        "  2) Reinstale o Python via pyenv com suporte a Tcl/Tk.\n\n"
        "Alternativas rápidas:\n"
        "  • Use o Python do instalador oficial (python.org), que já vem com Tkinter.\n"
        "  • Ou use /usr/bin/python3 (se tiver tkinter).\n\n"
        f"Detalhes do erro: {_TK_IMPORT_ERROR}\n"
    )
    raise SystemExit(1)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Cidades comuns (autocomplete / listas fixas)
CITIES_COMMON = [
    "Paulo Afonso-BA",
    "Petrolândia-PE",
    "Floresta-PE",
    "Cabrobó-PE",
    "Salgueiro-PE",
    "Juazeiro do Norte-CE",
    "Brejo Santo-CE",
]

# Rotas padrão (templates)
ROUTE_IDA_DEFAULT = [
    "Paulo Afonso-BA",
    "Petrolândia-PE",
    "Floresta-PE",
    "Cabrobó-PE",
    "Salgueiro-PE",
]
# Volta padrão: rota inversa
ROUTE_VOLTA_DEFAULT = list(reversed(ROUTE_IDA_DEFAULT))

# --- Last opened JSON (auto-open) ---
LAST_FILE_PATH = os.path.join(os.path.expanduser("~"), ".trips_editor_last_json.txt")


def load_last_json_path() -> str | None:
    try:
        if os.path.exists(LAST_FILE_PATH):
            p = open(LAST_FILE_PATH, "r", encoding="utf-8").read().strip()
            return p or None
    except Exception:
        pass
    return None


def save_last_json_path(path: str) -> None:
    try:
        with open(LAST_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(path)
    except Exception:
        pass

# --- Git helpers ---
def run_git(args: list[str], cwd: str, log=None) -> tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr). Non-interactive (won't prompt)."""
    env = os.environ.copy()
    # Prevent git from prompting for credentials/passphrases in a GUI-less subprocess.
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Force ssh to be non-interactive; if a passphrase is needed, it will fail quickly.
    # Use BatchMode only when we don't appear to have an agent with identities.
    # This avoids edge-cases where forcing BatchMode can interfere with some setups.
    def _agent_has_identities() -> bool:
        try:
            pp = subprocess.run(["ssh-add", "-l"], capture_output=True, text=True, env=os.environ.copy())
            if pp.returncode == 0 and pp.stdout and "The agent has no identities" not in pp.stdout:
                return True
        except Exception:
            pass
        return False

    if _agent_has_identities():
        env.pop("GIT_SSH_COMMAND", None)
    else:
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    if callable(log):
        try:
            log(f"$ git {' '.join(args)}")
        except Exception:
            pass
    p = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if callable(log):
        try:
            if out:
                log(out)
            if err:
                log(err)
        except Exception:
            pass
    return p.returncode, out, err



def find_repo_root(start_dir: str) -> str | None:
    """Return git repo root for start_dir, or None if not a repo."""
    code, out, err = run_git(["rev-parse", "--show-toplevel"], cwd=start_dir)
    if code != 0:
        return None
    return (out or "").strip() or None
    
def get_default_branch(cwd: str, log=None) -> str:
    """Detect default remote branch (origin/HEAD -> main/master). Falls back to main."""
    code, out, err = run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=cwd, log=log)
    if code == 0 and out:
        parts = out.strip().split("/")
        if parts:
            return parts[-1]
    return "main"

def git_pull_rebase(cwd: str, log=None) -> tuple[int, str, str]:
    branch = get_default_branch(cwd, log=log)
    return run_git(["pull", "--rebase", "origin", branch], cwd=cwd, log=log)

def ensure_ds_store_ignored(repo_root: str, log=None) -> None:
    """Ensure .DS_Store is ignored and (if tracked) removed from the index."""
    try:
        gitignore = os.path.join(repo_root, ".gitignore")
        line = ".DS_Store"

        existing = ""
        if os.path.exists(gitignore):
            existing = open(gitignore, "r", encoding="utf-8", errors="ignore").read()

        if line not in existing.splitlines():
            with open(gitignore, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(line + "\n")
            if callable(log):
                log("Adicionado .DS_Store ao .gitignore")

        # Se estiver trackeado, remover do índice (best-effort)
        run_git(["rm", "--cached", "-r", "--", ".DS_Store"], cwd=repo_root, log=log)
    except Exception:
        pass


# --- SSH agent/key helpers ---
def _parse_ssh_agent_output(agent_out: str) -> dict:
    """Parse `ssh-agent -s` output and return env vars."""
    env_vars: dict[str, str] = {}
    # Typical lines:
    # SSH_AUTH_SOCK=/var/folders/.../agent.12345; export SSH_AUTH_SOCK;
    # SSH_AGENT_PID=12345; export SSH_AGENT_PID;
    for line in agent_out.splitlines():
        line = line.strip()
        if line.startswith("SSH_AUTH_SOCK="):
            val = line.split("SSH_AUTH_SOCK=", 1)[1].split(";", 1)[0]
            if val:
                env_vars["SSH_AUTH_SOCK"] = val
        elif line.startswith("SSH_AGENT_PID="):
            val = line.split("SSH_AGENT_PID=", 1)[1].split(";", 1)[0]
            if val:
                env_vars["SSH_AGENT_PID"] = val
    return env_vars


def _prompt_passphrase(parent) -> str | None:
    """Prompt for SSH key passphrase (hidden). Returns None if cancelled."""
    win = tk.Toplevel(parent)
    win.title("Senha da chave SSH")
    win.transient(parent)
    win.grab_set()

    frm = ttk.Frame(win, padding=12)
    frm.pack(fill=tk.BOTH, expand=True)

    ttk.Label(frm, text="Digite a senha (passphrase) da sua chave SSH:").pack(anchor="w")
    var = tk.StringVar(value="")
    ent = ttk.Entry(frm, textvariable=var, show="*")
    ent.pack(fill=tk.X, pady=(6, 10))
    ent.focus_set()

    hint = ttk.Label(
        frm,
        text=(
            "Dica: esta senha é a que você definiu ao criar a chave (~/.ssh/id_ed25519).\n"
            "Se sua chave não tem senha, deixe em branco e clique em OK."
        )
    )
    hint.pack(anchor="w", pady=(0, 10))

    btns = ttk.Frame(frm)
    btns.pack(fill=tk.X)

    result = {"value": None}

    def ok():
        result["value"] = var.get()
        win.destroy()

    def cancel():
        result["value"] = None
        win.destroy()

    ttk.Button(btns, text="Cancelar", command=cancel).pack(side=tk.RIGHT, padx=4)
    ttk.Button(btns, text="OK", command=ok).pack(side=tk.RIGHT)

    win.bind("<Return>", lambda e: ok())
    win.bind("<Escape>", lambda e: cancel())

    win.update_idletasks()
    # Center the dialog over parent
    px = parent.winfo_rootx()
    py = parent.winfo_rooty()
    pw = parent.winfo_width()
    ph = parent.winfo_height()
    ww = win.winfo_reqwidth()
    wh = win.winfo_reqheight()
    win.geometry(f"+{px + (pw - ww)//2}+{py + (ph - wh)//2}")

    parent.wait_window(win)
    return result["value"]


def ensure_ssh_auth_ready(parent) -> bool:
    """Ensure an ssh-agent is running and the key is loaded. Returns True if ready."""
    # 1) If we already have identities, we're good.
    try:
        p = subprocess.run(
            ["ssh-add", "-l"],
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )
        if p.returncode == 0 and p.stdout and "The agent has no identities" not in p.stdout:
            return True
    except Exception:
        pass

    # 2) Start agent (or refresh env vars) so ssh-add can talk to it.
    agent = subprocess.run(
        ["ssh-agent", "-s"],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    if agent.returncode != 0:
        messagebox.showerror("GitHub", f"Falha ao iniciar ssh-agent.\n\n{(agent.stderr or agent.stdout).strip()}")
        return False

    env_vars = _parse_ssh_agent_output(agent.stdout or "")
    if not env_vars.get("SSH_AUTH_SOCK"):
        messagebox.showerror("GitHub", "Não consegui obter SSH_AUTH_SOCK do ssh-agent.")
        return False

    # Apply to current process so all subsequent git/ssh calls inherit it.
    os.environ.update(env_vars)

    # 3) Add key (with GUI passphrase prompt via SSH_ASKPASS to avoid terminal interaction).
    key_path = os.path.expanduser("~/.ssh/id_ed25519")
    if not os.path.exists(key_path):
        messagebox.showerror(
            "GitHub",
            "Não encontrei a chave SSH em ~/.ssh/id_ed25519.\n\n"
            "Verifique o caminho da chave ou gere uma nova chave (ssh-keygen).",
        )
        return False

    passphrase = _prompt_passphrase(parent)
    if passphrase is None:
        return False

    # Create a temporary askpass script that prints the passphrase.
    # Note: this keeps the passphrase only in memory/env during this call.
    askpass_path = None
    try:
        fd, askpass_path = tempfile.mkstemp(prefix="askpass_", text=True)
        os.close(fd)
        with open(askpass_path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
            f.write('printf "%s" "$SSH_PASSPHRASE"\n')
        os.chmod(askpass_path, 0o700)

        env = os.environ.copy()
        env["SSH_ASKPASS"] = askpass_path
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env["SSH_PASSPHRASE"] = passphrase
        # Prevent ssh-add from trying to read from stdin/tty and hanging.
        env["DISPLAY"] = env.get("DISPLAY", ":0")

        add = subprocess.run(
            ["ssh-add", "--apple-use-keychain", key_path],
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        if add.returncode != 0:
            details = (add.stderr or add.stdout).strip()
            messagebox.showerror(
                "GitHub",
                "Não consegui adicionar a chave ao ssh-agent.\n\n"
                f"{details}\n\n"
                "Se a senha estiver incorreta, tente novamente.\n"
                "Se sua chave não tiver senha, tente OK com o campo em branco.",
            )
            return False
    finally:
        # Best effort cleanup
        try:
            if askpass_path and os.path.exists(askpass_path):
                os.remove(askpass_path)
        except Exception:
            pass

    return True


def test_github_ssh(parent) -> None:
    """Run `ssh -T git@github.com` and show a friendly message."""
    p = subprocess.run(
        ["ssh", "-T", "git@github.com"],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    out = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
    # GitHub often returns exit code 1 even on success (auth success but no shell).
    if ("successfully authenticated" in out.lower()) or ("welcome" in out.lower()):
        messagebox.showinfo("GitHub", "Conexão SSH com GitHub OK ✅")
        return
    # Only show details if something looks wrong.
    if p.returncode != 0:
        messagebox.showwarning(
            "GitHub",
            "Teste SSH com GitHub retornou uma mensagem.\n\n"
            f"{out}\n\n"
            "Se aparecer 'Permission denied (publickey)', sua chave ainda não está autorizada no GitHub.",
        )


# --- Simple prompt dialog ---
def simple_prompt(parent, title: str, label: str, default: str = "") -> str | None:
    """Small modal prompt to ask for a single line string."""
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    win.grab_set()

    frm = ttk.Frame(win, padding=12)
    frm.pack(fill=tk.BOTH, expand=True)

    ttk.Label(frm, text=label).pack(anchor="w")
    var = tk.StringVar(value=default)
    ent = ttk.Entry(frm, textvariable=var)
    ent.pack(fill=tk.X, pady=(6, 10))
    ent.focus_set()

    btns = ttk.Frame(frm)
    btns.pack(fill=tk.X)

    result = {"value": None}

    def ok():
        result["value"] = var.get()
        win.destroy()

    def cancel():
        result["value"] = None
        win.destroy()

    ttk.Button(btns, text="Cancelar", command=cancel).pack(side=tk.RIGHT, padx=4)
    ttk.Button(btns, text="OK", command=ok).pack(side=tk.RIGHT)

    win.bind("<Return>", lambda e: ok())
    win.bind("<Escape>", lambda e: cancel())

    win.update_idletasks()
    # Center the dialog over parent
    px = parent.winfo_rootx()
    py = parent.winfo_rooty()
    pw = parent.winfo_width()
    ph = parent.winfo_height()
    ww = win.winfo_reqwidth()
    wh = win.winfo_reqheight()
    win.geometry(f"+{px + (pw - ww)//2}+{py + (ph - wh)//2}")

    parent.wait_window(win)
    return result["value"]


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

def _slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s

def make_backup(path: str, max_backups: int = 10) -> None:
    """Cria backup com timestamp em backups_trips (best effort)."""
    try:
        if not path or not os.path.exists(path):
            return
        folder = os.path.join(os.path.dirname(path), "backups_trips")
        os.makedirs(folder, exist_ok=True)

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.basename(path)
        dst = os.path.join(folder, f"{base}.{ts}.bak")
        shutil.copy2(path, dst)

        # mantém só os últimos N backups desse arquivo
        all_baks = sorted(
            [p for p in os.listdir(folder) if p.startswith(base + ".") and p.endswith(".bak")]
        )
        if len(all_baks) > max_backups:
            for old in all_baks[: len(all_baks) - max_backups]:
                try:
                    os.remove(os.path.join(folder, old))
                except Exception:
                    pass
    except Exception:
        pass

class TripsEditorApp(tk.Tk):

    def _refresh_segments_view(self, trip: dict | None):
        # Clear existing rows
        try:
            self.seg_tree.delete(*self.seg_tree.get_children())
        except Exception:
            return
        if not trip:
            return

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

        ranges: list[tuple[int, int]] = []
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

    def _generate_unique_id(self, base_id: str) -> str:
        base_id = base_id.strip()
        existing = {str(t.get("id", "")) for t in self.data.get("trips", []) if isinstance(t, dict)}
        if base_id and base_id not in existing:
            return base_id
        n = 2
        while True:
            cand = f"{base_id}_{n:02d}"
            if cand not in existing:
                return cand
            n += 1

    def generate_id(self):
        date = (self.var_date.get() or "").strip()
        direction = (self.var_direction.get() or "").strip()
        stops = [self.stops_listbox.get(i).strip() for i in range(self.stops_listbox.size())]
        stops = [s for s in stops if s]

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

        self.var_id.set(self._generate_unique_id(base))
        self.dirty = True
        self._update_dirty_ui()
        self._set_status("ID gerado")

    def _setup_theme(self):
        style = ttk.Style(self)
        try:
            # Use a modern, consistent theme
            if "clam" in style.theme_names():
                style.theme_use("clam")
        except Exception:
            pass

        # Fontes mais compactas (melhor em telas menores)
        try:
            default_font = ("Helvetica", 10)
            small_font = ("Helvetica", 9)
            bold_font = ("Helvetica", 10, "bold")

            self.option_add("*Font", default_font)

            style.configure("TLabel", font=default_font)
            # Padding menor nos botões para não ficarem "grandes"
            style.configure("TButton", font=default_font, padding=(6, 3))
            style.configure("TEntry", font=default_font, padding=(4, 2))
            style.configure("TCombobox", font=default_font, padding=(4, 2))
            style.configure("Treeview", font=small_font, rowheight=22)
            style.configure("Treeview.Heading", font=bold_font)
            style.configure("TLabelframe.Label", font=bold_font)
        except Exception:
            pass

    def _auto_open_last_json(self) -> None:
        """Try to auto-open the last used JSON file, if it still exists."""
        try:
            last = load_last_json_path()
            if not last:
                return
            if not os.path.exists(last):
                return
            if self.file_path:
                return

            self.data = safe_load_json(last)
            self.file_path = last
            self.current_index = None
            self.dirty = False
            self._update_dirty_ui()
            self._append_log(f"Arquivo aberto automaticamente: {last}")
        except Exception as e:
            # If auto-open fails, ignore and allow manual open
            try:
                self._append_log(f"Auto-abrir falhou: {e}")
            except Exception:
                pass

    def __init__(self):
        super().__init__()
        self.title("Editor de trips.json")

        # Abre já em um tamanho confortável (quase tela cheia), evitando precisar maximizar manualmente.
        try:
            self.update_idletasks()
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()

            # margem para dock/barra de tarefas
            margin_w = 40
            margin_h = 90

            self._undo_stack = []
            self._redo_stack = []
            self._undo_max = 50

            w = max(980, min(1400, sw - margin_w))
            h = max(700, min(900, sh - margin_h))

            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 4)
            self.geometry(f"{w}x{h}+{x}+{y}")

            self.minsize(980, 650)
        except Exception:
            # fallback
            self.geometry("1200x760")
            self.minsize(980, 650)

        self.file_path: str | None = None
        self.data: dict = {"trips": []}
        self.current_index: int | None = None
        self.dirty = False
        self._publishing = False
        self._log_lines_max = 400

        self._setup_theme()
        self._build_ui()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Auto-open: load the last JSON file used (if present)
        self._auto_open_last_json()

        self.refresh_ui()
        self._update_dirty_ui()

    # ---------- UI ----------

    def _snapshot(self):
        return json.loads(json.dumps(self.data, ensure_ascii=False))

    def _push_undo(self):
        try:
            self._undo_stack.append(self._snapshot())
            if len(self._undo_stack) > self._undo_max:
                self._undo_stack = self._undo_stack[-self._undo_max:]
            self._redo_stack.clear()
        except Exception:
            pass

    def undo(self):
        if not self._undo_stack:
            self._set_status("Nada para desfazer")
            return
        self._redo_stack.append(self._snapshot())
        self.data = self._undo_stack.pop()
        self.dirty = True
        self.refresh_ui()
        self._set_status("Desfazer ✅")

    def redo(self):
        if not self._redo_stack:
            self._set_status("Nada para refazer")
            return
        self._undo_stack.append(self._snapshot())
        self.data = self._redo_stack.pop()
        self.dirty = True
        self.refresh_ui()
        self._set_status("Refazer ✅")

    def on_close(self):
        if self.dirty:
            choice = messagebox.askyesnocancel(
                "Sair",
                "Você tem alterações não salvas. Deseja salvar antes de sair?\n\n"
                "SIM → Salvar e sair\n"
                "NÃO → Sair sem salvar\n"
                "CANCELAR → Voltar",
            )
            if choice is None:
                return
            if choice is True:
                self.save_file()
                if self.dirty:
                    return
        self.destroy()


    def _build_ui(self):
        # Topbar
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

        # Main split
        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        # Left panel (list)
        left = ttk.Frame(main, padding=10)
        main.add(left, weight=1)

        ttk.Label(left, text="Viagens").pack(anchor="w")

        # Container with fixed height so the trips list doesn't occupy the whole left panel
        left_top = ttk.Frame(left)
        left_top.pack(fill=tk.BOTH, expand=False, pady=(6, 8))
        left_top.configure(height=320)
        left_top.pack_propagate(False)

        self.nb_left = ttk.Notebook(left_top)
        self.nb_left.pack(fill=tk.BOTH, expand=True)

        tab_list = ttk.Frame(self.nb_left)
        tab_cal = ttk.Frame(self.nb_left)
        self.nb_left.add(tab_list, text="Lista")
        self.nb_left.add(tab_cal, text="Calendário")

        # Tab: Lista
        lb_frame = ttk.Frame(tab_list)
        lb_frame.pack(fill=tk.BOTH, expand=True)

        self.listbox = tk.Listbox(lb_frame, height=12)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        lb_scroll = ttk.Scrollbar(lb_frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=lb_scroll.set)
        lb_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.listbox.bind("<<ListboxSelect>>", self.on_select_trip)

        # Tab: Calendário
        cal_top = ttk.Frame(tab_cal, padding=(0, 0, 0, 6))
        cal_top.pack(fill=tk.X)

        # Ano (para evitar ambiguidade quando há viagens em anos diferentes)
        ttk.Label(cal_top, text="Ano:").pack(side=tk.LEFT)
        self.var_year = tk.StringVar(value="")
        self.cmb_year = ttk.Combobox(cal_top, textvariable=self.var_year, values=[], state="readonly", width=6)
        self.cmb_year.pack(side=tk.LEFT, padx=(6, 12))
        self.cmb_year.bind("<<ComboboxSelected>>", self.on_select_month)

        # 12 botões de mês (jan–dez) em 4 linhas x 3 colunas
        months_frame = ttk.Frame(tab_cal)
        months_frame.pack(fill=tk.X, pady=(0, 6))

        self._month_btns = {}
        month_labels = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
        for i, lab in enumerate(month_labels, start=1):
            r = (i - 1) // 3
            c = (i - 1) % 3
            btn = ttk.Button(months_frame, text=lab, width=4, command=lambda m=i: self._select_month_button(m))
            btn.grid(row=r, column=c, padx=1, pady=1, sticky="we")
            self._month_btns[i] = btn

        for c in range(3):
            months_frame.grid_columnconfigure(c, weight=1)

        # Mês selecionado (YYYY-MM)
        self.var_month = tk.StringVar(value="")

        self.cal_tree = ttk.Treeview(tab_cal, columns=("date", "trips"), show="headings", height=18)
        self.cal_tree.heading("date", text="Data")
        self.cal_tree.heading("trips", text="Viagens")
        self.cal_tree.column("date", width=110, anchor="w")
        self.cal_tree.column("trips", width=360, anchor="w")
        self.cal_tree.pack(fill=tk.BOTH, expand=True)
        self.cal_tree.bind("<<TreeviewSelect>>", self.on_select_calendar_row)

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

        # Editor (editable) moved to the left panel
        editor_left = ttk.Labelframe(left, text="Viagem selecionada (editar)", padding=10)
        editor_left.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        # Form grid (moved from right)
        form = ttk.Frame(editor_left)
        form.pack(fill=tk.X)

        def add_row(r, label, widget):
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", padx=(0, 10), pady=6)
            widget.grid(row=r, column=1, sticky="we", pady=6)

        self.var_id = tk.StringVar()
        self.var_date = tk.StringVar()
        self.var_direction = tk.StringVar()
        self.var_title = tk.StringVar()
        self.var_capacity = tk.StringVar()

        id_row = ttk.Frame(form)

        self.ent_id = ttk.Entry(id_row, textvariable=self.var_id)
        self.ent_id.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Button(id_row, text="Gerar", command=self.generate_id).pack(side=tk.LEFT, padx=(6, 0))

        add_row(0, "id", id_row)

        self.ent_date = ttk.Entry(form, textvariable=self.var_date)
        add_row(1, "date (YYYY-MM-DD)", self.ent_date)

        self.cmb_direction = ttk.Combobox(
            form,
            textvariable=self.var_direction,
            values=["ida", "volta"],
            state="readonly",
        )
        add_row(2, "direction", self.cmb_direction)

        add_row(3, "title", ttk.Entry(form, textvariable=self.var_title))

        self.ent_capacity = ttk.Entry(form, textvariable=self.var_capacity)
        add_row(4, "capacity", self.ent_capacity)

        self.lbl_validation = ttk.Label(editor_left, text="", foreground="#b00020")
        self.lbl_validation.pack(anchor="w", pady=(2, 6))

        # Stops editor (list + controls)
        stops_container = ttk.Frame(editor_left)
        stops_container.pack(fill=tk.X)

        stops_top = ttk.Frame(stops_container)
        stops_top.pack(fill=tk.X)

        self.var_stop_new = tk.StringVar()
        self.cmb_stop_new = ttk.Combobox(stops_top, textvariable=self.var_stop_new, values=CITIES_COMMON)
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

        ttk.Label(editor_left, text="Paradas (ordem)").pack(anchor="w", pady=(6, 0))

        # Right panel (editor)
        right = ttk.Frame(main, padding=10)
        main.add(right, weight=3)

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

        self.bookings = ttk.Treeview(bookings_frame, columns=("name", "from", "to"), show="headings", height=8)
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
        ttk.Combobox(booking_edit, textvariable=self.var_b_from, values=CITIES_COMMON).grid(row=0, column=3, sticky="we", padx=6)

        ttk.Label(booking_edit, text="Para").grid(row=0, column=4, sticky="w")
        ttk.Combobox(booking_edit, textvariable=self.var_b_to, values=CITIES_COMMON).grid(row=0, column=5, sticky="we", padx=6)

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
        self.btn_apply.pack(anchor="e", pady=(0, 8))

        # Log panel (moved to the right, below Apply)
        log_frame = ttk.Labelframe(right, text="Log", padding=8)
        log_frame.pack(fill=tk.BOTH, expand=False)

        log_btns = ttk.Frame(log_frame)
        log_btns.pack(fill=tk.X)
        ttk.Button(log_btns, text="Limpar log", command=self._clear_log).pack(side=tk.RIGHT)

        self.txt_log = tk.Text(log_frame, height=6, wrap="word", state=tk.DISABLED)
        self.txt_log.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

    def _bind_shortcuts(self):
        self.bind("<Control-s>", lambda e: self.save_file())
        self.bind("<Control-o>", lambda e: self.open_file())
        # Autocomplete básico para o campo de nova parada
        try:
            self.cmb_stop_new.bind("<KeyRelease>", self._on_stop_autocomplete)
        except Exception:
            pass
        # Atalho para alternar modo tela-cheia (útil em telas menores)
        self.bind("<F11>", lambda e: self.attributes("-fullscreen", not bool(self.attributes("-fullscreen"))))
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))

        # Atalhos úteis
        self.bind("<Control-n>", lambda e: self.new_trip())
        self.bind("<Control-Shift-N>", lambda e: self.new_trip_template())
        self.bind("<Control-d>", lambda e: self.duplicate_trip())
        self.bind("<Control-Return>", lambda e: self.apply_trip_changes())
        self.bind("<Control-z>", lambda e: self.undo())
        self.bind("<Control-y>", lambda e: self.redo())
        self.bind("<Control-Shift-Z>", lambda e: self.redo())

        # Delete/Backspace remove viagem (quando foco na lista)
        try:
            self.listbox.bind("<Delete>", lambda e: self.delete_trip())
            self.listbox.bind("<BackSpace>", lambda e: self.delete_trip())
        except Exception:
            pass

        # Delete/Backspace remove reserva (quando foco na tabela)
        try:
            self.bookings.bind("<Delete>", lambda e: self.remove_booking())
            self.bookings.bind("<BackSpace>", lambda e: self.remove_booking())
        except Exception:
            pass

        for ev in ("<Button-3>", "<Button-2>"):  # macOS pode ser Button-2
            try:
                self.listbox.bind(ev, self._show_trip_context_menu)
                self.bookings.bind(ev, self._show_booking_context_menu)
            except Exception:
                pass

    def _show_trip_context_menu(self, event):
        menu = tk.Menu(self, tearoff=0)
        has_sel = self.current_index is not None

        menu.add_command(label="Nova viagem", command=self.new_trip)
        menu.add_command(label="Nova (template)", command=self.new_trip_template)
        menu.add_separator()
        menu.add_command(
            label="Duplicar",
            command=self.duplicate_trip,
            state=(tk.NORMAL if has_sel else tk.DISABLED),
        )
        menu.add_command(
            label="Remover",
            command=self.delete_trip,
            state=(tk.NORMAL if has_sel else tk.DISABLED),
        )
        menu.add_separator()

        def _copy_id():
            if self.current_index is None:
                return
            tid = str(self.data["trips"][self.current_index].get("id", ""))
            self.clipboard_clear()
            self.clipboard_append(tid)

        def _copy_label():
            if self.current_index is None:
                return
            txt = make_trip_label(self.data["trips"][self.current_index])
            self.clipboard_clear()
            self.clipboard_append(txt)

        menu.add_command(label="Copiar id", command=_copy_id, state=(tk.NORMAL if has_sel else tk.DISABLED))
        menu.add_command(
            label="Copiar resumo (linha)",
            command=_copy_label,
            state=(tk.NORMAL if has_sel else tk.DISABLED),
        )

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _show_booking_context_menu(self, event):
        menu = tk.Menu(self, tearoff=0)
        has_trip = self.current_index is not None
        sel = self.bookings.selection()
        has_sel = bool(sel)

        menu.add_command(
            label="Adicionar reserva",
            command=self.add_booking,
            state=(tk.NORMAL if has_trip else tk.DISABLED),
        )
        menu.add_command(
            label="Atualizar seleção",
            command=self.update_booking,
            state=(tk.NORMAL if (has_trip and has_sel) else tk.DISABLED),
        )
        menu.add_command(
            label="Remover seleção",
            command=self.remove_booking,
            state=(tk.NORMAL if (has_trip and has_sel) else tk.DISABLED),
        )
        menu.add_separator()

        def _copy_booking_line():
            if not has_sel:
                return
            iid = sel[0]
            vals = self.bookings.item(iid, "values")
            txt = f"{vals[0]} ({vals[1]} → {vals[2]})" if len(vals) >= 3 else ""
            self.clipboard_clear()
            self.clipboard_append(txt)

        menu.add_command(label="Copiar reserva", command=_copy_booking_line, state=(tk.NORMAL if has_sel else tk.DISABLED))

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _update_dirty_ui(self):
        star = " *" if self.dirty else ""
        self.title(f"Editor de trips.json{star}")
        if self.file_path:
            extra = " (alterações pendentes)" if self.dirty else ""
            self.lbl_file.configure(text=f"Arquivo: {self.file_path}{extra}")
        else:
            self.lbl_file.configure(text="Arquivo: (nenhum)")
        
        self._update_controls_state()

    def _update_controls_state(self):
        has_file = self.file_path is not None
        has_sel = self.current_index is not None

        # Top
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

        # Trips
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

        # Bookings
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

    def _set_status(self, msg: str):
        try:
            self.lbl_status.configure(text=msg)
        except Exception:
            pass

    def _clear_validation(self):
        try:
            self.lbl_validation.configure(text="")
        except Exception:
            pass


    def _validation_error(self, message: str, focus_widget=None):
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

    def _append_log(self, text: str):
        if not hasattr(self, "txt_log"):
            return
        try:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            msg = f"[{ts}] {text}\n"
            self.txt_log.configure(state=tk.NORMAL)
            self.txt_log.insert(tk.END, msg)
            # Trim old lines
            lines = int(self.txt_log.index('end-1c').split('.')[0])
            if lines > self._log_lines_max:
                self.txt_log.delete('1.0', f"{lines - self._log_lines_max}.0")
            self.txt_log.see(tk.END)
            self.txt_log.configure(state=tk.DISABLED)
        except Exception:
            pass

    def _clear_log(self):
        if not hasattr(self, "txt_log"):
            return
        try:
            self.txt_log.configure(state=tk.NORMAL)
            self.txt_log.delete('1.0', tk.END)
            self.txt_log.configure(state=tk.DISABLED)
        except Exception:
            pass

    def _on_stop_autocomplete(self, _evt=None):
        """Autocomplete simples: completa a partir do prefixo digitado."""
        try:
            typed = (self.var_stop_new.get() or "").strip()
            if not typed:
                return
            typed_low = typed.lower()
            for city in CITIES_COMMON:
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

    def _select_month_button(self, month_num: int):
        """Select a month (1-12) using the current selected year."""
        year = (getattr(self, "var_year", None).get() if hasattr(self, "var_year") else "").strip()
        if not year:
            # If year is not set, try to infer from current var_month or default to current year
            cur = (self.var_month.get() or "").strip()
            if len(cur) >= 4 and cur[:4].isdigit():
                year = cur[:4]
            else:
                year = str(datetime.date.today().year)
            try:
                self.var_year.set(year)
            except Exception:
                pass

        self.var_month.set(f"{year}-{month_num:02d}")
        self._populate_calendar(self.var_month.get())
        self._update_month_buttons_state()

    def publish_to_github(self):
        if getattr(self, "_publishing", False):
            return
        self._publishing = True
        try:
            try:
                self.btn_publish.configure(state=tk.DISABLED)
            except Exception:
                pass
            self._append_log("—" * 40)
            self._append_log("Iniciando publicação...")
            # Choose a folder to run git commands from:
            # - if a json file is open, use its folder
            # - otherwise use the folder containing this script
            base_dir = os.path.dirname(self.file_path) if self.file_path else os.path.dirname(os.path.abspath(__file__))

            repo_root = find_repo_root(base_dir)
            if not repo_root:
                messagebox.showerror(
                    "GitHub",
                    "Não encontrei um repositório Git neste diretório.\n\n"
                    "Dica: abra esta pasta no terminal e rode:\n"
                    "  git init  (se ainda não)\n"
                    "  git remote add origin <SSH>\n"
                    "  git add .\n  git commit -m \"primeiro commit\"\n  git push -u origin main"
                )
                return
            ensure_ds_store_ignored(repo_root, log=self._append_log)

            # Ensure SSH agent/key are ready (avoids needing a separate terminal to type passphrase).
            if not ensure_ssh_auth_ready(self):
                return
            # Optional: quick diagnostic (doesn't block publishing if GitHub returns non-zero on success)
            try:
                test_github_ssh(self)
            except Exception:
                pass

            # Ensure file is saved first
            if self.dirty:
                if not messagebox.askyesno("GitHub", "Você tem alterações não salvas. Salvar antes de publicar?"):
                    return
                self.save_file()
                if self.dirty:
                    return  # save failed or user canceled

            # Ask commit message
            default_msg = f"atualiza calendário ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M')})"
            msg = simple_prompt(self, "Mensagem do commit", "Digite uma mensagem para o commit:", default_msg)
            if msg is None:
                return
            msg = msg.strip() or default_msg

            # --- Helper functions for status and staged files ---
            def _get_status_porcelain() -> str:
                scode, sout, serr = run_git(["status", "--porcelain"], cwd=repo_root, log=self._append_log)
                if scode != 0:
                    return ""
                return sout.strip()

            def _get_staged_names() -> list[str]:
                dcode, dout, derr = run_git(["diff", "--cached", "--name-only"], cwd=repo_root, log=self._append_log)
                if dcode != 0:
                    return []
                return [ln.strip() for ln in dout.splitlines() if ln.strip()]

            # Stage files
            status_before = _get_status_porcelain()
            if status_before and any(line.endswith(".DS_Store") and (line[:2].strip() != "??") for line in status_before.splitlines()):
                self._append_log(".DS_Store detectado; tentei ignorar/remover do índice automaticamente.")
                ensure_ds_store_ignored(repo_root, log=self._append_log)

            stage_all = False
            if self.file_path:
                stage_all = not messagebox.askyesno(
                    "GitHub",
                    "Publicar apenas o arquivo JSON aberto (recomendado)?\n\n"
                    "SIM → Apenas o arquivo atual\n"
                    "NÃO → Todos os arquivos do repositório",
                )

            if (not stage_all) and self.file_path:
                rel = os.path.relpath(self.file_path, repo_root)
                code, out, err = run_git(["add", rel], cwd=repo_root, log=self._append_log)
            else:
                code, out, err = run_git(["add", "-A"], cwd=repo_root, log=self._append_log)
            if code != 0:
                messagebox.showerror("GitHub", f"Falha no git add.\n\n{err or out}")
                return

            # If nothing is staged, offer to stage everything (common when JSON wasn't changed)
            staged = _get_staged_names()
            if not staged:
                if messagebox.askyesno(
                    "GitHub",
                    "Não há alterações preparadas para commit (staged).\n\n"
                    "Isso pode acontecer se o JSON não mudou, mas há outros arquivos modificados (ex.: editor_trips.py, .DS_Store).\n\n"
                    "Deseja preparar TODOS os arquivos do repositório para commit agora?",
                ):
                    code, out, err = run_git(["add", "-A"], cwd=repo_root, log=self._append_log)
                    if code != 0:
                        messagebox.showerror("GitHub", f"Falha no git add -A.\n\n{err or out}")
                        return
                    staged = _get_staged_names()

            # If still nothing staged, we can still push (useful if only pulling/rebasing), or abort.
            if not staged:
                if messagebox.askyesno(
                    "GitHub",
                    "Ainda não há nada para commitar.\n\n"
                    "Deseja tentar apenas o git push mesmo assim?",
                ):
                    code, out, err = run_git(["push"], cwd=repo_root, log=self._append_log)
                    if code != 0:
                        messagebox.showerror("GitHub", f"Falha no git push.\n\n{err or out}")
                        return
                    self._append_log("Push concluído ✅")
                    messagebox.showinfo("GitHub", "Push concluído ✅")
                return

            # Commit (may fail if nothing to commit)
            code, out, err = run_git(["commit", "-m", msg], cwd=repo_root, log=self._append_log)
            if code != 0:
                low_msg = (out + " " + err).lower()
                # If nothing to commit, allow pushing anyway (useful when remote changed, etc.)
                if ("nothing to commit" not in low_msg) and ("no changes added to commit" not in low_msg):
                    messagebox.showerror("GitHub", f"Falha no git commit.\n\n{err or out}")
                    return
                self._append_log("Nada para commitar (nenhuma mudança staged).")

            # Push
            code, out, err = run_git(["push"], cwd=repo_root, log=self._append_log)
            if code != 0:
                msg_all = (out + "\n" + err).strip().lower()

                # Common case: remote has commits not present locally (fetch first / rejected)
                if ("fetch first" in msg_all) or ("rejected" in msg_all) or ("non-fast-forward" in msg_all):
                    choice = messagebox.askyesnocancel(
                        "GitHub",
                        "O repositório remoto já tem commits e o push foi rejeitado.\n\n"
                        "SIM  → Integrar mudanças do remoto (git pull --rebase) e tentar de novo (recomendado)\n"
                        "NÃO  → Forçar push e sobrescrever o remoto (git push --force)\n"
                        "CANCELAR → Não fazer nada agora"
                    )
                    if choice is None:
                        return

                    if choice is True:
                        pcode, pout, perr = git_pull_rebase(repo_root, log=self._append_log)
                        if pcode != 0:
                            messagebox.showerror(
                                "GitHub",
                                "Falha ao integrar mudanças do remoto (git pull --rebase).\n\n"
                                f"{perr or pout}\n\n"
                                "Se aparecer conflito, resolva no VS Code e rode novamente.\n"
                                "Dica terminal:\n"
                                "  git status\n"
                                "  git rebase --continue\n"
                                "  git rebase --abort"
                            )
                            return

                        # Try push again
                        code2, out2, err2 = run_git(["push"], cwd=repo_root, log=self._append_log)
                        if code2 != 0:
                            messagebox.showerror("GitHub", f"Falha no git push após pull --rebase.\n\n{err2 or out2}")
                            return
                        self._append_log("Publicado com sucesso ✅")
                        messagebox.showinfo("GitHub", "Publicado com sucesso! ✅")
                        return

                    # Force push (overwrite remote)
                    fcode, fout, ferr = run_git(["push", "--force"], cwd=repo_root, log=self._append_log)
                    if fcode != 0:
                        messagebox.showerror("GitHub", f"Falha no git push --force.\n\n{ferr or fout}")
                        return
                    self._append_log("Publicado com sucesso ✅")
                    messagebox.showinfo("GitHub", "Publicado com sucesso! ✅")
                    return

                # Other push errors
                details = (err or out).strip()
                low = (out + "\n" + err).lower()

                if ("permission denied" in low) or ("publickey" in low) or ("could not read from remote repository" in low) or ("host key verification failed" in low) or ("batchmode" in low):
                    messagebox.showerror(
                        "GitHub",
                        "Falha de autenticação SSH ao publicar.\n\n"
                        f"{details}\n\n"
                        "Como corrigir (no Terminal):\n"
                        "1) Teste:  ssh -T git@github.com\n"
                        "2) Carregue a chave no agente (macOS):\n"
                        "   eval \"$(ssh-agent -s)\"\n"
                        "   ssh-add --apple-use-keychain ~/.ssh/id_ed25519\n"
                        "3) Confirme: ssh-add -l\n"
                        "4) Tente novamente o push.\n\n"
                        "Se sua rede bloquear a porta 22, configure GitHub via 443 em ~/.ssh/config:\n"
                        "Host github.com\n"
                        "  HostName ssh.github.com\n"
                        "  User git\n"
                        "  Port 443\n"
                        "  IdentityFile ~/.ssh/id_ed25519\n"
                    )
                    return

                messagebox.showerror(
                    "GitHub",
                    "Falha no git push.\n\n"
                    f"{details}\n\n"
                    "Dica: no terminal, confira:\n"
                    "  git remote -v\n"
                    "  git status\n"
                    "  git branch\n"
                )
                return

            self._append_log("Publicado com sucesso ✅")
            messagebox.showinfo("GitHub", "Publicado com sucesso! ✅")
        finally:
            self._publishing = False
            try:
                self.btn_publish.configure(state=tk.NORMAL)
            except Exception:
                pass

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
        save_last_json_path(path)
        self.lbl_file.configure(text=f"Arquivo: {path}")
        self.current_index = None
        self.dirty = False
        self._update_dirty_ui()
        self._update_controls_state()
        self.refresh_ui()

    def save_file(self):
        if self.file_path is None:
            return self.save_file_as()

        try:
            # backup antes de sobrescrever
            make_backup(self.file_path, max_backups=10)

            safe_save_json(self.file_path, self.data)
            self.dirty = False
            self._update_dirty_ui()
            self._append_log("Arquivo salvo ✅")
            self._set_status("Salvo")
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
        save_last_json_path(path)
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

        # Clear editor if no selection
        if self.current_index is None:
            self._load_trip_into_form(None)
            self._clear_validation()

        self._update_dirty_ui()
        self._update_controls_state()

        # Update calendar month/year options
        try:
            months = self._month_options()  # list of YYYY-MM
            years = sorted({m[:4] for m in months if len(m) >= 7})

            # Update year combobox
            try:
                self.cmb_year.configure(values=years)
            except Exception:
                pass

            # Choose a default selection
            if months:
                # Prefer existing selection if still valid
                cur = (self.var_month.get() or "").strip()
                if cur not in months:
                    cur = months[-1]
                    self.var_month.set(cur)

                # Set year from selected month
                sel_year = cur[:4]
                try:
                    if (not self.var_year.get()) or (self.var_year.get() not in years):
                        self.var_year.set(sel_year)
                    else:
                        # keep year, but align var_month to that year if needed
                        pass
                except Exception:
                    pass

                self._populate_calendar(self.var_month.get())
            else:
                self.var_month.set("")
                try:
                    self.var_year.set("")
                except Exception:
                    pass
                for iid in self.cal_tree.get_children():
                    self.cal_tree.delete(iid)

            self._update_month_buttons_state()
        except Exception:
            pass

    def _month_options(self) -> list[str]:
        months: set[str] = set()
        for t in self.data.get("trips", []):
            d = str(t.get("date", "")).strip()
            if DATE_RE.match(d):
                months.add(d[:7])  # YYYY-MM
        return sorted(months)

    def _populate_calendar(self, month: str):
        for iid in self.cal_tree.get_children():
            self.cal_tree.delete(iid)

        grouped: dict[str, list[dict]] = {}
        for idx, t in enumerate(self.data.get("trips", [])):
            d = str(t.get("date", "")).strip()
            if DATE_RE.match(d) and d.startswith(month):
                grouped.setdefault(d, []).append({"idx": idx, "trip": t})

        for d in sorted(grouped.keys()):
            labels = []
            for item in grouped[d]:
                t = item["trip"]
                direction = t.get("direction", "")
                short = "IDA" if direction == "ida" else ("VOLTA" if direction == "volta" else str(direction))
                title = (t.get("title", "") or "").strip()
                labels.append(f"{short} {title}".strip())
            iids = ",".join(str(item["idx"]) for item in grouped[d])
            self.cal_tree.insert("", tk.END, iid=iids, values=(d, " | ".join(labels)))

    def on_select_month(self, _evt=None):
        # If user changed year, keep the month number (if any) and switch year
        year = (self.var_year.get() or "").strip() if hasattr(self, "var_year") else ""
        cur = (self.var_month.get() or "").strip()

        if year and DATE_RE.match(cur + "-01") is None:
            # cur might be empty; choose January by default
            self.var_month.set(f"{year}-01")
        elif year and len(cur) >= 7 and cur[:4].isdigit():
            # preserve month part
            mm = cur[5:7] if len(cur) >= 7 else "01"
            self.var_month.set(f"{year}-{mm}")

        month = (self.var_month.get() or "").strip()
        if not month:
            return
        self._populate_calendar(month)
        self._update_month_buttons_state()

    def on_select_calendar_row(self, _evt=None):
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

    def _update_month_buttons_state(self):
        """Enable/disable month buttons based on available months and highlight selected month."""
        if not hasattr(self, "_month_btns"):
            return

        months = self._month_options()
        available = set()
        for m in months:
            if len(m) >= 7 and m[5:7].isdigit():
                available.add((m[:4], int(m[5:7])))

        sel = (self.var_month.get() or "").strip()
        sel_year = sel[:4] if len(sel) >= 7 else ""
        sel_m = int(sel[5:7]) if len(sel) >= 7 and sel[5:7].isdigit() else None

        cur_year = (self.var_year.get() or "").strip() if hasattr(self, "var_year") else sel_year

        for mnum, btn in self._month_btns.items():
            # enable only if there is at least one trip in that (year, month); if no year selected, enable all
            if cur_year:
                state = tk.NORMAL if (cur_year, mnum) in available else tk.DISABLED
            else:
                state = tk.NORMAL
            try:
                btn.configure(state=state)
            except Exception:
                pass

            # simple visual cue: put brackets around selected month
            try:
                base = btn.cget("text").strip("[]")
                if sel_m == mnum and sel_year == cur_year and cur_year:
                    btn.configure(text=f"[{base}]")
                else:
                    btn.configure(text=base)
            except Exception:
                pass

    def sort_trips(self):
        self.data["trips"].sort(key=lambda t: (t.get("date", ""), t.get("direction", ""), t.get("id", "")))
        self.dirty = True
        self._update_dirty_ui()
        self.refresh_ui()

    def add_stop(self):
        self._push_undo()
        s = (self.var_stop_new.get() if hasattr(self, "var_stop_new") else "").strip()
        if not s:
            return
        self.stops_listbox.insert(tk.END, s)
        self.var_stop_new.set("")
        self.dirty = True
        self._update_dirty_ui()

    def remove_stop(self):
        self._push_undo()
        sel = self.stops_listbox.curselection()
        if not sel:
            return
        self.stops_listbox.delete(sel[0])
        self.dirty = True
        self._update_dirty_ui()

    def move_stop_up(self):
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

    def move_stop_down(self):
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

    def on_select_trip(self, _evt=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        self.current_index = idx
        trip = self.data["trips"][idx]
        self._load_trip_into_form(trip)
        self._update_controls_state()

    def new_trip(self):
        self._push_undo()
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
        self._update_dirty_ui()
        self.refresh_ui()
        self.current_index = len(self.data["trips"]) - 1
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(self.current_index)
        self.listbox.see(self.current_index)
        self._load_trip_into_form(trip)

    def new_trip_template(self):
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

        direction = "ida" if choice is True else "volta"

        extra = messagebox.askyesno(
            "Template",
            "Incluir cidades opcionais na rota?\n\n"
            "• Juazeiro do Norte-CE\n"
            "• Brejo Santo-CE",
        )

        if direction == "ida":
            stops = ROUTE_IDA_DEFAULT.copy()
            if extra:
                if "Juazeiro do Norte-CE" not in stops:
                    stops.append("Juazeiro do Norte-CE")
                if "Brejo Santo-CE" not in stops:
                    stops.append("Brejo Santo-CE")
        else:
            if extra:
                ext = ROUTE_IDA_DEFAULT.copy() + ["Juazeiro do Norte-CE", "Brejo Santo-CE"]
                stops = list(reversed(ext))
            else:
                stops = ROUTE_VOLTA_DEFAULT.copy()

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

    def duplicate_trip(self):
        self._push_undo()
        if self.current_index is None:
            messagebox.showwarning("Selecione", "Selecione uma viagem para duplicar.")
            return
        src = self.data["trips"][self.current_index]
        dup = json.loads(json.dumps(src))  # deep copy
        dup["id"] = (dup.get("id", "") + "-copy").strip("-")
        self.data["trips"].append(dup)
        self.dirty = True
        self._update_dirty_ui()
        self.refresh_ui()

    def delete_trip(self):
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

    def apply_trip_changes(self):
        self._push_undo()
        if self.current_index is None:
            self._validation_error("Selecione uma viagem na lista para editar.")
            return
        self._clear_validation()

        tid = self.var_id.get().strip()
        date = self.var_date.get().strip()
        # Normalize date like 2026-2-3 -> 2026-02-03
        if date and "-" in date and DATE_RE.match(date) is None:
            parts = date.split("-")
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
            self._validation_error(
                "Data inválida. Use YYYY-MM-DD (ex.: 2026-02-03).",
                self.ent_date
            )
            return

        try:
            datetime.date.fromisoformat(date)
        except Exception:
            self._validation_error(
                "Data inexistente no calendário. Verifique dia/mês/ano.",
                self.ent_date
            )
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

        for i, t in enumerate(self.data["trips"]):
            if i != self.current_index and t.get("id") == tid:
                self._validation_error(f'Já existe outra viagem com id="{tid}".', self.ent_id)
                return

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

        # (bookings são editadas na seção abaixo)

        # ---------- Bookings CRUD ----------

    def _load_trip_into_form(self, trip: dict | None):
        """Carrega uma viagem no formulário; se trip=None, limpa tudo."""
        self._clear_validation()

        # Campos principais
        self.var_id.set("")
        self.var_date.set("")
        self.var_direction.set("ida")
        self.var_title.set("")
        self.var_capacity.set("")

        # Stops
        try:
            self.stops_listbox.delete(0, tk.END)
        except Exception:
            pass

        # Editor de reservas
        try:
            self.var_b_name.set("")
            self.var_b_from.set("")
            self.var_b_to.set("")
        except Exception:
            pass

        # Tabela de reservas
        try:
            self._reload_bookings([])
        except Exception:
            try:
                for iid in self.bookings.get_children():
                    self.bookings.delete(iid)
            except Exception:
                pass

        # Tabela de trechos
        try:
            self._refresh_segments_view(None)
        except Exception:
            pass

        if not trip:
            self._update_controls_state()
            return

        # Preenche campos
        self.var_id.set(str(trip.get("id", "")))
        self.var_date.set(str(trip.get("date", "")))
        self.var_direction.set(str(trip.get("direction", "ida")))
        self.var_title.set(str(trip.get("title", "")))
        self.var_capacity.set(str(trip.get("capacity", "")))

        # Preenche stops
        stops = trip.get("stops", [])
        if isinstance(stops, list):
            for s in stops:
                ss = str(s).strip()
                if ss:
                    self.stops_listbox.insert(tk.END, ss)

        # Preenche reservas
        self._reload_bookings(trip.get("bookings", []))

        # Atualiza vagas por trecho
        self._refresh_segments_view(trip)

        self._update_controls_state()

    def _reload_bookings(self, bookings: list[dict]):
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

    def on_select_booking(self, _evt=None):
        try:
            sel = self.bookings.selection()
            if not sel:
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

    def add_booking(self):
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

    def update_booking(self):
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

        row_index = self.bookings.index(sel[0])
        if row_index < 0 or row_index >= len(bookings):
            self._validation_error("Não consegui localizar esta reserva na lista.")
            return

        bookings[row_index] = {"name": name, "from": frm, "to": to}

        self._reload_bookings(bookings)
        self._refresh_segments_view(trip)
        self.dirty = True
        self._update_dirty_ui()
        self._set_status("Reserva atualizada")

    def remove_booking(self):
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

        row_index = self.bookings.index(sel[0])
        if row_index < 0 or row_index >= len(bookings):
            self._validation_error("Não consegui localizar esta reserva na lista.")
            return

        del bookings[row_index]

        self._reload_bookings(bookings)
        self._refresh_segments_view(trip)
        self.dirty = True
        self._update_dirty_ui()
        self._set_status("Reserva removida")


# Entrypoint to start the GUI
if __name__ == "__main__":
    app = TripsEditorApp()
    app.mainloop()