"""Jarvis desktop window: a digital, Iron Man style HUD.

Everything sits on one canvas with a faint grid: an animated arc-reactor orb with a radar sweep,
a live voice-level meter, data readouts, bracketed panels, a boot sequence and typed-out replies.

The orb shows what Jarvis is doing:
    standing by (dim blue)   listening (bright cyan)   thinking (amber, spinning)
    speaking (cyan, pulsing) locked (red, padlock)     error (red)
"""
import datetime
import math
import os
import queue
import random
import socket
import threading
import time
import tkinter as tk

# --- Palette ---------------------------------------------------------------
BG = "#03070D"
GRID = "#08131F"
GRID_MARK = "#0F2537"
PANEL = "#060D17"
EDGE = "#13405C"
TEXT = "#D7E9F7"
TEXT_DIM = "#5D7A92"
CYAN = "#27D8FF"
AMBER = "#FFB238"
RED = "#FF4D5E"
GREEN = "#38E8A0"

STATES = {
    #           colour     dim colour  title          subtitle                        spin pulse size
    "idle":      (CYAN, "#0F5A70", "STANDING BY", "SAY “JARVIS” OR TYPE BELOW", 0.6, 0.8, 0.04),
    "listening": (CYAN, "#1690B0", "LISTENING", "GO AHEAD, {name}", 1.4, 2.2, 0.10),
    "thinking":  (AMBER, "#8A5A12", "PROCESSING", "WORKING ON IT", 4.5, 3.0, 0.06),
    "speaking":  ("#5CF2FF", "#1AA6C4", "SPEAKING", "VOICE OUTPUT ACTIVE", 1.8, 6.0, 0.16),
    "locked":    (RED, "#7A1E28", "LOCKED", "AUTHORIZATION REQUIRED", 0.4, 1.0, 0.05),
    "error":     (RED, "#7A1E28", "MIC ERROR", "", 0.3, 0.6, 0.03),
}

HEAD = "Bahnschrift SemiBold"    # ships with Windows 10/11; Tk falls back quietly elsewhere
HEAD_LIGHT = "Bahnschrift Light"
MONO = "Consolas"
BODY = "Segoe UI"
PLACEHOLDER = "ENTER COMMAND…"

MARGIN = 26
TOP = 58
BOTTOM = 34
LEFT_W = 360
ORB = 290
BARS = 36


