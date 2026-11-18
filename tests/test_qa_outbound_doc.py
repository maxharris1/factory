from __future__ import annotations

from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs/plans/qa-outbound-and-upload.md"


def outbound_email_section() -> str:
    text = DOC.read_text(encoding="utf-8")
    start = text.index("### Outbound email")
    return text[start : text.index("\n### ", start + 1)]


def test_the_sender_names_the_central_sender_and_the_sales_exception() -> None:
    sender = next(line for line in outbound_email_section().splitlines() if line.startswith("- **Sender.**"))
    for fact in (
        "CENTRAL_SENDER_EMAIL_ADDRESS",
        "SALES_SENDER_EMAIL",
        "sales@aut0.io",
        "submit-website-inquiry/index.ts",
        "_shared/gmailClient.ts",
        "_shared/gmailSendPolicy.ts",
    ):
        assert fact in sender, f"sender statement is missing {fact}"
