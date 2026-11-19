"""The leading paragraph of each CommonMark list item, outside blockquotes and code."""

from __future__ import annotations

from markdown_it import MarkdownIt

MARKDOWN = MarkdownIt("commonmark").enable("table")


def list_item_leads(body: str) -> list[str]:
    tokens = MARKDOWN.parse(body)
    leads: list[str] = []
    quoted = 0
    for index, token in enumerate(tokens):
        if token.type == "blockquote_open":
            quoted += 1
        elif token.type == "blockquote_close":
            quoted -= 1
        elif (
            not quoted
            and token.type == "inline"
            and index >= 2
            and tokens[index - 2].type == "list_item_open"
            and tokens[index - 1].type == "paragraph_open"
        ):
            leads.append(" ".join(token.content.split()))
    return leads
