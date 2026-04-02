from playwright.sync_api import sync_playwright
from pathlib import Path

USERNAME = "17717603343"
PASSWORD = "Jsj_2454761741"
STATE_FILE = Path(__file__).parent / "state.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()

    page.goto("https://passport2.chaoxing.com/login")

    page.fill("#phone", USERNAME)
    page.fill("#pwd", PASSWORD)
    page.click("#loginBtn")

    page.wait_for_timeout(5000)

    context.storage_state(path=STATE_FILE)
    browser.close()