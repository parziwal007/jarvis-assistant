"""Startup animation: the arc reactor assembles, J.A.R.V.I.S. decodes onto the screen, systems load,
then the reactor flares and Jarvis opens its main screen.

Turn it off with JARVIS_INTRO=off in .env. Click or press any key to skip it.
"""
import math
import random
import string
import time

BG = "#03070D"
GRID = "#08131F"
EDGE = "#13405C"
CYAN = "#27D8FF"
TEXT_DIM = "#5D7A92"
HEAD = "Bahnschrift SemiBold"
MONO = "Consolas"

DURATION = 5.2
TITLE = "J.A.R.V.I.S."
SUBTITLE = "JUST A RATHER VERY INTELLIGENT SYSTEM"
STEPS = [
    (0.00, "INITIALIZING CORE SYSTEMS"),
    (0.22, "LOADING VOICE RECOGNITION"),
    (0.44, "CONNECTING LANGUAGE MODEL"),
    (0.64, "ARMING MAIL MONITOR"),
    (0.82, "RUNNING SECURITY CHECK"),
    (0.97, "ALL SYSTEMS ONLINE"),
]


def _blend(a, b, amount):
    amount = max(0.0, min(1.0, amount))
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(ca, cb))


def _ease(t):
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _span(t, start, end):
    """0 before start, 1 after end, eased in between."""
    return _ease((t - start) / (end - start)) if end > start else float(t >= start)


