#!/usr/bin/env python3
"""Nibbles - Enhanced Edition.

A modern re-imagination of the 1990 QBasic NIBBLES.BAS game.  The original
grid (80x50), the ten maze layouts, the scoring curve (1..9), the lives
counter and the two-player head-to-head are preserved faithfully.

What's layered on top:
    * Nine hand-drawn food sprites in place of the giant numeric digits.
    * Snake with gradient body, animated eyes that track the direction,
      and a subtle trail of particles.
    * 3D-ish beveled walls, radial-vignette background, grid overlay.
    * Particle system for eating, dying and level-ups.
    * Screen shake on crash, white flash + fanfare on level up.
    * HUD with heart lives, upcoming-item preview, combo multiplier.
    * Procedural audio: distinct tone per fruit, death arpeggio,
      victory chime, soft menu melody.
    * Persistent high scores at ~/.nibbles_scores.json.

Controls
    Player 1 .... Arrow keys
    Player 2 .... W A S D
    P / Space ... Pause
    Esc ......... Back / quit
"""

from __future__ import annotations

import array
import json
import math
import os
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

GRID_COLS = 80
GRID_ROWS = 50
CELL = 14
HUD_H = 52
SCREEN_W = GRID_COLS * CELL
SCREEN_H = GRID_ROWS * CELL + HUD_H
FPS = 60
MAX_LEN = 1000

HISCORE_FILE = Path.home() / ".nibbles_scores.json"
HISCORE_LIMIT = 10

DIRS = {1: (-1, 0), 2: (1, 0), 3: (0, -1), 4: (0, 1)}
OPP = {1: 2, 2: 1, 3: 4, 4: 3}

# Palette
BG_DEEP = (6, 8, 36)
BG_MID = (12, 14, 60)
BG_SOFT = (24, 26, 86)
GRID_COL = (24, 28, 92)
WALL_FILL = (190, 46, 70)
WALL_HI = (255, 150, 150)
WALL_LO = (90, 10, 24)
NUM_COL = (255, 240, 120)
HUD_BG = (0, 0, 0)
HUD_FG = (240, 240, 240)
DIALOG_BG = (24, 10, 48)
DIALOG_BORDER = (255, 220, 140)
DIALOG_FG = (255, 255, 255)
HEART_COL = (240, 70, 90)
HEART_SHINE = (255, 180, 180)

SAMMY_DARK = (140, 110, 20)
SAMMY_LIGHT = (255, 220, 70)
SAMMY_HEAD = (255, 245, 150)

JAKE_DARK = (120, 30, 110)
JAKE_LIGHT = (240, 100, 210)
JAKE_HEAD = (255, 180, 240)

WHITE = (250, 250, 250)
GREY = (170, 170, 180)
DIM = (110, 110, 140)

EMPTY, WALL = "empty", "wall"

# --------------------------------------------------------------------------- #
# Audio                                                                       #
# --------------------------------------------------------------------------- #

SAMPLE_RATE = 22050
_sound_cache: dict = {}
_mixer_ok = False


def _init_audio() -> None:
    global _mixer_ok
    try:
        pygame.mixer.pre_init(SAMPLE_RATE, -16, 1, 256)
        pygame.mixer.init()
        _mixer_ok = True
    except pygame.error:
        _mixer_ok = False


def _tone(freq: float, duration_ms: int, volume: float = 0.3,
          wave: str = "square") -> Optional[pygame.mixer.Sound]:
    if not _mixer_ok:
        return None
    key = (round(freq, 2), duration_ms, round(volume, 2), wave)
    if key in _sound_cache:
        return _sound_cache[key]
    n = int(SAMPLE_RATE * duration_ms / 1000)
    if n <= 0:
        return None
    buf = array.array("h")
    amp = int(32767 * volume)
    attack = max(1, int(SAMPLE_RATE * 0.005))
    release = max(1, int(SAMPLE_RATE * 0.05))
    for i in range(n):
        t = i / SAMPLE_RATE
        if wave == "square":
            v = amp if math.sin(2 * math.pi * freq * t) >= 0 else -amp
        elif wave == "triangle":
            x = (freq * t) % 1
            v = int(amp * (4 * abs(x - 0.5) - 1))
        else:
            v = int(amp * math.sin(2 * math.pi * freq * t))
        if i < attack:
            v = int(v * (i / attack))
        elif i > n - release:
            v = int(v * max(0.0, (n - i) / release))
        buf.append(v)
    snd = pygame.mixer.Sound(buffer=buf.tobytes())
    snd.set_volume(volume)
    _sound_cache[key] = snd
    return snd


# Distinct eating tones per food value
EAT_FREQS = (523, 587, 659, 698, 784, 880, 988, 1046, 1318)


def play_eat(value: int) -> None:
    f = EAT_FREQS[max(1, min(9, value)) - 1]
    s = _tone(f, 70, 0.25, "triangle")
    if s:
        s.play()


def play_die() -> None:
    if not _mixer_ok:
        return
    for f, d in ((440, 90), (330, 90), (247, 120), (175, 220)):
        snd = _tone(f, d, 0.35)
        if snd:
            snd.play()
            pygame.time.delay(d)


def play_levelup() -> None:
    if not _mixer_ok:
        return
    for f in (523, 659, 784, 1046, 1319):
        snd = _tone(f, 80, 0.35, "triangle")
        if snd:
            snd.play()
            pygame.time.delay(80)


def play_blip() -> None:
    s = _tone(660, 30, 0.2, "triangle")
    if s:
        s.play()


def play_menu_chord() -> None:
    if not _mixer_ok:
        return
    # Short arpeggio when entering a menu
    for f in (523, 659, 784):
        s = _tone(f, 110, 0.25, "triangle")
        if s:
            s.play()
            pygame.time.delay(110)


# --------------------------------------------------------------------------- #
# Food sprites: hand-drawn with pygame primitives                             #
# --------------------------------------------------------------------------- #

FOOD_NAMES = [
    "Apple", "Cherries", "Orange", "Banana", "Grapes",
    "Strawberry", "Watermelon", "Pineapple", "Golden Star",
]


def _soft_shadow(surface: pygame.Surface, cx: int, cy: int, radius: int) -> None:
    shadow = pygame.Surface((radius * 2, radius * 2), pygame.SRCALPHA)
    for r in range(radius, 0, -1):
        alpha = int(70 * (r / radius) ** 2)
        pygame.draw.circle(shadow, (0, 0, 0, 90 - alpha),
                           (radius, radius), r)
    surface.blit(shadow, (cx - radius, cy - radius))


