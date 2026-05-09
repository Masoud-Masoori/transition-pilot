"""Headless Playwright recording of the TransitionPilot demo flow.

Produces a silent MP4 reference video that the operator can re-record over
with narration, or use as a fallback if their own recording fails.

Captures the cinematic split-screen forensic flow:
  - DAY 6 readmission story (left panel, already on screen at load)
  - DAY 0 prevention populates after Run click
  - Logic-Link click reveals raw FHIR JSON (the AI-Factor demo moment)
  - Switch case to allergy_conflict, run again
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

DEMO_URL = "http://127.0.0.1:8089/demo/ui/"
OUT_DIR = Path(__file__).resolve().parents[2].parent / "submission" / "demo-recording"


async def beat(page, ms: int):
    """Hold a frame for `ms` milliseconds."""
    await page.wait_for_timeout(ms)


async def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 810},
            record_video_dir=str(OUT_DIR),
            record_video_size={"width": 1440, "height": 810},
        )
        page = await ctx.new_page()

        # ── Beat 0: Opening on the DAY 6 panel ──
        await page.goto(DEMO_URL, wait_until="networkidle")
        await beat(page, 4000)

        # ── Beat 1: Run the warfarin case ──
        await page.click("button#run")
        await page.wait_for_selector(".failure-card", timeout=20_000)
        await beat(page, 3500)

        # ── Beat 2: Logic-Link click → FHIR evidence panel ──
        warf = page.locator(".failure-card .logic-link").first
        await warf.scroll_into_view_if_needed()
        await warf.click()
        await page.wait_for_selector(".evidence-row pre", timeout=5000)
        await beat(page, 4000)
        await page.click("#close-evidence")
        await beat(page, 800)

        # ── Beat 3: Click second Logic-Link (TMP-SMX) ──
        try:
            tmp_link = page.locator(".failure-card .logic-link").nth(1)
            await tmp_link.click()
            await beat(page, 3000)
            await page.click("#close-evidence")
            await beat(page, 600)
        except Exception:
            pass

        # ── Beat 4: Switch to allergy case + run ──
        await page.select_option("#case-select", value="case_5_allergy_conflict")
        await beat(page, 700)
        await page.click("button#run")
        await page.wait_for_selector(".failure-card", timeout=20_000)
        await beat(page, 3500)

        # ── Beat 5: Show allergy Logic-Link evidence ──
        try:
            allergy_link = page.locator(".failure-card .logic-link").first
            await allergy_link.click()
            await beat(page, 4000)
            await page.click("#close-evidence")
            await beat(page, 800)
        except Exception:
            pass

        # ── Beat 6: Final hold on memo ──
        await beat(page, 2500)

        await ctx.close()
        await browser.close()

    # Find the produced webm and report
    files = sorted(OUT_DIR.glob("*.webm"))
    if files:
        print(f"[record_demo] saved {files[-1]}")
    else:
        print("[record_demo] no video produced", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
