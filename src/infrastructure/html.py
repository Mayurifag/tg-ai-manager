from html import escape as html_escape
from html.parser import HTMLParser
from urllib.parse import urlparse

_ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "i",
    "pre",
    "s",
    "strong",
    "u",
}
_ALLOWED_SCHEMES = {"http", "https", "tg"}


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS:
            return
        if tag == "br":
            self.parts.append("<br>")
            return
        if tag == "a":
            href = next(
                (value for name, value in attrs if name.lower() == "href" and value),
                None,
            )
            href = href.strip() if href else None
            if href and _is_safe_href(href):
                self.parts.append(f'<a href="{html_escape(href, quote=True)}">')
            else:
                self.parts.append("<a>")
            return
        self.parts.append(f"<{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _ALLOWED_TAGS and tag != "br":
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(html_escape(data))


def _is_safe_href(href: str) -> bool:
    href = href.strip()
    parsed = urlparse(href)
    return not parsed.scheme or parsed.scheme in _ALLOWED_SCHEMES


def sanitize_html(value: str) -> str:
    sanitizer = _Sanitizer()
    sanitizer.feed(value)
    sanitizer.close()
    return "".join(sanitizer.parts)
