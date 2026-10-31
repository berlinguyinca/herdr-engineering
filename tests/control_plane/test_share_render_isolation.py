"""The parent's side of the isolated render: it treats the child as compromised and checks everything it sends.

Each test drives the parent against a FAKE child (a few lines of Python) through the module's spawn seam.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from herdr_engineering.control_plane import share_render as sr

REPO = str(Path(__file__).resolve().parents[2])
FAKE_PRE = """
import sys, os, struct, json, time
i = os.fdopen(0, 'rb'); o = os.fdopen(1, 'wb')
def rd():
    h = i.read(8)
    if len(h) < 8: sys.exit(0)
    return json.loads(i.read(struct.unpack('>Q', h)[0]))
def fr(d):
    b = d if isinstance(d, bytes) else json.dumps(d).encode()
    return struct.pack('>Q', len(b)) + b
def wr(*ds):
    o.write(b''.join(fr(d) for d in ds)); o.flush()
wr({'t': 'ready'})
"""
ECHO_LOOP = "while True:\n    job = rd(); wr({'t': 'done', 'id': job['id'], 'out': job['text']})\n"
JOB = "job = rd(); J = job['id']\n"


def keep(context: str, url: str) -> str | None:
    return url


@pytest.fixture(autouse=True)
def clean_worker():
    sr._WORKER._kill()
    sr.clear_sanitize_caches()
    yield
    sr._WORKER._kill()
    sr.clear_sanitize_caches()


def use_fake(monkeypatch: pytest.MonkeyPatch, body: str, pre: str = FAKE_PRE) -> None:
    code = pre + body
    monkeypatch.setattr(sr, "_child_command", lambda: [sys.executable, "-I", "-c", code])
    sr._WORKER._kill()


def call(data: bytes = b"<p>x</p>", **kw):
    return sr.sanitize_html(data, resolve=kw.pop("resolve", keep), **kw)


# ----------------------------------------------------------------------------------------------- B1: job ids
def test_echo_fake_child_is_a_valid_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake(monkeypatch, ECHO_LOOP)
    assert call(b"<p>one</p>") == "<p>one</p>" and call(b"<p>two</p>") == "<p>two</p>"


def test_extra_done_frame_never_shifts_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "while True:\n    job = rd(); J = job['id']; wr({'t':'done','id':J,'out':'A'}, {'t':'done','id':J,'out':'EXTRA'})\n"
    use_fake(monkeypatch, body)
    for n in range(3):
        with pytest.raises(sr.RenderFailed):
            call(f"<p>{n}</p>".encode())


def test_wrong_id_done_is_refused_and_the_child_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake(monkeypatch, JOB + "wr({'t':'done','id':'0'*32,'out':'<p>forged</p>'})\ntime.sleep(5)\n")
    with pytest.raises(sr.RenderFailed):
        call(cache_key="k")
    assert sr._WORKER._proc is None
    assert sr._results().get("x", "y") is None and len(sr._results()) == 0


def test_stale_frame_from_a_previous_job_is_never_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        "first = rd(); wr({'t':'done','id':first['id'],'out':'JOB1-OUT'}); old = first['id']\n"
        "second = rd(); wr({'t':'done','id':old,'out':'JOB1-OUT'}); time.sleep(5)\n"
    )
    use_fake(monkeypatch, body)
    assert call(b"<p>1</p>", cache_key=1) == "JOB1-OUT"
    with pytest.raises(sr.RenderFailed):
        call(b"<p>2</p>", cache_key=2)
    use_fake(monkeypatch, ECHO_LOOP)
    assert call(b"<p>3</p>", cache_key=3) == "<p>3</p>"


@pytest.mark.parametrize(
    "frame",
    [
        "{'t':'done','id':J}",
        "{'t':'done','id':J,'out':5}",
        "{'t':'done','id':J,'out':'x','extra':1}",
        "{'t':'failed','id':J,'x':1}",
        "{'t':'resolve','id':J,'c':'a'}",
        "{'t':'resolve','id':J,'c':'nope','u':'u'}",
        "{'t':'resolve','id':J,'c':5,'u':'u'}",
        "{'t':'ready'}",
        "{'t':'surprise','id':J}",
        "{'id':J}",
        "[1, 2, 3]",
        "'a string'",
    ],
)
def test_frame_schema_is_strict(monkeypatch: pytest.MonkeyPatch, frame: str) -> None:
    use_fake(monkeypatch, JOB + f"wr({frame})\ntime.sleep(5)\n")
    start = time.monotonic()
    with pytest.raises(sr.RenderFailed):
        call()
    assert time.monotonic() - start < 2


# ------------------------------------------------------------------------------------------ B2: the output tripwire
_LT = "<"
_TRIPWIRE_OUTPUTS = [
    _LT + "script>alert(1)</" + "script>",
    _LT + "SCRIPT src=x>",
    _LT + "i" + "frame src=x>",
    _LT + " f" + "rame>",
    _LT + "f" + "rameset>",
    _LT + "ob" + "ject data=x>",
    _LT + "em" + "bed src=x>",
    _LT + 'img src=x onerror=alert(1)>',
    _LT + 'p ONCLICK = "x">',
    _LT + 'a href="javascript:alert(1)">x</a>',
    _LT + "i" + "mg src=x srcdoc=1>",
    _LT + 'meta http-equiv=refresh content="0;url=//e">',
    _LT + "base href=//e/>",
    _LT + "form action=//e>",
]


@pytest.mark.parametrize("out", _TRIPWIRE_OUTPUTS)
def test_output_tripwire_refuses_and_kills_the_child(monkeypatch: pytest.MonkeyPatch, out: str) -> None:
    use_fake(monkeypatch, JOB + f"wr({{'t':'done','id':J,'out':{out!r}}})\ntime.sleep(5)\n")
    with pytest.raises(sr.RenderFailed):
        call(cache_key="k")
    assert sr._WORKER._proc is None and len(sr._results()) == 0


def test_tripwire_lets_prose_that_merely_mentions_the_words_through(monkeypatch: pytest.MonkeyPatch) -> None:
    prose = "<p>Set onclick = handler; javascript: URLs and srcdoc are blocked. 1 &lt; 2 &lt;script&gt;</p>"
    use_fake(monkeypatch, JOB + f"wr({{'t':'done','id':J,'out':{prose!r}}})\ntime.sleep(5)\n")
    assert call() == prose


# ------------------------------------------------------------------------------ M3: framing, caps and garbage
@pytest.mark.parametrize(
    "body",
    [
        "wr(b'not json at all')\ntime.sleep(5)\n",
        "o.write(b'\\xff' * 64); o.flush(); time.sleep(5)\n",
        "wr(b'[' * 200000 + b']' * 200000)\ntime.sleep(5)\n",
        "wr(b'{\"a\":' * 100000 + b'1' + b'}' * 100000)\ntime.sleep(5)\n",
        "wr(b'\\xff\\xfe\\x00')\ntime.sleep(5)\n",
    ],
)
def test_garbage_and_deep_json_are_a_prompt_failure(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    use_fake(monkeypatch, JOB + body)
    start = time.monotonic()
    with pytest.raises(sr.RenderFailed) as info:
        call()
    assert not isinstance(info.value, sr.RenderTimeout) and time.monotonic() - start < 2


def test_huge_length_prefix_is_refused_without_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake(monkeypatch, JOB + "o.write(struct.pack('>Q', 2**63)); o.flush(); time.sleep(10)\n")
    start = time.monotonic()
    with pytest.raises(sr.RenderFailed) as info:
        call()
    assert type(info.value) is sr.RenderFailed and time.monotonic() - start < 1.5


def test_slow_drip_of_a_maximum_frame_hits_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "SANITIZE_DEADLINE_SECONDS", 0.5)
    use_fake(monkeypatch, JOB + "o.write(struct.pack('>Q', 16*1024*1024) + b'{'); o.flush()\nwhile True: time.sleep(0.05); o.write(b' '); o.flush()\n")
    start = time.monotonic()
    with pytest.raises(sr.RenderTimeout):
        call()
    assert time.monotonic() - start < 2.5 and sr._WORKER._proc is None


# --------------------------------------------------------------------------------------- I2: child environment
def test_child_has_no_secrets_cwd_root_and_is_hardened(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "hunter2")
    assert call(b"<p>x</p>") == "<p>x</p>"
    pid = sr._WORKER._proc.pid
    assert b"hunter2" not in Path(f"/proc/{pid}/environ").read_bytes()
    assert Path(f"/proc/{pid}/environ").read_bytes() == b""
    assert os.readlink(f"/proc/{pid}/cwd") == "/"
    assert os.getsid(pid) == pid  # its own session
    status = Path(f"/proc/{pid}/status").read_text()
    assert "NoNewPrivs:\t1" in status
    limits = Path(f"/proc/{pid}/limits").read_text()
    assert any(line.startswith("Max open files") and line.split()[3] == "32" for line in limits.splitlines())
    assert any(line.startswith("Max processes") and line.split()[2] == "0" for line in limits.splitlines())
    assert sorted(os.listdir(f"/proc/{pid}/fd"))[:3] == ["0", "1", "2"] and len(os.listdir(f"/proc/{pid}/fd")) <= 6


# ---------------------------------------------------------------------------------- I3: spawn and the handshake
def test_spawn_failure_is_a_render_failure_not_an_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "_child_command", lambda: ["/nonexistent/python", "-c", "pass"])
    with pytest.raises(sr.RenderFailed):
        call()
    assert sr._WORKER._proc is None and sr._WORKER._lock.acquire(blocking=False)
    sr._WORKER._lock.release()


def test_cold_start_does_not_count_against_the_job_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "SANITIZE_DEADLINE_SECONDS", 0.5)
    pre = FAKE_PRE.replace("wr({'t': 'ready'})", "time.sleep(1.2); wr({'t': 'ready'})")
    use_fake(monkeypatch, ECHO_LOOP, pre)
    assert call(b"<p>slow start</p>") == "<p>slow start</p>"


def test_missing_handshake_has_its_own_timeout_and_is_not_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "HANDSHAKE_TIMEOUT_SECONDS", 0.3)
    use_fake(monkeypatch, "time.sleep(30)\n", FAKE_PRE.replace("wr({'t': 'ready'})", ""))
    start = time.monotonic()
    with pytest.raises(sr.RenderFailed) as info:
        call()
    assert type(info.value) is sr.RenderFailed and time.monotonic() - start < 2
    use_fake(monkeypatch, ECHO_LOOP)
    assert call() == "<p>x</p>"  # same input: the start-up failure was not remembered


# ---------------------------------------------------------------------- I4: resolver and lock behaviour
def test_resolver_round_trip_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    body = JOB + "for n in range(6000):\n    wr({'t':'resolve','id':J,'c':'a','u':'u%d' % n}); rd()\nwr({'t':'done','id':J,'out':'ok'})\n"
    use_fake(monkeypatch, body)
    with pytest.raises(sr.RenderFailed):
        call()


def test_resolve_url_length_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    body = JOB + "wr({'t':'resolve','id':J,'c':'a','u':'h'*9000}); rd()\nwr({'t':'done','id':J,'out':'ok'})\n"
    use_fake(monkeypatch, body)
    with pytest.raises(sr.RenderFailed):
        call()


def test_long_embedded_image_survives_and_long_links_are_dropped() -> None:
    image = "data:image/png;base64," + "A" * 100_000
    out = call(f'<img src="{image}"><a href="http://example.org/{"p" * 20_000}">t</a>'.encode())
    assert image in out and "example.org" not in out


def test_a_slow_resolver_cannot_extend_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "SANITIZE_DEADLINE_SECONDS", 0.4)

    def slow(context: str, url: str) -> str | None:
        time.sleep(10)
        return url

    start = time.monotonic()
    with pytest.raises(sr.RenderTimeout):
        call(b'<a href="http://example.org/">t</a>', resolve=slow)
    assert time.monotonic() - start < 2.5
    sr.clear_sanitize_caches()
    assert call(b"<p>fine</p>") == "<p>fine</p>"  # lock released, child replaced
    assert sr._FAILURES.get(b"nothing") is None


def test_a_reentrant_resolver_does_not_hang() -> None:
    def reenter(context: str, url: str) -> str | None:
        return call(b"<p>inner</p>")

    done: list[str] = []
    thread = threading.Thread(target=lambda: done.append(call(b'<a href="http://example.org/">t</a>', resolve=reenter)), daemon=True)
    thread.start()
    thread.join(8)
    assert not thread.is_alive() and done and "href" not in done[0]


def test_a_stuck_holder_gives_busy_not_a_hang(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "SANITIZE_DEADLINE_SECONDS", 0.2)
    assert sr._WORKER._lock.acquire(blocking=False)
    try:
        start = time.monotonic()
        with pytest.raises(sr.RenderBusy):
            call()
        assert 1.5 < time.monotonic() - start < 4
    finally:
        sr._WORKER._lock.release()


# --------------------------------------------------------------------------------- M1: what is remembered
def _dead_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "_child_command", lambda: ["/nonexistent/python"])
    sr._WORKER._kill()


def test_child_failed_frame_is_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake(monkeypatch, JOB + "wr({'t':'failed','id':J}); time.sleep(5)\n")
    with pytest.raises(sr.RenderFailed):
        call(b"<p>bad</p>")
    _dead_command(monkeypatch)
    with pytest.raises(sr.RenderFailed) as info:
        call(b"<p>bad</p>")
    assert "recently" in str(info.value)


def test_oversize_output_is_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake(monkeypatch, JOB + "wr({'t':'done','id':J,'out':'x' * (5 * 1024 * 1024)})\ntime.sleep(5)\n")
    with pytest.raises(sr.RenderTooLarge):
        call(b"<p>big</p>")
    _dead_command(monkeypatch)
    with pytest.raises(sr.RenderTooLarge):
        call(b"<p>big</p>")


def test_timeout_is_remembered_but_crashes_and_spawn_failures_are_not(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "SANITIZE_DEADLINE_SECONDS", 0.3)
    use_fake(monkeypatch, JOB + "time.sleep(30)\n")
    with pytest.raises(sr.RenderTimeout):
        call(b"<p>hang</p>")
    _dead_command(monkeypatch)
    with pytest.raises(sr.RenderTimeout) as info:
        call(b"<p>hang</p>")
    assert "recently" in str(info.value)
    # a crash during the job, a death before the job and a spawn failure are not input-determined
    use_fake(monkeypatch, JOB + "os._exit(9)\n")
    with pytest.raises(sr.RenderFailed):
        call(b"<p>crash</p>")
    use_fake(monkeypatch, "os._exit(9)\n")
    with pytest.raises(sr.RenderFailed):
        call(b"<p>early</p>")
    _dead_command(monkeypatch)
    with pytest.raises(sr.RenderFailed):
        call(b"<p>nospawn</p>")
    use_fake(monkeypatch, ECHO_LOOP)
    for text in (b"<p>crash</p>", b"<p>early</p>", b"<p>nospawn</p>"):
        assert call(text) == text.decode()


def test_a_slow_resolver_timeout_is_not_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "SANITIZE_DEADLINE_SECONDS", 0.3)
    data = b'<a href="http://example.org/">t</a>'
    with pytest.raises(sr.RenderTimeout):
        call(data, resolve=lambda c, u: time.sleep(5) or u)
    assert "href" in call(data)


# ------------------------------------------------------------------------------------------- M2: fork safety
def test_a_forked_child_does_not_share_the_parents_worker() -> None:
    code = f"""
