"""File Organizer - sort files into folders by type.

Features: custom rules, preview, watch mode, skips in-progress downloads,
import/export rules, duplicate finder, full run history with undo,
and a system tray icon (optional: pip install pystray pillow).
"""
import hashlib
import json
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:  # optional, only needed for the system tray icon
    import pystray
    from PIL import Image
    TRAY_OK = True
except Exception:
    TRAY_OK = False

APP_NAME = "FileOrganizer"
_BASE = Path(os.getenv("APPDATA") or Path.home()) / APP_NAME
_BASE.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = _BASE / "config.json"

MAX_HISTORY = 30        # runs kept for undo
WATCH_INTERVAL = 3      # seconds between watch checks
SETTLE_SECONDS = 3      # a file must be untouched this long before watch mode moves it
DUP_FOLDER = "_duplicates"
# temporary files that browsers/downloaders create while a download is running
PARTIAL_EXTS = {".crdownload", ".part", ".partial", ".tmp", ".download",
                ".opdownload", ".aria2", ".!ut"}

DEFAULT_RULES = [
    {"folder": "csv_files", "exts": [".csv"]},
    {"folder": "text_files", "exts": [".txt"]},
    {"folder": "image_files", "exts": [".png", ".jpg", ".jpeg", ".webp"]},
    {"folder": "executable_files", "exts": [".exe", ".msi"]},
    {"folder": "audio_files", "exts": [".mp3", ".wav"]},
    {"folder": "custom_skins", "exts": [".fantome"]},
    {"folder": "zip_files", "exts": [".zip", ".rar", ".7z"]},
    {"folder": "video_files", "exts": [".mp4", ".mkv", ".mov", ".avi", ".webm"]},
    {"folder": "document_files", "exts": [".pdf", ".doc", ".docx", ".odt", ".rtf"]},
    {"folder": "spreadsheet_files", "exts": [".xlsx", ".xls", ".ods"]},
    {"folder": "presentation_files", "exts": [".ppt", ".pptx", ".odp"]},
    {"folder": "code_files", "exts": [".py", ".js", ".java", ".html", ".css", ".cpp", ".json"]},
    {"folder": "disk_images", "exts": [".iso", ".img"]},
]

PRESETS = {
    "Archives": ".zip, .rar, .7z, .tar, .gz",
    "Images": ".jpg, .jpeg, .png, .gif, .webp",
    "Audio": ".mp3, .wav, .flac, .ogg",
    "Video": ".mp4, .mkv, .avi, .mov",
    "Documents": ".pdf, .doc, .docx, .pptx",
    "Code": ".py, .js, .java, .cpp, .html, .css",
    "Installers": ".exe, .msi",
}


# ---------------------------------------------------------------- core logic
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def load_config():
    cfg = {"approved": {}, "folder": "", "rules": [dict(r) for r in DEFAULT_RULES],
           "sort_other": False, "history": [], "watch": False, "tray": True}
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    old = cfg.pop("last_moves", None)  # migrate the old single-undo format
    if old:
        cfg["history"].append({"time": now_str(), "kind": "organize",
                               "folder": cfg.get("folder", ""), "moves": old})
    return cfg


def save_config(cfg):
    try:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
    except OSError:
        pass


def parse_exts(text):
    """'zip, .RAR 7z' -> ['.zip', '.rar', '.7z']"""
    out = []
    for part in text.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip().lower()
        if part:
            out.append(part if part.startswith(".") else "." + part)
    return list(dict.fromkeys(out))


def safe_folder_name(name):
    bad = '<>:"/\\|?*'
    return "".join(c for c in name.strip() if c not in bad).strip(". ")


def find_conflicts(others, folder, exts):
    """Check a new/edited rule against the other rules.
    Returns (name_clash_folder or None, {ext: owning_folder})."""
    name_clash = next((r["folder"] for r in others
                       if r["folder"].lower() == folder.lower()), None)
    owners = {}
    for r in others:
        for e in r["exts"]:
            owners[e] = r["folder"]
    return name_clash, {e: owners[e] for e in exts if e in owners}


