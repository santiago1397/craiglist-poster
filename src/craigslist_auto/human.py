from __future__ import annotations

import random
import time

from patchright.sync_api import Locator, Page


def sleep_jitter(base: float, spread: float = 0.4) -> None:
    """Sleep base seconds ± spread fraction."""
    delta = base * spread
    time.sleep(max(0.05, random.uniform(base - delta, base + delta)))


def read_pause(text_length_chars: int = 0) -> None:
    """Simulate a human reading the page before acting."""
    base = 1.2 + min(text_length_chars / 800, 4.0)
    sleep_jitter(base, 0.5)


def human_type(locator: Locator, text: str, *, mistake_rate: float = 0.015) -> None:
    """
    Type character-by-character with realistic per-char delays.
    Occasionally fat-finger a key and backspace it.
    """
    locator.click()
    sleep_jitter(0.35)
    for ch in text:
        # Occasional typo
        if random.random() < mistake_rate and ch.isalpha():
            wrong = random.choice("qwertyuiopasdfghjklzxcvbnm")
            locator.type(wrong, delay=random.uniform(40, 140))
            sleep_jitter(0.18)
            locator.press("Backspace")
            sleep_jitter(0.12)
        delay_ms = random.uniform(45, 180)
        # Occasional longer pause (thinking)
        if random.random() < 0.04:
            time.sleep(random.uniform(0.4, 1.4))
        locator.type(ch, delay=delay_ms)


def human_click(page: Page, locator: Locator) -> None:
    """Move-then-click with a small reading pause."""
    sleep_jitter(0.4, 0.6)
    try:
        locator.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    box = locator.bounding_box()
    if box:
        # Click somewhere inside the element, not dead center
        x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        page.mouse.move(x, y, steps=random.randint(8, 20))
        sleep_jitter(0.15)
        page.mouse.click(x, y)
    else:
        locator.click()


def scroll_a_bit(page: Page) -> None:
    page.mouse.wheel(0, random.randint(120, 480))
    sleep_jitter(0.5)
