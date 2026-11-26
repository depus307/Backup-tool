"""Tk widgets for the main window's action and progress styles."""
import math
import time
import tkinter as tk


def button_layout(style, name, palette):
    """Create a stretchable rounded border with native ttk button behavior."""
    radius = 7
    size = radius * 2 + 4
    images = []
    for fill, border in palette:
        image = tk.PhotoImage(width=size, height=size)
        for inset, color in enumerate((border, fill)):
            corner = radius - inset
            for y in range(inset, size - inset):
                dy = max(0, corner - min(y - inset, size - inset - 1 - y) - .5)
                dx = math.ceil(corner - math.sqrt(corner ** 2 - dy ** 2) - .5)
                image.put(color, to=(inset + dx, y, size - inset - dx, y + 1))
        images.append(image)
    normal, disabled, pressed, active = images
    element = name.removesuffix('.TButton') + '.rounded'
    style.element_create(element, 'image', normal, ('disabled', disabled), ('pressed', pressed),
                         ('active', active), border=radius + 1, padding=1, sticky='nsew')
    children = style.layout('TButton')[0][1]['children']
    style.layout(name, [(element, {'sticky': 'nsew', 'children': children})])
    return images


class ProgressTrack(tk.Canvas):
    HEIGHT = 12

    def __init__(self, parent, *, background, track, fill, highlight):
        super().__init__(parent, background=background, height=self.HEIGHT, highlightthickness=0, borderwidth=0)
        self.background, self.track, self.fill, self.highlight = (
            bytes(channel // 257 for channel in self.winfo_rgb(color))
            for color in (background, track, fill, highlight)
        )
        self.image = tk.PhotoImage(master=self)
        self.create_image(0, 0, anchor='nw', image=self.image)
        self.track_width = 0
        self.track_pixels = bytearray()
        self.target = self.fraction = 0
        self.active = False
        self.timer = None
        self.started_at = self.last_frame = 0
        self.bind('<Configure>', self.draw)

    def set(self, done, total):
        target = min(1, max(0, done / total)) if total else 0
        if target != self.target:
            self.target = target
            if self.timer is None or target < self.fraction:
                self.fraction = target
            self.draw()

    def animate(self, active):
        if active and not self.active:
            self.started_at = self.last_frame = time.monotonic()
        self.active = active
        if active and self.timer is None:
            self.tick()

    def tick(self):
        now = time.monotonic()
        delta = self.target - self.fraction
        self.fraction += delta * (1 - math.exp(-(now - self.last_frame) / .11)) if delta > .0005 else delta
        self.last_frame = now
        self.draw()
        self.timer = self.after(16, self.tick) if self.active or self.fraction != self.target else None

    @staticmethod
    def blend(base, overlay, opacity):
        return bytes(round(a + (b - a) * opacity) for a, b in zip(base, overlay))

    def paint_capsule(self, pixels, width, end, color, glow=None):
        """Composite the rounded ends with fractional pixel coverage."""
        radius = min(5., (end - 1) / 2)
        if radius <= 0:
            return
        strip = bytearray(color * width)
        if glow is not None:
            center, spread, tint, strength = glow
            for x in range(max(1, math.floor(center - spread)), min(width, math.ceil(center + spread))):
                fade = max(0, 1 - ((x + .5 - center) / spread) ** 2)
                strip[x * 3:x * 3 + 3] = self.blend(color, tint, strength * fade ** 3)
        for y in range(self.HEIGHT):
            dy = abs(y + .5 - self.HEIGHT / 2)
            if dy >= radius:
                continue
            inset = radius - math.sqrt(radius ** 2 - dy ** 2)
            left, right = 1 + inset, end - inset
            row = y * width * 3
            first, last = math.ceil(left), math.floor(right)
            if first < last:
                pixels[row + first * 3:row + last * 3] = strip[first * 3:last * 3]
            for x in {math.floor(left), last}:
                coverage = min(x + 1, right) - max(x, left)
                if coverage > 0:
                    offset = row + x * 3
                    pixels[offset:offset + 3] = self.blend(pixels[offset:offset + 3], strip[x * 3:x * 3 + 3],
                                                           coverage)

    def draw(self, event=None):
        width = self.winfo_width()
        if width < 12:
            return
        if width != self.track_width:
            self.track_width = width
            self.track_pixels = bytearray(self.background * (width * self.HEIGHT))
            self.paint_capsule(self.track_pixels, width, width - 1, self.track)
        pixels = self.track_pixels.copy()
        if self.fraction or self.active:
            end = 1 + (width - 2) * self.fraction if self.fraction else width - 1
            color = self.fill if self.fraction else self.track
            glow = None
            if self.active:
                spread = max(24, min(100, (end - 1) * .24))
                phase = ((time.monotonic() - self.started_at) % 2.5) / 2.5
                center = phase * (end + spread * 2) - spread
                glow = (center, spread, self.highlight, .48 if self.fraction else .32)
            if not self.fraction:
                pixels = bytearray(self.background * (width * self.HEIGHT))
            self.paint_capsule(pixels, width, end, color, glow)
        self.image.configure(data=f'P6\n{width} {self.HEIGHT}\n255\n'.encode('ascii') + pixels, format='PPM')