def validate_rules(data):
    """Clean up imported rules. Returns (rules, warnings) or raises ValueError."""
    if not isinstance(data, list):
        raise ValueError("That file doesn't contain a list of rules.")
    clean, warns, seen_ext, seen_names = [], [], {}, set()
    for r in data:
        if not isinstance(r, dict):
            continue
        folder = safe_folder_name(str(r.get("folder", "")))
        raw = r.get("exts", [])
        exts = parse_exts(",".join(map(str, raw)) if isinstance(raw, list) else str(raw))
        if not folder or not exts:
            warns.append("Skipped a rule with no folder name or no file types.")
            continue
        if folder.lower() in seen_names:
            warns.append(f"Skipped a second rule for '{folder}'.")
            continue
        keep = []
        for e in exts:
            if e in seen_ext:
                warns.append(f"{e} is already used by '{seen_ext[e]}', left out of '{folder}'.")
            else:
                seen_ext[e] = folder
                keep.append(e)
        if keep:
            seen_names.add(folder.lower())
            clean.append({"folder": folder, "exts": keep})
    if not clean:
        raise ValueError("No valid rules found in that file.")
    return clean, warns


def unique_path(dest):
    if not dest.exists():
        return dest
    i = 1
    while True:
        candidate = dest.with_name(f"{dest.stem} ({i}){dest.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def plan_moves(folder, rules, sort_other, settle=0):
    """List (source, destination) pairs without touching anything.
    Skips hidden files, in-progress downloads, and (if settle>0) files changed recently."""
    lookup = {}
    for rule in rules:
        for ext in rule["exts"]:
            lookup.setdefault(ext, rule["folder"])
    moves = []
    for item in Path(folder).iterdir():
        try:
            if not item.is_file() or item.name.startswith("."):
                continue
            if item.suffix.lower() in PARTIAL_EXTS:
                continue
            if settle and time.time() - item.stat().st_mtime < settle:
                continue
        except OSError:
            continue
        target = lookup.get(item.suffix.lower())
        if target is None and sort_other:
            target = "other"
        if target:
            moves.append((item, Path(folder) / target / item.name))
    return moves


def needed_folders(rules, sort_other):
    names = [r["folder"] for r in rules]
    if sort_other:
        names.append("other")
    return list(dict.fromkeys(names))


def existing_folders(folder, names):
    return [n for n in names if (Path(folder) / n).is_dir()]


def create_folders(folder, names):
    for n in names:
        (Path(folder) / n).mkdir(parents=True, exist_ok=True)


def run_moves(moves):
    """Move files. A file that is locked or vanished is skipped, not fatal."""
    done, failed = [], []
    for src, dest in moves:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            final = unique_path(dest)
            shutil.move(str(src), str(final))
            done.append([str(src), str(final)])
        except OSError:
            failed.append(str(src))
    return done, failed


def undo_moves(moves):
    restored = 0
    for src, dest in reversed(moves):
        d, s = Path(dest), Path(src)
        try:
            if d.exists() and not s.exists():
                s.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(d), str(s))
                restored += 1
            d.parent.rmdir()  # remove the folder if it is now empty
        except OSError:
            pass
    return restored


def add_history(cfg, kind, folder, done):
    """Record a run. Watch-mode moves within an hour are merged into one entry."""
    if not done:
        return
    hist = cfg.setdefault("history", [])
    if kind == "watch" and hist and hist[-1]["kind"] == "watch" and hist[-1]["folder"] == folder:
        try:
            last = datetime.strptime(hist[-1]["time"], "%Y-%m-%d %H:%M")
            if datetime.now() - last < timedelta(hours=1):
                hist[-1]["moves"].extend(done)
                return
        except ValueError:
            pass
    hist.append({"time": now_str(), "kind": kind, "folder": folder, "moves": done})
    del hist[:-MAX_HISTORY]


def silent_pass(cfg, settle=0):
    """One organize run with no prompts (used at startup and for 'Run now')."""
    folder = cfg.get("folder", "")
    if not folder or not Path(folder).is_dir():
        return [], []
    create_folders(folder, needed_folders(cfg["rules"], cfg["sort_other"]))
    done, failed = run_moves(plan_moves(folder, cfg["rules"], cfg["sort_other"], settle))
    add_history(cfg, "organize", folder, done)
    save_config(cfg)
    return done, failed


