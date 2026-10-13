from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from tracd.engine import Engine
from tracd.notify import Alert
from tracd.samples import SampleFetcher, demo_catalog
from tracd.store import Store

def _find_chromium() -> str | None:
    """A Chromium to drive in tests: TRACD_TEST_CHROME, Playwright's own, or a known install."""
    candidates = [os.environ.get("TRACD_TEST_CHROME")]
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            candidates.append(p.chromium.executable_path)
    except Exception:  # noqa: BLE001
        pass
    candidates += sorted(str(p) for p in Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome"))
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


_CHROMIUM = _find_chromium()


def chromium_path() -> str | None:
    return _CHROMIUM


class FakeNotifier:
    def __init__(self, fail: str | None = None):
        self.sent: list[Alert] = []
        self.fail = fail
        self.on = True

    def channels(self, s=None):
        return ["ntfy"] if self.on else []

    async def send(self, alert: Alert):
        self.sent.append(alert)
        if not self.on:
            return {}
        return {"ntfy": self.fail}

    async def close(self):
        pass

    @property
    def urgent(self):
        return [a for a in self.sent if a.urgent]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s: float):
        self.t += s


@pytest.fixture
def catalog():
    return demo_catalog()


@pytest.fixture
def fetcher(catalog):
    return SampleFetcher(catalog)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def engine(store, fetcher, notifier, clock):
    opened: list[str] = []
    beeps: list[int] = []
    e = Engine(store, fetcher, notifier, clock=clock, jitter=0, opener=opened.append, beeper=lambda: beeps.append(1),
               confirm_after=0)
    e.opened = opened  # type: ignore[attr-defined]
    e.beeps = beeps  # type: ignore[attr-defined]
    return e


async def settle(engine: Engine) -> None:
    """Let the engine's background tasks (alerts, page opens) finish."""
    for _ in range(5):
        await asyncio.sleep(0)
        if engine._tasks:
            await asyncio.gather(*list(engine._tasks), return_exceptions=True)