def _blend(colour_a, colour_b, amount):
    """Mixes two hex colours (Tk has no transparency, so glow is drawn with blended rings)."""
    amount = max(0.0, min(1.0, amount))
    a = [int(colour_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(colour_b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b))


def brackets(canvas, x0, y0, x1, y1, colour, length=14, width=2, tag="deco"):
    """HUD-style corner brackets around a rectangle."""
    for (x, y, dx, dy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        canvas.create_line(x, y + dy * length, x, y, x + dx * length, y, fill=colour, width=width, tags=tag)


class ArcReactor:
    """The animated orb, drawn straight onto the window's canvas."""

    def __init__(self, canvas, cx, cy, size):
        self.canvas = canvas
        self.size = size
        self.cx, self.cy = cx, cy
        self.state = "idle"
        self.angle = 0.0
        self.sweep = 0.0
        self.phase = 0.0
        self.colour = CYAN
        self._build()

    def _build(self):
        c, r = self.canvas, self.size / 2
        self.glow = [c.create_oval(0, 0, 0, 0, outline="", fill=BG) for _ in range(6)]
        self.ticks = []
        for i in range(72):
            angle = math.radians(i * 5)
            inner = r - (16 if i % 6 == 0 else 9)
            self.ticks.append(c.create_line(
                self.cx + math.cos(angle) * inner, self.cy + math.sin(angle) * inner,
                self.cx + math.cos(angle) * (r - 4), self.cy + math.sin(angle) * (r - 4),
                width=2 if i % 6 == 0 else 1, fill=EDGE))
        self.outer_ring = c.create_oval(0, 0, 0, 0, outline=EDGE, width=1)
        self.dash_ring = c.create_oval(0, 0, 0, 0, outline=EDGE, width=1, dash=(2, 6))
        # radar sweep: a bright line with a fading trail
        self.trail = [c.create_line(0, 0, 0, 0, fill=BG, width=2) for _ in range(10)]
        self.arcs = []
        for radius, extent, width, count in ((r * 0.80, 70, 4, 3), (r * 0.67, 38, 2, 4), (r * 0.55, 110, 3, 2)):
            for k in range(count):
                self.arcs.append([c.create_arc(self.cx - radius, self.cy - radius, self.cx + radius, self.cy + radius,
                                               start=0, extent=extent, style="arc", width=width, outline=CYAN),
                                  360 / count * k, radius])
        self.inner_ring = c.create_oval(0, 0, 0, 0, outline=CYAN, width=2)
        self.core_glow = [c.create_oval(0, 0, 0, 0, outline="", fill=BG) for _ in range(5)]
        self.core = c.create_oval(0, 0, 0, 0, outline="", fill=CYAN)
        self.cross = [c.create_line(0, 0, 0, 0, fill=EDGE) for _ in range(4)]
        self.lock_body = c.create_rectangle(0, 0, 0, 0, outline="", fill=BG, state="hidden")
        self.lock_shackle = c.create_arc(0, 0, 0, 0, start=0, extent=180, style="arc", width=4,
                                         outline=BG, state="hidden")
        self.lock_hole = c.create_oval(0, 0, 0, 0, outline="", fill=RED, state="hidden")

    def set_state(self, state):
        self.state = state if state in STATES else "idle"

    def _circle(self, item, radius):
        self.canvas.coords(item, self.cx - radius, self.cy - radius, self.cx + radius, self.cy + radius)

    def tick(self, dt):
        colour, dim, _, _, spin, pulse_speed, pulse_size = STATES[self.state]
        self.colour = _blend(self.colour, colour, min(1.0, dt * 6))
        self.angle = (self.angle + spin * 60 * dt) % 360
        self.sweep = (self.sweep + (90 + spin * 40) * dt) % 360
        self.phase += pulse_speed * dt
        pulse = (math.sin(self.phase * math.pi) + 1) / 2
        r = self.size / 2
        c = self.canvas

        for i, ring in enumerate(self.glow):
            self._circle(ring, r * (0.93 - i * 0.05) + pulse * r * pulse_size * 0.3)
            c.itemconfigure(ring, fill=_blend(BG, self.colour, 0.03 + i * 0.02 + pulse * 0.02))
        self._circle(self.outer_ring, r - 2)
        c.itemconfigure(self.outer_ring, outline=_blend(EDGE, self.colour, 0.4))
        self._circle(self.dash_ring, r * 0.90)
        c.itemconfigure(self.dash_ring, outline=_blend(EDGE, self.colour, 0.3))
        tick_colour = _blend(EDGE, self.colour, 0.3 + pulse * 0.25)
        for line in self.ticks[::6]:
            c.itemconfigure(line, fill=tick_colour)

        for i, line in enumerate(self.trail):
            angle = math.radians(self.sweep - i * 3)
            c.coords(line, self.cx + math.cos(angle) * r * 0.45, self.cy + math.sin(angle) * r * 0.45,
                     self.cx + math.cos(angle) * r * 0.86, self.cy + math.sin(angle) * r * 0.86)
            c.itemconfigure(line, fill=_blend(BG, self.colour, 0.55 * (1 - i / len(self.trail))))

        for index, (arc, offset, radius) in enumerate(self.arcs):
            direction = -1 if r * 0.6 < radius < r * 0.7 else 1
            speed = 1.0 if radius > r * 0.7 else 1.7
            c.itemconfigure(arc, start=(offset + self.angle * direction * speed) % 360,
                            outline=self.colour if index % 2 == 0 else _blend(self.colour, BG, 0.45))

        core_radius = r * (0.19 + pulse * pulse_size * 0.6)
        self._circle(self.inner_ring, r * 0.40 + pulse * r * pulse_size * 0.2)
        c.itemconfigure(self.inner_ring, outline=_blend(dim, self.colour, pulse))
        for i, ring in enumerate(self.core_glow):
            self._circle(ring, core_radius * (1.9 - i * 0.2))
            c.itemconfigure(ring, fill=_blend(BG, self.colour, 0.10 + i * 0.12))
        self._circle(self.core, core_radius)
        c.itemconfigure(self.core, fill=_blend(self.colour, "#FFFFFF", 0.35 + pulse * 0.3))
        for i, line in enumerate(self.cross):  # crosshair marks
            angle = math.radians(i * 90 + 45)
            c.coords(line, self.cx + math.cos(angle) * r * 0.30, self.cy + math.sin(angle) * r * 0.30,
                     self.cx + math.cos(angle) * r * 0.36, self.cy + math.sin(angle) * r * 0.36)
            c.itemconfigure(line, fill=_blend(EDGE, self.colour, 0.6))

        locked = "normal" if self.state == "locked" else "hidden"
        w = r * 0.15
        c.coords(self.lock_body, self.cx - w, self.cy - w * 0.2, self.cx + w, self.cy + w * 1.2)
        c.coords(self.lock_shackle, self.cx - w * 0.65, self.cy - w * 1.15, self.cx + w * 0.65, self.cy + w * 0.25)
        c.coords(self.lock_hole, self.cx - w * 0.2, self.cy + w * 0.2, self.cx + w * 0.2, self.cy + w * 0.6)
        white = _blend(BG, "#FFFFFF", 0.92)
        c.itemconfigure(self.lock_body, state=locked, fill=white)
        c.itemconfigure(self.lock_shackle, state=locked, outline=white)
        c.itemconfigure(self.lock_hole, state=locked, fill=self.colour)
        if locked == "normal":
            for item in (self.lock_shackle, self.lock_body, self.lock_hole):
                c.tag_raise(item)


class VoiceMeter:
    """A row of bars that moves with the microphone (listening) or Jarvis's voice (speaking)."""

    def __init__(self, canvas, x0, y0, width, height):
        self.canvas, self.x0, self.y0, self.width, self.height = canvas, x0, y0, width, height
        self.levels = [0.05] * BARS
        gap = 3
        self.bar_w = (width - gap * (BARS - 1)) / BARS
        self.bars = [canvas.create_rectangle(0, 0, 0, 0, outline="", fill=EDGE) for _ in range(BARS)]
        self.base = canvas.create_line(x0, y0 + height, x0 + width, y0 + height, fill=EDGE)
        self.t = 0.0

    def tick(self, dt, state, colour, mic):
        self.t += dt
        for i in range(BARS):
            centre = 1 - abs(i - (BARS - 1) / 2) / (BARS / 2)  # taller in the middle
            if state == "speaking":
                target = 0.25 + 0.75 * random.random() * (0.4 + 0.6 * centre)
            elif state == "listening":
                target = 0.06 + mic * (0.5 + 0.5 * random.random()) * (0.3 + 0.7 * centre)
            elif state == "thinking":
                position = (self.t * 14) % (BARS * 1.6)
                target = 0.08 + 0.7 * max(0.0, 1 - abs(i - position) / 3)
            else:
                target = 0.04 + 0.03 * (math.sin(self.t * 2 + i * 0.5) + 1)
            self.levels[i] += (target - self.levels[i]) * min(1.0, dt * 14)
            level = self.levels[i]
            x = self.x0 + i * (self.bar_w + 3)
            top = self.y0 + self.height * (1 - level)
            self.canvas.coords(self.bars[i], x, top, x + self.bar_w, self.y0 + self.height)
            self.canvas.itemconfigure(self.bars[i], fill=_blend(EDGE, colour, 0.35 + level * 0.65))
        self.canvas.itemconfigure(self.base, fill=_blend(EDGE, colour, 0.4))


class JarvisWindow:
    def __init__(self, handle_command, listener, speak):
        self.handle_command = handle_command
        self.listener = listener
        self.speak = speak
        self.events = queue.Queue()
        self.commands = queue.Queue()
        self.running = True
        self.busy = 0
        self._busy_lock = threading.Lock()
        self.state = "idle"
        self.error_text = ""
        self.online = None
        self.commands_done = 0
        self._last_frame = time.time()
        self._typing = []      # Jarvis messages waiting to be typed out
        self._typing_now = None
        self._typing_gen = 0

        try:
            from automation import security as _security
        except ImportError:
            _security = None
        self.security = _security
        try:
            from automation import tools as _tools

            self.user = _tools.user_name()
        except Exception:
            self.user = "Sarthak"

        self.root = tk.Tk()
        self.root.title("J.A.R.V.I.S.")
        self.root.geometry("1080x720")
        self.root.minsize(960, 690)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.report_callback_exception = self._callback_error

        if os.getenv("JARVIS_INTRO", "on").strip().lower() in {"off", "0", "false", "no"}:
            self._start_main()
        else:
            self._play_intro()
        threading.Thread(target=self._network_check, daemon=True).start()

        self.speech_lock = threading.Lock()
        try:
            from automation import tools

            tools.set_notifier(self.notify)
            restored = tools.restore_reminders()
            if restored:
                print(f"[Schedule] Restored {restored} reminder(s) for upcoming events.")
        except Exception as exc:
            print(f"[Schedule] Couldn't restore reminders: {exc}")

        threading.Thread(target=self._voice_loop, daemon=True).start()
        threading.Thread(target=self._command_loop, daemon=True).start()

        try:
            from automation import tools
            from mouth import speaker as speaker_module
        except ImportError:
            speaker_module = None
        if speaker_module is not None and hasattr(speaker_module, "warm_cache"):
            phrases = [
                f"Yes, {tools.user_name()}?", "Toggled play and pause.", "Toggled mute.", "Sent.",
                "Okay, I won't send it.", "Okay.", "Closed the tab.", "Skipping to the next track.", "Email sent.",
            ]
            threading.Thread(target=speaker_module.warm_cache, args=(phrases,), daemon=True).start()

        self.mail_watcher = None
        try:
            from automation import mail

            self.mail_watcher = mail.MailWatcher(self.notify)
            self.mail_watcher.start()
        except Exception as exc:
            print(f"[Mail] Background mail check disabled: {exc}")

    # ----------------------------------------------------------- start-up
    def _play_intro(self):
        """Borderless start-up animation; Whisper and the rest keep loading in the background meanwhile."""
        import intro

        width, height = 1080, 720
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.overrideredirect(True)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.intro_canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0, bd=0, width=width, height=height)
        self.intro_canvas.pack(fill="both", expand=True)
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.intro = intro.Intro(self.root, self.intro_canvas, width, height, self._end_intro)

    def _end_intro(self):
        self.intro_canvas.destroy()
        self.root.withdraw()
        self.root.overrideredirect(False)
        try:
            self.root.attributes("-topmost", False)
            self.root.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self._start_main()
        self._fade_in(0.0)

    def _fade_in(self, alpha):
        alpha = min(1.0, alpha + 0.08)
        try:
            self.root.attributes("-alpha", alpha)
        except tk.TclError:
            return
        if alpha < 1.0 and self.running:
            self.root.after(20, self._fade_in, alpha)

    def _start_main(self):
        self._build_widgets()
        self._show_state(self._resting_state())
        self._boot_sequence()
        self._focus_window()
        self.root.after(100, self._poll_events)
        self.root.after(33, self._animate)
        self.root.after(300, self._update_hud)

    # ------------------------------------------------------------------ layout
    def _build_widgets(self):
        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        c = self.canvas

        # Left column: orb, status, voice meter, readouts (all drawn on the canvas)
        x0 = MARGIN
        cx = x0 + LEFT_W / 2
        orb_top = TOP + 26
        self.reactor = ArcReactor(c, cx, orb_top + ORB / 2, ORB)
        y = orb_top + ORB + 22
        self.state_title = c.create_text(cx, y, text="", fill=CYAN, font=(HEAD, 20))
        self.state_sub = c.create_text(cx, y + 26, text="", fill=TEXT_DIM, font=(MONO, 9))
        self.meter = VoiceMeter(c, x0 + 30, y + 46, LEFT_W - 60, 34)

        info_top = y + 98
        self.info_box = (x0, info_top, x0 + LEFT_W, info_top + 104)
        c.create_rectangle(*self.info_box, fill=PANEL, outline=_blend(PANEL, EDGE, 0.6))
        brackets(c, *self.info_box, CYAN, length=10, tag="fixed")
        self.clock = c.create_text(x0 + 16, info_top + 26, anchor="w", text="", fill=TEXT, font=(MONO, 22))
        self.date = c.create_text(x0 + LEFT_W - 16, info_top + 18, anchor="e", text="", fill=TEXT_DIM,
                                  font=(MONO, 9))
        self.day = c.create_text(x0 + LEFT_W - 16, info_top + 34, anchor="e", text="", fill=TEXT_DIM,
                                 font=(MONO, 9))
        c.create_line(x0 + 16, info_top + 52, x0 + LEFT_W - 16, info_top + 52, fill=_blend(PANEL, EDGE, 0.7))
        labels = ("SECURITY", "NETWORK", "CPU", "RAM")
        self.readouts = {}
        for i, label in enumerate(labels):
            column = x0 + 16 + i * ((LEFT_W - 32) / 4)
            c.create_text(column, info_top + 68, anchor="w", text=label, fill=TEXT_DIM, font=(MONO, 7))
            self.readouts[label] = c.create_text(column, info_top + 86, anchor="w", text="--", fill=TEXT,
                                                 font=(MONO, 10, "bold"))

        # Right column: conversation log and command line (real widgets placed on the canvas)
        self.transcript = tk.Text(c, wrap="word", state="disabled", relief="flat", bd=0, bg=PANEL, fg=TEXT,
                                  font=(BODY, 11), padx=18, pady=14, spacing1=1, spacing3=3,
                                  insertbackground=TEXT, selectbackground="#1D4A66", cursor="arrow",
                                  highlightthickness=0)
        t = self.transcript
        t.tag_configure("jarvis_head", font=(MONO, 9, "bold"), foreground=CYAN, spacing1=10)
        t.tag_configure("user_head", font=(MONO, 9, "bold"), foreground=AMBER, spacing1=10, justify="right")
        t.tag_configure("jarvis_body", foreground=TEXT, rmargin=60)
        t.tag_configure("user_body", foreground="#F3D9A8", justify="right", lmargin1=90, lmargin2=90)
        t.tag_configure("time", font=(MONO, 8), foreground=TEXT_DIM)
        t.tag_configure("boot", font=(MONO, 9), foreground=TEXT_DIM, spacing1=0, spacing3=0)
        t.tag_configure("boot_ok", font=(MONO, 9, "bold"), foreground=GREEN)
        t.tag_configure("boot_warn", font=(MONO, 9, "bold"), foreground=AMBER)
        t.tag_configure("cursor", foreground=CYAN)
        self.transcript_window = c.create_window(0, 0, anchor="nw", window=t)

        self.entry_frame = tk.Frame(c, bg=PANEL, highlightthickness=1, highlightbackground=EDGE,
                                    highlightcolor=CYAN)
        self.entry_frame.columnconfigure(1, weight=1)
        self.prompt_mark = tk.Label(self.entry_frame, text=">_", bg=PANEL, fg=CYAN, font=(MONO, 12, "bold"))
        self.prompt_mark.grid(row=0, column=0, padx=(12, 6))
        self.command_entry = tk.Entry(self.entry_frame, relief="flat", bd=0, bg=PANEL, fg=TEXT_DIM,
                                      insertbackground=CYAN, font=(MONO, 12), highlightthickness=0)
        self.command_entry.grid(row=0, column=1, sticky="ew", ipady=10, padx=(0, 12))
        self.command_entry.insert(0, PLACEHOLDER)
        self.command_entry.bind("<FocusIn>", self._clear_placeholder)
        self.command_entry.bind("<FocusOut>", self._restore_placeholder)
        self.command_entry.bind("<Return>", self._send_command)
        self.entry_window = c.create_window(0, 0, anchor="nw", window=self.entry_frame)

        self.send_button = tk.Button(c, text="EXECUTE", command=self._send_command, relief="flat", bd=0,
                                     bg=CYAN, activebackground="#7BE8FF", fg=BG, activeforeground=BG,
                                     font=(HEAD, 10), padx=18, pady=10, cursor="hand2")
        self.send_window = c.create_window(0, 0, anchor="nw", window=self.send_button)

        # Top and bottom bars
        self.title = c.create_text(MARGIN, TOP / 2 + 2, anchor="w", text="J.A.R.V.I.S.", fill=CYAN,
                                   font=(HEAD, 20))
        self.subtitle = c.create_text(MARGIN + 170, TOP / 2 + 6, anchor="w", text="PERSONAL DESKTOP ASSISTANT",
                                      fill=TEXT_DIM, font=(MONO, 8))
        self.top_status = c.create_text(0, TOP / 2 + 2, anchor="e", text="", fill=TEXT_DIM, font=(MONO, 9))
        self.ticker = c.create_text(MARGIN, 0, anchor="w", text="", fill=TEXT_DIM, font=(MONO, 8))
        self.log_label = c.create_text(0, 0, anchor="w", text="COMMUNICATION LOG  //  CH-01", fill=TEXT_DIM,
                                       font=(MONO, 8))

        c.bind("<Configure>", self._layout)

    def _layout(self, event=None):
        """Places the right-hand panels and redraws the grid and frame decorations for the window size."""
        c = self.canvas
        width = max(c.winfo_width(), 900)
        height = max(c.winfo_height(), 640)
        c.delete("deco")

        for x in range(0, width, 28):
            c.create_line(x, 0, x, height, fill=GRID, tags="deco")
        for y in range(0, height, 28):
            c.create_line(0, y, width, y, fill=GRID, tags="deco")
        for x in range(0, width, 112):
            for y in range(0, height, 112):
                c.create_line(x - 3, y, x + 4, y, fill=GRID_MARK, tags="deco")
                c.create_line(x, y - 3, x, y + 4, fill=GRID_MARK, tags="deco")

        # top bar
        c.create_line(MARGIN, TOP, width - MARGIN, TOP, fill=EDGE, tags="deco")
        for x in range(MARGIN, width - MARGIN, 14):
            c.create_line(x, TOP, x, TOP + (5 if (x - MARGIN) % 70 == 0 else 2), fill=EDGE, tags="deco")
        c.create_line(MARGIN, TOP, MARGIN + 140, TOP, fill=CYAN, width=2, tags="deco")
        c.coords(self.top_status, width - MARGIN, TOP / 2 + 2)
        title_box = c.bbox(self.title)
        if title_box:
            c.coords(self.subtitle, title_box[2] + 16, TOP / 2 + 6)
        # bottom bar
        bottom = height - BOTTOM
        c.create_line(MARGIN, bottom, width - MARGIN, bottom, fill=EDGE, tags="deco")
        c.create_line(width - MARGIN - 140, bottom, width - MARGIN, bottom, fill=CYAN, width=2, tags="deco")
        c.coords(self.ticker, MARGIN, bottom + BOTTOM / 2)

        # conversation log and command line
        right_x = MARGIN + LEFT_W + 30
        right_w = width - right_x - MARGIN
        log_top = TOP + 34
        entry_h = 46
        entry_y = bottom - 22 - entry_h
        log_bottom = entry_y - 18
        c.coords(self.log_label, right_x, TOP + 20)
        c.coords(self.transcript_window, right_x, log_top)
        c.itemconfigure(self.transcript_window, width=right_w, height=log_bottom - log_top)
        c.create_rectangle(right_x - 1, log_top - 1, right_x + right_w, log_bottom, outline=_blend(PANEL, EDGE, 0.6),
                           tags="deco")
        brackets(c, right_x - 6, log_top - 6, right_x + right_w + 5, log_bottom + 5, CYAN, length=16)
        send_w = 120
        c.coords(self.entry_window, right_x, entry_y)
        c.itemconfigure(self.entry_window, width=right_w - send_w - 12, height=entry_h)
        c.coords(self.send_window, right_x + right_w - send_w, entry_y)
        c.itemconfigure(self.send_window, width=send_w, height=entry_h)
        brackets(c, right_x - 6, entry_y - 6, right_x + right_w + 5, entry_y + entry_h + 5, EDGE, length=10)
        # left column frame
        brackets(c, MARGIN - 6, TOP + 14, MARGIN + LEFT_W + 6, self.info_box[1] - 12, EDGE, length=16)
        c.tag_lower("deco")

    def _focus_window(self):
        self.root.update_idletasks()
        width, height = 1080, 720
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.command_entry.focus_set()

    def _boot_sequence(self):
        locked = self.security is not None and self.security.is_locked()
        enabled = self.security is not None and self.security.enabled()
        lines = [
            ("CORE SYSTEMS", "ONLINE", "boot_ok"),
            ("VOICE RECOGNITION", "ACTIVE", "boot_ok"),
            ("LANGUAGE MODEL", "CLOUD + LOCAL", "boot_ok"),
            ("MAIL MONITOR", "ARMED", "boot_ok"),
            ("SECURITY", "LOCKED" if locked else ("UNLOCKED" if enabled else "NO PASSWORD"),
             "boot_warn" if locked or not enabled else "boot_ok"),
        ]
        t = self.transcript

        def show(index=0):
            if not self.running:
                return
            if index < len(lines):
                name, value, tag = lines[index]
                t.configure(state="normal")
                t.insert("end", f"[BOOT] {name} ".ljust(32, "."), "boot")
                t.insert("end", f" {value}\n", ("boot", tag))
                t.configure(state="disabled")
                self.root.after(140, show, index + 1)
            elif locked:
                self.root.after(300, self.ask_for_password)
            else:
                self._append_message("Jarvis", f"All systems online. At your service, {self.user}.")

        self.root.after(250, show)

    # ------------------------------------------------------------- behaviour
    def notify(self, message):
        """Shows and speaks a message that didn't come from a command (e.g. a reminder)."""
        if self.security is not None and self.security.is_locked():
            self.security.hold_message(message)  # keep mail and reminders private until unlocked
            message = "You have a new notification. Unlock me to hear it."
        print(f"[Jarvis] {message}")
        self.events.put(("message", ("Jarvis", message)))
        threading.Thread(target=self._say, args=(message,), daemon=True).start()

    def _change_busy(self, delta):
        with self._busy_lock:
            self.busy += delta

    def _say(self, text):
        """Speaks with the microphone muted so Jarvis doesn't hear itself."""
        set_muted = getattr(self.listener, "set_muted", None)
        with self.speech_lock:
            self._change_busy(1)
            self.events.put(("state", "speaking"))
            if set_muted:
                set_muted(True)
            try:
                self.speak(text)
            finally:
                if set_muted:
                    time.sleep(0.3)  # let the speaker's echo die down
                    set_muted(False)
                self._change_busy(-1)

    def ask_for_password(self):
        """Jarvis asks for the password out loud and listens for it straight away."""
        message = "Jarvis is locked. Please say the password, or type it below."
        wake_up = getattr(self.listener, "wake_up", None)
        if wake_up:
            wake_up()
        self.events.put(("message", ("Jarvis", message)))
        print(f"[Jarvis] {message}")
        threading.Thread(target=self._say, args=(message,), daemon=True).start()

    def _resting_state(self):
        if self.error_text:
            return "error"
        if self.security is not None and self.security.is_locked():
            return "locked"
        is_awake = getattr(self.listener, "is_awake", None)
        if is_awake and is_awake():
            return "listening"
        return "idle"

    def _idle_status(self):  # text version of the resting state (kept for compatibility)
        return STATES[self._resting_state()][2]

    def _clear_placeholder(self, _event=None):
        if self.command_entry.get() == PLACEHOLDER:
            self.command_entry.delete(0, "end")
            self.command_entry.configure(fg=TEXT)

    def _restore_placeholder(self, _event=None):
        locked = self.security is not None and self.security.is_locked()
        if not self.command_entry.get() and not locked:
            self.command_entry.insert(0, PLACEHOLDER)
            self.command_entry.configure(fg=TEXT_DIM)

    def _send_command(self, _event=None):
        text = self.command_entry.get().strip()
        if not text or text == PLACEHOLDER:
            return "break"
        self.command_entry.delete(0, "end")
        self.commands.put(("typed", text))
        return "break"

    def _voice_loop(self):
        while self.running:
            try:
                text = self.listener.listen()
                self.error_text = ""
                if text:
                    self.commands.put(("voice", text))
            except Exception as exc:
                self.error_text = str(exc)[:60]
                time.sleep(1)

    def _network_check(self):
        while self.running:
            try:
                socket.create_connection(("8.8.8.8", 53), timeout=2).close()
                self.online = True
            except OSError:
                self.online = False
            time.sleep(15)

    def _command_loop(self):
        while self.running:
            try:
                source, text = self.commands.get(timeout=0.2)
            except queue.Empty:
                continue

            is_control = text in ("WAKE_SIGNAL_CONFIRMED", "SLEEP_SIGNAL_CONFIRMED")
            if not is_control:
                shown = self.security.hide(text) if self.security is not None else text
                self.events.put(("message", ("You", shown)))
            self._change_busy(1)
            self.events.put(("state", "thinking"))

            set_busy = getattr(self.listener, "set_busy", None)
            if set_busy:
                set_busy(True)  # don't fall asleep while a long task runs
            started = time.time()
            try:
                response, should_close = self.handle_command(text, source=source)
            except Exception as exc:
                response, should_close = f"I am sorry, {self.user}. I encountered an error: {exc}", False

            if response:
                print(f"[Jarvis] {response}")
                self.events.put(("message", ("Jarvis", response)))
                thought = time.time() - started
                try:
                    self._say(response)
                except Exception as exc:
                    print(f"[Speech] {exc}")
                print(f"[Timing] thinking/doing {thought:.1f}s, speaking {time.time() - started - thought:.1f}s")
            if not is_control:
                self.commands_done += 1

            self._change_busy(-1)
            if set_busy:
                set_busy(False)
            if should_close:
                self.events.put(("close", None))
                return

    # ------------------------------------------------------------ messages
    def _append_message(self, speaker, message):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        t = self.transcript
        if speaker == "You":
            self._finish_typing()
            t.configure(state="normal")
            t.insert("end", "YOU  ", "user_head")
            t.insert("end", f"// {stamp}  <\n", ("user_head", "time"))
            t.insert("end", f"{message}\n", "user_body")
            t.configure(state="disabled")
            t.see("end")
        else:
            self._typing.append((stamp, str(message)))
            self._type_next()

    def _type_next(self):
        if self._typing_now is not None or not self._typing or not self.running:
            return
        stamp, message = self._typing.pop(0)
        self._write_jarvis_head(stamp)
        t = self.transcript
        t.configure(state="normal")
        t.insert("end", "\u2588", ("jarvis_body", "cursor"))
        t.configure(state="disabled")
        step = max(2, len(message) // 70)  # long replies type faster, so typing takes about a second
        self._typing_now = [message, 0, step]
        self._typing_gen += 1
        self._type_tick(self._typing_gen)

    def _write_jarvis_head(self, stamp):
        t = self.transcript
        t.configure(state="normal")
        t.insert("end", "> JARVIS  ", "jarvis_head")
        t.insert("end", f"// {stamp}\n", ("jarvis_head", "time"))
        t.configure(state="disabled")

    def _type_tick(self, generation):
        if generation != self._typing_gen or self._typing_now is None or not self.running:
            return  # an older reply that was already finished
        message, done, step = self._typing_now
        chunk = message[done:done + step]
        t = self.transcript
        t.configure(state="normal")
        t.insert("end-2c", chunk, "jarvis_body")  # just before the blinking block
        t.configure(state="disabled")
        t.see("end")
        done += len(chunk)
        if done >= len(message):
            self._end_typing()
            self.root.after(60, self._type_next)
        else:
            self._typing_now[1] = done
            self.root.after(15, self._type_tick, generation)

    def _end_typing(self):
        t = self.transcript
        t.configure(state="normal")
        t.delete("end-2c")  # the block cursor
        t.insert("end", "\n", "jarvis_body")
        t.configure(state="disabled")
        self._typing_now = None

    def _finish_typing(self):
        """Prints any half-typed or waiting replies in full, so a new message never lands inside one."""
        t = self.transcript
        if self._typing_now is not None:
            message, done, _ = self._typing_now
            t.configure(state="normal")
            t.insert("end-2c", message[done:], "jarvis_body")
            t.configure(state="disabled")
            self._end_typing()
        while self._typing:
            stamp, message = self._typing.pop(0)
            self._write_jarvis_head(stamp)
            t.configure(state="normal")
            t.insert("end", f"{message}\n", "jarvis_body")
            t.configure(state="disabled")
        t.see("end")

    # ---------------------------------------------------------------- refresh
    def _show_state(self, state):
        self.state = state
        self.reactor.set_state(state)
        colour, _, title, subtitle, *_ = STATES[state]
        if state == "error":
            subtitle = self.error_text.upper()
        self.canvas.itemconfigure(self.state_title, text=title, fill=colour)
        self.canvas.itemconfigure(self.state_sub, text=f"// {subtitle.format(name=self.user.upper())}")
        self.prompt_mark.configure(fg=colour)

    def _update_password_entry(self):
        """While locked, the command box hides what's typed (it's the password)."""
        if self.security is None:
            return
        locked = self.security.is_locked()
        wanted = "•" if locked else ""
        if self.command_entry.cget("show") != wanted:
            if locked and self.command_entry.get() == PLACEHOLDER:
                self.command_entry.delete(0, "end")
                self.command_entry.configure(fg=TEXT)
            self.command_entry.configure(show=wanted)
            if not locked:
                self._restore_placeholder()

    def _update_hud(self):
        if not self.running:
            return
        c = self.canvas
        now = datetime.datetime.now()
        c.itemconfigure(self.clock, text=now.strftime("%H:%M:%S"))
        c.itemconfigure(self.date, text=now.strftime("%d.%m.%Y"))
        c.itemconfigure(self.day, text=now.strftime("%A").upper())

        locked = self.security is not None and self.security.is_locked()
        if self.security is None or not self.security.enabled():
            c.itemconfigure(self.readouts["SECURITY"], text="OPEN", fill=TEXT_DIM)
        elif locked:
            c.itemconfigure(self.readouts["SECURITY"], text="LOCKED", fill=RED)
        else:
            c.itemconfigure(self.readouts["SECURITY"], text="CLEARED", fill=GREEN)
        net = {True: ("ONLINE", GREEN), False: ("OFFLINE", AMBER), None: ("...", TEXT_DIM)}[self.online]
        c.itemconfigure(self.readouts["NETWORK"], text=net[0], fill=net[1])
        try:
            import psutil

            c.itemconfigure(self.readouts["CPU"], text=f"{psutil.cpu_percent():.0f}%")
            c.itemconfigure(self.readouts["RAM"], text=f"{psutil.virtual_memory().percent:.0f}%")
        except Exception:
            pass

        model = "CLOUD AI" if self.online else "LOCAL AI"
        c.itemconfigure(self.top_status, text=f"{model}  //  MIC {'ON' if not self.error_text else 'ERROR'}  //  "
                                              f"{now:%H:%M}")
        c.itemconfigure(self.ticker, text=f"SESSION {self.commands_done:03d} CMDS  //  VOICE: WHISPER  //  "
                                          f"MAIL: WATCHING  //  SAY “LOCK YOURSELF” TO SECURE")
        self.root.after(1000, self._update_hud)

    def _animate(self):
        if not self.running:
            return
        now = time.time()
        dt = min(0.1, now - self._last_frame)
        self._last_frame = now
        self.reactor.tick(dt)
        mic_level = getattr(self.listener, "mic_level", None)
        try:
            mic = mic_level() if mic_level else 0.0
        except Exception:
            mic = 0.0
        self.meter.tick(dt, self.state, self.reactor.colour, mic)
        self.root.after(33, self._animate)

    def _poll_events(self):
        self._update_password_entry()
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "message":
                    self._append_message(*payload)
                elif event == "state":
                    self._show_state(payload)
                elif event == "close":
                    self.close()
                    return
        except queue.Empty:
            pass
        if self.busy <= 0:
            resting = self._resting_state()
            if resting != self.state:
                self._show_state(resting)
        if self.running:
            self.root.after(100, self._poll_events)

    def _callback_error(self, exc_type, exc, tb):
        """Ctrl+C in the terminal closes Jarvis cleanly instead of printing a traceback."""
        if issubclass(exc_type, KeyboardInterrupt):
            self.close()
            return
        import traceback

        print("[UI] " + "".join(traceback.format_exception(exc_type, exc, tb)).strip())

    def close(self):
        if self.running:
            self.running = False
            if getattr(self, "mail_watcher", None):
                self.mail_watcher.stop()
            try:
                from automation import camera

                camera.stop()  # finish the video file properly if a recording is running
            except Exception:
                pass
            self.root.destroy()

    def run(self):
        self.root.mainloop()


def run_ui(handle_command, listener, speak):
    JarvisWindow(handle_command, listener, speak).run()