def file_hash(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def find_duplicates(folder):
    """Groups of identical files (same size, then same SHA-256), oldest first."""
    by_size = {}
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != DUP_FOLDER and not d.startswith(".")]
        for n in files:
            if n.startswith("."):
                continue
            p = Path(root) / n
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > 0:
                by_size.setdefault(size, []).append(p)
    groups = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        by_hash = {}
        for p in paths:
            try:
                by_hash.setdefault(file_hash(p), []).append(p)
            except OSError:
                pass
        for same in by_hash.values():
            if len(same) > 1:
                same.sort(key=lambda x: (x.stat().st_mtime, str(x)))
                groups.append((size, same))
    groups.sort(key=lambda g: -g[0] * (len(g[1]) - 1))
    return groups


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ------------------------------------------------------------------- startup
def startup_command():
    if getattr(sys, "frozen", False):
        # keep a permanent copy so deleting the downloaded EXE can't break startup
        permanent = _BASE / Path(sys.executable).name
        try:
            if Path(sys.executable).resolve() != permanent.resolve():
                shutil.copy2(sys.executable, permanent)
        except OSError:
            permanent = Path(sys.executable)
        return f'"{permanent}" --background'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    return f'"{exe}" "{Path(__file__).resolve()}" --background'


def startup_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except Exception:
        return False


def set_startup(enable):
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion\Run",
                         0, winreg.KEY_SET_VALUE)
    with key:
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def find_asset(name):
    """Look for an optional file next to the app (or inside the PyInstaller bundle)."""
    places = [Path(getattr(sys, "_MEIPASS", "")), Path(sys.executable).parent,
              Path(__file__).resolve().parent]
    for base in places:
        if (base / name).is_file():
            return base / name
    return None


# ----------------------------------------------------------------------- GUI
BG, PANEL, FIELD = "#16181d", "#1f232b", "#2a2f39"
FG, MUTED, ACCENT = "#e8eaed", "#8b93a1", "#5b8def"


def apply_theme(root):
    root.configure(bg=BG)
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=BG, foreground=FG, fieldbackground=FIELD,
                font=("Segoe UI", 10), borderwidth=0)
    s.configure("Panel.TFrame", background=PANEL)
    s.configure("TLabel", background=BG, foreground=FG)
    s.configure("Muted.TLabel", background=BG, foreground=MUTED)
    s.configure("Title.TLabel", font=("Segoe UI Semibold", 16))
    s.configure("TButton", background=FIELD, foreground=FG, padding=(12, 7))
    s.map("TButton", background=[("active", "#353b47")])
    s.configure("Accent.TButton", background=ACCENT, foreground="white",
                font=("Segoe UI Semibold", 10))
    s.map("Accent.TButton", background=[("active", "#4a7bdb")])
    s.configure("TCheckbutton", background=BG, foreground=FG)
    s.map("TCheckbutton", background=[("active", BG)],
          foreground=[("disabled", MUTED)])
    s.configure("TEntry", fieldbackground=FIELD, foreground=FG, padding=6)
    s.configure("TCombobox", fieldbackground=FIELD, foreground=FG, padding=5,
                arrowcolor=FG, background=FIELD)
    s.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                foreground=FG, rowheight=28)
    s.map("Treeview", background=[("selected", ACCENT)])
    s.configure("Treeview.Heading", background=FIELD, foreground=FG,
                font=("Segoe UI Semibold", 10), padding=6)