def _draw_apple(s: pygame.Surface, sz: int) -> None:
    cx, cy = sz // 2, sz // 2 + 1
    r = int(sz * 0.33)
    _soft_shadow(s, cx, cy + r - 2, r)
    pygame.draw.circle(s, (170, 20, 20), (cx + 2, cy + 1), r)
    pygame.draw.circle(s, (235, 50, 50), (cx - 1, cy), r)
    pygame.draw.circle(s, (255, 150, 150), (cx - r // 2, cy - r // 2),
                       max(2, r // 3))
    # Leaf
    leaf = pygame.Surface((sz // 3, sz // 5), pygame.SRCALPHA)
    pygame.draw.ellipse(leaf, (60, 180, 70), leaf.get_rect())
    pygame.draw.ellipse(leaf, (100, 220, 100), leaf.get_rect().inflate(-2, -2))
    leaf = pygame.transform.rotate(leaf, 35)
    s.blit(leaf, (cx, cy - r - 3))
    # Stem
    pygame.draw.line(s, (80, 50, 20), (cx, cy - r),
                     (cx + 1, cy - r - 4), 2)


def _draw_cherries(s: pygame.Surface, sz: int) -> None:
    r = int(sz * 0.23)
    cy = int(sz * 0.65)
    for off, cshade in ((-r, (200, 20, 30)), (r + 1, (235, 45, 60))):
        cx = sz // 2 + off
        _soft_shadow(s, cx, cy + r - 2, r)
        pygame.draw.circle(s, (120, 10, 15), (cx + 1, cy + 1), r)
        pygame.draw.circle(s, cshade, (cx, cy), r)
        pygame.draw.circle(s, (255, 170, 170),
                           (cx - r // 2, cy - r // 2), max(1, r // 4))
    # Stems meeting at the top
    top = (sz // 2, int(sz * 0.15))
    pygame.draw.line(s, (90, 150, 40), (sz // 2 - r, cy - r), top, 2)
    pygame.draw.line(s, (90, 150, 40), (sz // 2 + r + 1, cy - r), top, 2)
    # Little leaf
    leaf = pygame.Surface((sz // 3, sz // 6), pygame.SRCALPHA)
    pygame.draw.ellipse(leaf, (70, 180, 70), leaf.get_rect())
    s.blit(leaf, (top[0] - 1, top[1] - 2))


def _draw_orange(s: pygame.Surface, sz: int) -> None:
    cx, cy = sz // 2, sz // 2 + 1
    r = int(sz * 0.35)
    _soft_shadow(s, cx, cy + r - 2, r)
    pygame.draw.circle(s, (190, 100, 10), (cx + 1, cy + 1), r)
    pygame.draw.circle(s, (255, 150, 40), (cx, cy), r)
    # Texture dots
    for _ in range(10):
        tx = cx + random.randint(-r + 2, r - 2)
        ty = cy + random.randint(-r + 2, r - 2)
        if (tx - cx) ** 2 + (ty - cy) ** 2 < (r - 2) ** 2:
            pygame.draw.circle(s, (230, 120, 20), (tx, ty), 1)
    pygame.draw.circle(s, (255, 220, 180),
                       (cx - r // 2, cy - r // 2), max(2, r // 3))
    # Leaf
    pygame.draw.polygon(s, (60, 170, 70),
                        [(cx, cy - r + 1), (cx + 4, cy - r - 2),
                         (cx - 2, cy - r - 2)])


def _draw_banana(s: pygame.Surface, sz: int) -> None:
    # Flattened ellipse rotated to taste - that's a convincing banana.
    ell = pygame.Surface((int(sz * 0.9), int(sz * 0.42)), pygame.SRCALPHA)
    pygame.draw.ellipse(ell, (170, 115, 10), ell.get_rect().move(1, 2))
    pygame.draw.ellipse(ell, (255, 215, 50), ell.get_rect())
    hi = ell.get_rect().inflate(-6, -ell.get_height() // 2 - 2)
    hi.y = 2
    pygame.draw.ellipse(ell, (255, 240, 140), hi)
    rotated = pygame.transform.rotate(ell, -30)
    s.blit(rotated, rotated.get_rect(center=(sz // 2, sz // 2)))
    pygame.draw.circle(s, (90, 60, 20),
                       (sz // 2 - int(sz * 0.33), int(sz * 0.68)), 2)
    pygame.draw.circle(s, (90, 60, 20),
                       (sz // 2 + int(sz * 0.33), int(sz * 0.32)), 2)


def _draw_grapes(s: pygame.Surface, sz: int) -> None:
    r = max(2, int(sz * 0.12))
    centres = [
        (0.5, 0.28), (0.35, 0.42), (0.65, 0.42),
        (0.25, 0.56), (0.5, 0.56), (0.75, 0.56),
        (0.37, 0.72), (0.63, 0.72), (0.5, 0.86),
    ]
    for fx, fy in centres:
        cx = int(sz * fx)
        cy = int(sz * fy)
        pygame.draw.circle(s, (60, 20, 90), (cx + 1, cy + 1), r)
        pygame.draw.circle(s, (150, 60, 200), (cx, cy), r)
        pygame.draw.circle(s, (210, 160, 240),
                           (cx - r // 2, cy - r // 2), max(1, r // 3))
    # Leaf
    pygame.draw.polygon(s, (70, 170, 70),
                        [(sz // 2 - 4, 4), (sz // 2 + 4, 4),
                         (sz // 2 + 6, int(sz * 0.22)),
                         (sz // 2 - 6, int(sz * 0.22))])


def _draw_strawberry(s: pygame.Surface, sz: int) -> None:
    # Rounded triangle body
    pts = [(sz // 2, int(sz * 0.95)),
           (int(sz * 0.1), int(sz * 0.45)),
           (int(sz * 0.9), int(sz * 0.45))]
    _soft_shadow(s, sz // 2 + 1, int(sz * 0.75), sz // 3)
    pygame.draw.polygon(s, (220, 30, 50), pts)
    # Soften corners
    pygame.draw.circle(s, (220, 30, 50), (int(sz * 0.1), int(sz * 0.45)), 5)
    pygame.draw.circle(s, (220, 30, 50), (int(sz * 0.9), int(sz * 0.45)), 5)
    pygame.draw.circle(s, (220, 30, 50), (sz // 2, int(sz * 0.95)), 4)
    # Highlight
    pygame.draw.circle(s, (255, 170, 170),
                       (int(sz * 0.35), int(sz * 0.55)), 3)
    # Seeds
    for fx, fy in [(0.3, 0.6), (0.5, 0.55), (0.68, 0.6),
                   (0.4, 0.72), (0.6, 0.72), (0.5, 0.85)]:
        pygame.draw.circle(s, (255, 240, 120),
                           (int(sz * fx), int(sz * fy)), 1)
    # Calyx
    for angle in (-30, 0, 30):
        tip = (sz // 2 + int(math.sin(math.radians(angle)) * sz * 0.3),
               int(sz * 0.45) - int(math.cos(math.radians(angle)) * sz * 0.18))
        pygame.draw.polygon(s, (60, 170, 70),
                            [(sz // 2, int(sz * 0.45)), tip,
                             (sz // 2 + 2, int(sz * 0.45) - 2)])


def _draw_watermelon(s: pygame.Surface, sz: int) -> None:
    # Draw a full ellipse but clip the top half away for a slice look.
    rect = pygame.Rect(2, int(sz * 0.1), sz - 4, int(sz * 0.8))
    clip = pygame.Rect(0, int(sz * 0.3), sz, sz)
    s.set_clip(clip)
    pygame.draw.ellipse(s, (30, 90, 40), rect.move(1, 1))
    pygame.draw.ellipse(s, (60, 160, 50), rect)
    pygame.draw.ellipse(s, (170, 230, 140), rect.inflate(-4, -4))
    pygame.draw.ellipse(s, (240, 90, 110), rect.inflate(-10, -10))
    s.set_clip(None)
    pygame.draw.line(s, (30, 90, 40),
                     (2, int(sz * 0.3) + 1),
                     (sz - 3, int(sz * 0.3) + 1), 2)
    pygame.draw.line(s, (170, 230, 140),
                     (6, int(sz * 0.3) + 3),
                     (sz - 7, int(sz * 0.3) + 3), 1)
    for fx, fy in [(0.3, 0.55), (0.5, 0.5), (0.7, 0.55),
                   (0.4, 0.72), (0.6, 0.72)]:
        pygame.draw.ellipse(s, (30, 20, 20),
                            (int(sz * fx) - 1, int(sz * fy), 2, 3))


def _draw_pineapple(s: pygame.Surface, sz: int) -> None:
    body = pygame.Rect(int(sz * 0.22), int(sz * 0.3),
                       int(sz * 0.56), int(sz * 0.6))
    _soft_shadow(s, sz // 2, int(sz * 0.85), sz // 3)
    pygame.draw.ellipse(s, (190, 130, 10), body.move(1, 1))
    pygame.draw.ellipse(s, (240, 190, 50), body)
    # Diamond pattern
    for i in range(3):
        y = body.top + 6 + i * 6
        for j in range(3):
            x = body.left + 5 + j * 7
            pygame.draw.polygon(s, (160, 100, 10),
                                [(x, y - 2), (x + 3, y),
                                 (x, y + 2), (x - 3, y)])
    # Crown
    for off in (-6, -2, 2, 6):
        pygame.draw.polygon(s, (60, 160, 60),
                            [(sz // 2 + off, int(sz * 0.3)),
                             (sz // 2 + off - 2, int(sz * 0.1)),
                             (sz // 2 + off + 2, int(sz * 0.1))])
    pygame.draw.polygon(s, (100, 200, 100),
                        [(sz // 2, int(sz * 0.3) - 2),
                         (sz // 2 - 3, int(sz * 0.05)),
                         (sz // 2 + 3, int(sz * 0.05))])


def _draw_star(s: pygame.Surface, sz: int) -> None:
    cx, cy = sz // 2, sz // 2
    outer = sz * 0.42
    inner = sz * 0.18
    pts = []
    for i in range(10):
        angle = -math.pi / 2 + math.pi * i / 5
        r = outer if i % 2 == 0 else inner
        pts.append((cx + math.cos(angle) * r,
                    cy + math.sin(angle) * r))
    # Glow
    glow = pygame.Surface((sz, sz), pygame.SRCALPHA)
    for g in range(6, 0, -1):
        alpha = 25
        exp = [(cx + (p[0] - cx) * (1 + g * 0.08),
                cy + (p[1] - cy) * (1 + g * 0.08)) for p in pts]
        pygame.draw.polygon(glow, (255, 220, 60, alpha), exp)
    s.blit(glow, (0, 0))
    pygame.draw.polygon(s, (200, 150, 20), [(x + 1, y + 1) for x, y in pts])
    pygame.draw.polygon(s, (255, 230, 80), pts)
    pygame.draw.polygon(s, (255, 250, 180),
                        [(cx + (p[0] - cx) * 0.5,
                          cy + (p[1] - cy) * 0.5) for p in pts])


_FOOD_DRAWERS: List[Callable[[pygame.Surface, int], None]] = [
    _draw_apple, _draw_cherries, _draw_orange, _draw_banana,
    _draw_grapes, _draw_strawberry, _draw_watermelon,
    _draw_pineapple, _draw_star,
]


def build_food_sprites() -> List[pygame.Surface]:
    sz = int(CELL * 2.4)
    sprites: List[pygame.Surface] = []
    for drawer in _FOOD_DRAWERS:
        surf = pygame.Surface((sz, sz), pygame.SRCALPHA)
        drawer(surf, sz)
        sprites.append(surf)
    return sprites


def build_food_icon(sprite: pygame.Surface, size: int) -> pygame.Surface:
    return pygame.transform.smoothscale(sprite, (size, size))


# --------------------------------------------------------------------------- #
# Wall & background tiles                                                     #
# --------------------------------------------------------------------------- #


def build_wall_tile() -> pygame.Surface:
    s = pygame.Surface((CELL, CELL), pygame.SRCALPHA)
    pygame.draw.rect(s, WALL_FILL, (0, 0, CELL, CELL))
    # Bevel: light on top-left, dark on bottom-right
    pygame.draw.line(s, WALL_HI, (0, 0), (CELL - 1, 0), 2)
    pygame.draw.line(s, WALL_HI, (0, 0), (0, CELL - 1), 2)
    pygame.draw.line(s, WALL_LO, (CELL - 1, 1), (CELL - 1, CELL - 1), 2)
    pygame.draw.line(s, WALL_LO, (1, CELL - 1), (CELL - 1, CELL - 1), 2)
    # Rivets
    pygame.draw.circle(s, WALL_HI, (CELL // 2, CELL // 2), 1)
    return s


def build_background() -> pygame.Surface:
    """Pre-render the parallax starfield + vignette."""
    s = pygame.Surface((SCREEN_W, SCREEN_H - HUD_H))
    # Vertical gradient
    for y in range(SCREEN_H - HUD_H):
        t = y / (SCREEN_H - HUD_H)
        r = int(BG_DEEP[0] * (1 - t) + BG_MID[0] * t)
        g = int(BG_DEEP[1] * (1 - t) + BG_MID[1] * t)
        b = int(BG_DEEP[2] * (1 - t) + BG_MID[2] * t)
        pygame.draw.line(s, (r, g, b), (0, y), (SCREEN_W, y))
    # Scatter stars
    rng = random.Random(1337)
    for _ in range(240):
        x = rng.randint(0, SCREEN_W - 1)
        y = rng.randint(0, SCREEN_H - HUD_H - 1)
        br = rng.choice((60, 90, 140, 200))
        s.set_at((x, y), (br, br, min(255, br + 30)))
    # A few brighter sparkles
    for _ in range(28):
        x = rng.randint(2, SCREEN_W - 3)
        y = rng.randint(2, SCREEN_H - HUD_H - 3)
        pygame.draw.circle(s, (220, 220, 255), (x, y), 1)
    # Vignette
    vign = pygame.Surface((SCREEN_W, SCREEN_H - HUD_H), pygame.SRCALPHA)
    cx, cy = SCREEN_W // 2, (SCREEN_H - HUD_H) // 2
    max_d = int(math.hypot(cx, cy))
    for ring in range(6):
        alpha = 18
        r = int(max_d * (0.7 + ring * 0.07))
        pygame.draw.circle(vign, (0, 0, 0, alpha),
                           (cx, cy), r, width=max(1, max_d // 10))
    s.blit(vign, (0, 0))
    return s


# --------------------------------------------------------------------------- #
# Particles                                                                   #
# --------------------------------------------------------------------------- #


class ParticleSystem:
    """A tiny CPU-side particle system - enough to garnish feedback events."""

    def __init__(self) -> None:
        self.parts: List[List[float]] = []

    def emit(self, x: float, y: float, color: Tuple[int, int, int],
             count: int = 14, speed: float = 160.0,
             life: float = 0.55, size: int = 3,
             gravity: float = 220.0, spread: float = math.tau) -> None:
        for _ in range(count):
            ang = random.random() * spread
            spd = random.uniform(speed * 0.4, speed)
            self.parts.append([
                x, y,
                math.cos(ang) * spd, math.sin(ang) * spd,
                life, life,
                color[0], color[1], color[2],
                size, gravity,
            ])

    def spark(self, x: float, y: float, color: Tuple[int, int, int]) -> None:
        self.emit(x, y, color, count=18, speed=180, life=0.5,
                  size=3, gravity=200)

    def burst(self, x: float, y: float, color: Tuple[int, int, int]) -> None:
        self.emit(x, y, color, count=40, speed=260, life=0.9,
                  size=4, gravity=180)

    def trail(self, x: float, y: float, color: Tuple[int, int, int]) -> None:
        self.emit(x, y, color, count=1, speed=20, life=0.3,
                  size=2, gravity=10)

    def update(self, dt: float) -> None:
        kept: List[List[float]] = []
        for p in self.parts:
            p[0] += p[2] * dt
            p[1] += p[3] * dt
            p[3] += p[10] * dt
            p[4] -= dt
            if p[4] > 0:
                kept.append(p)
        self.parts = kept

    def draw(self, surface: pygame.Surface) -> None:
        for p in self.parts:
            life_frac = max(0.0, p[4] / p[5])
            alpha = int(255 * life_frac)
            size = max(1, int(p[9] * (0.4 + 0.6 * life_frac)))
            tmp = pygame.Surface((size * 2, size * 2), pygame.SRCALPHA)
            pygame.draw.circle(tmp,
                               (int(p[6]), int(p[7]), int(p[8]), alpha),
                               (size, size), size)
            surface.blit(tmp, (p[0] - size, p[1] - size))


# --------------------------------------------------------------------------- #
# Snake entity                                                                #
# --------------------------------------------------------------------------- #


@dataclass
class Snake:
    name: str
    dark: Tuple[int, int, int]
    light: Tuple[int, int, int]
    head_col: Tuple[int, int, int]
    body: List[Tuple[int, int]] = field(default_factory=list)
    direction: int = 4
    pending: int = 4
    target_length: int = 2
    score: int = 0
    lives: int = 5
    alive: bool = True
    combo: int = 0
    combo_timer: float = 0.0

    @property
    def head(self) -> Tuple[int, int]:
        return self.body[-1]

    def reset(self, row: int, col: int, direction: int) -> None:
        self.body = [(row, col)]
        self.direction = direction
        self.pending = direction
        self.target_length = 2
        self.alive = True
        self.combo = 0
        self.combo_timer = 0.0


def _lerp(a: Tuple[int, int, int], b: Tuple[int, int, int],
          t: float) -> Tuple[int, int, int]:
    return (int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))


def draw_snake(surface: pygame.Surface, snake: Snake, time_s: float) -> None:
    if not snake.body:
        return
    n = len(snake.body)
    for i, (r, c) in enumerate(snake.body):
        t = i / max(1, n - 1)
        col = _lerp(snake.dark, snake.light, 0.3 + 0.7 * t)
        rect = cell_rect(r, c).inflate(-1, -1)
        pygame.draw.rect(surface, col, rect, border_radius=4)
    # Head overlay + eyes
    hr, hc = snake.head
    head_rect = cell_rect(hr, hc)
    pygame.draw.rect(surface, snake.head_col,
                     head_rect.inflate(1, 1), border_radius=5)
    pygame.draw.rect(surface, (20, 20, 30),
                     head_rect.inflate(1, 1), 1, border_radius=5)

    # Eyes
    dr, dc = DIRS[snake.direction]
    cx = head_rect.centerx
    cy = head_rect.centery
    # Perpendicular offset for the two eyes
    px, py = -dc, dr
    eye_off = max(2, CELL // 4)
    eye_r = max(2, CELL // 5)
    for sign in (-1, 1):
        ex = cx + px * sign * eye_off
        ey = cy + py * sign * eye_off
        pygame.draw.circle(surface, (240, 240, 250), (ex, ey), eye_r)
        # Pupil shifted toward moving direction
        pupil_shift = max(1, eye_r // 2)
        pygame.draw.circle(surface, (20, 20, 40),
                           (ex + dc * pupil_shift,
                            ey + dr * pupil_shift),
                           max(1, eye_r - 2))

    # Tongue flicker every ~0.6s
    if math.sin(time_s * 10) > 0.9 and len(snake.body) > 1:
        tongue_tip = (cx + dc * CELL,
                      cy + dr * CELL)
        pygame.draw.line(surface, (240, 60, 70),
                         (cx + dc * (CELL // 2),
                          cy + dr * (CELL // 2)),
                         tongue_tip, 2)
        # Fork
        pygame.draw.line(surface, (240, 60, 70), tongue_tip,
                         (tongue_tip[0] - dr * 2,
                          tongue_tip[1] + dc * 2), 2)


# --------------------------------------------------------------------------- #
# Level data (identical to BAS original)                                      #
# --------------------------------------------------------------------------- #


def build_walls(grid: List[List[str]], level: int) -> None:
    for c in range(1, GRID_COLS + 1):
        grid[3][c] = WALL
        grid[50][c] = WALL
    for r in range(4, 50):
        grid[r][1] = WALL
        grid[r][GRID_COLS] = WALL

    if level == 1:
        return
    if level == 2:
        for c in range(20, 61):
            grid[25][c] = WALL
        return
    if level == 3:
        for r in range(10, 41):
            grid[r][20] = WALL
            grid[r][60] = WALL
        return
    if level == 4:
        for r in range(4, 31):
            grid[r][20] = WALL
        for r in range(23, 50):
            grid[r][60] = WALL
        for c in range(2, 41):
            grid[38][c] = WALL
        for c in range(41, 80):
            grid[15][c] = WALL
        return
    if level == 5:
        for r in range(13, 40):
            grid[r][21] = WALL
            grid[r][59] = WALL
        for c in range(23, 58):
            grid[11][c] = WALL
            grid[41][c] = WALL
        return
    if level == 6:
        for r in range(4, 50):
            if r > 30 or r < 23:
                for c in (10, 20, 30, 40, 50, 60, 70):
                    grid[r][c] = WALL
        return
    if level == 7:
        for r in range(4, 50, 2):
            grid[r][40] = WALL
        return
    if level == 8:
        for i in range(4, 41):
            grid[i][10] = WALL
            grid[53 - i][20] = WALL
            grid[i][30] = WALL
            grid[53 - i][40] = WALL
            grid[i][50] = WALL
            grid[53 - i][60] = WALL
            grid[i][70] = WALL
        return
    if level == 9:
        for i in range(6, 48):
            if 1 <= i <= GRID_ROWS and 1 <= i <= GRID_COLS:
                grid[i][i] = WALL
            if 1 <= i <= GRID_ROWS and 1 <= i + 28 <= GRID_COLS:
                grid[i][i + 28] = WALL
        return
    # 10+: alternating zigzag
    for i in range(4, 50, 2):
        grid[i][10] = WALL
        if i + 1 < 50:
            grid[i + 1][20] = WALL
        grid[i][30] = WALL
        if i + 1 < 50:
            grid[i + 1][40] = WALL
        grid[i][50] = WALL
        if i + 1 < 50:
            grid[i + 1][60] = WALL
        grid[i][70] = WALL


SPAWN_TABLE = {
    1:  ((25, 50, 4), (25, 30, 3)),
    2:  ((7, 60, 3), (43, 20, 4)),
    3:  ((25, 50, 1), (25, 30, 2)),
    4:  ((7, 60, 3), (43, 20, 4)),
    5:  ((25, 50, 1), (25, 30, 2)),
    6:  ((7, 65, 2), (43, 15, 1)),
    7:  ((7, 65, 2), (43, 15, 1)),
    8:  ((7, 65, 2), (43, 15, 1)),
    9:  ((40, 75, 1), (15, 5, 2)),
}


def spawn_for(level: int) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    return SPAWN_TABLE.get(level, ((7, 65, 2), (43, 15, 1)))


# --------------------------------------------------------------------------- #
# High-score persistence                                                      #
# --------------------------------------------------------------------------- #


def load_hiscores() -> List[dict]:
    if not HISCORE_FILE.exists():
        return []
    try:
        data = json.loads(HISCORE_FILE.read_text())
        if isinstance(data, list):
            return data[:HISCORE_LIMIT]
    except (json.JSONDecodeError, OSError):
        pass
    return []


def save_hiscore(name: str, score: int, level: int, players: int) -> List[dict]:
    entries = load_hiscores()
    entries.append({"name": (name or "PLAYER")[:12],
                    "score": int(score),
                    "level": int(level),
                    "players": int(players)})
    entries.sort(key=lambda e: e["score"], reverse=True)
    entries = entries[:HISCORE_LIMIT]
    try:
        HISCORE_FILE.write_text(json.dumps(entries, indent=2))
    except OSError:
        pass
    return entries


# --------------------------------------------------------------------------- #
# Rendering helpers                                                           #
# --------------------------------------------------------------------------- #


def cell_rect(r: int, c: int) -> pygame.Rect:
    return pygame.Rect((c - 1) * CELL, HUD_H + (r - 1) * CELL, CELL, CELL)


def cell_center(r: int, c: int) -> Tuple[int, int]:
    rect = cell_rect(r, c)
    return rect.centerx, rect.centery


def draw_walls(surface: pygame.Surface, grid: List[List[str]],
               tile: pygame.Surface) -> None:
    for r in range(1, GRID_ROWS + 1):
        row = grid[r]
        for c in range(1, GRID_COLS + 1):
            if row[c] == WALL:
                surface.blit(tile, ((c - 1) * CELL, HUD_H + (r - 1) * CELL))


def draw_food(surface: pygame.Surface, sprite: pygame.Surface,
              pos: Tuple[int, int], time_s: float) -> None:
    r, c = pos
    cx, cy = cell_center(r, c)
    bob = int(math.sin(time_s * 4.0) * 2)
    scale = 1.0 + 0.05 * math.sin(time_s * 6.0)
    sz = sprite.get_width()
    scaled = pygame.transform.smoothscale(
        sprite, (int(sz * scale), int(sz * scale)))
    rect = scaled.get_rect(center=(cx, cy + bob))
    surface.blit(scaled, rect)


def draw_heart(surface: pygame.Surface, cx: int, cy: int,
               size: int = 10, color=HEART_COL) -> None:
    r = size // 2
    pygame.draw.circle(surface, color, (cx - r // 2, cy - r // 3), r)
    pygame.draw.circle(surface, color, (cx + r // 2, cy - r // 3), r)
    pygame.draw.polygon(surface, color,
                        [(cx - r, cy), (cx + r, cy),
                         (cx, cy + r + r // 2)])
    pygame.draw.circle(surface, HEART_SHINE,
                       (cx - r // 2 - 1, cy - r // 2), max(1, r // 3))


def draw_hud(surface: pygame.Surface, font_med: pygame.font.Font,
             font_small: pygame.font.Font,
             num_players: int, level: int,
             sammy: Snake, jake: Snake,
             next_food_icon: pygame.Surface,
             next_value: int) -> None:
    pygame.draw.rect(surface, HUD_BG, pygame.Rect(0, 0, SCREEN_W, HUD_H))
    pygame.draw.line(surface, (90, 90, 110),
                     (0, HUD_H - 1), (SCREEN_W, HUD_H - 1), 1)

    # Level + next item
    lvl_txt = font_med.render(f"LEVEL {level}", True, HUD_FG)
    surface.blit(lvl_txt, lvl_txt.get_rect(midleft=(SCREEN_W // 2 - 50,
                                                    HUD_H // 2)))
    icon_x = SCREEN_W // 2 + 20
    surface.blit(next_food_icon,
                 next_food_icon.get_rect(center=(icon_x, HUD_H // 2)))
    tag = font_small.render(f"x{next_value}", True, HUD_FG)
    surface.blit(tag, tag.get_rect(midleft=(icon_x + 20, HUD_H // 2)))

    # Sammy (right side)
    _draw_player_block(surface, font_med, font_small, sammy,
                       SCREEN_W - 16, HUD_H // 2, align="right",
                       accent=SAMMY_LIGHT)
    # Jake (left side, 2-player only)
    if num_players == 2:
        _draw_player_block(surface, font_med, font_small, jake,
                           16, HUD_H // 2, align="left",
                           accent=JAKE_LIGHT)


def _draw_player_block(surface, font_med, font_small, snake: Snake,
                       x: int, y: int, align: str,
                       accent: Tuple[int, int, int]) -> None:
    name_txt = font_med.render(snake.name, True, accent)
    score_txt = font_med.render(f"{snake.score:>6}", True, HUD_FG)
    anchor = {"right": "midright", "left": "midleft"}[align]
    # Score and name
    if align == "right":
        s_rect = score_txt.get_rect(**{anchor: (x, y)})
        surface.blit(score_txt, s_rect)
        n_rect = name_txt.get_rect(midright=(s_rect.left - 10, y))
        surface.blit(name_txt, n_rect)
        # Hearts above
        for i in range(snake.lives):
            draw_heart(surface, s_rect.left - 10 - i * 14, y - 15, 8)
        if snake.combo >= 2:
            c_txt = font_small.render(f"x{min(snake.combo, 3)} COMBO!",
                                      True, (255, 220, 80))
            surface.blit(c_txt, c_txt.get_rect(midright=(s_rect.left - 10,
                                                        y + 14)))
    else:
        n_rect = name_txt.get_rect(**{anchor: (x, y)})
        surface.blit(name_txt, n_rect)
        s_rect = score_txt.get_rect(midleft=(n_rect.right + 10, y))
        surface.blit(score_txt, s_rect)
        for i in range(snake.lives):
            draw_heart(surface, s_rect.right + 10 + i * 14, y - 15, 8)
        if snake.combo >= 2:
            c_txt = font_small.render(f"x{min(snake.combo, 3)} COMBO!",
                                      True, (255, 220, 80))
            surface.blit(c_txt, c_txt.get_rect(midleft=(s_rect.right + 10,
                                                       y + 14)))


def draw_dialog(surface: pygame.Surface, font: pygame.font.Font,
                lines: List[str], y_bias: int = 0) -> None:
    width = max(font.size(l)[0] for l in lines) + 70
    height = font.get_linesize() * len(lines) + 40
    rect = pygame.Rect(0, 0, width, height)
    rect.center = (SCREEN_W // 2,
                   HUD_H + (SCREEN_H - HUD_H) // 2 + y_bias)
    shadow = pygame.Surface((rect.w, rect.h), pygame.SRCALPHA)
    pygame.draw.rect(shadow, (0, 0, 0, 160), shadow.get_rect(),
                     border_radius=12)
    surface.blit(shadow, (rect.x + 5, rect.y + 6))
    pygame.draw.rect(surface, DIALOG_BG, rect, border_radius=12)
    pygame.draw.rect(surface, DIALOG_BORDER, rect, 2, border_radius=12)
    y = rect.top + 20
    for i, line in enumerate(lines):
        colour = (DIALOG_BORDER if i == 0 else DIALOG_FG)
        surf = font.render(line, True, colour)
        surface.blit(surf, surf.get_rect(midtop=(rect.centerx, y)))
        y += font.get_linesize()


# --------------------------------------------------------------------------- #
# Generic UI primitives                                                       #
# --------------------------------------------------------------------------- #


def wait_space(screen: pygame.Surface, font: pygame.font.Font,
               draw_world: Callable[[], None],
               lines: List[str]) -> bool:
    clock = pygame.time.Clock()
    while True:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return False
            if ev.type == pygame.KEYDOWN:
                if ev.key in (pygame.K_SPACE, pygame.K_RETURN):
                    return True
                if ev.key == pygame.K_ESCAPE:
                    return False
        draw_world()
        draw_dialog(screen, font, lines)
        pygame.display.flip()
        clock.tick(FPS)


def text_input(screen: pygame.Surface, font: pygame.font.Font,
               prompt: str, default: str = "") -> str:
    text = default
    clock = pygame.time.Clock()
    while True:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return text
            if ev.type == pygame.KEYDOWN:
                if ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    return text or default
                if ev.key == pygame.K_BACKSPACE:
                    text = text[:-1]
                elif ev.key == pygame.K_ESCAPE:
                    return default
                elif len(text) < 12 and ev.unicode and ev.unicode.isprintable():
                    text += ev.unicode
        screen.fill(BG_DEEP)
        draw_dialog(screen, font, [prompt, "", text + "_",
                                   "", "(Enter to confirm)"])
        pygame.display.flip()
        clock.tick(FPS)


def fade_out(screen: pygame.Surface, duration_ms: int = 250) -> None:
    overlay = pygame.Surface((SCREEN_W, SCREEN_H))
    overlay.fill((0, 0, 0))
    clock = pygame.time.Clock()
    steps = max(1, duration_ms // 16)
    for i in range(steps + 1):
        alpha = int(255 * i / steps)
        overlay.set_alpha(alpha)
        screen.blit(overlay, (0, 0))
        pygame.display.flip()
        clock.tick(FPS)


# --------------------------------------------------------------------------- #
# Intro / menu                                                                #
# --------------------------------------------------------------------------- #


def intro_screen(screen: pygame.Surface, fonts: dict,
                 bg: pygame.Surface, food_sprites: List[pygame.Surface]
                 ) -> Optional[Tuple[int, int, bool]]:
    options = [
        ("Players", [1, 2], 0),
        ("Skill", list(range(10, 101, 10)), 6),
        ("Accelerate each level", ["No", "Yes"], 1),
    ]
    selected = 0
    clock = pygame.time.Clock()
    big = fonts["big"]
    med = fonts["med"]
    small = fonts["small"]
    t = 0.0
    play_menu_chord()

    while True:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return None
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    return None
                if ev.key in (pygame.K_DOWN, pygame.K_s):
                    selected = (selected + 1) % len(options)
                    play_blip()
                elif ev.key in (pygame.K_UP, pygame.K_w):
                    selected = (selected - 1) % len(options)
                    play_blip()
                elif ev.key in (pygame.K_LEFT, pygame.K_a):
                    lbl, choices, idx = options[selected]
                    options[selected] = (lbl, choices, (idx - 1) % len(choices))
                    play_blip()
                elif ev.key in (pygame.K_RIGHT, pygame.K_d):
                    lbl, choices, idx = options[selected]
                    options[selected] = (lbl, choices, (idx + 1) % len(choices))
                    play_blip()
                elif ev.key in (pygame.K_RETURN, pygame.K_SPACE):
                    return (options[0][1][options[0][2]],
                            options[1][1][options[1][2]],
                            options[2][2] == 1)

        screen.blit(bg, (0, HUD_H))
        pygame.draw.rect(screen, HUD_BG, pygame.Rect(0, 0, SCREEN_W, HUD_H))

        t += 1 / FPS
        # Decorative floating foods
        for i, sprite in enumerate(food_sprites):
            fx = 60 + i * (SCREEN_W - 120) / (len(food_sprites) - 1)
            fy = SCREEN_H - 110 + math.sin(t * 2 + i) * 10
            scaled = pygame.transform.rotozoom(sprite, math.sin(t + i) * 10, 1.0)
            screen.blit(scaled, scaled.get_rect(center=(fx, fy)))

        title = big.render("N I B B L E S", True,
                           (210 + int(40 * math.sin(t * 2)),
                            210 + int(40 * math.sin(t * 2 + 1)),
                            160))
        screen.blit(title, title.get_rect(center=(SCREEN_W // 2, 120)))

        sub = med.render("Enhanced Edition - based on QBasic NIBBLES.BAS (1990)",
                         True, GREY)
        screen.blit(sub, sub.get_rect(center=(SCREEN_W // 2, 170)))

        base_y = 260
        for i, (label, choices, idx) in enumerate(options):
            y = base_y + i * 54
            is_sel = i == selected
            colour = WHITE if is_sel else GREY
            caret = ">" if is_sel else " "
            txt = med.render(f"{caret}  {label}:  < {choices[idx]} >",
                             True, colour)
            screen.blit(txt, txt.get_rect(center=(SCREEN_W // 2, y)))

        hint_lines = [
            "Player 1: Arrow keys     Player 2: W A S D",
            "Pause: P or Space        Quit: Esc",
            "Up/Down to pick row, Left/Right to change, Enter to start",
        ]
        for j, line in enumerate(hint_lines):
            surf = small.render(line, True, DIM)
            screen.blit(surf, surf.get_rect(center=(SCREEN_W // 2,
                                                   460 + j * 22)))

        scores = load_hiscores()[:5]
        if scores:
            t_head = small.render("- High scores -", True, NUM_COL)
            screen.blit(t_head, t_head.get_rect(center=(SCREEN_W // 2, 560)))
            for k, e in enumerate(scores):
                line = (f"{k+1}. {e['name']:<12} {e['score']:>6}"
                        f"  L{e['level']:<2}  {e['players']}P")
                ts = small.render(line, True, WHITE)
                screen.blit(ts, ts.get_rect(center=(SCREEN_W // 2,
                                                   582 + k * 20)))

        pygame.display.flip()
        clock.tick(FPS)


# --------------------------------------------------------------------------- #
# Game loop                                                                   #
# --------------------------------------------------------------------------- #


def random_number_pos(grid: List[List[str]],
                      snakes: List[Snake]) -> Optional[Tuple[int, int]]:
    occ = {pos for s in snakes for pos in s.body}
    for _ in range(2000):
        r = random.randint(4, 49)
        c = random.randint(2, 79)
        if grid[r][c] == EMPTY and (r, c) not in occ:
            return (r, c)
    return None


def countdown(screen: pygame.Surface, font: pygame.font.Font,
              draw_world: Callable[[], None]) -> None:
    big = pygame.font.SysFont("consolas,monospace", 120, bold=True)
    clock = pygame.time.Clock()
    for n in ("3", "2", "1", "GO!"):
        t = 0.0
        while t < 0.55:
            dt = clock.tick(FPS) / 1000.0
            t += dt
            draw_world()
            scale = 1.0 + (1.0 - t / 0.55) * 1.5
            txt = big.render(n, True, (255, 240, 120))
            txt = pygame.transform.rotozoom(txt, 0, 1 / scale)
            screen.blit(txt, txt.get_rect(center=(SCREEN_W // 2,
                                                 HUD_H + (SCREEN_H - HUD_H) // 2)))
            pygame.display.flip()
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                    return


def game_loop(screen: pygame.Surface, fonts: dict,
              num_players: int, skill: int, accelerate: bool,
              assets: dict) -> None:
    clock = pygame.time.Clock()
    med = fonts["med"]
    small = fonts["small"]

    base_period = max(20, 220 - skill * 2)
    level = 1

    sammy = Snake("SAMMY", SAMMY_DARK, SAMMY_LIGHT, SAMMY_HEAD)
    jake = Snake("JAKE", JAKE_DARK, JAKE_LIGHT, JAKE_HEAD)
    snakes = [sammy] if num_players == 1 else [sammy, jake]

    grid: List[List[str]] = [[EMPTY] * (GRID_COLS + 2)
                             for _ in range(GRID_ROWS + 2)]
    number_pos: Optional[Tuple[int, int]] = None
    number_value = 1
    particles = ParticleSystem()
    shake_t = 0.0
    flash_t = 0.0

    bg = assets["bg"]
    wall_tile = assets["wall_tile"]
    food_sprites: List[pygame.Surface] = assets["food"]
    food_icons = assets["food_icons"]

    period = max(20, base_period - (level - 1) * 8) if accelerate else base_period

    def reset_level() -> None:
        nonlocal grid, number_pos, number_value
        grid = [[EMPTY] * (GRID_COLS + 2) for _ in range(GRID_ROWS + 2)]
        build_walls(grid, level)
        sp1, sp2 = spawn_for(level)
        sammy.reset(*sp1)
        jake.reset(*sp2)
        number_pos = None
        number_value = 1

    def draw_world(now_s: float = 0.0) -> None:
        # Shake offset
        ox = oy = 0
        if shake_t > 0:
            amp = int(min(8, 60 * shake_t))
            ox = random.randint(-amp, amp)
            oy = random.randint(-amp, amp)
        screen.fill((0, 0, 0))
        screen.blit(bg, (ox, HUD_H + oy))
        draw_walls_shifted(screen, grid, wall_tile, ox, oy)
        if number_pos is not None:
            draw_food_shifted(screen, food_sprites[number_value - 1],
                              number_pos, now_s, ox, oy)
        for s in snakes:
            draw_snake_shifted(screen, s, now_s, ox, oy)
        particles.draw(screen)
        next_icon = food_icons[number_value - 1]
        draw_hud(screen, med, small, num_players, level,
                 sammy, jake, next_icon, number_value)
        if flash_t > 0:
            overlay = pygame.Surface((SCREEN_W, SCREEN_H), pygame.SRCALPHA)
            overlay.fill((255, 255, 255, int(255 * min(1.0, flash_t))))
            screen.blit(overlay, (0, 0))

    def draw_walls_shifted(surf, g, tile, ox, oy):
        for r in range(1, GRID_ROWS + 1):
            row = g[r]
            for c in range(1, GRID_COLS + 1):
                if row[c] == WALL:
                    surf.blit(tile, ((c - 1) * CELL + ox,
                                     HUD_H + (r - 1) * CELL + oy))

    def draw_food_shifted(surf, sprite, pos, now_s, ox, oy):
        r, c = pos
        rect = cell_rect(r, c).move(ox, oy)
        cx, cy = rect.centerx, rect.centery
        bob = int(math.sin(now_s * 4.0) * 2)
        scale = 1.0 + 0.05 * math.sin(now_s * 6.0)
        sz = sprite.get_width()
        scaled = pygame.transform.smoothscale(
            sprite, (int(sz * scale), int(sz * scale)))
        surf.blit(scaled, scaled.get_rect(center=(cx, cy + bob)))

    def draw_snake_shifted(surf, snake, now_s, ox, oy):
        if not snake.body:
            return
        n = len(snake.body)
        for i, (r, c) in enumerate(snake.body):
            t = i / max(1, n - 1)
            col = _lerp(snake.dark, snake.light, 0.3 + 0.7 * t)
            rect = cell_rect(r, c).move(ox, oy).inflate(-1, -1)
            pygame.draw.rect(surf, col, rect, border_radius=4)
        hr, hc = snake.head
        head_rect = cell_rect(hr, hc).move(ox, oy)
        pygame.draw.rect(surf, snake.head_col,
                         head_rect.inflate(1, 1), border_radius=5)
        pygame.draw.rect(surf, (20, 20, 30),
                         head_rect.inflate(1, 1), 1, border_radius=5)
        dr, dc = DIRS[snake.direction]
        cx, cy = head_rect.centerx, head_rect.centery
        px, py = -dc, dr
        eye_off = max(2, CELL // 4)
        eye_r = max(2, CELL // 5)
        for sign in (-1, 1):
            ex = cx + px * sign * eye_off
            ey = cy + py * sign * eye_off
            pygame.draw.circle(surf, (240, 240, 250), (ex, ey), eye_r)
            ps = max(1, eye_r // 2)
            pygame.draw.circle(surf, (20, 20, 40),
                               (ex + dc * ps, ey + dr * ps),
                               max(1, eye_r - 2))
        if math.sin(now_s * 10) > 0.9 and len(snake.body) > 1:
            tip = (cx + dc * CELL, cy + dr * CELL)
            pygame.draw.line(surf, (240, 60, 70),
                             (cx + dc * (CELL // 2), cy + dr * (CELL // 2)),
                             tip, 2)

    reset_level()
    now_s = 0.0

    if not wait_space(screen, med, lambda: draw_world(now_s),
                      [f"Level {level}",
                       "Press SPACE to begin",
                       "(Esc to quit)"]):
        return
    countdown(screen, med, lambda: draw_world(now_s))

    move_accum = 0.0
    paused = False
    running = True
    while running:
        dt = clock.tick(FPS)
        dt_s = dt / 1000.0
        now_s += dt_s
        if shake_t > 0:
            shake_t = max(0.0, shake_t - dt_s)
        if flash_t > 0:
            flash_t = max(0.0, flash_t - dt_s * 2)

        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    return
                if ev.key in (pygame.K_p, pygame.K_SPACE):
                    paused = True
                elif ev.key == pygame.K_UP and sammy.direction != 2:
                    sammy.pending = 1
                elif ev.key == pygame.K_DOWN and sammy.direction != 1:
                    sammy.pending = 2
                elif ev.key == pygame.K_LEFT and sammy.direction != 4:
                    sammy.pending = 3
                elif ev.key == pygame.K_RIGHT and sammy.direction != 3:
                    sammy.pending = 4
                elif num_players == 2:
                    if ev.key == pygame.K_w and jake.direction != 2:
                        jake.pending = 1
                    elif ev.key == pygame.K_s and jake.direction != 1:
                        jake.pending = 2
                    elif ev.key == pygame.K_a and jake.direction != 4:
                        jake.pending = 3
                    elif ev.key == pygame.K_d and jake.direction != 3:
                        jake.pending = 4

        if paused:
            if not wait_space(screen, med, lambda: draw_world(now_s),
                              ["P A U S E D", "Press SPACE to resume",
                               "(Esc to quit)"]):
                return
            paused = False
            continue

        if number_pos is None:
            number_pos = random_number_pos(grid, snakes)

        # Combo countdown
        for s in snakes:
            if s.combo > 0:
                s.combo_timer -= dt_s
                if s.combo_timer <= 0:
                    s.combo = 0

        particles.update(dt_s)

        move_accum += dt
        while move_accum >= period:
            move_accum -= period

            # Commit direction choices
            for s in snakes:
                s.direction = s.pending

            # Compute new heads
            new_heads: List[Tuple[Snake, Tuple[int, int]]] = []
            for s in snakes:
                dr, dc = DIRS[s.direction]
                hr, hc = s.head
                new_heads.append((s, (hr + dr, hc + dc)))

            # Resolve eating (snakes can pass through where food was)
            ate = []
            for s, (nr, nc) in new_heads:
                if number_pos is not None and (nr, nc) == number_pos:
                    bonus = 1 + 0.1 * min(s.combo, 3)
                    gained = int(round(number_value * bonus))
                    s.score += gained
                    if s.target_length < MAX_LEN - 30:
                        s.target_length += number_value * 4
                    s.combo = min(3, s.combo + 1)
                    s.combo_timer = 2.0
                    cx, cy = cell_center(nr, nc)
                    particles.spark(cx, cy, (255, 220, 90))
                    play_eat(number_value)
                    ate.append(s)

            if ate:
                if number_value >= 9:
                    # Big burst
                    for s in ate:
                        hr, hc = s.head
                        cx, cy = cell_center(hr, hc)
                        particles.burst(cx, cy, (255, 230, 100))
                    flash_t = 0.6
                    level += 1
                    play_levelup()
                    reset_level()
                    if accelerate:
                        period = max(20, base_period - (level - 1) * 8)
                    draw_world(now_s)
                    pygame.display.flip()
                    if not wait_space(screen, med, lambda: draw_world(now_s),
                                      [f"LEVEL {level}",
                                       "Press SPACE to continue"]):
                        return
                    countdown(screen, med, lambda: draw_world(now_s))
                    move_accum = 0.0
                    continue
                number_value += 1
                number_pos = None

            # Collisions
            died: List[Snake] = []
            head_counts: dict = {}
            for s, pos in new_heads:
                head_counts.setdefault(pos, []).append(s)

            for s, (nr, nc) in new_heads:
                if not s.alive:
                    continue
                if not (1 <= nr <= GRID_ROWS and 1 <= nc <= GRID_COLS):
                    died.append(s)
                    continue
                if grid[nr][nc] == WALL:
                    died.append(s)
                    continue
                collide = False
                for other in snakes:
                    growing = other.target_length > len(other.body)
                    occupied = other.body if growing else other.body[1:]
                    if (nr, nc) in occupied:
                        collide = True
                        break
                if collide:
                    died.append(s)
                    continue
                if len(head_counts[(nr, nc)]) > 1:
                    died.append(s)

            if died:
                for s in died:
                    s.alive = False
                    s.lives = max(0, s.lives - 1)
                    s.score = max(0, s.score - 10)
                    s.combo = 0
                    hr, hc = s.head
                    cx, cy = cell_center(hr, hc)
                    particles.burst(cx, cy, s.light)
                    particles.burst(cx, cy, (255, 240, 100))
                shake_t = 0.5
                play_die()

                # Let the particles play for a short moment
                t0 = 0.0
                while t0 < 0.6:
                    dt2 = clock.tick(FPS) / 1000.0
                    t0 += dt2
                    now_s += dt2
                    particles.update(dt2)
                    if shake_t > 0:
                        shake_t = max(0.0, shake_t - dt2)
                    draw_world(now_s)
                    pygame.display.flip()

                msg = " / ".join(s.name for s in died) + "  CRASHED"
                if not wait_space(screen, med, lambda: draw_world(now_s),
                                  [msg, "Press SPACE to continue"]):
                    return

                if any(s.lives == 0 for s in snakes):
                    _handle_game_over(screen, fonts, num_players,
                                      level, sammy, jake)
                    return

                sp1, sp2 = spawn_for(level)
                sammy.reset(*sp1)
                jake.reset(*sp2)
                number_pos = None
                number_value = 1
                move_accum = 0.0
                break

            # Move heads: append + trim
            for s, pos in new_heads:
                s.body.append(pos)
                # Trail
                cx, cy = cell_center(*pos)
                particles.trail(cx + random.uniform(-2, 2),
                                cy + random.uniform(-2, 2),
                                _lerp(s.dark, s.light, 0.8))
                while len(s.body) > s.target_length:
                    s.body.pop(0)

        draw_world(now_s)
        pygame.display.flip()


def _handle_game_over(screen: pygame.Surface, fonts: dict,
                      num_players: int, level: int,
                      sammy: Snake, jake: Snake) -> None:
    med = fonts["med"]

    if num_players == 1:
        winner_name = "SAMMY"
        score = sammy.score
    else:
        winner_name = "SAMMY" if sammy.score >= jake.score else "JAKE"
        score = max(sammy.score, jake.score)

    lines = ["G A M E   O V E R", ""]
    if num_players == 1:
        lines.append(f"Final score: {sammy.score}")
        lines.append(f"Level reached: {level}")
    else:
        lines.append(f"Sammy: {sammy.score}    Jake: {jake.score}")
        lines.append(f"Winner: {winner_name}")
    lines.append("")
    lines.append("Press SPACE to record your score, Esc to skip")

    overlay = pygame.Surface((SCREEN_W, SCREEN_H), pygame.SRCALPHA)
    overlay.fill((0, 0, 0, 160))
    screen.blit(overlay, (0, 0))
    draw_dialog(screen, med, lines)
    pygame.display.flip()

    waiting = True
    skip = False
    clock = pygame.time.Clock()
    while waiting:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return
            if ev.type == pygame.KEYDOWN:
                if ev.key in (pygame.K_SPACE, pygame.K_RETURN):
                    waiting = False
                elif ev.key == pygame.K_ESCAPE:
                    waiting = False
                    skip = True
        clock.tick(FPS)

    if not skip and score > 0:
        name = text_input(screen, med,
                          f"{winner_name} - enter your name:",
                          default=winner_name)
        save_hiscore(name, score, level, num_players)


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #


def main() -> int:
    pygame.init()
    _init_audio()
    pygame.display.set_caption("Nibbles - Enhanced Edition")
    screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))

    fonts = {
        "big": pygame.font.SysFont("consolas,menlo,monospace", 56, bold=True),
        "med": pygame.font.SysFont("consolas,menlo,monospace", 22, bold=True),
        "small": pygame.font.SysFont("consolas,menlo,monospace", 18),
    }

    bg = build_background()
    wall_tile = build_wall_tile()
    food_sprites = build_food_sprites()
    food_icons = [build_food_icon(sp, 28) for sp in food_sprites]
    assets = {
        "bg": bg,
        "wall_tile": wall_tile,
        "food": food_sprites,
        "food_icons": food_icons,
    }

    try:
        while True:
            choice = intro_screen(screen, fonts, bg, food_icons)
            if choice is None:
                return 0
            num_players, skill, accelerate = choice
            game_loop(screen, fonts, num_players, skill, accelerate, assets)
    finally:
        pygame.quit()


if __name__ == "__main__":
    sys.exit(main())
