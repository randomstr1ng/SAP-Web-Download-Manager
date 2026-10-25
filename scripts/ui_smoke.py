import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium-browser", args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto("http://127.0.0.1:8000/ui/", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1500)
        await page.keyboard.press("Escape")  # close wizard
        await page.wait_for_timeout(500)
        await page.screenshot(path="/tmp/ui_basket.png")
        # settings round trip
        await page.locator('button[id*="btnSettings"]').first.click()
        await page.wait_for_timeout(1200)
        await page.screenshot(path="/tmp/ui_settings.png")
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
        await page.screenshot(path="/tmp/ui_dark.png")
        print("theme:", theme_before, "->", theme_after)
        print("errors:", errors or "(none)")
        await browser.close()

asyncio.run(main())
