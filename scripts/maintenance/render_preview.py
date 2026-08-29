import os
from playwright.sync_api import sync_playwright

html_path = os.path.abspath("motivation-theme-preview.html")
output_path = os.path.abspath("motivation_preview.png")

with open(html_path, "r", encoding="utf-8") as f:
    html = f.read()

html = html.replace("{{CATEGORY}}", "Mindset")
html = html.replace("{{TAGLINE}}", "Daily Motivation")
html = html.replace("{{QUOTE}}", "Push yourself, because no one else is going to do it for you.")
html = html.replace("{{AUTHOR}}", "Carrot Owl Education")
html = html.replace("{{SUBTEXT}}", "Consistency is the bridge between you and your placement offer. Keep grinding.")
html = html.replace("{{WEBSITE}}", "carrotowleducation.com")
html = html.replace("{{CTA}}", "Save this post")

base_url = f"file:///{os.path.dirname(html_path).replace(chr(92), '/')}/"
if "<head>" in html:
    html = html.replace("<head>", f"<head>\\n<base href='{base_url}'>")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_viewport_size({"width": 1080, "height": 1350})
    page.set_content(html, wait_until="networkidle")
    page.evaluate("document.fonts.ready")
    page.screenshot(path=output_path, full_page=True)
    browser.close()

print(f"Generated preview at {output_path}")
