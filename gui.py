#!/usr/bin/env python3
"""
MetaGatherer GUI — desktop launcher for the FB/TikTok ad scanner.
Run with:  python gui.py
"""

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, scrolledtext, ttk

# Load .env into os.environ immediately so the AI expert can read the API key
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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
PURPLE   = "#9c27b0"

FONT        = ("Segoe UI", 10)
FONT_BOLD   = ("Segoe UI", 10, "bold")
FONT_TITLE  = ("Segoe UI", 14, "bold")
FONT_MONO   = ("Consolas", 9)

# Control / inject files — same names the scraper reads
_CTRL_FILE   = "mg_control.json"
_INJECT_FILE = "mg_inject.json"


def _write_ctrl(cmd: str):
    try:
        with open(_CTRL_FILE, "w") as f:
            json.dump({"cmd": cmd}, f)
    except Exception:
        pass


def _write_inject(keywords: list):
    try:
        with open(_INJECT_FILE, "w") as f:
            json.dump(keywords, f)
    except Exception:
        pass


# ── Main window ─────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MetaGatherer")
        self.geometry("1100x900")
        self.minsize(900, 780)
        self.configure(bg=BG)

        self._running   = False
        self._paused    = False
        self._thread    = None
        self._log_queue = queue.Queue()

        # Scan stats tracked from log output — fed to the AI expert
        self._stats = {
            "keywords_searched": [],
            "total_ads": 0,
            "products_found": 0,
            "last_keyword": "",
            "last_ads": 0,
        }
        self._expert       = None  # lazy-loaded DropshippingExpert
        self._expert_lock  = threading.Lock()
        self._keywords_done = 0   # how many keywords have completed this run

        self._build()
        self._poll_log()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build(self):
        # Title bar
        tk.Label(self, text="MetaGatherer", font=FONT_TITLE, bg=BG, fg=ACCENT).pack(pady=(14, 2))
        tk.Label(self, text="Facebook Ads Library  ·  TikTok  product scanner",
                 font=FONT, bg=BG, fg=FG_DIM).pack()

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=10)

        # ── Scrollable left panel (so Controls never get pushed off-screen) ─────
        left_outer = tk.Frame(body, bg=BG, width=290)
        left_outer.pack(side="left", fill="y", padx=(0, 10))
        left_outer.pack_propagate(False)

        _lcanvas = tk.Canvas(left_outer, bg=BG, highlightthickness=0, width=274)
        _lscroll = ttk.Scrollbar(left_outer, orient="vertical", command=_lcanvas.yview)
        _lcanvas.configure(yscrollcommand=_lscroll.set)
        _lscroll.pack(side="right", fill="y")
        _lcanvas.pack(side="left", fill="both", expand=True)

        left = tk.Frame(_lcanvas, bg=BG)
        _lcw  = _lcanvas.create_window((0, 0), window=left, anchor="nw")

        def _on_left_resize(e):
            _lcanvas.configure(scrollregion=_lcanvas.bbox("all"))
            _lcanvas.itemconfig(_lcw, width=_lcanvas.winfo_width())
        left.bind("<Configure>", _on_left_resize)
        _lcanvas.bind("<Configure>", lambda e: _lcanvas.itemconfig(_lcw, width=e.width))

        def _on_scroll(e):
            _lcanvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        _lcanvas.bind("<MouseWheel>", _on_scroll)
        left.bind("<MouseWheel>", _on_scroll)

        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        # ── Settings ─────────────────────────────────────────────────────────
        self._section(left, "Platforms")
        self.v_facebook = tk.BooleanVar(value=True)
        self.v_tiktok   = tk.BooleanVar(value=True)
        self._check(left, "Facebook Ads Library", self.v_facebook)
        self._check(left, "TikTok (50k views / 30 days)", self.v_tiktok)
        tk.Button(left, text="🔑  TikTok Login", font=FONT, bg=BG3, fg=FG,
                  activebackground=ACCENT2, bd=0, pady=4, cursor="hand2",
                  command=self._tiktok_login).pack(fill="x", pady=(4, 0))

        self._section(left, "Search")
        self.v_niche    = self._row_entry(left, "Niche", "e.g. dogs, fitness, jewelry")
        self.v_keywords = self._row_entry(left, "Extra keywords", "comma-separated")

        # Niche queue — overnight chaining (one niche per line)
        _nq_hdr = tk.Frame(left, bg=BG)
        _nq_hdr.pack(fill="x", pady=(6, 0))
        tk.Label(_nq_hdr, text="Niche queue", font=FONT, bg=BG, fg=FG,
                 width=14, anchor="w").pack(side="left")
        tk.Label(_nq_hdr, text="one per line · overnight chaining",
                 font=("Segoe UI", 8), bg=BG, fg=FG_DIM).pack(side="left")
        self._nq_placeholder = "fitness gear\njewelry\nhome gadgets"
        self.niche_queue_box = tk.Text(
            left, font=FONT, bg=BG2, fg=FG_DIM,
            insertbackground=FG, bd=0,
            highlightbackground=BG3, highlightthickness=1,
            height=3, wrap="word")
        self.niche_queue_box.insert("1.0", self._nq_placeholder)
        self.niche_queue_box.pack(fill="x", pady=(2, 4))

        def _nq_focus_in(e):
            if self.niche_queue_box.get("1.0", "end").strip() == self._nq_placeholder:
                self.niche_queue_box.delete("1.0", "end")
                self.niche_queue_box.config(fg=FG)
        def _nq_focus_out(e):
            if not self.niche_queue_box.get("1.0", "end").strip():
                self.niche_queue_box.insert("1.0", self._nq_placeholder)
                self.niche_queue_box.config(fg=FG_DIM)
        self.niche_queue_box.bind("<FocusIn>",  _nq_focus_in)
        self.niche_queue_box.bind("<FocusOut>", _nq_focus_out)

        self._section(left, "Filters")
        self.v_country     = self._row_entry(left, "Country",       "US", width=6)
        self.v_days        = self._row_spin(left,  "Ad lookback",   1, 90,  7)
        self.v_min_ads      = self._row_spin(left,  "Min ads",        1, 100,    5)
        self.v_max_kw       = self._row_spin(left,  "Max keywords",   5, 200,   30)
        self.v_min_fol      = self._row_spin(left,  "Min followers",  0, 9999,  10)
        self.v_max_fol      = self._row_spin(left,  "Max followers", 10, 50000, 400)
        self.v_max_tot_ads  = self._row_spin(left,  "Max total ads",  0, 5000,  250)
        self.v_active_ratio = self._row_spin(left,  "Min active %",   0, 100,    85)

        self._section(left, "Options")
        self.v_headless     = tk.BooleanVar(value=False)
        self.v_reset        = tk.BooleanVar(value=False)
        self.v_no_csv       = tk.BooleanVar(value=False)
        self.v_until_winner = tk.BooleanVar(value=False)
        self._check(left, "Headless (no visible browser)", self.v_headless)
        self._check(left, "Reset saved state",              self.v_reset)

        # ── Fast Mode (Apify) ────────────────────────────────────────────────
        self._section(left, "⚡ Fast Mode (Apify)")
        ap_row = tk.Frame(left, bg=BG)
        ap_row.pack(fill="x", pady=2)
        tk.Label(ap_row, text="Apify Key", font=FONT, bg=BG, fg=FG,
                 width=9, anchor="w").pack(side="left")
        self.v_apify_key = tk.StringVar(value=os.environ.get("APIFY_API_KEY", ""))
        tk.Entry(ap_row, textvariable=self.v_apify_key, font=FONT,
                 bg=BG2, fg=FG, insertbackground=FG, bd=0,
                 highlightbackground=BG3, highlightthickness=1,
                 show="*").pack(side="left", expand=True, fill="x")
        tk.Button(ap_row, text="Save", font=FONT, bg=BG3, fg=FG,
                  activebackground=GREEN, bd=0, padx=6,
                  command=self._save_apify_key).pack(side="left", padx=(4, 0))
        self.apify_status = tk.Label(left, text="", font=("Segoe UI", 8),
                                     bg=BG, fg=FG_DIM)
        self.apify_status.pack(anchor="w")
        self._refresh_apify_status()

        # ── API key ───────────────────────────────────────────────────────────
        self._section(left, "AI Expert (Anthropic)")
        api_row = tk.Frame(left, bg=BG)
        api_row.pack(fill="x", pady=2)
        tk.Label(api_row, text="API Key", font=FONT, bg=BG, fg=FG,
                 width=8, anchor="w").pack(side="left")
        self.v_api_key = tk.StringVar(value=self._load_api_key())
        api_entry = tk.Entry(api_row, textvariable=self.v_api_key, font=FONT,
                             bg=BG2, fg=FG, insertbackground=FG, bd=0,
                             highlightbackground=BG3, highlightthickness=1,
                             show="*")          # mask like a password field
        api_entry.pack(side="left", expand=True, fill="x")
        tk.Button(api_row, text="Save", font=FONT, bg=BG3, fg=FG,
                  activebackground=GREEN, bd=0, padx=6,
                  command=self._save_api_key).pack(side="left", padx=(4, 0))
        self.api_status = tk.Label(left, text="", font=("Segoe UI", 8),
                                   bg=BG, fg=FG_DIM)
        self.api_status.pack(anchor="w")
        self._refresh_api_status()
        wh_row = tk.Frame(left, bg=BG)
        wh_row.pack(fill="x", pady=2)
        tk.Label(wh_row, text="Discord WH", font=FONT, bg=BG, fg=FG,
                 width=8, anchor="w").pack(side="left")
        self.v_discord_wh = tk.StringVar(value=os.environ.get("DISCORD_WEBHOOK_URL", ""))
        tk.Entry(wh_row, textvariable=self.v_discord_wh, font=FONT,
                 bg=BG2, fg=FG, insertbackground=FG, bd=0,
                 highlightbackground=BG3, highlightthickness=1,
                 show="*").pack(side="left", expand=True, fill="x")
        tk.Button(wh_row, text="Save", font=FONT, bg=BG3, fg=FG,
                  activebackground=GREEN, bd=0, padx=6,
                  command=self._save_discord_webhook).pack(side="left", padx=(4, 0))
        self._check(left, "Skip CSV export",                self.v_no_csv)

        out_row = tk.Frame(left, bg=BG)
        out_row.pack(fill="x", pady=2)
        tk.Label(out_row, text="Output CSV", font=FONT, bg=BG, fg=FG,
                 width=14, anchor="w").pack(side="left")
        self.v_output = tk.StringVar()
        tk.Entry(out_row, textvariable=self.v_output, font=FONT,
                 bg=BG2, fg=FG, insertbackground=FG, bd=0,
                 highlightbackground=BG3, highlightthickness=1, width=10
                 ).pack(side="left", expand=True, fill="x")
        tk.Button(out_row, text="…", font=FONT, bg=BG3, fg=FG,
                  activebackground=ACCENT2, bd=0, padx=6,
                  command=self._browse_output).pack(side="left", padx=(4, 0))

        # ── Control buttons ───────────────────────────────────────────────────
        self._section(left, "Controls")

        self._check(left, "Keep scanning until winner", self.v_until_winner)

        # Overnight mode — chain queued niches until N winners found
        overnight_row = tk.Frame(left, bg=BG)
        overnight_row.pack(fill="x", pady=1)
        self.v_overnight = tk.BooleanVar(value=False)
        tk.Checkbutton(overnight_row, text="Overnight — chain niches",
                       variable=self.v_overnight, font=FONT,
                       bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
                       selectcolor=BG3, cursor="hand2").pack(side="left")
        tk.Label(overnight_row, text="→", font=FONT, bg=BG, fg=FG_DIM).pack(side="left", padx=4)
        self.v_winner_target = tk.IntVar(value=10)
        tk.Spinbox(overnight_row, from_=1, to=100, textvariable=self.v_winner_target,
                   font=FONT, bg=BG2, fg=FG, buttonbackground=BG3,
                   insertbackground=FG, bd=0,
                   highlightbackground=BG3, highlightthickness=1,
                   width=4).pack(side="left")
        tk.Label(overnight_row, text="winners", font=FONT, bg=BG, fg=FG_DIM).pack(
            side="left", padx=(4, 0))

        tk.Button(left, text="🗑  Clear ban list", font=FONT, bg=BG3, fg=FG,
                  activebackground=YELLOW, bd=0, pady=4, cursor="hand2",
                  command=self._clear_ban_list).pack(fill="x", pady=(4, 2))

        btn_row1 = tk.Frame(left, bg=BG)
        btn_row1.pack(fill="x", pady=(4, 2))

        self.btn_start = tk.Button(
            btn_row1, text="▶  START", font=FONT_BOLD,
            bg=GREEN, fg="white", activebackground="#388e3c",
            bd=0, pady=6, cursor="hand2", command=self._start)
        self.btn_start.pack(side="left", expand=True, fill="x", padx=(0, 2))

        self.btn_pause = tk.Button(
            btn_row1, text="⏸  PAUSE", font=FONT_BOLD,
            bg=YELLOW, fg="#111", activebackground="#f9a825",
            bd=0, pady=6, cursor="hand2", command=self._pause,
            state="disabled")
        self.btn_pause.pack(side="left", expand=True, fill="x", padx=(2, 0))

        btn_row2 = tk.Frame(left, bg=BG)
        btn_row2.pack(fill="x", pady=(0, 4))

        self.btn_continue = tk.Button(
            btn_row2, text="▶  CONTINUE", font=FONT_BOLD,
            bg=ACCENT2, fg="white", activebackground="#0096b7",
            bd=0, pady=6, cursor="hand2", command=self._resume,
            state="disabled")
        self.btn_continue.pack(side="left", expand=True, fill="x", padx=(0, 2))

        self.btn_stop = tk.Button(
            btn_row2, text="⏹  STOP", font=FONT_BOLD,
            bg=ACCENT, fg="white", activebackground="#c73652",
            bd=0, pady=6, cursor="hand2", command=self._stop,
            state="disabled")
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=(2, 0))

        self.status_var = tk.StringVar(value="Ready")
        tk.Label(left, textvariable=self.status_var, font=FONT,
                 bg=BG, fg=FG_DIM).pack()

        # ── Right panel: Notebook tabs ─────────────────────────────────────────
        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("Dark.TNotebook", background=BG, borderwidth=0)
        style.configure("Dark.TNotebook.Tab", background=BG3, foreground=FG,
                        padding=[10, 4], font=FONT)
        style.map("Dark.TNotebook.Tab",
                  background=[("selected", ACCENT2)],
                  foreground=[("selected", "white")])

        self._notebook = ttk.Notebook(right, style="Dark.TNotebook")
        self._notebook.pack(fill="both", expand=True)

        # ── Tab 1: Scan (log + AI Expert) ─────────────────────────────────────
        scan_tab = tk.Frame(self._notebook, bg=BG)
        self._notebook.add(scan_tab, text="  Scan  ")

        pane = tk.PanedWindow(scan_tab, orient="vertical", bg=BG,
                              sashwidth=6, sashrelief="flat", sashpad=2)
        pane.pack(fill="both", expand=True)

        # Log panel (top)
        log_frame = tk.Frame(pane, bg=BG)
        log_header = tk.Frame(log_frame, bg=BG)
        log_header.pack(fill="x")
        tk.Label(log_header, text="Live output", font=FONT_BOLD,
                 bg=BG, fg=FG).pack(side="left")
        tk.Button(log_header, text="Clear", font=FONT, bg=BG2, fg=FG_DIM,
                  activebackground=BG3, bd=0, padx=8,
                  command=self._clear_log).pack(side="right")
        self.log_box = scrolledtext.ScrolledText(
            log_frame, font=FONT_MONO, bg="#0d0d1a", fg=FG,
            insertbackground=FG, wrap="word", bd=0,
            highlightbackground=BG3, highlightthickness=1, state="disabled")
        self.log_box.pack(fill="both", expand=True, pady=(6, 0))
        self.log_box.tag_config("INFO",    foreground=FG)
        self.log_box.tag_config("WARNING", foreground=YELLOW)
        self.log_box.tag_config("ERROR",   foreground=RED)
        self.log_box.tag_config("SUCCESS", foreground=GREEN)
        self.log_box.tag_config("DIM",     foreground=FG_DIM)
        self.log_box.tag_config("EXPERT",  foreground=PURPLE)

        # AI Expert panel (bottom)
        ai_frame = tk.Frame(pane, bg=BG)
        ai_header = tk.Frame(ai_frame, bg=BG)
        ai_header.pack(fill="x")
        tk.Label(ai_header, text="🤖  Dropshipping Expert", font=FONT_BOLD,
                 bg=BG, fg=PURPLE).pack(side="left")
        tk.Button(ai_header, text="Clear", font=FONT, bg=BG2, fg=FG_DIM,
                  activebackground=BG3, bd=0, padx=8,
                  command=self._clear_chat).pack(side="right")

        self.chat_box = scrolledtext.ScrolledText(
            ai_frame, font=FONT_MONO, bg="#0d0d1a", fg=FG,
            insertbackground=FG, wrap="word", bd=0,
            highlightbackground=BG3, highlightthickness=1,
            height=8, state="disabled")
        self.chat_box.pack(fill="both", expand=True, pady=(4, 4))
        self.chat_box.tag_config("user",   foreground=ACCENT2)
        self.chat_box.tag_config("expert", foreground=PURPLE)
        self.chat_box.tag_config("system", foreground=FG_DIM)

        chat_input_row = tk.Frame(ai_frame, bg=BG)
        chat_input_row.pack(fill="x", pady=(0, 4))
        self.chat_entry = tk.Entry(
            chat_input_row, font=FONT, bg=BG2, fg=FG,
            insertbackground=FG, bd=0,
            highlightbackground=BG3, highlightthickness=1)
        self.chat_entry.pack(side="left", expand=True, fill="x")
        self.chat_entry.bind("<Return>", lambda _: self._send_chat())
        tk.Button(chat_input_row, text="Ask", font=FONT, bg=BG3, fg=FG,
                  activebackground=ACCENT2, bd=0, padx=10,
                  command=self._send_chat).pack(side="left", padx=(6, 0))

        pane.add(log_frame, stretch="always")
        pane.add(ai_frame,  stretch="always")
        pane.paneconfig(ai_frame, minsize=160)
        self.after(100, lambda: pane.sash_place(0, 0, int(self.winfo_height() * 0.62)))

        # ── Tab 2: Results ────────────────────────────────────────────────────
        results_tab = tk.Frame(self._notebook, bg=BG)
        self._notebook.add(results_tab, text="  Results  ")

        res_header = tk.Frame(results_tab, bg=BG)
        res_header.pack(fill="x", pady=(6, 4), padx=6)
        tk.Label(res_header, text="Current scan winners & near-misses",
                 font=FONT_BOLD, bg=BG, fg=FG).pack(side="left")
        tk.Button(res_header, text="Refresh", font=FONT, bg=BG3, fg=FG,
                  activebackground=ACCENT2, bd=0, padx=8,
                  command=self._refresh_results).pack(side="right")

        res_cols = ("score", "page_name", "ad_count", "followers",
                    "shopify", "freshness", "saturation", "store_url")
        self._res_tree = ttk.Treeview(results_tab, columns=res_cols,
                                       show="headings", selectmode="browse")
        _res_headers = {
            "score": ("Score", 60), "page_name": ("Page", 160),
            "ad_count": ("Ads", 50), "followers": ("Followers", 80),
            "shopify": ("Shopify", 60), "freshness": ("Fresh%", 65),
            "saturation": ("Sat.", 50), "store_url": ("Store URL", 220),
        }
        for col, (hdr, w) in _res_headers.items():
            self._res_tree.heading(col, text=hdr)
            self._res_tree.column(col, width=w, minwidth=40)

        res_scroll = ttk.Scrollbar(results_tab, orient="vertical",
                                    command=self._res_tree.yview)
        self._res_tree.configure(yscrollcommand=res_scroll.set)
        self._res_tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=(0, 6))
        res_scroll.pack(side="right", fill="y", pady=(0, 6))

        # ── Tab 3: History ────────────────────────────────────────────────────
        history_tab = tk.Frame(self._notebook, bg=BG)
        self._notebook.add(history_tab, text="  History  ")

        hist_header = tk.Frame(history_tab, bg=BG)
        hist_header.pack(fill="x", pady=(6, 4), padx=6)
        tk.Label(hist_header, text="Past scan history (from local DB)",
                 font=FONT_BOLD, bg=BG, fg=FG).pack(side="left")
        tk.Button(hist_header, text="Refresh", font=FONT, bg=BG3, fg=FG,
                  activebackground=ACCENT2, bd=0, padx=8,
                  command=self._refresh_history).pack(side="right")

        hist_cols = ("timestamp", "niche", "keywords", "ads", "pages",
                     "winners", "near_misses")
        self._hist_tree = ttk.Treeview(history_tab, columns=hist_cols,
                                        show="headings", selectmode="browse")
        _hist_headers = {
            "timestamp": ("Time", 140), "niche": ("Niche", 100),
            "keywords": ("Keywords", 70), "ads": ("Ads", 60),
            "pages": ("Pages", 60), "winners": ("Winners", 60),
            "near_misses": ("Near-Miss", 70),
        }
        for col, (hdr, w) in _hist_headers.items():
            self._hist_tree.heading(col, text=hdr)
            self._hist_tree.column(col, width=w, minwidth=40)

        hist_scroll = ttk.Scrollbar(history_tab, orient="vertical",
                                     command=self._hist_tree.yview)
        self._hist_tree.configure(yscrollcommand=hist_scroll.set)
        self._hist_tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=(0, 6))
        hist_scroll.pack(side="right", fill="y", pady=(0, 6))

        # Bind tab selection to refresh history when History tab is shown
        self._notebook.bind("<<NotebookTabChanged>>", self._on_tab_change)

        # Load history on start
        self.after(500, self._refresh_history)

    # ── Widget helpers ───────────────────────────────────────────────────────

    def _section(self, parent, text: str):
        tk.Frame(parent, bg=BG3, height=1).pack(fill="x", pady=(7, 2))
        tk.Label(parent, text=text.upper(), font=("Segoe UI", 8, "bold"),
                 bg=BG, fg=ACCENT2).pack(anchor="w")

    def _check(self, parent, text: str, var: tk.BooleanVar):
        tk.Checkbutton(parent, text=text, variable=var, font=FONT,
                       bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
                       selectcolor=BG3, cursor="hand2").pack(anchor="w", pady=1)

    def _row_entry(self, parent, label: str, placeholder: str = "", width: int = 18):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label, font=FONT, bg=BG, fg=FG,
                 width=14, anchor="w").pack(side="left")
        var = tk.StringVar()
        e = tk.Entry(row, textvariable=var, font=FONT, bg=BG2, fg=FG,
                     insertbackground=FG, bd=0,
                     highlightbackground=BG3, highlightthickness=1, width=width)
        e.pack(side="left", expand=True, fill="x")
        if placeholder:
            e.insert(0, placeholder)
            e.config(fg=FG_DIM)
            def on_in(_, w=e, p=placeholder):
                if w.get() == p: w.delete(0, "end"); w.config(fg=FG)
            def on_out(_, w=e, p=placeholder):
                if not w.get(): w.insert(0, p); w.config(fg=FG_DIM)
            e.bind("<FocusIn>",  on_in)
            e.bind("<FocusOut>", on_out)
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

    # ── API key persistence ───────────────────────────────────────────────────

    def _env_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

    def _load_api_key(self) -> str:
        # os.environ is already populated by load_dotenv() at module level,
        # so just return whatever is set (file or existing env var).
        return os.environ.get("ANTHROPIC_API_KEY", "")

    def _save_api_key(self):
        key = self.v_api_key.get().strip()
        env_path = self._env_path()

        # Read existing .env lines, remove any old ANTHROPIC_API_KEY lines
        lines = []
        try:
            with open(env_path) as f:
                lines = [l for l in f.readlines()
                         if not l.startswith("ANTHROPIC_API_KEY=")]
        except FileNotFoundError:
            pass

        if key:
            lines.append(f"ANTHROPIC_API_KEY={key}\n")

        with open(env_path, "w") as f:
            f.writelines(lines)

        # Also update the running process environment so the expert picks it up
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
        else:
            os.environ.pop("ANTHROPIC_API_KEY", None)

        # Reset expert so it re-reads the key on next use
        with self._expert_lock:
            self._expert = None

        self._refresh_api_status()

    def _refresh_api_status(self):
        key = self.v_api_key.get().strip()
        if key and key.startswith("sk-"):
            self.api_status.config(
                text=f"✓ Key set ({key[:8]}…)", fg=GREEN)
        elif key:
            self.api_status.config(text="⚠ Key format looks wrong", fg=YELLOW)
        else:
            self.api_status.config(text="No key — AI expert disabled", fg=FG_DIM)

    def _save_apify_key(self):
        key = self.v_apify_key.get().strip()
        env_path = self._env_path()
        lines = []
        try:
            with open(env_path) as f:
                lines = [l for l in f.readlines()
                         if not l.startswith("APIFY_API_KEY=")]
        except FileNotFoundError:
            pass
        if key:
            lines.append(f"APIFY_API_KEY={key}\n")
        with open(env_path, "w") as f:
            f.writelines(lines)
        if key:
            os.environ["APIFY_API_KEY"] = key
        else:
            os.environ.pop("APIFY_API_KEY", None)
        self._refresh_apify_status()

    def _refresh_apify_status(self):
        key = self.v_apify_key.get().strip()
        if key:
            self.apify_status.config(
                text=f"⚡ Apify mode active ({key[:8]}…)", fg=ACCENT2)
        else:
            self.apify_status.config(text="No key — Apify disabled", fg=FG_DIM)

    def _save_discord_webhook(self):
        url = self.v_discord_wh.get().strip()
        env_path = self._env_path()
        lines = []
        try:
            with open(env_path) as f:
                lines = [l for l in f.readlines()
                         if not l.startswith("DISCORD_WEBHOOK_URL=")]
        except FileNotFoundError:
            pass
        if url:
            lines.append(f"DISCORD_WEBHOOK_URL={url}\n")
        with open(env_path, "w") as f:
            f.writelines(lines)
        if url:
            os.environ["DISCORD_WEBHOOK_URL"] = url
        else:
            os.environ.pop("DISCORD_WEBHOOK_URL", None)

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            self.v_output.set(path)

    def _set_buttons(self, state: str):
        """state: 'idle' | 'running' | 'paused'"""
        if state == "idle":
            self.btn_start.config(state="normal")
            self.btn_pause.config(state="disabled", text="⏸  PAUSE")
            self.btn_continue.config(state="disabled")
            self.btn_stop.config(state="disabled")
        elif state == "running":
            self.btn_start.config(state="disabled")
            self.btn_pause.config(state="normal", text="⏸  PAUSE")
            self.btn_continue.config(state="disabled")
            self.btn_stop.config(state="normal")
        elif state == "paused":
            self.btn_start.config(state="disabled")
            self.btn_pause.config(state="disabled", text="⏸  PAUSE")
            self.btn_continue.config(state="normal")
            self.btn_stop.config(state="normal")

    def _start(self):
        if not self.v_facebook.get() and not self.v_tiktok.get():
            self._append("ERROR", "Select at least one platform.\n")
            return
        # Clean up stale control files from a previous run
        for f in (_CTRL_FILE, _INJECT_FILE):
            try: os.remove(f)
            except FileNotFoundError: pass

        self._running = True
        self._paused  = False
        self._keywords_done = 0
        self._stats = {"keywords_searched": [], "total_ads": 0,
                       "products_found": 0, "last_keyword": "", "last_ads": 0}
        self._set_buttons("running")
        self.status_var.set("Running…")
        self._thread = threading.Thread(target=self._run_scan, daemon=True)
        self._thread.start()

    def _pause(self):
        _write_ctrl("pause")
        self._paused = True
        self._set_buttons("paused")
        self.status_var.set("Pausing…")
        self._append("DIM", "⏸  Pause requested — finishing current keyword...\n")

    def _resume(self):
        _write_ctrl("resume")
        self._paused = False
        self._set_buttons("running")
        self.status_var.set("Running…")
        self._append("DIM", "▶  Resuming scan...\n")

    def _stop(self):
        _write_ctrl("stop")
        self._running = False
        self._paused  = False
        self._set_buttons("idle")
        self.status_var.set("Stopping…")
        self._append("DIM", "⏹  Stop requested — exporting results and shutting down...\n")

    def _clear_ban_list(self):
        """Clear visited pages + searched keywords from state without losing ad data."""
        if self._running:
            self._append("WARNING", "Stop the scan before clearing the ban list.\n")
            return
        state_file = "scraper_state.json"
        if not os.path.exists(state_file):
            self._append("DIM", "No state file found — nothing to clear.\n")
            return
        try:
            with open(state_file) as f:
                data = json.load(f)
            cleared_pages = len(data.get("visited_page_ids", []))
            cleared_kws   = len(data.get("searched_keywords", []))
            data["visited_page_ids"]  = []
            data["searched_keywords"] = []
            with open(state_file, "w") as f:
                json.dump(data, f, ensure_ascii=False)
            self._append(
                "SUCCESS",
                f"✓ Ban list cleared — {cleared_pages} visited pages and "
                f"{cleared_kws} searched keywords reset.\n"
                f"  (Collected ad data and winner history kept.)\n"
            )
        except Exception as e:
            self._append("ERROR", f"Failed to clear ban list: {e}\n")

    # ── TikTok login ─────────────────────────────────────────────────────────

    def _tiktok_login(self):
        if self._running:
            self._append("WARNING", "Stop the current scan before logging in.\n")
            return
        self._append("DIM", "Opening TikTok login browser — log in, then wait.\n")
        self.status_var.set("TikTok login…")
        threading.Thread(target=self._run_tiktok_login_proc, daemon=True).start()

    def _run_tiktok_login_proc(self):
        py  = sys.executable
        cmd = [py, os.path.join(os.path.dirname(__file__), "main.py"), "--tiktok-login"]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", bufsize=1,
                                    cwd=os.path.dirname(__file__), env=env)
            for line in proc.stdout:
                self._classify_and_append(line)
            proc.wait()
            msg = "\nTikTok session saved — you can now run a scan.\n" if proc.returncode == 0 \
                  else f"\nTikTok login exited with code {proc.returncode}.\n"
            self._append("SUCCESS" if proc.returncode == 0 else "WARNING", msg)
        except Exception as exc:
            self._append("ERROR", f"\nFailed to start TikTok login: {exc}\n")
        finally:
            self.after(0, lambda: self.status_var.set("Ready"))

    # ── Scan subprocess ──────────────────────────────────────────────────────

    def _build_niche_list(self):
        """Return ordered list of niches to scan (primary field first, then queue)."""
        niches = []
        primary = self.v_niche.get().strip()
        if primary and primary != "e.g. dogs, fitness, jewelry":
            niches.append(primary)
        queue_text = self.niche_queue_box.get("1.0", "end").strip()
        if queue_text and queue_text != self._nq_placeholder:
            for line in queue_text.splitlines():
                line = line.strip()
                if line and line not in niches:
                    niches.append(line)
        return niches if niches else [None]  # [None] = no niche specified

    def _run_scan(self):
        niches = self._build_niche_list()
        overnight = self.v_overnight.get()
        winner_target = self.v_winner_target.get() if overnight else 0
        total_winners = 0

        kw_val = self.v_keywords.get().strip()
        extra_kws = [k.strip() for k in kw_val.split(",")
                     if k.strip() and k.strip() != "comma-separated"]

        for i, niche in enumerate(niches):
            if not self._running:
                break
            if winner_target > 0 and total_winners >= winner_target:
                self._append("SUCCESS",
                    f"\n✓ Overnight target reached: {total_winners}/{winner_target} "
                    f"winner(s) found — stopping.\n")
                break

            if len(niches) > 1:
                self._append("INFO",
                    f"\n{'─' * 46}\n"
                    f"  Niche {i + 1}/{len(niches)}: {niche or 'general'}"
                    f"  (winners so far: {total_winners})\n"
                    f"{'─' * 46}\n")

            won = self._run_single_scan(niche, extra_kws if i == 0 else [])
            total_winners += won

            if overnight and winner_target > 0 and total_winners < winner_target \
                    and i < len(niches) - 1:
                self._append("INFO",
                    f"  {total_winners}/{winner_target} winners — "
                    f"moving to next niche...\n")

        if overnight and winner_target > 0 and total_winners < winner_target:
            self._append("WARNING",
                f"\nOvernight run complete: {total_winners}/{winner_target} winners "
                f"found across {len(niches)} niche(s).\n")

        self.after(0, self._scan_done)

    def _run_single_scan(self, niche, extra_kws=None):
        """Run one scan subprocess. Returns number of winners found."""
        import re as _re
        py  = sys.executable
        cmd = [py, os.path.join(os.path.dirname(__file__), "main.py")]

        if not self.v_facebook.get(): cmd.append("--no-facebook")
        if not self.v_tiktok.get():   cmd.append("--no-tiktok")

        if niche:
            cmd += ["--niche", niche]

        for kw in (extra_kws or []):
            cmd += ["-k", kw]

        cmd += [
            "--countries",        self.v_country.get().strip() or "US",
            "--days",             str(self.v_days.get()),
            "--min-ads",          str(self.v_min_ads.get()),
            "--max-keywords",     str(self.v_max_kw.get()),
            "--min-followers",    str(self.v_min_fol.get()),
            "--max-followers",    str(self.v_max_fol.get()),
            "--max-total-ads",    str(self.v_max_tot_ads.get()),
            "--min-active-ratio", str(self.v_active_ratio.get() / 100.0),
        ]
        if self.v_headless.get():     cmd.append("--headless")
        if self.v_reset.get():        cmd.append("--reset")
        if self.v_no_csv.get():       cmd.append("--no-csv")
        if self.v_until_winner.get(): cmd.append("--until-winner")
        wh = self.v_discord_wh.get().strip()
        if wh:
            cmd += ["--discord-webhook", wh]
        out_path = self.v_output.get().strip()
        if out_path: cmd += ["--output", out_path]

        self._append("DIM", f"$ {' '.join(cmd)}\n\n")
        self._get_expert().update_context(
            niche=niche or "general",
            country=self.v_country.get().strip() or "US",
            days=self.v_days.get(),
            min_ads=self.v_min_ads.get(),
            min_followers=self.v_min_fol.get(),
            max_followers=self.v_max_fol.get(),
        )
        self._chat_system(
            f"Scan started{f' — niche: {niche}' if niche else ''}. "
            "I'll monitor progress and suggest keywords as we go.")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        winners_found = 0
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", bufsize=1,
                cwd=os.path.dirname(__file__), env=env)
            for line in proc.stdout:
                if not self._running and not self._paused:
                    proc.terminate()
                    break
                self._classify_and_append(line)
                self._parse_stats_from_line(line)
                m = _re.search(r"(\d+)\s+winner", line.lower())
                if m:
                    winners_found = max(winners_found, int(m.group(1)))
            proc.wait()
            code = proc.returncode
            if code == 0:
                self._append("SUCCESS",
                    f"\n✓ Scan complete ({niche or 'general'}) — "
                    f"{winners_found} winner(s).\n")
            else:
                self._append("WARNING", f"\nProcess exited with code {code}.\n")
        except Exception as exc:
            self._append("ERROR", f"\nFailed to start scan: {exc}\n")

        return winners_found

    def _scan_done(self):
        self._running = False
        self._paused  = False
        self._set_buttons("idle")
        self.status_var.set("Done")
        self.after(1000, self._refresh_results)   # give DB a moment to be written

    def _on_tab_change(self, event):
        tab = self._notebook.tab(self._notebook.select(), "text").strip()
        if tab == "History":
            self._refresh_history()

    def _refresh_results(self):
        """Populate Results tab from the most recent scan's winners in the DB."""
        try:
            from fb_ads_scraper.db import load_recent_scans, load_winners_for_scan
            scans = load_recent_scans(limit=1)
            if not scans:
                return
            scan_id = scans[0]["id"]
            rows = load_winners_for_scan(scan_id)
            self._res_tree.delete(*self._res_tree.get_children())
            for r in rows:
                fresh_pct = f"{r['freshness_rate']*100:.0f}%" if r.get('freshness_rate') else ""
                self._res_tree.insert("", "end", values=(
                    r.get("score", ""),
                    r.get("page_name", ""),
                    r.get("ad_count", ""),
                    r.get("page_followers", ""),
                    "✓" if r.get("is_shopify") else "",
                    fresh_pct,
                    r.get("saturation_count", ""),
                    r.get("store_url", ""),
                ))
        except Exception:
            pass

    def _refresh_history(self):
        """Populate History tab from DB."""
        try:
            from fb_ads_scraper.db import load_recent_scans
            scans = load_recent_scans(limit=50)
            self._hist_tree.delete(*self._hist_tree.get_children())
            for s in scans:
                self._hist_tree.insert("", "end", values=(
                    s.get("timestamp", "")[:16],
                    s.get("niche", "") or "—",
                    s.get("keywords_count", ""),
                    s.get("ads_count", ""),
                    s.get("pages_count", ""),
                    s.get("winner_count", ""),
                    s.get("near_miss_count", ""),
                ))
        except Exception:
            pass

    # ── Stats parsing (feeds AI expert) ─────────────────────────────────────

    def _parse_stats_from_line(self, line: str):
        """Extract scan progress signals from log lines to feed the expert."""
        import re
        low = line.lower()

        # "Scraped N ads for 'keyword'"
        m = re.search(r"scraped\s+(\d+)\s+ads\s+for\s+'([^']+)'", low)
        if m:
            n, kw = int(m.group(1)), m.group(2)
            self._stats["last_keyword"] = kw
            self._stats["last_ads"]     = n
            self._stats["total_ads"]   += n
            if kw not in self._stats["keywords_searched"]:
                self._stats["keywords_searched"].append(kw)
            self._keywords_done += 1
            # Ask expert every 5 keywords
            if self._keywords_done % 5 == 0:
                threading.Thread(target=self._expert_analysis, daemon=True).start()

        # "N winners" / "Qualifying products found: N"
        m2 = re.search(r"(\d+)\s+winner", low)
        if m2:
            self._stats["products_found"] = max(self._stats["products_found"], int(m2.group(1)))

    # ── AI Expert ────────────────────────────────────────────────────────────

    def _get_expert(self):
        with self._expert_lock:
            if self._expert is None:
                from fb_ads_scraper.expert import DropshippingExpert
                self._expert = DropshippingExpert()
        return self._expert

    def _expert_analysis(self):
        """Background: ask expert to analyse current progress + inject keywords."""
        try:
            expert = self._get_expert()
            s = self._stats
            already = set(s["keywords_searched"])
            commentary, new_kws = expert.analyze_progress(
                keywords_searched=s["keywords_searched"],
                products_found=s["products_found"],
                recent_keyword=s["last_keyword"],
                recent_ads_found=s["last_ads"],
                total_ads=s["total_ads"],
                already_queued=already,
            )
            if commentary:
                self.after(0, lambda c=commentary: self._chat_expert(c))
            if new_kws and self._running and not self._paused:
                _write_inject(new_kws)
                kw_str = ", ".join(f"'{k}'" for k in new_kws)
                self.after(0, lambda s=kw_str: self._chat_system(
                    f"Injecting keywords into scan queue: {s}"))
        except Exception as e:
            pass  # expert is advisory only; never crash the scan

    def _send_chat(self):
        msg = self.chat_entry.get().strip()
        if not msg:
            return
        self.chat_entry.delete(0, "end")
        self._chat_user(msg)
        threading.Thread(target=self._chat_worker, args=(msg,), daemon=True).start()

    def _chat_worker(self, message: str):
        try:
            expert = self._get_expert()
            response = expert.chat(message)
            self.after(0, lambda r=response: self._chat_expert(r))

            # If expert suggests keywords and scan is running, inject them
            if self._running and not self._paused:
                from fb_ads_scraper.expert import DropshippingExpert
                kws = DropshippingExpert._extract_keywords(response)
                if kws:
                    _write_inject(kws)
                    kw_str = ", ".join(f"'{k}'" for k in kws)
                    self.after(0, lambda s=kw_str: self._chat_system(
                        f"Injecting suggested keywords: {s}"))
        except Exception as e:
            self.after(0, lambda: self._chat_system(f"Error: {e}"))

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _clear_log(self):
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.config(state="disabled")

    def _clear_chat(self):
        self.chat_box.config(state="normal")
        self.chat_box.delete("1.0", "end")
        self.chat_box.config(state="disabled")

    def _classify_and_append(self, line: str):
        line = line.rstrip("\n") + "\n"
        low = line.lower()
        if "error" in low or "traceback" in low or "exception" in low:
            tag = "ERROR"
        elif "warning" in low or "warn" in low:
            tag = "WARNING"
        elif any(x in low for x in ["winner", "✓", "shopify", "scraped"]):
            tag = "SUCCESS"
        elif line.startswith(" ") or "[dim]" in low:
            tag = "DIM"
        else:
            tag = "INFO"
        self._log_queue.put((tag, line))

    def _append(self, tag: str, text: str):
        self._log_queue.put((tag, text))

    def _poll_log(self):
        try:
            while True:
                item = self._log_queue.get_nowait()
                tag, text = item if isinstance(item, tuple) else ("INFO", item)
                self.log_box.config(state="normal")
                self.log_box.insert("end", text, tag)
                self.log_box.see("end")
                self.log_box.config(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    # ── Chat panel helpers ────────────────────────────────────────────────────

    def _chat_append(self, prefix: str, text: str, tag: str):
        self.chat_box.config(state="normal")
        self.chat_box.insert("end", f"{prefix} {text}\n\n", tag)
        self.chat_box.see("end")
        self.chat_box.config(state="disabled")

    def _chat_user(self, text: str):
        self._chat_append("You:", text, "user")

    def _chat_expert(self, text: str):
        self._chat_append("Expert:", text, "expert")

    def _chat_system(self, text: str):
        self._chat_append("•", text, "system")


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = App()
    app.mainloop()