class RuleDialog(tk.Toplevel):
    def __init__(self, parent, rule=None, others=None):
        super().__init__(parent, bg=BG)
        self.others = others or []
        self.title("Edit rule" if rule else "New rule")
        self.resizable(False, False)
        self.transient(parent)
        self.result = None

        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="Folder name").grid(row=0, column=0, sticky="w")
        self.name = ttk.Entry(body, width=38)
        self.name.grid(row=1, column=0, sticky="ew", pady=(2, 12))

        ttk.Label(body, text="File types (comma separated)").grid(row=2, column=0, sticky="w")
        self.exts = ttk.Entry(body, width=38)
        self.exts.grid(row=3, column=0, sticky="ew", pady=(2, 6))
        ttk.Label(body, text="Example: .zip, .rar, .7z", style="Muted.TLabel").grid(
            row=4, column=0, sticky="w")

        ttk.Label(body, text="Quick fill").grid(row=5, column=0, sticky="w", pady=(12, 0))
        self.preset = ttk.Combobox(body, values=list(PRESETS), state="readonly")
        self.preset.grid(row=6, column=0, sticky="ew", pady=(2, 16))
        self.preset.bind("<<ComboboxSelected>>", self._fill)

        btns = ttk.Frame(body)
        btns.grid(row=7, column=0, sticky="e")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=6)
        ttk.Button(btns, text="Save", style="Accent.TButton",
                   command=self._save).pack(side="left")

        if rule:
            self.name.insert(0, rule["folder"])
            self.exts.insert(0, ", ".join(rule["exts"]))
        self.name.focus_set()
        self.bind("<Return>", lambda e: self._save())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.wait_window()

    def _fill(self, _event):
        label = self.preset.get()
        self.exts.delete(0, "end")
        self.exts.insert(0, PRESETS[label])
        if not self.name.get().strip():
            self.name.insert(0, label.lower())

    def _save(self):
        folder = safe_folder_name(self.name.get())
        exts = parse_exts(self.exts.get())
        if not folder:
            messagebox.showwarning("Missing name", "Enter a folder name.", parent=self)
            return
        if not exts:
            messagebox.showwarning("Missing file types",
                                   "Enter at least one file type, e.g. .zip", parent=self)
            return
        name_clash, ext_clash = find_conflicts(self.others, folder, exts)
        if name_clash:
            messagebox.showwarning(
                "Folder already has a rule",
                f"A rule for '{name_clash}' already exists.\n"
                "Edit that rule instead of making a second one.", parent=self)
            return
        if ext_clash:
            lines = "\n".join(f"  \u2022 {e}  (already in '{f}')"
                              for e, f in ext_clash.items())
            messagebox.showwarning(
                "File type already used",
                f"A file type can only go in one folder:\n\n{lines}\n\n"
                "Remove it from the other rule first, or take it out of this one.",
                parent=self)
            return
        self.result = {"folder": folder, "exts": exts}
        self.destroy()


class HistoryDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app, bg=BG)
        self.app = app
        self.title("Run history")
        self.geometry("720x420")
        self.transient(app)

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Undo any past run, newest at the top.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 8))

        self.tree = ttk.Treeview(body, columns=("when", "kind", "files", "folder"),
                                 show="headings", height=12)
        for col, text, width in (("when", "When", 130), ("kind", "Type", 90),
                                 ("files", "Files", 60), ("folder", "Folder", 380)):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=width, stretch=(col == "folder"))
        self.tree.pack(fill="both", expand=True)

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Undo selected run", style="Accent.TButton",
                   command=self.undo_selected).pack(side="left")
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        hist = self.app.cfg["history"]
        for i in range(len(hist) - 1, -1, -1):
            h = hist[i]
            self.tree.insert("", "end", iid=str(i),
                             values=(h["time"], h["kind"], len(h["moves"]), h["folder"]))

    def undo_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        self.app.undo_entry(int(sel[0]))
        self.refresh()


class DuplicateDialog(tk.Toplevel):
    def __init__(self, app, folder):
        super().__init__(app, bg=BG)
        self.app, self.folder, self.groups = app, folder, []
        self.results = queue.Queue()
        self.title("Find duplicates")
        self.geometry("740x520")
        self.transient(app)

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        self.status = ttk.Label(body, text="Scanning for identical files\u2026")
        self.status.pack(anchor="w")
        ttk.Label(body, text=f"Extra copies are moved to a '{DUP_FOLDER}' folder, never deleted. "
                  "You can undo from History.", style="Muted.TLabel").pack(anchor="w", pady=(2, 8))

        self.tree = ttk.Treeview(body, columns=("size", "action"), show="tree headings")
        self.tree.heading("#0", text="File", anchor="w")
        self.tree.heading("size", text="Size", anchor="w")
        self.tree.heading("action", text="Action", anchor="w")
        self.tree.column("#0", width=470)
        self.tree.column("size", width=90, stretch=False)
        self.tree.column("action", width=90, stretch=False)
        self.tree.pack(fill="both", expand=True)

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        self.move_btn = ttk.Button(btns, text="Move extra copies", style="Accent.TButton",
                                   command=self.move_extras, state="disabled")
        self.move_btn.pack(side="left")
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")

        threading.Thread(target=self._scan, daemon=True).start()
        self.after(200, self._check)

    def _scan(self):
        try:
            self.results.put(find_duplicates(self.folder))
        except Exception as e:  # never leave the dialog stuck on "Scanning"
            self.results.put(e)

    def _check(self):
        try:
            res = self.results.get_nowait()
        except queue.Empty:
            if self.winfo_exists():
                self.after(200, self._check)
            return
        if isinstance(res, Exception):
            self.status.config(text=f"Scan failed: {res}")
            return
        self.groups = res
        if not res:
            self.status.config(text="No duplicate files found.")
            return
        spare = sum(size * (len(p) - 1) for size, p in res)
        self.status.config(text=f"Found {len(res)} set(s) of duplicates "
                                f"({fmt_size(spare)} in extra copies).")
        for i, (size, paths) in enumerate(res):
            gid = self.tree.insert("", "end", iid=f"g{i}", open=True,
                                   text=f"{len(paths)} identical copies",
                                   values=(fmt_size(size), ""))
            for j, p in enumerate(paths):
                rel = os.path.relpath(p, self.folder)
                self.tree.insert(gid, "end", text=rel,
                                 values=("", "keep" if j == 0 else "move"))
        self.move_btn.config(state="normal")

    def move_extras(self):
        dup_dir = Path(self.folder) / DUP_FOLDER
        moves = [(p, dup_dir / p.name) for _, paths in self.groups for p in paths[1:]]
        done, failed = run_moves(moves)
        add_history(self.app.cfg, "duplicates", self.folder, done)
        self.app._persist()
        msg = f"Moved {len(done)} duplicate file(s) to '{DUP_FOLDER}'."
        if failed:
            msg += f" {len(failed)} skipped (in use)."
        self.app.log(msg)
        self.destroy()


