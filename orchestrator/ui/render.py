"""Showing agent-written text safely: Markdown without raw HTML, everything else escaped."""

import json

from markdown_it import MarkdownIt

_MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")


def markdown_html(text: str) -> str:
    """Markdown to HTML. Raw HTML in the text is escaped, never passed through."""
    return str(_MARKDOWN.render(text))


def pretty_json(text: str) -> str:
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except ValueError:
        return text


def artifact_kind(name: str) -> str:
    """How to show an artifact: 'markdown' or 'code'."""
    return "markdown" if name.endswith(".md") else "code"
