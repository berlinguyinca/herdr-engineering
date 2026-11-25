"""AssistantService scans of the store dicts run under the store lock (#976).

Each test holds the store's real lock from the test thread, calls the service method in another thread and
checks the call waits for the lock (it would otherwise iterate a dict a writer may be changing, raising
``RuntimeError: dictionary changed size during iteration``). Real service, real store, no stand-ins."""
import threading
import time

import pytest

from herdr_engineering.control_plane.assistant import (
    AssistantService,
    assistant_config,
)

T, U = "t", "u1"


@pytest.fixture
def svc():
    s = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}))
    a = s.create_assistant(tenant_id=T, user_id=U, name="A")
    return s, a


def _blocks_on_store_lock(svc, call) -> bool:
    """True when ``call`` does not finish while another thread holds the store lock, and finishes after."""
    done = threading.Event()
    errors: list[BaseException] = []

    def run():
        try:
            call()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            done.set()

    with svc.store._lock:
        threading.Thread(target=run, daemon=True).start()
        blocked = not done.wait(0.4)
    assert done.wait(5), "call never finished after the lock was released"
    assert not errors, errors
    return blocked


def test_notify_duplicate_scan_waits_for_the_store_lock(svc):
    s, _ = svc
    s.notify(tenant_id=T, user_id=U, key="k", priority="normal", text="x")   # the repeat returns from the scan alone
    assert _blocks_on_store_lock(s, lambda: s.notify(tenant_id=T, user_id=U, key="k", priority="normal", text="x"))


def test_list_notifications_waits_for_the_store_lock(svc):
    s, _ = svc
    assert _blocks_on_store_lock(s, lambda: s.list_notifications(tenant_id=T, user_id=U))


def test_digest_waits_for_the_store_lock(svc):
    s, _ = svc
    assert _blocks_on_store_lock(s, lambda: s.digest(tenant_id=T, user_id=U))


def test_list_proposals_waits_for_the_store_lock(svc):
    s, _ = svc
    assert _blocks_on_store_lock(s, lambda: s.list_proposals(tenant_id=T, user_id=U))


def test_create_proposal_dedupe_scan_waits_for_the_store_lock(svc):
    s, a = svc
    args = dict(tenant_id=T, user_id=U, assistant_id=a.assistant_id, kind="propose_branch", title="t",
                payload={"repo": "r", "title": "t"}, requested_by=U)
    assert s.create_proposal(**args)[1] is True        # the repeat returns from the scan alone
    assert _blocks_on_store_lock(s, lambda: s.create_proposal(**args))


def test_repair_pending_audits_waits_for_the_store_lock(svc):
    s, _ = svc
    assert _blocks_on_store_lock(s, lambda: s.repair_pending_audits(tenant_id=T, user_id=U))


def test_open_tool_run_scan_waits_for_the_store_lock(svc):
    s, a = svc
    s.open_tool_run(tenant_id=T, user_id=U, assistant_id=a.assistant_id, purpose="p")   # the repeat returns from the scan
    assert _blocks_on_store_lock(s, lambda: s.open_tool_run(
        tenant_id=T, user_id=U, assistant_id=a.assistant_id, purpose="p"))


def test_export_memory_waits_for_the_store_lock(svc):
    s, a = svc
    assert _blocks_on_store_lock(s, lambda: s.export_memory(
        tenant_id=T, user_id=U, assistant_id=a.assistant_id))


def test_count_memory_waits_for_the_store_lock(svc):
    s, a = svc
    assert _blocks_on_store_lock(s, lambda: s.store.count_memory(
        tenant_id=T, user_id=U, assistant_id=a.assistant_id))


def test_list_proposals_survives_concurrent_inserts(svc):
    """Writers inserting proposals while list_proposals scans never break the scan."""
    s, a = svc
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer():
        n = 0
        while not stop.is_set():
            n += 1
            try:
                s.create_proposal(tenant_id=T, user_id=U, assistant_id=a.assistant_id, kind="propose_branch",
                                  title=f"t{n}", payload={"repo": "r", "title": f"t{n}"}, requested_by=U,
                                  dedupe_key=f"d{n}")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        deadline = time.time() + 1.5
        while time.time() < deadline:
            s.list_proposals(tenant_id=T, user_id=U)
    finally:
        stop.set()
        t.join(5)
    assert not errors, errors
