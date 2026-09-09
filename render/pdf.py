"""Render the page to PDF with headless Chrome.

Chrome is the only thing on a stock Mac that renders modern CSS grid to PDF
faithfully. macOS's own cupsfilter mangles it. If Chrome is not installed the
pipeline still produces HTML - a missing PDF must never fail the nightly run.
"""

import os, re, shutil, subprocess, urllib.parse

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


def page_count(pdf_path):
    """Read the page count straight out of the PDF bytes.

    Chrome does not report it and the standard library has no PDF parser. The
    page tree's /Count is authoritative when present; counting /Type /Page
    objects is the fallback. Only ever asked "is this one page or more", so
    approximate is fine.
    """
    with open(pdf_path, "rb") as f:
        raw = f.read()
    counts = [int(n) for n in re.findall(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)", raw)]
    if counts:
        return max(counts)
    pages = len(re.findall(rb"/Type\s*/Page\b(?!s)", raw))
    return pages or 1


def fit(html_path, pdf_path, floor=8.0, start=11.0, step=0.5, timeout=90):
    """Render, and if it spills past one page, shrink and render again.

    The alternative is hand-tuning the print stylesheet every time the amount of
    content shifts, which is a losing game - a quiet week fits at 10pt and a
    busy one does not fit at 8. This makes the page self-regulating within a
    readable range. Below the floor the answer is not smaller type, it is less
    content, so the caller trims instead.
    """
    size = start
    attempts = []
    while True:
        _write_scale(html_path, size)
        render(html_path, pdf_path, timeout=timeout)
        pages = page_count(pdf_path)
        attempts.append((size, pages))
        if pages <= 1 or size <= floor:
            return {"pt": size, "pages": pages, "attempts": attempts,
                    "fitted": pages <= 1}
        size = round(size - step, 2)


SCALE_MARK = "/* fit-scale */"


def _write_scale(html_path, pt):
    """Set the print font size in the page's own stylesheet, in place."""
    with open(html_path) as f:
        html = f.read()
    # MUST be the root element, not body. Almost every rule on this page sizes
    # in rem, and rem resolves against :root - setting body did nothing to the
    # elements that actually fill the page.
    rule = "%s\n    html { font-size:%.2fpt !important; }" % (SCALE_MARK, pt)
    if SCALE_MARK in html:
        html = re.sub(re.escape(SCALE_MARK) + r"\n    html \{ font-size:[\d.]+pt !important; \}",
                      rule, html)
        html = re.sub(re.escape(SCALE_MARK) + r"\n    body \{ font-size:[\d.]+pt !important; \}",
                      rule, html)
    else:
        html = html.replace("@media print {", "@media print {\n    " + rule, 1)
    with open(html_path, "w") as f:
        f.write(html)


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
