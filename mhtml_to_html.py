"""Convert .mhtml files to self-contained .html with assets inlined as data: URIs.

Usage: python mhtml_to_html.py
Walks UseCase*/ folders, converts each .mhtml in place to a sibling .html.
"""
from __future__ import annotations

import base64
import email
import email.policy
import os
import re
import html as html_module
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urldefrag

FETCH_CACHE: dict[str, tuple[bytes, str]] = {}


def fetch(url: str) -> tuple[bytes, str] | None:
    """Fetch a URL, return (bytes, content_type)."""
    if url in FETCH_CACHE:
        return FETCH_CACHE[url]
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
            ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
            result = (data, ctype)
            FETCH_CACHE[url] = result
            return result
    except Exception as e:
        print(f"  WARN: failed to fetch {url[:80]}: {e}")
        return None


def guess_mime(url: str, default: str = "application/octet-stream") -> str:
    ext = url.lower().rsplit(".", 1)[-1].split("?")[0]
    return {
        "woff2": "font/woff2",
        "woff": "font/woff",
        "ttf": "font/ttf",
        "otf": "font/otf",
        "eot": "application/vnd.ms-fontobject",
        "svg": "image/svg+xml",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
        "css": "text/css",
    }.get(ext, default)


def inline_remote_css_urls(css_text: str, depth: int = 0) -> str:
    """Replace url(http(s)://...) and @import url(...) in CSS with data: URIs.
    Recurses into fetched CSS so nested @font-face rules get their fonts inlined."""
    if depth > 4:
        return css_text
    pattern = re.compile(r"""url\(\s*(['"]?)(https?://[^'")]+)\1\s*\)""", re.IGNORECASE)

    def repl(m: re.Match) -> str:
        quote, url = m.group(1), m.group(2)
        result = fetch(url)
        if result is None:
            return m.group(0)
        data, server_ctype = result
        # Decide MIME: prefer server, fall back to extension guess
        if server_ctype:
            mime = server_ctype
        else:
            mime = guess_mime(url)
        # If it's CSS, recursively inline nested URLs first
        if mime == "text/css" or mime.startswith("text/css"):
            try:
                nested = data.decode("utf-8", errors="replace")
            except Exception:
                nested = ""
            nested = inline_remote_css_urls(nested, depth + 1)
            data = nested.encode("utf-8")
            mime = "text/css;charset=utf-8"
        b64 = base64.b64encode(data).decode("ascii")
        return f"url({quote}data:{mime};base64,{b64}{quote})"

    return pattern.sub(repl, css_text)

ROOT = Path(__file__).parent

MHTML_FILES = []
for d in sorted(ROOT.glob("UseCase*")):
    for f in d.glob("*.mhtml"):
        if not f.name.endswith(".bak"):
            MHTML_FILES.append(f)


def part_payload(part) -> bytes:
    payload = part.get_payload(decode=True)
    return payload if payload is not None else b""


def to_data_uri(part) -> str:
    ctype = part.get_content_type() or "application/octet-stream"
    payload = part_payload(part)
    if ctype == "text/css":
        charset = part.get_content_charset() or "utf-8"
        try:
            css_text = payload.decode(charset, errors="replace")
        except LookupError:
            css_text = payload.decode("utf-8", errors="replace")
        css_text = inline_remote_css_urls(css_text)
        payload = css_text.encode("utf-8")
        ctype = "text/css;charset=utf-8"
    b64 = base64.b64encode(payload).decode("ascii")
    return f"data:{ctype};base64,{b64}"