class Intro:
    def __init__(self, root, canvas, width, height, on_done):
        self.root, self.canvas = root, canvas
        self.w, self.h = width, height
        self.on_done = on_done
        self.started = time.time()
        self.finished = False
        self.glitch = random.Random(7)
        canvas.bind("<Button-1>", self.skip)
        root.bind("<Key>", self.skip)
        self._frame()

    def skip(self, _event=None):
        if not self.finished:
            self.started = time.time() - DURATION

    def _frame(self):
        if self.finished:
            return
        t = time.time() - self.started
        self._draw(min(t, DURATION))
        if t >= DURATION:
            self.finished = True
            self.canvas.unbind("<Button-1>")
            self.root.unbind("<Key>")
            self.on_done()
            return
        self.root.after(16, self._frame)

    def _draw(self, t):
        c = self.canvas
        c.delete("intro")
        w, h = self.w, self.h
        cx, cy = w / 2, h / 2 - 40
        r = min(w, h) * 0.24
        flare = _span(t, 4.55, 5.2)            # final burst
        fade = 1 - flare

        # grid sweeping out from the centre
        grid = _span(t, 0.0, 1.0)
        for x in range(int(cx % 28), w, 28):
            if abs(x - cx) <= grid * w / 2:
                c.create_line(x, 0, x, h, fill=_blend(BG, GRID, fade), tags="intro")
        for y in range(int(cy % 28), h, 28):
            if abs(y - cy) <= grid * h / 2:
                c.create_line(0, y, w, y, fill=_blend(BG, GRID, fade), tags="intro")

        # scan line
        if t < 1.6:
            y = cy - r * 1.6 + (t / 1.6) * r * 3.2
            c.create_line(0, y, w, y, fill=_blend(BG, CYAN, 0.35), tags="intro")

        zoom = 1 + flare * 2.2                 # rings rush outward at the end
        spin = t * 70

        # tick marks appearing around the circle
        ticks = int(72 * _span(t, 0.4, 1.6))
        for i in range(ticks):
            angle = math.radians(i * 5 - 90)
            inner = r * zoom * (0.88 if i % 6 == 0 else 0.93)
            c.create_line(cx + math.cos(angle) * inner, cy + math.sin(angle) * inner,
                          cx + math.cos(angle) * r * zoom, cy + math.sin(angle) * r * zoom,
                          fill=_blend(BG, CYAN if i % 6 == 0 else EDGE, fade), width=2 if i % 6 == 0 else 1,
                          tags="intro")

        # rings drawing themselves in, then spinning
        rings = ((0.80, 0.5, 1.6, 4, 1), (0.66, 0.8, 1.9, 2, -1.6), (0.54, 1.0, 2.2, 3, 1.3), (0.42, 1.3, 2.4, 2, -2.2))
        for size, start, end, width, speed in rings:
            grow = _span(t, start, end)
            if grow <= 0:
                continue
            radius = r * size * zoom
            extent = 359 * grow
            colour = _blend(BG, CYAN, (0.55 + 0.45 * grow) * fade)
            if grow < 1:
                c.create_arc(cx - radius, cy - radius, cx + radius, cy + radius, start=90 + spin * speed,
                             extent=-extent, style="arc", width=width, outline=colour, tags="intro")
            else:  # finished ring becomes rotating segments
                for k in range(3):
                    c.create_arc(cx - radius, cy - radius, cx + radius, cy + radius,
                                 start=spin * speed + k * 120, extent=80, style="arc", width=width,
                                 outline=colour, tags="intro")

        # core: a dot that grows into the glowing centre, then flares white
        core = _span(t, 0.2, 1.4)
        pulse = (math.sin(t * 6) + 1) / 2
        core_r = r * (0.04 + 0.15 * core + 0.02 * pulse) * (1 + flare * 4)
        for i in range(5):
            gr = core_r * (2.0 - i * 0.22)
            c.create_oval(cx - gr, cy - gr, cx + gr, cy + gr, outline="",
                          fill=_blend(BG, CYAN, (0.08 + i * 0.12) * max(fade, flare * 0.8)), tags="intro")
        c.create_oval(cx - core_r, cy - core_r, cx + core_r, cy + core_r, outline="",
                      fill=_blend(CYAN, "#FFFFFF", 0.4 + 0.6 * flare), tags="intro")

        # title decoding letter by letter
        text_y = cy + r + 64
        letters = []
        for i, letter in enumerate(TITLE):
            settle = 1.6 + i * 0.12
            if t < settle - 0.35:
                letters.append(" ")
            elif t < settle and letter != ".":
                letters.append(self.glitch.choice(string.ascii_uppercase + string.digits))
            else:
                letters.append(letter)
        c.create_text(cx, text_y, text="".join(letters), fill=_blend(BG, CYAN, fade),
                      font=(HEAD, 34), tags="intro")
        shown = int(len(SUBTITLE) * _span(t, 2.6, 3.4))
        c.create_text(cx, text_y + 38, text=SUBTITLE[:shown], fill=_blend(BG, TEXT_DIM, fade),
                      font=(MONO, 10), tags="intro")

        # loading bar with system checks
        progress = _span(t, 2.4, 4.4)
        if progress > 0:
            bar_w = min(420, w * 0.5)
            x0, y0 = cx - bar_w / 2, text_y + 74
            c.create_rectangle(x0, y0, x0 + bar_w, y0 + 4, outline="", fill=_blend(BG, EDGE, fade), tags="intro")
            c.create_rectangle(x0, y0, x0 + bar_w * progress, y0 + 4, outline="",
                               fill=_blend(BG, CYAN, fade), tags="intro")
            step = [label for at, label in STEPS if progress >= at][-1]
            c.create_text(x0, y0 + 20, anchor="w", text=step, fill=_blend(BG, TEXT_DIM, fade),
                          font=(MONO, 9), tags="intro")
            c.create_text(x0 + bar_w, y0 + 20, anchor="e", text=f"{progress * 100:3.0f}%",
                          fill=_blend(BG, CYAN, fade), font=(MONO, 9), tags="intro")

        # corner brackets
        frame = _span(t, 0.3, 1.2)
        if frame > 0:
            length, m = 28 * frame, 24
            colour = _blend(BG, EDGE, fade)
            for x, y, dx, dy in ((m, m, 1, 1), (w - m, m, -1, 1), (m, h - m, 1, -1), (w - m, h - m, -1, -1)):
                c.create_line(x, y + dy * length, x, y, x + dx * length, y, fill=colour, width=2, tags="intro")
        if t < 4.4:
            c.create_text(w - 64, h - 34, anchor="e", text="CLICK TO SKIP", fill=_blend(BG, TEXT_DIM, 0.6),
                          font=(MONO, 8), tags="intro")

        # white flash at the very end
        if flare > 0.55:
            c.create_rectangle(0, 0, w, h, outline="", fill=_blend(BG, "#E8FBFF", (flare - 0.55) / 0.45 * 0.9),
                               tags="intro")
