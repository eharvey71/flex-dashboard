"""Tiny JSON-over-HTTPS helper. Standard library only."""

import json, time, urllib.error, urllib.parse, urllib.request

TIMEOUT = 30
RETRIES = 3


class HttpError(Exception):
    def __init__(self, status, body, url):
        self.status, self.body, self.url = status, body, url
        super().__init__("HTTP %s from %s: %s" % (status, url, body[:400]))


def request(url, headers=None, data=None, method=None, form=False):
    """GET, or POST when data is given. Returns parsed JSON (or {} if empty)."""
    headers = dict(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = json.dumps(data).encode()
            headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept", "application/json")
    headers.setdefault("User-Agent", "flex-dashboard/1.0")

    last = None
    for attempt in range(RETRIES):
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method=method or ("POST" if body else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            # 4xx other than rate-limiting is a real error; don't burn retries.
            if e.code < 500 and e.code != 429:
                raise HttpError(e.code, detail, url)
            last = HttpError(e.code, detail, url)
        except Exception as e:  # network hiccup, DNS, timeout
            last = e
        if attempt < RETRIES - 1:
            time.sleep(2 ** attempt)
    raise last


def get_all_pages(url, headers, page_param="page", limit=5):
    """GitHub-style pagination: follow ?page=N until a short page comes back."""
    out = []
    for page in range(1, limit + 1):
        sep = "&" if "?" in url else "?"
        chunk = request("%s%s%s=%d&per_page=100" % (url, sep, page_param, page), headers=headers)
        items = chunk if isinstance(chunk, list) else chunk.get("items", [])
        out.extend(items)
        if len(items) < 100:
            break
    return out
