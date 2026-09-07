"""Render the page to PDF with headless Chrome.

Chrome is the only thing on a stock Mac that renders modern CSS grid to PDF
faithfully. macOS's own cupsfilter mangles it. If Chrome is not installed the
pipeline still produces HTML - a missing PDF must never fail the nightly run.
"""

import os, shutil, subprocess, urllib.parse

CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]


class NoRenderer(Exception):
    pass


def find_chrome():
    for path in CANDIDATES:
        if os.path.exists(path):
            return path
    for name in ("google-chrome", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise NoRenderer(
        "No Chrome-family browser found. Install Google Chrome, or set "
        "output.pdf to false in config.json to skip PDF generation.")


def render(html_path, pdf_path, timeout=90):
    chrome = find_chrome()
    url = "file://" + urllib.parse.quote(os.path.abspath(html_path))
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-first-run",
        "--no-pdf-header-footer",
        # Web fonts need a moment to arrive or the PDF falls back to serif.
        "--virtual-time-budget=6000",
        "--print-to-pdf=%s" % os.path.abspath(pdf_path),
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) < 1000:
        raise NoRenderer("Chrome produced no usable PDF.\n%s"
                         % (proc.stderr or proc.stdout or "")[:400])
    return pdf_path
