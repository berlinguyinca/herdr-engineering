"""Typing an adapter's input into a pane the way it was measured to work."""
import time

from herdr_engineering.herdr_pane_client import HerdrPaneClient

from .base import ExitPlan, TypedInput


def send_typed(client: HerdrPaneClient, pane_id: str, typed: TypedInput | ExitPlan) -> None:
    """Type the text, wait ``submit_delay_s``, then send the submit keys.

    Codex 0.169.0 ignored an Enter sent in the same instant as the text, so the pause is part of every typed input,
    not something a caller may skip. An ExitPlan with no text sends only its keys; Herdr takes lowercase key names.
    """
    keys = typed.submit_keys if isinstance(typed, TypedInput) else typed.keys
    if typed.text:
        client.send_text(pane_id, typed.text)
        if keys:
            time.sleep(typed.submit_delay_s)
    if keys:
        client.send_keys(pane_id, [key.lower() for key in keys])