class App(tk.Tk):
    def __init__(self, background=False):
        super().__init__()
        self.title("File Organizer")
        self.geometry("800x800")
        self.minsize(740, 720)
        apply_theme(self)
        self.cfg = load_config()
        self.q = queue.Queue()      # messages from the tray thread
        self.tray = None
        self.watching = False
        self._last_watch = 0.0

        self.folder_var = tk.StringVar(value=self.cfg["folder"])
        self.other_var = tk.BooleanVar(value=self.cfg["sort_other"])
        self.startup_var = tk.BooleanVar(value=startup_enabled())
        self.watch_var = tk.BooleanVar(value=False)
        self.tray_var = tk.BooleanVar(value=bool(self.cfg.get("tray")) and TRAY_OK)

        self.icon_img = self.logo_img = None
        for name, attr in (("walrus_icon.png", "icon_img"), ("walrus_logo.png", "logo_img")):
            path = find_asset(name)
            if path:
                try:
                    setattr(self, attr, tk.PhotoImage(file=str(path)))
                except tk.TclError:
                    pass
        if self.icon_img:
            self.iconphoto(True, self.icon_img)
        ico = find_asset("walrus.ico")
        if ico and os.name == "nt":
            try:
                self.iconbitmap(str(ico))
            except tk.TclError:
                pass

        self._build()
        self._refresh_rules()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.log("Ready. Pick a folder, then Preview or Organize." if self.cfg["folder"]
                 else "No folder chosen yet. Click Browse to pick one - it will be remembered.")
        if not TRAY_OK:
            self.log("Tray icon is off. To enable it: pip install pystray pillow")
        self._resume_watch()
        self.after(250, self._poll)
        if background:
            self.after(400, self._background_start)

    # -- layout
    def _build(self):
        wrap = ttk.Frame(self, padding=20)
        wrap.pack(fill="both", expand=True)

        header = ttk.Frame(wrap)
        header.pack(fill="x", pady=(0, 14))
        if self.logo_img:
            ttk.Label(header, image=self.logo_img).pack(side="right")
        titles = ttk.Frame(header)
        titles.pack(side="left", anchor="center")
        ttk.Label(titles, text="File Organizer", style="Title.TLabel").pack(anchor="w")
        ttk.Label(titles, text="Sort a folder into sub-folders by file type.",
                  style="Muted.TLabel").pack(anchor="w")

        row = ttk.Frame(wrap)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.folder_var).pack(
            side="left", fill="x", expand=True, ipady=2)
        ttk.Button(row, text="Browse\u2026", command=self.pick_folder).pack(side="left", padx=(8, 0))

        head = ttk.Frame(wrap)
        head.pack(fill="x", pady=(16, 6))
        ttk.Label(head, text="Rules", font=("Segoe UI Semibold", 11)).pack(side="left")
        ttk.Button(head, text="Remove", command=self.remove_rule).pack(side="right")
        ttk.Button(head, text="Edit", command=self.edit_rule).pack(side="right", padx=6)
        ttk.Button(head, text="+ Add rule", command=self.add_rule).pack(side="right")

        self.tree = ttk.Treeview(wrap, columns=("folder", "exts"), show="headings", height=7)
        self.tree.heading("folder", text="Folder", anchor="w")
        self.tree.heading("exts", text="File types", anchor="w")
        self.tree.column("folder", width=170, stretch=False)
        self.tree.column("exts", width=500)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.edit_rule())

        tools = ttk.Frame(wrap)
        tools.pack(fill="x", pady=(8, 0))
        ttk.Button(tools, text="Import rules", command=self.import_rules).pack(side="left")
        ttk.Button(tools, text="Export rules", command=self.export_rules).pack(side="left", padx=6)
        ttk.Button(tools, text="Reset to default", command=self.reset_rules).pack(side="left")

        opts = ttk.Frame(wrap)
        opts.pack(fill="x", pady=(12, 0))
        opts.columnconfigure(0, weight=1)
        opts.columnconfigure(1, weight=1)
        ttk.Checkbutton(opts, text="Watch folder and sort new files automatically",
                        variable=self.watch_var, command=self.toggle_watch).grid(
            row=0, column=0, sticky="w", pady=2)
        ttk.Checkbutton(opts, text="Put unmatched files in an 'other' folder",
                        variable=self.other_var, command=self._persist).grid(
            row=1, column=0, sticky="w", pady=2)
        tray_text = ("Keep running in the system tray when closed" if TRAY_OK
                     else "System tray (needs: pip install pystray pillow)")
        ttk.Checkbutton(opts, text=tray_text, variable=self.tray_var,
                        command=self.toggle_tray,
                        state="normal" if TRAY_OK else "disabled").grid(
            row=0, column=1, sticky="w", pady=2)
        ttk.Checkbutton(opts, text="Run in the background when Windows starts",
                        variable=self.startup_var, command=self.toggle_startup).grid(
            row=1, column=1, sticky="w", pady=2)

        actions = ttk.Frame(wrap)
        actions.pack(fill="x", pady=14)
        ttk.Button(actions, text="Organize", style="Accent.TButton",
                   command=self.organize).pack(side="left")
        ttk.Button(actions, text="Preview", command=self.preview).pack(side="left", padx=6)
        ttk.Button(actions, text="Undo last run", command=self.undo).pack(side="left")
        ttk.Button(actions, text="History", command=self.open_history).pack(side="left", padx=6)
        ttk.Button(actions, text="Find duplicates", command=self.open_duplicates).pack(side="left")

        self.log_box = tk.Text(wrap, height=6, bg=PANEL, fg=MUTED, relief="flat",
                               font=("Consolas", 9), padx=10, pady=8, state="disabled",
                               highlightthickness=0, wrap="word")
        self.log_box.pack(fill="x")

    # -- helpers
    def log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{datetime.now():%H:%M}  {text}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _persist(self):
        self.cfg["folder"] = self.folder_var.get()
        self.cfg["sort_other"] = self.other_var.get()
        save_config(self.cfg)

    def _refresh_rules(self):
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.cfg["rules"]):
            self.tree.insert("", "end", iid=str(i), values=(r["folder"], ", ".join(r["exts"])))

    def _selected(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _valid_folder(self):
        folder = self.folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("No folder", "Click Browse and pick a folder first.")
            return None
        return folder

    def _key(self, folder):
        return os.path.normcase(os.path.abspath(folder))

    def _unapproved_clash(self, folder, names):
        approved = set(self.cfg.setdefault("approved", {}).get(self._key(folder), []))
        return [n for n in existing_folders(folder, names) if n not in approved]

    def _confirm_folders(self, folder, names):
        """Ask once about existing folders; remember a Yes."""
        clash = self._unapproved_clash(folder, names)
        if clash:
            listed = "\n".join(f"  \u2022 {n}" for n in clash)
            if not messagebox.askyesno(
                "Folders already exist",
                f"These folders already exist in this location:\n\n{listed}\n\n"
                "They will be replaced with the organizer's folders. Files already "
                "inside them are kept, and new files are added in.\n\n"
                "You will only be asked once.\n\nContinue?",
                icon="warning",
            ):
                return False
        approved = set(self.cfg["approved"].get(self._key(folder), []))
        self.cfg["approved"][self._key(folder)] = sorted(approved | set(names))
        return True

    # -- rules
    def pick_folder(self):
        f = filedialog.askdirectory(initialdir=self.folder_var.get() or str(Path.home()))
        if f:
            self.folder_var.set(os.path.normpath(f))
            self._persist()

    def add_rule(self):
        dlg = RuleDialog(self, others=self.cfg["rules"])
        if dlg.result:
            self.cfg["rules"].append(dlg.result)
            self._refresh_rules()
            self._persist()

    def edit_rule(self):
        i = self._selected()
        if i is None:
            return
        others = [r for j, r in enumerate(self.cfg["rules"]) if j != i]
        dlg = RuleDialog(self, self.cfg["rules"][i], others=others)
        if dlg.result:
            self.cfg["rules"][i] = dlg.result
            self._refresh_rules()
            self._persist()

    def remove_rule(self):
        i = self._selected()
        if i is not None:
            del self.cfg["rules"][i]
            self._refresh_rules()
            self._persist()

    def reset_rules(self):
        if messagebox.askyesno("Reset rules", "Replace all rules with the defaults?"):
            self.cfg["rules"] = [dict(r) for r in DEFAULT_RULES]
            self._refresh_rules()
            self._persist()

    def export_rules(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("JSON", "*.json")],
            initialfile="file_organizer_rules.json")
        if not path:
            return
        data = {"app": APP_NAME, "version": 1, "sort_other": self.other_var.get(),
                "rules": self.cfg["rules"]}
        try:
            Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
            self.log(f"Exported {len(self.cfg['rules'])} rule(s) to {Path(path).name}")
        except OSError as e:
            messagebox.showerror("Export failed", str(e))

    def import_rules(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            raw = data["rules"] if isinstance(data, dict) else data
            clean, warns = validate_rules(raw)
        except (OSError, ValueError, KeyError, TypeError) as e:
            messagebox.showerror("Import failed", f"Couldn't read that rules file.\n\n{e}")
            return
        msg = f"Import {len(clean)} rule(s)? This replaces your current rules."
        if warns:
            msg += "\n\nNotes:\n" + "\n".join(f"  \u2022 {w}" for w in warns[:8])
        if not messagebox.askyesno("Import rules", msg):
            return
        self.cfg["rules"] = clean
        self._refresh_rules()
        self._persist()
        self.log(f"Imported {len(clean)} rule(s).")

    # -- organizing
    def preview(self):
        folder = self._valid_folder()
        if not folder:
            return
        moves = plan_moves(folder, self.cfg["rules"], self.other_var.get())
        if not moves:
            self.log("Preview: nothing to move.")
            return
        self.log(f"Preview: {len(moves)} file(s) would move:")
        for src, dest in moves[:25]:
            self.log(f"  {src.name}  ->  {dest.parent.name}/")
        if len(moves) > 25:
            self.log(f"  \u2026and {len(moves) - 25} more")

    def organize(self):
        folder = self._valid_folder()
        if not folder:
            return
        rules, sort_other = self.cfg["rules"], self.other_var.get()
        names = needed_folders(rules, sort_other)
        if not self._confirm_folders(folder, names):
            self.log("Cancelled - nothing was changed.")
            return
        new_folders = [n for n in names if not (Path(folder) / n).is_dir()]
        try:
            create_folders(folder, names)
            done, failed = run_moves(plan_moves(folder, rules, sort_other))
        except OSError as e:
            messagebox.showerror("Error", f"Could not complete the run:\n{e}")
            return
        add_history(self.cfg, "organize", folder, done)
        self._persist()
        note = f" {len(failed)} skipped (in use)." if failed else ""
        self.log(f"Created {len(new_folders)} new folder(s), moved {len(done)} file(s).{note}")
        messagebox.showinfo("Done", f"{len(done)} file(s) organized.{note}")

    # -- undo / history
    def undo_entry(self, index):
        hist = self.cfg["history"]
        if not 0 <= index < len(hist):
            return
        entry = hist.pop(index)
        restored = undo_moves(entry["moves"])
        self._persist()
        self.log(f"Undo ({entry['kind']}, {entry['time']}): restored {restored} file(s).")

    def undo(self):
        if not self.cfg["history"]:
            self.log("Nothing to undo.")
            return
        self.undo_entry(len(self.cfg["history"]) - 1)

    def open_history(self):
        HistoryDialog(self)

    def open_duplicates(self):
        folder = self._valid_folder()
        if folder:
            DuplicateDialog(self, folder)

    # -- watch mode
    def _resume_watch(self):
        folder = self.cfg.get("folder", "")
        if self.cfg.get("watch") and folder and Path(folder).is_dir():
            names = needed_folders(self.cfg["rules"], self.cfg["sort_other"])
            if not self._unapproved_clash(folder, names):
                self.watching = True
                self.watch_var.set(True)
                self.log("Watching " + folder)

    def toggle_watch(self, from_tray=False):
        want = (not self.watching) if from_tray else self.watch_var.get()
        if want:
            folder = self._valid_folder()
            names = needed_folders(self.cfg["rules"], self.other_var.get())
            if not folder or not self._confirm_folders(folder, names):
                want = False
            else:
                create_folders(folder, names)
                self.log(f"Watching {folder} - new files are sorted automatically.")
        else:
            self.log("Stopped watching.")
        self.watching = want
        self.watch_var.set(want)
        self.cfg["watch"] = want
        self._persist()

    def _watch_tick(self):
        folder = self.folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            return
        try:
            moves = plan_moves(folder, self.cfg["rules"], self.other_var.get(),
                               settle=SETTLE_SECONDS)
            if not moves:
                return
            done, _failed = run_moves(moves)
        except OSError:
            return
        if done:
            add_history(self.cfg, "watch", folder, done)
            self._persist()
            self.log(f"Watching: sorted {len(done)} new file(s).")

    def _poll(self):
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        if self.watching and time.monotonic() - self._last_watch >= WATCH_INTERVAL:
            self._last_watch = time.monotonic()
            self._watch_tick()
        self.after(250, self._poll)

    # -- tray
    def toggle_tray(self):
        self.cfg["tray"] = self.tray_var.get()
        self._persist()

    def _tray_image(self):
        path = find_asset("walrus_icon.png")
        if path:
            try:
                return Image.open(path).convert("RGBA")
            except Exception:
                pass
        return Image.new("RGBA", (64, 64), (91, 141, 239, 255))

    def _start_tray(self):
        if not TRAY_OK or self.tray:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Open File Organizer", lambda: self.q.put("show"), default=True),
            pystray.MenuItem("Watch folder", lambda: self.q.put("toggle_watch"),
                             checked=lambda item: self.watching),
            pystray.MenuItem("Run now", lambda: self.q.put("run")),
            pystray.MenuItem("Quit", lambda: self.q.put("quit")),
        )
        self.tray = pystray.Icon(APP_NAME, self._tray_image(), "File Organizer", menu)
        self.tray.run_detached()

    def _handle(self, msg):
        if msg == "show":
            self.deiconify()
            self.lift()
            self.focus_force()
        elif msg == "toggle_watch":
            self.toggle_watch(from_tray=True)
        elif msg == "run":
            self._run_now()
        elif msg == "quit":
            self._quit()

    def _run_now(self):
        folder = self.folder_var.get().strip()
        names = needed_folders(self.cfg["rules"], self.other_var.get())
        if folder and Path(folder).is_dir() and not self._unapproved_clash(folder, names):
            self._persist()
            done, failed = silent_pass(self.cfg)
            note = f" {len(failed)} skipped (in use)." if failed else ""
            self.log(f"Run now: moved {len(done)} file(s).{note}")
        else:
            self._handle("show")   # needs a click on the folder warning
            self.organize()

    def _background_start(self):
        folder = self.cfg.get("folder", "")
        if not folder or not Path(folder).is_dir():
            return  # nothing set up yet, so stay visible
        names = needed_folders(self.cfg["rules"], self.cfg["sort_other"])
        if not self._unapproved_clash(folder, names):
            done, _ = silent_pass(self.cfg)
            self.log(f"Startup: moved {len(done)} file(s).")
        if TRAY_OK:
            self._start_tray()
            self.withdraw()
        else:
            self.iconify()

    def _on_close(self):
        self._persist()
        if self.tray_var.get() and TRAY_OK:
            self._start_tray()
            self.withdraw()
            return
        if self.watching and not messagebox.askyesno(
                "Still watching",
                "Watching is on. Closing the app will stop it.\nClose anyway?"):
            return
        self._quit()

    def _quit(self):
        self._persist()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.destroy()

    # -- startup
    def toggle_startup(self):
        try:
            set_startup(self.startup_var.get())
            self.log("Startup " + ("enabled." if self.startup_var.get() else "disabled."))
        except Exception as e:
            self.startup_var.set(False)
            messagebox.showerror("Startup", f"Only works on Windows.\n{e}")


if __name__ == "__main__":
    if "--auto" in sys.argv:        # older startup entries: run once and exit
        silent_pass(load_config())
    else:
        App(background="--background" in sys.argv).mainloop()
