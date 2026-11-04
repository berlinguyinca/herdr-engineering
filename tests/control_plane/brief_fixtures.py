"""Recorded-shape Gmail API ``users.messages.get`` dicts for the assistant team-brief tests (fixture data, no service stub).

Every identifier a leak could carry is a unique sentinel, so a test can search every surface the team can reach for it."""
from __future__ import annotations

import base64

MESSAGE_ID = "18c0ffee5ec0de01"              # Gmail's own id: must never reach the room
THREAD_ID = "18c0ffee7a11ed02"
SENDER = "Vendor Person <billing.person@vendor-corp.example>"
SENDER_ADDRESS = "billing.person@vendor-corp.example"
HEADER_ID = "<CAF0rM3ss4geId9z@mail.vendor-corp.example>"
ATTACHMENT = "invoice-7731-private.pdf"
SECRET = "sk-LIVEKEY0123456789abcdefghijkl"
BODY_PHRASE = "wire the 40000 USD retainer"
RECIPIENT = "operator.private@example-home.org"
SENTINELS = (MESSAGE_ID, THREAD_ID, SENDER_ADDRESS, HEADER_ID, ATTACHMENT, SECRET, RECIPIENT, "billing.person")


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmail(message_id: str = MESSAGE_ID, *, thread_id: str = THREAD_ID, sender: str = SENDER,
          subject: str = "Invoice 7731 payment overdue", body: str | None = None, attachments: tuple[str, ...] = (ATTACHMENT,),
          labels: tuple[str, ...] = ("INBOX",)) -> dict:
    text = body if body is not None else (
        f"Hello,\nplease {BODY_PHRASE} before Friday. Reply to {SENDER_ADDRESS} or open https://pay.vendor-corp.example/p?t=abc123.\n"
        f"Our API token is {SECRET} for the portal.\n> quoted older line from {RECIPIENT}\n")
    parts = [{"mimeType": "text/plain", "filename": "", "body": {"size": len(text), "data": _b64(text)}}]
    for name in attachments:
        parts.append({"mimeType": "application/pdf", "filename": name, "body": {"size": 4096, "attachmentId": "ANGjdJ8x" + name[:4]}})
    return {"id": message_id, "threadId": thread_id, "internalDate": "1790000000000", "labelIds": list(labels),
            "snippet": text[:80], "payload": {"mimeType": "multipart/mixed", "headers": [
                {"name": "From", "value": sender}, {"name": "To", "value": RECIPIENT},
                {"name": "Subject", "value": subject}, {"name": "Message-ID", "value": HEADER_ID}], "parts": parts}}
