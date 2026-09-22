"""What the reviewer's scripts import inside the container: a browser, and a record of what the page
did while it was being driven.

This file is copied into the container, not imported by the host -- playwright lives there, next to
the browsers the image bakes in, and not in the workspace. Nothing here knows about tasks, packets or the board.

A text model cannot see a screenshot, so `sketch` is what the reviewer actually reads: the address,
the title, the visible words and the controls it could press next. The screenshots are for you, and
come out beside the trajectory.
"""

import contextlib
from pathlib import Path

from playwright.sync_api import sync_playwright

SHOTS = Path("/out/shots")

CONTROLS = "button, a[href], input, select, textarea, [role=button], [role=tab], [role=link]"


def sketch(tab, chars: int = 4000) -> str:
    """The page as the reviewer reads it. Hidden controls are left out: a button nobody can see is
    not a way forward, and listing them all is how a model talks itself into clicking nothing."""
    controls = tab.eval_on_selector_all(
        CONTROLS,
        "els => els.filter(e => e.offsetParent !== null).map(e => e.tagName.toLowerCase()"
        " + (e.id ? '#' + e.id : '') + ': '"
        " + (e.innerText || e.value || e.placeholder || e.getAttribute('aria-label') || '').trim().slice(0, 60))",
    )
    return (
        f"{tab.url}\n{tab.title()}\n\n{tab.inner_text('body')[:chars]}"
        f"\n\ncontrols:\n" + "\n".join(f"  {c}" for c in controls[:60])
    )


@contextlib.contextmanager
def page(url: str, name: str, width: int = 1440, height: int = 900):
    """A browser on `url`, yielded for you to drive.

    On the way out, whatever happened: a screenshot called `name`, and everything the page
    complained about while it was open. Those complaints are the half of a review a screenshot
    cannot show -- a component that threw, a request that 404ed, a script that never loaded.
    """
    SHOTS.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        tab = browser.new_page(viewport={"width": width, "height": height})
        tab.on("console", lambda m: problems.append(f"console error: {m.text}") if m.type == "error" else None)
        tab.on("pageerror", lambda e: problems.append(f"uncaught: {e}"))
        tab.on("requestfailed", lambda r: problems.append(f"request failed: {r.method} {r.url}"))
        tab.on("response", lambda r: problems.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
        try:
            tab.goto(url, wait_until="networkidle")
            yield tab
        finally:
            with contextlib.suppress(Exception):
                tab.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)
            print(f"\n--- {name} ---")
            print("\n".join(dict.fromkeys(problems)) or "no console errors, no failed requests")
            browser.close()