def convert(mhtml_path: Path) -> Path:
    with open(mhtml_path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=email.policy.default)

    # Build a map: Content-Location URL -> data: URI (for non-root parts)
    parts = list(msg.walk()) if msg.is_multipart() else [msg]
    # Determine root html part: prefer the one whose Content-Location matches
    # the message's Content-Location, else first text/html part.
    root_loc = msg.get("Content-Location")
    root_part = None
    for p in parts:
        if p.is_multipart():
            continue
        if p.get_content_type() == "text/html":
            if root_loc and p.get("Content-Location") == root_loc:
                root_part = p
                break
    if root_part is None:
        for p in parts:
            if not p.is_multipart() and p.get_content_type() == "text/html":
                root_part = p
                break
    if root_part is None:
        raise RuntimeError(f"No HTML part found in {mhtml_path}")

    # Decode root HTML
    root_charset = root_part.get_content_charset() or "utf-8"
    root_raw = part_payload(root_part)
    try:
        html = root_raw.decode(root_charset, errors="replace")
    except LookupError:
        html = root_raw.decode("utf-8", errors="replace")

    # First pass: register non-HTML parts. HTML parts (other than root) need
    # URL resolution against asset_map BEFORE being encoded as data: URIs,
    # otherwise their inner cid: refs end up dead.
    asset_map: dict[str, str] = {}
    html_parts: list = []  # (part, loc, cid_clean)
    for p in parts:
        if p is root_part or p.is_multipart():
            continue
        loc = p.get("Content-Location")
        cid = p.get("Content-ID")
        cid_clean = cid.strip().lstrip("<").rstrip(">") if cid else None
        if p.get_content_type() == "text/html":
            html_parts.append((p, loc, cid_clean))
            continue
        data_uri = to_data_uri(p)
        if loc:
            asset_map[loc] = data_uri
        if cid_clean:
            asset_map[f"cid:{cid_clean}"] = data_uri

    def make_resolve(base_url: str):
        def resolve(url: str) -> str:
            url = url.strip()
            if not url:
                return url
            if url.startswith(("data:", "javascript:", "mailto:", "#")):
                return url
            decoded = html_module.unescape(url)
            defragged, _ = urldefrag(decoded)
            if decoded.startswith("cid:"):
                return asset_map.get(decoded, url)
            candidates = []
            if base_url:
                try:
                    candidates.append(urljoin(base_url, defragged))
                except Exception:
                    pass
            candidates.append(defragged)
            candidates.append(decoded)
            for c in candidates:
                if c in asset_map:
                    return asset_map[c]
            return url
        return resolve

    def process_html(html_text: str, base_url: str) -> str:
        resolve = make_resolve(base_url)
        attr_re = re.compile(
            r"""(\s(?:src|href|poster|data-src)\s*=\s*)(['"])(.*?)\2""",
            re.IGNORECASE | re.DOTALL,
        )
        html_text = attr_re.sub(
            lambda m: f"{m.group(1)}{m.group(2)}{resolve(m.group(3))}{m.group(2)}",
            html_text,
        )
        srcset_re = re.compile(
            r"""(\ssrcset\s*=\s*)(['"])(.*?)\2""", re.IGNORECASE | re.DOTALL
        )

        def srcset_sub(m: re.Match) -> str:
            prefix, quote, val = m.group(1), m.group(2), m.group(3)
            new_items = []
            for item in val.split(","):
                item = item.strip()
                if not item:
                    continue
                bits = item.split(None, 1)
                u = bits[0]
                descriptor = f" {bits[1]}" if len(bits) > 1 else ""
                new_items.append(f"{resolve(u)}{descriptor}")
            return f"{prefix}{quote}{', '.join(new_items)}{quote}"

        html_text = srcset_re.sub(srcset_sub, html_text)
        url_re = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.IGNORECASE)
        html_text = url_re.sub(
            lambda m: f"url({m.group(1)}{resolve(m.group(2))}{m.group(1)})",
            html_text,
        )
        return html_text

    GFONTS_LINK = (
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        'family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500'
        '&display=swap">'
    )

    def inject_gfonts(html_text: str) -> str:
        if "fonts.googleapis.com/css2?family=Inter" in html_text:
            return html_text
        return re.sub(
            r"(<head[^>]*>)", lambda m: m.group(1) + GFONTS_LINK,
            html_text, count=1,
        )

    # Now process each non-root HTML part with URL resolution, encode it, and
    # register it in the asset_map so the root HTML's iframe references resolve
    # to a fully-self-contained data: URI.
    for p, loc, cid_clean in html_parts:
        charset = p.get_content_charset() or "utf-8"
        raw_inner = part_payload(p)
        try:
            inner_html = raw_inner.decode(charset, errors="replace")
        except LookupError:
            inner_html = raw_inner.decode("utf-8", errors="replace")
        inner_base = loc or ""
        inner_html = process_html(inner_html, inner_base)
        inner_html = inject_gfonts(inner_html)
        encoded = base64.b64encode(inner_html.encode("utf-8")).decode("ascii")
        data_uri = f"data:text/html;charset=utf-8;base64,{encoded}"
        if loc:
            asset_map[loc] = data_uri
        if cid_clean:
            asset_map[f"cid:{cid_clean}"] = data_uri

    base = root_part.get("Content-Location") or ""
    html = process_html(html, base)
    html = inject_gfonts(html)

    # Replace <link rel="stylesheet" href="data:text/css..."> with inline <style>
    # blocks. This avoids data:-URI size limits and CORS quirks, and lets us
    # flatten @import data: chains so the browser doesn't have to follow them.
    def _decode_data_css(uri: str) -> str | None:
        if not uri.startswith("data:"):
            return None
        try:
            head, payload = uri.split(",", 1)
        except ValueError:
            return None
        if "base64" in head:
            try:
                return base64.b64decode(payload).decode("utf-8", errors="replace")
            except Exception:
                return None
        from urllib.parse import unquote
        return unquote(payload)

    def _flatten_imports(css: str, depth: int = 0) -> str:
        if depth > 6:
            return css
        pat = re.compile(
            r"""@import\s+url\(\s*(['"]?)(data:text/css[^'")]+)\1\s*\)\s*;?""",
            re.IGNORECASE,
        )

        def repl(m: re.Match) -> str:
            inner = _decode_data_css(m.group(2))
            return _flatten_imports(inner, depth + 1) if inner is not None else m.group(0)

        return pat.sub(repl, css)

    link_re = re.compile(
        r"""<link\b[^>]*rel=['"]stylesheet['"][^>]*href=['"](data:text/css[^'"]+)['"][^>]*>""",
        re.IGNORECASE,
    )

    def _link_to_style(m: re.Match) -> str:
        css = _decode_data_css(m.group(1))
        if css is None:
            return m.group(0)
        return f"<style>{_flatten_imports(css)}</style>"

    html = link_re.sub(_link_to_style, html)

    out_path = mhtml_path.with_suffix(".html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def main() -> None:
    if not MHTML_FILES:
        print("No .mhtml files found.")
        return
    for m in MHTML_FILES:
        try:
            out = convert(m)
            size_in = m.stat().st_size
            size_out = out.stat().st_size
            print(f"OK  {m.relative_to(ROOT)} -> {out.name} "
                  f"({size_in/1024:.0f} KB -> {size_out/1024:.0f} KB)")
        except Exception as e:
            print(f"ERR {m.relative_to(ROOT)}: {e}")


if __name__ == "__main__":
    main()
