"""
Minimal Tkinter front-end.  Everything it does is a CLI command run in a
worker thread with its output captured into the text box, so the GUI can
never do anything the command line cannot.
"""
from __future__ import annotations

import io
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import cli
from .gamefiles import default_data_dir, find_game_dir


class _Writer(io.TextIOBase):
    def __init__(self, q: "queue.Queue[str]"):
        self.q = q

    def write(self, s: str) -> int:  # type: ignore[override]
        if s:
            self.q.put(s)
        return len(s)

    def flush(self) -> None:
        pass


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("L4D2 Index Buffer Fixer")
        self.geometry("1000x680")
        self.q: "queue.Queue[str]" = queue.Queue()
        self.busy = False

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Game folder:").grid(row=0, column=0, sticky="w")
        found = find_game_dir(None)
        self.game_var = tk.StringVar(value=str(found) if found else "")
        ttk.Entry(top, textvariable=self.game_var, width=80).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(top, text="Browse...", command=self._browse).grid(row=0, column=2)
        ttk.Label(top, text="Data folder:").grid(row=1, column=0, sticky="w")
        self.data_var = tk.StringVar(value=str(default_data_dir()))
        ttk.Entry(top, textvariable=self.data_var, width=80).grid(row=1, column=1, sticky="we", padx=4)
        top.columnconfigure(1, weight=1)

        opts = ttk.Frame(self, padding=(8, 0))
        opts.pack(fill="x")
        ttk.Label(opts, text="Fix level:").pack(side="left")
        self.level_var = tk.StringVar(value="HIGH")
        ttk.Combobox(opts, textvariable=self.level_var, values=["MODERATE", "HIGH", "CRITICAL"], width=10, state="readonly").pack(side="left", padx=4)
        ttk.Label(opts, text="Mode:").pack(side="left")
        self.mode_var = tk.StringVar(value="replace")
        ttk.Combobox(opts, textvariable=self.mode_var, values=["replace", "overlay", "inplace"], width=9, state="readonly").pack(side="left", padx=4)
        self.allmat_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="$nodecal on every material of flagged models", variable=self.allmat_var).pack(side="left", padx=8)
        self.verbose_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="verbose scan", variable=self.verbose_var).pack(side="left", padx=8)

        btns = ttk.Frame(self, padding=8)
        btns.pack(fill="x")
        for text, cmd in (
            ("Scan add-ons", self.scan),
            ("Preview fix (dry run)", lambda: self.fix(apply=False)),
            ("Apply fix", lambda: self.fix(apply=True)),
            ("Status", self.status),
            ("Revert all", self.revert),
            ("Diagnose", self.diagnose),
            ("Write probe cfg", self.probe),
        ):
            ttk.Button(btns, text=text, command=cmd).pack(side="left", padx=3)

        self.out = tk.Text(self, wrap="none", font=("Consolas", 9))
        self.out.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        ysb = ttk.Scrollbar(self.out, orient="vertical", command=self.out.yview)
        self.out.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.after(100, self._drain)
        self._log("Ready. Start with 'Scan add-ons'.\n")

    # ------------------------------------------------------------ helpers
    def _browse(self) -> None:
        d = filedialog.askdirectory(title="Select the 'Left 4 Dead 2' folder")
        if d:
            self.game_var.set(d)

    def _log(self, s: str) -> None:
        self.out.insert("end", s)
        self.out.see("end")

    def _drain(self) -> None:
        try:
            while True:
                self._log(self.q.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _run(self, argv: list) -> None:
        if self.busy:
            messagebox.showinfo("Busy", "A command is still running.")
            return
        base = []
        if self.game_var.get().strip():
            base += ["--game-dir", self.game_var.get().strip()]
        if self.data_var.get().strip():
            base += ["--data-dir", self.data_var.get().strip()]
        argv = base + argv
        self.busy = True
        self._log(f"\n$ ibfix {' '.join(argv)}\n")

        def work() -> None:
            w = _Writer(self.q)
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = w, w
            try:
                cli.main(argv)
            except SystemExit as e:
                if e.code not in (0, None):
                    self.q.put(f"\n{e.code}\n")
            except Exception as e:  # pragma: no cover
                self.q.put(f"\nERROR: {e!r}\n")
            finally:
                sys.stdout, sys.stderr = old_out, old_err
                self.q.put("\n[done]\n")
                self.busy = False

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ actions
    def scan(self) -> None:
        argv = ["scan"]
        if self.verbose_var.get():
            argv.append("--verbose")
        self._run(argv)

    def fix(self, apply: bool) -> None:
        argv = ["fix", "--level", self.level_var.get(), "--mode", self.mode_var.get()]
        if self.allmat_var.get():
            argv.append("--all-materials")
        if apply:
            if not messagebox.askyesno("Apply fix", "Write patched add-on copies and edit addonlist.txt?\nEverything is recorded and can be undone with 'Revert all'."):
                return
            argv.append("--apply")
        self._run(argv)

    def status(self) -> None:
        self._run(["status"])

    def revert(self) -> None:
        if messagebox.askyesno("Revert", "Undo every fix made by this tool?"):
            self._run(["revert", "--all"])

    def diagnose(self) -> None:
        self._run(["diagnose"])

    def probe(self) -> None:
        self._run(["probe"])


def main() -> int:
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