import os, sys
sys.path.insert(0, {REPO!r})
from herdr_engineering.control_plane import share_render as sr
keep = lambda c, u: u
assert sr.sanitize_html(b'<p>parent</p>', resolve=keep) == '<p>parent</p>'
parent_pid = sr._WORKER._proc.pid
pid = os.fork()
if pid == 0:
    ok = sr._WORKER._proc is None and sr._WORKER._lock.acquire(False)
    sr._WORKER._lock.release()
    out = sr.sanitize_html(b'<p>forked</p>', resolve=keep)
    ok = ok and out == '<p>forked</p>' and sr._WORKER._proc.pid != parent_pid
    os._exit(0 if ok else 1)
_, status = os.waitpid(pid, 0)
assert os.WEXITSTATUS(status) == 0, status
assert sr._WORKER._proc.poll() is None  # the parent's child survived the fork child exiting
assert sr.sanitize_html(b'<p>again</p>', resolve=keep) == '<p>again</p>'
print('ok')
"""
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60, check=False)
    assert done.stdout.strip() == "ok", done.stderr[-800:]


# -------------------------------------------------------------------------------------- the real child's lifetime

def test_child_survives_the_thread_that_spawned_it() -> None:
    """The child must not be tied to its spawning thread (request threads are short-lived)."""
    sr._WORKER._kill()
    box: list[str] = []
    worker = threading.Thread(target=lambda: box.append(sr.sanitize_html(b"<p>a</p>", resolve=keep)))
    worker.start()
    worker.join()
    assert box
    proc = sr._WORKER._proc
    assert proc is not None
    time.sleep(0.3)
    assert proc.poll() is None
    assert "<p>b</p>" in sr.sanitize_html(b"<p>b</p>", resolve=keep)


def test_real_child_exits_when_the_parent_pipe_closes() -> None:
    sr._WORKER._kill()
    sr.sanitize_html(b"<p>a</p>", resolve=keep)
    proc = sr._WORKER._proc
    assert proc is not None and proc.stdin is not None
    proc.stdin.close()
    assert proc.wait(timeout=10) == 0
    sr._WORKER._proc = None
