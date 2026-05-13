from src.infrastructure.html import sanitize_html


def test_sanitize_html_allows_basic_formatting():
    assert sanitize_html("<strong>Hello</strong> <em>world</em>") == (
        "<strong>Hello</strong> <em>world</em>"
    )


def test_sanitize_html_removes_scriptable_markup():
    assert sanitize_html("<img src=x onerror=alert(1)><script>alert(2)</script>") == (
        "alert(2)"
    )


def test_sanitize_html_rejects_javascript_href():
    assert sanitize_html('<a href="javascript:alert(1)">click</a>') == "<a>click</a>"


def test_sanitize_html_rejects_padded_javascript_href():
    assert sanitize_html('<a href=" javascript:alert(1)">click</a>') == "<a>click</a>"


def test_sanitize_html_allows_safe_href():
    assert sanitize_html('<a href="https://example.com?a=1&b=2">click</a>') == (
        '<a href="https://example.com?a=1&amp;b=2">click</a>'
    )
