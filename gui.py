#!/usr/bin/env python3
"""
MetaGatherer GUI — desktop launcher for the FB/TikTok ad scanner.
Run with:  python gui.py
"""

import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, scrolledtext, ttk

# ── Colour palette ──────────────────────────────────────────────────────────
BG       = "#1a1a2e"
BG2      = "#16213e"
BG3      = "#0f3460"
ACCENT   = "#e94560"
ACCENT2  = "#00b4d8"
FG       = "#eaeaea"
FG_DIM   = "#888888"
GREEN    = "#4caf50"
YELLOW   = "#ffc107"
RED      = "#f44336"

FONT        = ("Segoe UI", 10)
FONT_BOLD   = ("Segoe UI", 10, "bold")
FONT_TITLE  = ("Segoe UI", 14, "bold")
FONT_MONO   = ("Consolas", 9)


# ── Logging handler that feeds a Queue ──────────────────────────────────────
class QueueHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        self.log_queue.put(self.format(record))


# ── Main window ─────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MetaGatherer")
        self.geometry("900x720")
        self.minsize(800, 600)
        self.configure(bg=BG)
        self._running   = False
        self._thread    = None
        self._log_queue = queue.Queue()
        self._build()
        self._poll_log()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build(self):
        # Title bar
        title = tk.Label(
            self, text="MetaGatherer", font=FONT_TITLE,
            bg=BG, fg=ACCENT,
        )
        title.pack(pady=(16, 4))
        tk.Label(
            self, text="Facebook Ads Library  ·  TikTok  product scanner",
            font=FONT, bg=BG, fg=FG_DIM,
        ).pack()

        # ── Main content: left settings + right log ──────────────────────────
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=12)

        left  = tk.Frame(body, bg=BG, width=300)
        right = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="y", padx=(0, 12))
        left.pack_propagate(False)
        right.pack(side="left", fill="both", expand=True)

        # ── Settings panel ───────────────────────────────────────────────────
        self._section(left, "Platforms")
        self.v_facebook = tk.BooleanVar(value=True)
        self.v_tiktok   = tk.BooleanVar(value=True)
        self._check(left, "Facebook Ads Library", self.v_facebook)
        self._check(left, "TikTok (50k views / 30 days)", self.v_tiktok)

        tk.Button(
            left, text="🔑  TikTok Login",
            font=FONT, bg=BG3, fg=FG,
            activebackground=ACCENT2, bd=0, pady=4, cursor="hand2",
            command=self._tiktok_login,
        ).pack(fill="x", pady=(4, 0))

        self._section(left, "Search")
        self.v_niche    = self._row_entry(left, "Niche", "e.g. dogs, fitness, jewelry")
        self.v_keywords = self._row_entry(left, "Extra keywords", "comma-separated")

        self._section(left, "Filters")
        self.v_country    = self._row_entry(left, "Country",      "US", width=6)
        self.v_days       = self._row_spin(left,  "Ad lookback",  1, 90,  7)
        self.v_min_ads    = self._row_spin(left,  "Min ads",      1, 100, 5)
        self.v_max_kw     = self._row_spin(left,  "Max keywords", 5, 200, 30)
        self.v_min_fol    = self._row_spin(left,  "Min followers",0, 9999, 10)
        self.v_max_fol    = self._row_spin(left,  "Max followers",100,500000, 2000)

        self._section(left, "Options")
        self.v_headless   = tk.BooleanVar(value=False)
        self.v_reset      = tk.BooleanVar(value=False)
        self.v_no_csv     = tk.BooleanVar(value=False)
        self._check(left, "Headless (no visible browser)", self.v_headless)
        self._check(left, "Reset saved state",              self.v_reset)
        self._check(left, "Skip CSV export",                self.v_no_csv)

        # Output path row
        out_row = tk.Frame(left, bg=BG)
        out_row.pack(fill="x", pady=2)
        tk.Label(out_row, text="Output CSV", font=FONT, bg=BG, fg=FG, width=14, anchor="w"
                 ).pack(side="left")
        self.v_output = tk.StringVar()
        tk.Entry(out_row, textvariable=self.v_output, font=FONT,
                 bg=BG2, fg=FG, insertbackground=FG, bd=0,
                 highlightbackground=BG3, highlightthickness=1, width=12
                 ).pack(side="left", expand=True, fill="x")
        tk.Button(out_row, text="…", font=FONT, bg=BG3, fg=FG,
                  activebackground=ACCENT2, bd=0, padx=6,
                  command=self._browse_output).pack(side="left", padx=(4, 0))

        # Start / Stop
        self.btn = tk.Button(
            left, text="▶  START SCAN",
            font=("Segoe UI", 11, "bold"),
            bg=ACCENT, fg="white", activebackground="#c73652",
            bd=0, pady=10, cursor="hand2",
            command=self._toggle,
        )
        self.btn.pack(fill="x", pady=(16, 4))

        self.status_var = tk.StringVar(value="Ready")
        tk.Label(left, textvariable=self.status_var, font=FONT,
                 bg=BG, fg=FG_DIM).pack()

        # ── Log panel ────────────────────────────────────────────────────────
        log_header = tk.Frame(right, bg=BG)
        log_header.pack(fill="x")
        tk.Label(log_header, text="Live output", font=FONT_BOLD,
                 bg=BG, fg=FG).pack(side="left")
        tk.Button(log_header, text="Clear", font=FONT, bg=BG2, fg=FG_DIM,
                  activebackground=BG3, bd=0, padx=8,
                  command=self._clear_log).pack(side="right")

        self.log_box = scrolledtext.ScrolledText(
            right, font=FONT_MONO, bg="#0d0d1a", fg=FG,
            insertbackground=FG, wrap="word", bd=0,
            highlightbackground=BG3, highlightthickness=1,
            state="disabled",
        )
        self.log_box.pack(fill="both", expand=True, pady=(6, 0))

        # colour tags
        self.log_box.tag_config("INFO",    foreground=FG)
        self.log_box.tag_config("WARNING", foreground=YELLOW)
        self.log_box.tag_config("ERROR",   foreground=RED)
        self.log_box.tag_config("SUCCESS", foreground=GREEN)
        self.log_box.tag_config("DIM",     foreground=FG_DIM)

    # ── Widget helpers ───────────────────────────────────────────────────────

    def _section(self, parent, text: str):
        f = tk.Frame(parent, bg=BG3, height=1)
        f.pack(fill="x", pady=(12, 4))
        tk.Label(parent, text=text.upper(), font=("Segoe UI", 8, "bold"),
                 bg=BG, fg=ACCENT2).pack(anchor="w")

    def _check(self, parent, text: str, var: tk.BooleanVar):
        cb = tk.Checkbutton(
            parent, text=text, variable=var, font=FONT,
            bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
            selectcolor=BG3, cursor="hand2",
        )
        cb.pack(anchor="w", pady=1)

    def _row_entry(self, parent, label: str, placeholder: str = "", width: int = 18):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label, font=FONT, bg=BG, fg=FG,
                 width=14, anchor="w").pack(side="left")
        var = tk.StringVar()
        e = tk.Entry(row, textvariable=var, font=FONT,
                     bg=BG2, fg=FG, insertbackground=FG, bd=0,
                     highlightbackground=BG3, highlightthickness=1, width=width)
        e.pack(side="left", expand=True, fill="x")
        if placeholder:
            e.insert(0, placeholder)
            e.config(fg=FG_DIM)
            def on_focus_in(_, w=e, p=placeholder):
                if w.get() == p:
                    w.delete(0, "end"); w.config(fg=FG)
            def on_focus_out(_, w=e, p=placeholder):
                if not w.get():
                    w.insert(0, p); w.config(fg=FG_DIM)
            e.bind("<FocusIn>",  on_focus_in)
            e.bind("<FocusOut>", on_focus_out)
            var._placeholder = placeholder
        return var

    def _row_spin(self, parent, label: str, from_: int, to: int, default: int):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label, font=FONT, bg=BG, fg=FG,
                 width=14, anchor="w").pack(side="left")
        var = tk.IntVar(value=default)
        tk.Spinbox(row, from_=from_, to=to, textvariable=var, font=FONT,
                   bg=BG2, fg=FG, buttonbackground=BG3,
                   insertbackground=FG, bd=0,
                   highlightbackground=BG3, highlightthickness=1,
                   width=8).pack(side="left")
        return var

    # ── Actions ──────────────────────────────────────────────────────────────

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self.v_output.set(path)

    def _clear_log(self):
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.config(state="disabled")

    def _toggle(self):
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self):
        if not self.v_facebook.get() and not self.v_tiktok.get():
            self._append("ERROR", "Select at least one platform.\n")
            return

        self._running = True
        self.btn.config(text="■  STOP", bg="#555")
        self.status_var.set("Running…")

        self._thread = threading.Thread(target=self._run_scan, daemon=True)
        self._thread.start()

    def _stop(self):
        # Just flag it — the subprocess will finish its current operation
        self._running = False
        self.btn.config(text="▶  START SCAN", bg=ACCENT)
        self.status_var.set("Stopping…")

    def _tiktok_login(self):
        """Run --tiktok-login in a background thread (opens browser for manual login)."""
        if self._running:
            self._append("WARNING", "Stop the current scan before logging in.\n")
            return
        self._append("DIM", "Opening TikTok login browser — log in, then close or wait.\n")
        self.status_var.set("TikTok login…")
        threading.Thread(target=self._run_tiktok_login_proc, daemon=True).start()

    def _run_tiktok_login_proc(self):
        py  = sys.executable
        cmd = [py, os.path.join(os.path.dirname(__file__), "main.py"), "--tiktok-login"]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                bufsize=1,
                cwd=os.path.dirname(__file__),
                env=env,
            )
            for line in proc.stdout:
                self._classify_and_append(line)
            proc.wait()
            if proc.returncode == 0:
                self._append("SUCCESS", "\nTikTok session saved — you can now run a scan.\n")
            else:
                self._append("WARNING", f"\nTikTok login exited with code {proc.returncode}.\n")
        except Exception as exc:
            self._append("ERROR", f"\nFailed to start TikTok login: {exc}\n")
        finally:
            self.after(0, lambda: self.status_var.set("Ready"))

    def _run_scan(self):
        """Build the command and run it as a subprocess, streaming output."""
        py  = sys.executable
        cmd = [py, os.path.join(os.path.dirname(__file__), "main.py")]

        # Platforms
        if not self.v_facebook.get():
            cmd.append("--no-facebook")
        if not self.v_tiktok.get():
            cmd.append("--no-tiktok")

        # Niche
        niche_val = self.v_niche.get().strip()
        if niche_val and niche_val != "e.g. dogs, fitness, jewelry":
            cmd += ["--niche", niche_val]

        # Extra keywords
        kw_val = self.v_keywords.get().strip()
        if kw_val and kw_val != "comma-separated":
            for kw in [k.strip() for k in kw_val.split(",") if k.strip()]:
                cmd += ["-k", kw]

        # Numeric options
        cmd += [
            "--countries",       self.v_country.get().strip() or "US",
            "--days",            str(self.v_days.get()),
            "--min-ads",         str(self.v_min_ads.get()),
            "--max-keywords",    str(self.v_max_kw.get()),
            "--min-followers",   str(self.v_min_fol.get()),
            "--max-followers",   str(self.v_max_fol.get()),
        ]

        # Flags
        if self.v_headless.get():  cmd.append("--headless")
        if self.v_reset.get():     cmd.append("--reset")
        if self.v_no_csv.get():    cmd.append("--no-csv")

        out_path = self.v_output.get().strip()
        if out_path:
            cmd += ["--output", out_path]

        self._append("DIM", f"$ {' '.join(cmd)}\n\n")

        # Force UTF-8 so Rich's box-drawing chars don't crash on Windows cp1252
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                bufsize=1,
                cwd=os.path.dirname(__file__),
                env=env,
            )
            for line in proc.stdout:
                if not self._running:
                    proc.terminate()
                    break
                self._classify_and_append(line)
            proc.wait()
            code = proc.returncode
            if code == 0:
                self._append("SUCCESS", "\n✓ Scan complete.\n")
            else:
                self._append("WARNING", f"\nProcess exited with code {code}.\n")
        except Exception as exc:
            self._append("ERROR", f"\nFailed to start scan: {exc}\n")
        finally:
            self.after(0, self._scan_done)

    def _scan_done(self):
        self._running = False
        self.btn.config(text="▶  START SCAN", bg=ACCENT)
        self.status_var.set("Done")

    # ── Log output ───────────────────────────────────────────────────────────

    def _classify_and_append(self, line: str):
        line = line.rstrip("\n") + "\n"
        lower = line.lower()
        if "error" in lower or "traceback" in lower or "exception" in lower:
            tag = "ERROR"
        elif "warning" in lower or "warn" in lower:
            tag = "WARNING"
        elif any(x in lower for x in ["winner", "✓", "shopify", "scraped"]):
            tag = "SUCCESS"
        elif line.startswith(" ") or "[dim]" in line:
            tag = "DIM"
        else:
            tag = "INFO"
        self._log_queue.put((tag, line))

    def _append(self, tag: str, text: str):
        self._log_queue.put((tag, text))

    def _poll_log(self):
        """Drain the log queue and write to the text box (runs on main thread)."""
        try:
            while True:
                item = self._log_queue.get_nowait()
                if isinstance(item, tuple):
                    tag, text = item
                else:
                    tag, text = "INFO", item
                self.log_box.config(state="normal")
                self.log_box.insert("end", text, tag)
                self.log_box.see("end")
                self.log_box.config(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_log)


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = App()
    app.mainloop()
