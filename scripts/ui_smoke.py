#!/usr/bin/env python3
"""Headless UI smoke test (Playwright + Chromium).

Exercises the flows that broke most often during development: app bootstrap
(OpenUI5), first-run wizard, settings navigation, theme toggle - and fails
on any uncaught page error.

Environment:
    UI_SMOKE_BASE_URL   target URL (default http://127.0.0.1:8000/ui/)
    CHROMIUM_PATH       system Chromium executable; unset = Playwright's own

Run against a stub portal without a real account:

    python scripts/mock_ui_server.py --port 8010 &
    UI_SMOKE_BASE_URL=http://127.0.0.1:8010/ui/ python scripts/ui_smoke.py
"""

import asyncio
import os
import sys

from playwright.async_api import async_playwright

BASE_URL = os.environ.get("UI_SMOKE_BASE_URL", "http://127.0.0.1:8000/ui/")
CHROMIUM_PATH = os.environ.get("CHROMIUM_PATH") or None
SHOT_DIR = os.environ.get("UI_SMOKE_SHOT_DIR", "/tmp")


async def main() -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROMIUM_PATH, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1500)
        await page.keyboard.press("Escape")  # close wizard
        await page.wait_for_timeout(500)
        await page.screenshot(path=f"{SHOT_DIR}/ui_basket.png")

        # settings round trip
        await page.locator('button[id*="btnSettings"]').first.click()
        await page.wait_for_timeout(1200)
        await page.screenshot(path=f"{SHOT_DIR}/ui_settings.png")
        back = page.locator('button[id*="navButton"], button[id*="BtnNavBack"]')
        if not await back.count():
            # nav button has no id; use the page's back arrow (first button in header)
            back = page.locator(".sapMPageHeader button").first
        await back.click()
        await page.wait_for_timeout(1200)

        # theme toggle on basket page
        theme_before = await page.evaluate("sap.ui.getCore().getConfiguration().getTheme()")
        await page.locator('button[id*="btnTheme"]').first.click()
        await page.wait_for_timeout(2000)
        theme_after = await page.evaluate("sap.ui.getCore().getConfiguration().getTheme()")
        await page.screenshot(path=f"{SHOT_DIR}/ui_dark.png")
        await browser.close()

    print("theme:", theme_before, "->", theme_after)
    if theme_before == theme_after:
        print("FAIL: theme toggle did not change the theme")
        return 1
    if errors:
        print("FAIL: uncaught page errors:", errors)
        return 1
    print("UI smoke OK (no page errors)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
