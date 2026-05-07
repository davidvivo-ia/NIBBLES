#!/usr/bin/env python3
"""Nibbles - a Python/pygame port of QBasic NIBBLES.BAS (Microsoft, 1990).

Original logic, level layouts and scoring are preserved.  Additions over the
original BASIC version:

    * Frame-rate independent movement (no busy-wait loop).
    * Smooth pygame rendering with a pseudo-CGA palette.
    * Procedurally generated sound effects (eat / die / level-up / menu).
    * Persistent high-score table stored in ~/.nibbles_scores.json.
    * Polished menu and game-over screens with keyboard navigation.
    * Pause overlay, countdown before each life, and per-level speed-up.

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
from typing import List, Optional, Tuple

# pygame is noisy on import; silence the welcome banner.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

GRID_COLS = 80                         # 1-indexed: cols 1..80
GRID_ROWS = 50                         # 1-indexed: rows 1..50
CELL = 12                              # pixel size of a grid cell
HUD_H = 40                             # top status bar in pixels
SCREEN_W = GRID_COLS * CELL
SCREEN_H = GRID_ROWS * CELL + HUD_H
FPS = 60
MAX_LEN = 1000

HISCORE_FILE = Path.home() / ".nibbles_scores.json"
HISCORE_LIMIT = 10

# Direction encoding matches the BASIC original (1=up, 2=down, 3=left, 4=right)
DIRS = {1: (-1, 0), 2: (1, 0), 3: (0, -1), 4: (0, 1)}
OPP = {1: 2, 2: 1, 3: 4, 4: 3}

# Colours (R, G, B) -- a saturated, slightly modernised CGA palette.
BG_COL = (12, 14, 64)
GRID_COL = (18, 22, 80)
WALL_COL = (220, 70, 70)
WALL_HI = (255, 140, 140)
NUM_COL = (255, 240, 120)
HUD_BG = (0, 0, 0)
HUD_FG = (240, 240, 240)
DIALOG_BG = (140, 0, 0)
DIALOG_FG = (255, 255, 255)
SAMMY_COL = (255, 215, 60)
SAMMY_HEAD = (255, 255, 170)
JAKE_COL = (240, 90, 200)
JAKE_HEAD = (255, 180, 240)
WHITE = (250, 250, 250)
GREY = (150, 150, 150)
DIM = (90, 90, 110)

# Cell-content tags used in the playfield grid.
EMPTY, WALL, NUMBER = "empty", "wall", "number"

# --------------------------------------------------------------------------- #
# Sound: tiny PCM tone generator so we ship no audio assets                   #
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


def _make_tone(freq: float, duration_ms: int, volume: float = 0.35,
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
    release = max(1, int(SAMPLE_RATE * 0.04))
    for i in range(n):
        t = i / SAMPLE_RATE
        if wave == "square":
            v = amp if math.sin(2 * math.pi * freq * t) >= 0 else -amp
        else:
            v = int(amp * math.sin(2 * math.pi * freq * t))
        # Simple ADSR-ish envelope to avoid clicks.
        if i < attack:
            v = int(v * (i / attack))
        elif i > n - release:
            v = int(v * max(0.0, (n - i) / release))
        buf.append(v)
    snd = pygame.mixer.Sound(buffer=buf.tobytes())
    snd.set_volume(volume)
    _sound_cache[key] = snd
    return snd


def play_eat() -> None:
    s = _make_tone(880, 60)
    if s:
        s.play()


def play_die() -> None:
    if not _mixer_ok:
        return
    for f, d in ((440, 80), (330, 80), (220, 120), (165, 200)):
        snd = _make_tone(f, d, 0.4)
        if snd:
            snd.play()
            pygame.time.delay(d)


def play_levelup() -> None:
    if not _mixer_ok:
        return
    for f in (523, 659, 784, 1046):
        snd = _make_tone(f, 90, 0.4)
        if snd:
            snd.play()
            pygame.time.delay(90)


def play_blip() -> None:
    s = _make_tone(660, 40, 0.25)
    if s:
        s.play()


# --------------------------------------------------------------------------- #
# Game data                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class Snake:
    name: str
    body_color: Tuple[int, int, int]
    head_color: Tuple[int, int, int]
    body: List[Tuple[int, int]] = field(default_factory=list)
    direction: int = 4
    pending: int = 4
    target_length: int = 2
    score: int = 0
    lives: int = 5
    alive: bool = True

    @property
    def head(self) -> Tuple[int, int]:
        return self.body[-1]

    def reset(self, row: int, col: int, direction: int) -> None:
        self.body = [(row, col)]
        self.direction = direction
        self.pending = direction
        self.target_length = 2
        self.alive = True


# --------------------------------------------------------------------------- #
# Maze definitions - one for each original CASE in the BAS Level subroutine   #
# --------------------------------------------------------------------------- #


def build_walls(grid: List[List[str]], level: int) -> None:
    """Lay out walls for the given level number (1-based)."""
    # Outer arena border (rows 3 & 50, cols 1 & 80) is shared by every level.
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
    # Level 10 and beyond: the BASIC source's CASE ELSE - alternating zigzag.
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
    entries.append({"name": name[:12] or "PLAYER",
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
# Drawing helpers                                                             #
# --------------------------------------------------------------------------- #


def cell_rect(r: int, c: int) -> pygame.Rect:
    return pygame.Rect((c - 1) * CELL, HUD_H + (r - 1) * CELL, CELL, CELL)


def draw_grid_background(surface: pygame.Surface) -> None:
    surface.fill(BG_COL, pygame.Rect(0, HUD_H, SCREEN_W, SCREEN_H - HUD_H))
    # Faint grid lines for that pixel-art feel.
    for x in range(0, SCREEN_W, CELL):
        pygame.draw.line(surface, GRID_COL, (x, HUD_H), (x, SCREEN_H), 1)
    for y in range(HUD_H, SCREEN_H, CELL):
        pygame.draw.line(surface, GRID_COL, (0, y), (SCREEN_W, y), 1)


def draw_walls(surface: pygame.Surface, grid: List[List[str]]) -> None:
    for r in range(1, GRID_ROWS + 1):
        for c in range(1, GRID_COLS + 1):
            if grid[r][c] == WALL:
                rect = cell_rect(r, c)
                pygame.draw.rect(surface, WALL_COL, rect)
                pygame.draw.rect(surface, WALL_HI, rect, 1)


def draw_snake(surface: pygame.Surface, snake: Snake) -> None:
    if not snake.body:
        return
    n = len(snake.body)
    for i, (r, c) in enumerate(snake.body):
        rect = cell_rect(r, c).inflate(-1, -1)
        is_head = (i == n - 1)
        col = snake.head_color if is_head else snake.body_color
        pygame.draw.rect(surface, col, rect, border_radius=3)
    # Eye dot on the head for personality.
    hr, hc = snake.head
    cx = (hc - 1) * CELL + CELL // 2
    cy = HUD_H + (hr - 1) * CELL + CELL // 2
    pygame.draw.circle(surface, (20, 20, 20), (cx, cy), max(1, CELL // 5))


def draw_number(surface: pygame.Surface, font: pygame.font.Font,
                pos: Optional[Tuple[int, int]], value: int) -> None:
    if pos is None:
        return
    r, c = pos
    rect = cell_rect(r, c)
    pygame.draw.rect(surface, BG_COL, rect)
    glyph = font.render(str(value), True, NUM_COL)
    grect = glyph.get_rect(center=rect.center)
    surface.blit(glyph, grect)


def draw_hud(surface: pygame.Surface, font: pygame.font.Font,
             num_players: int, level: int, sammy: Snake, jake: Snake) -> None:
    pygame.draw.rect(surface, HUD_BG, pygame.Rect(0, 0, SCREEN_W, HUD_H))
    pygame.draw.line(surface, GREY, (0, HUD_H - 1), (SCREEN_W, HUD_H - 1), 1)

    lvl = font.render(f"LEVEL {level}", True, HUD_FG)
    surface.blit(lvl, lvl.get_rect(center=(SCREEN_W // 2, HUD_H // 2)))

    s_txt = font.render(
        f"SAMMY  Lives {sammy.lives}   {sammy.score:>6}", True, SAMMY_COL)
    surface.blit(s_txt, s_txt.get_rect(midright=(SCREEN_W - 12, HUD_H // 2)))

    if num_players == 2:
        j_txt = font.render(
            f"{jake.score:<6}   Lives {jake.lives}  JAKE", True, JAKE_COL)

        surface.blit(j_txt, j_txt.get_rect(midleft=(12, HUD_H // 2)))


def draw_dialog(surface: pygame.Surface, font: pygame.font.Font,
                lines: List[str]) -> None:
    width = max(font.size(l)[0] for l in lines) + 60
    height = font.get_linesize() * len(lines) + 36
    rect = pygame.Rect(0, 0, width, height)
    rect.center = (SCREEN_W // 2, HUD_H + (SCREEN_H - HUD_H) // 2)
    pygame.draw.rect(surface, DIALOG_BG, rect, border_radius=8)
    pygame.draw.rect(surface, DIALOG_FG, rect, 2, border_radius=8)
    y = rect.top + 18
    for line in lines:
        surf = font.render(line, True, DIALOG_FG)
        surface.blit(surf, surf.get_rect(midtop=(rect.centerx, y)))
        y += font.get_linesize()


# --------------------------------------------------------------------------- #
# Generic UI primitives                                                       #
# --------------------------------------------------------------------------- #


def wait_space(screen: pygame.Surface, font: pygame.font.Font,
               draw_world, lines: List[str]) -> bool:
    """Block until the player hits Space (returns True) or Esc (returns False)."""
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
        pygame.time.Clock().tick(FPS)


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
        screen.fill(BG_COL)
        draw_dialog(screen, font, [prompt, "", text + "_",
                                   "", "(Enter to confirm)"])
        pygame.display.flip()
        clock.tick(FPS)


# --------------------------------------------------------------------------- #
# Menu screens                                                                #
# --------------------------------------------------------------------------- #


def intro_screen(screen: pygame.Surface,
                 fonts: dict) -> Optional[Tuple[int, int, bool]]:
    """Return (num_players, skill, accelerate) or None to quit."""
    options = [
        ("Players", [1, 2], 0),
        ("Skill (1-100)", list(range(10, 101, 10)), 4),
        ("Speed up each level", ["No", "Yes"], 0),
    ]
    selected = 0
    clock = pygame.time.Clock()
    big = fonts["big"]
    med = fonts["med"]
    small = fonts["small"]
    title_phase = 0.0

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
                    label, choices, idx = options[selected]
                    options[selected] = (label, choices, (idx - 1) % len(choices))
                    play_blip()
                elif ev.key in (pygame.K_RIGHT, pygame.K_d):
                    label, choices, idx = options[selected]
                    options[selected] = (label, choices, (idx + 1) % len(choices))
                    play_blip()
                elif ev.key in (pygame.K_RETURN, pygame.K_SPACE):
                    n = options[0][1][options[0][2]]
                    sk = options[1][1][options[1][2]]
                    acc = options[2][2] == 1
                    return n, sk, acc

        screen.fill(BG_COL)
        title_phase += 0.05
        title = big.render("N I B B L E S", True,
                           (200 + int(50 * math.sin(title_phase)),
                            200 + int(50 * math.sin(title_phase + 1)),
                            120))
        screen.blit(title, title.get_rect(center=(SCREEN_W // 2, 90)))
        sub = med.render("Python edition - based on QBasic NIBBLES.BAS (1990)",
                         True, GREY)
        screen.blit(sub, sub.get_rect(center=(SCREEN_W // 2, 140)))

        # Options
        base_y = 230
        for i, (label, choices, idx) in enumerate(options):
            y = base_y + i * 50
            colour = WHITE if i == selected else GREY
            arrow = ">" if i == selected else " "
            txt = med.render(f"{arrow} {label}: < {choices[idx]} >",
                             True, colour)
            screen.blit(txt, txt.get_rect(center=(SCREEN_W // 2, y)))

        # Controls help
        help_lines = [
            "Player 1: Arrow keys     Player 2: W A S D",
            "Pause: P or Space        Quit: Esc",
            "",
            "Up/Down to choose row, Left/Right to change, Enter to start",
        ]
        for j, line in enumerate(help_lines):
            t = small.render(line, True, DIM)
            screen.blit(t, t.get_rect(center=(SCREEN_W // 2, 430 + j * 22)))

        # Hi-scores
        scores = load_hiscores()[:5]
        if scores:
            t = small.render("- High scores -", True, NUM_COL)
            screen.blit(t, t.get_rect(center=(SCREEN_W // 2, 540)))
            for k, e in enumerate(scores):
                line = f"{k+1}. {e['name']:<12} {e['score']:>6}  L{e['level']:<2}  {e['players']}P"
                ts = small.render(line, True, WHITE)
                screen.blit(ts, ts.get_rect(center=(SCREEN_W // 2, 560 + k * 18)))

        pygame.display.flip()
        clock.tick(FPS)


# --------------------------------------------------------------------------- #
# Core gameplay                                                               #
# --------------------------------------------------------------------------- #


def random_number_pos(grid: List[List[str]], snakes: List[Snake]
                      ) -> Optional[Tuple[int, int]]:
    snake_cells = {pos for s in snakes for pos in s.body}
    for _ in range(2000):
        r = random.randint(4, 49)
        c = random.randint(2, 79)
        if grid[r][c] == EMPTY and (r, c) not in snake_cells:
            return (r, c)
    return None


def game_loop(screen: pygame.Surface, fonts: dict,
              num_players: int, skill: int, accelerate: bool) -> None:
    clock = pygame.time.Clock()
    big = fonts["big"]
    med = fonts["med"]
    small = fonts["small"]

    # Move period in milliseconds.  skill 1 -> ~218 ms, 100 -> ~20 ms.
    base_period = max(20, 220 - skill * 2)
    level = 1

    sammy = Snake("SAMMY", SAMMY_COL, SAMMY_HEAD)
    jake = Snake("JAKE", JAKE_COL, JAKE_HEAD)
    snakes = [sammy] if num_players == 1 else [sammy, jake]

    grid: List[List[str]] = [[EMPTY] * (GRID_COLS + 2)
                             for _ in range(GRID_ROWS + 2)]
    number_pos: Optional[Tuple[int, int]] = None
    number_value = 1

    def reset_level(new_level: bool) -> None:
        nonlocal grid, number_pos, number_value
        grid = [[EMPTY] * (GRID_COLS + 2) for _ in range(GRID_ROWS + 2)]
        build_walls(grid, level)
        sp1, sp2 = spawn_for(level)
        sammy.reset(*sp1)
        jake.reset(*sp2)
        number_pos = None
        number_value = 1

    def draw_world() -> None:
        draw_grid_background(screen)
        draw_walls(screen, grid)
        draw_number(screen, big, number_pos, number_value)
        for s in snakes:
            draw_snake(screen, s)
        draw_hud(screen, med, num_players, level, sammy, jake)

    reset_level(new_level=True)

    move_accumulator = 0.0
    period = base_period - (level - 1) * 8 if accelerate else base_period
    period = max(20, period)

    if not wait_space(screen, med, draw_world,
                      [f"Level {level}", "Press SPACE to begin",
                       "(Esc to abandon)"]):
        return

    paused = False
    running = True
    while running:
        dt = clock.tick(FPS)
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    return
                if ev.key in (pygame.K_p, pygame.K_SPACE):
                    paused = True
                # Player 1 (arrow keys) - this is "Sammy" in the original.
                elif ev.key == pygame.K_UP and sammy.direction != 2:
                    sammy.pending = 1
                elif ev.key == pygame.K_DOWN and sammy.direction != 1:
                    sammy.pending = 2
                elif ev.key == pygame.K_LEFT and sammy.direction != 4:
                    sammy.pending = 3
                elif ev.key == pygame.K_RIGHT and sammy.direction != 3:
                    sammy.pending = 4
                # Player 2 (WASD) -> Jake.
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
            if not wait_space(screen, med, draw_world,
                              ["P A U S E D", "Press SPACE to resume",
                               "(Esc to quit)"]):
                return
            paused = False
            continue

        # Lay down a number if the field has none.
        if number_pos is None:
            number_pos = random_number_pos(grid, snakes)
            number_value = 1 if number_value > 9 else number_value
            if number_pos is None:
                # Field is full of stuff; treat as level cleared.
                number_value = 10

        # Fixed-timestep snake movement.
        move_accumulator += dt
        moved = False
        while move_accumulator >= period:
            move_accumulator -= period
            moved = True

            for s in snakes:
                s.direction = s.pending

            new_heads: List[Tuple[Snake, Tuple[int, int]]] = []
            for s in snakes:
                dr, dc = DIRS[s.direction]
                hr, hc = s.head
                new_heads.append((s, (hr + dr, hc + dc)))

            # Eat numbers first so the snake can pass through the spot.
            ate_count = 0
            for s, (nr, nc) in new_heads:
                if number_pos is not None and (nr, nc) == number_pos:
                    s.score += number_value
                    if s.target_length < MAX_LEN - 30:
                        s.target_length += number_value * 4
                    play_eat()
                    ate_count += 1

            if ate_count:
                if number_value >= 9:
                    # Level cleared.
                    level += 1
                    play_levelup()
                    reset_level(new_level=True)
                    if accelerate:
                        period = max(20, base_period - (level - 1) * 8)
                    if not wait_space(screen, med, draw_world,
                                      [f"Level {level}",
                                       "Press SPACE to continue"]):
                        return
                    move_accumulator = 0.0
                    continue
                else:
                    number_value += 1
                    number_pos = None

            # Now resolve collisions for the new heads.
            died = []
            heads_dict = {}
            for s, pos in new_heads:
                heads_dict.setdefault(pos, []).append(s)

            for s, (nr, nc) in new_heads:
                if not s.alive:
                    continue
                # Out-of-grid check.  Walls already cover the perimeter, but be
                # defensive in case of off-by-one weirdness.
                if not (1 <= nr <= GRID_ROWS and 1 <= nc <= GRID_COLS):
                    died.append(s)
                    continue
                if grid[nr][nc] == WALL:
                    died.append(s)
                    continue
                # Collision with any snake's existing body (excluding tail-cell
                # that's about to be vacated).
                hit = False
                for other in snakes:
                    body = other.body
                    # Tail will move out of the way unless that snake is
                    # growing this tick (target_length > current length).
                    growing = other.target_length > len(other.body)
                    occupied = body if growing else body[1:]
                    if (nr, nc) in occupied:
                        hit = True
                        break
                if hit:
                    died.append(s)
                    continue
                # Two heads slamming into the same square.
                if len(heads_dict[(nr, nc)]) > 1:
                    died.append(s)
                    continue

            # Apply movement / death.
            if died:
                for s in died:
                    s.alive = False
                    s.lives = max(0, s.lives - 1)
                    s.score = max(0, s.score - 10)
                play_die()

                msg = " / ".join(s.name for s in died) + "  CRASHED"
                if not wait_space(screen, med, draw_world,
                                  [msg, "Press SPACE to continue"]):
                    return

                if any(s.lives == 0 for s in snakes):
                    _handle_game_over(screen, fonts, num_players,
                                      level, sammy, jake, skill, accelerate)
                    return

                sp1, sp2 = spawn_for(level)
                sammy.reset(*sp1)
                jake.reset(*sp2)
                number_pos = None
                number_value = 1
                move_accumulator = 0.0
                break  # exit the inner accumulator loop

            # Apply head moves.
            for s, pos in new_heads:
                s.body.append(pos)
                while len(s.body) > s.target_length:
                    s.body.pop(0)

        draw_world()
        pygame.display.flip()


def _handle_game_over(screen: pygame.Surface, fonts: dict,
                      num_players: int, level: int,
                      sammy: Snake, jake: Snake,
                      skill: int, accelerate: bool) -> None:
    med = fonts["med"]

    if num_players == 1:
        winner_name = "SAMMY"
        score = sammy.score
    else:
        winner_name = "SAMMY" if sammy.score >= jake.score else "JAKE"
        score = max(sammy.score, jake.score)

    lines = ["G A M E   O V E R", ""]
    if num_players == 1:
        lines.append(f"Final score: {sammy.score}    Level reached: {level}")
    else:
        lines.append(f"Sammy: {sammy.score}    Jake: {jake.score}")
        lines.append(f"Winner: {winner_name}")
    lines.append("")
    lines.append("Press SPACE to record your score, Esc to skip")

    screen.fill(BG_COL)
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
    pygame.display.set_caption("Nibbles!")
    screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))

    fonts = {
        "big": pygame.font.SysFont("consolas,menlo,monospace", 48, bold=True),
        "med": pygame.font.SysFont("consolas,menlo,monospace", 22, bold=True),
        "small": pygame.font.SysFont("consolas,menlo,monospace", 18),
    }

    try:
        while True:
            choice = intro_screen(screen, fonts)
            if choice is None:
                return 0
            num_players, skill, accelerate = choice
            game_loop(screen, fonts, num_players, skill, accelerate)
    finally:
        pygame.quit()


if __name__ == "__main__":
    sys.exit(main())
