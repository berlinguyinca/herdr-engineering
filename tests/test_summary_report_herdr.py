"""/summary publish against a REAL headless herdr server and a real viewer process (no fakes)."""
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from herdr_engineering import summary_report as sr

ROOT = Path(sr.__file__).resolve().parents[1]
REPORT = "# Session summary\n**Session:** claude · **Repository:** r · **Branch/worktree:** b\n**Overall status:** in progress\n\nGoal alpha beta.\n\n## Evidence coverage\ncomplete\n"


class _Cfg:
    control_plane_url = "http://cp.test"
    host_id = "h"
    api_key = "k"


@pytest.fixture
def herdr():
    exe = shutil.which("herdr")
    if not exe:
        pytest.skip("herdr binary not installed")
    home = Path(tempfile.mkdtemp(prefix="hd."))
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    env.update(HOME=str(home), HERDR_SOCKET_PATH=str(home / "t.sock"), PYTHONPATH=str(ROOT))
    proc = subprocess.Popen([exe, "server"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=home)
    for _ in range(80):
        if subprocess.run([exe, "workspace", "list"], env=env, capture_output=True).returncode == 0:
            break
        time.sleep(0.25)
    else:
        proc.kill()
        pytest.skip("could not start an isolated herdr server here")
    made = json.loads(subprocess.run([exe, "workspace", "create", "--cwd", str(home)], env=env,
                                     capture_output=True, text=True).stdout)
    yield {"exe": exe, "env": env, "home": home, "agent_pane": made["result"]["root_pane"]["pane_id"]}
    subprocess.run([exe, "server", "stop"], env=env, capture_output=True, timeout=10)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(home, ignore_errors=True)


def _publish(h, text, **kw):
    env = {**h["env"], "HERDR_PANE_ID": h["agent_pane"]}
    # the publisher shells out to a bare "herdr"; put the real one first on PATH
    env["PATH"] = f"{Path(h['exe']).parent}:{os.environ['PATH']}"
    old = os.environ.get("PATH")
    os.environ["PATH"] = env["PATH"]
    try:
        return sr.publish_report("claude", "abc-123", text, home=h["home"], environ=env,
                                 load_config=lambda: _Cfg, post=lambda cfg, body: (200, {}),
                                 history=lambda a, s: {"prompts": [], "prompts_total": 0, "dispatch_ts": []}, **kw)
    finally:
        os.environ["PATH"] = old


def _panes(h):
    done = subprocess.run([h["exe"], "pane", "list"], env=h["env"], capture_output=True, text=True)
    return {p["pane_id"]: p for p in json.loads(done.stdout)["result"]["panes"]}


def _wait(pred, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        value = pred()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("condition not met in time")


def test_publish_focuses_the_report_pane_and_prints_a_digest(herdr):
    out = _publish(herdr, REPORT)
    assert out["ok"] and out["pane"]["ok"]
    assert out["digest"].splitlines()[0] == "**Overall status:** in progress"
    assert f"Report file: {out['file']}" in out["digest"] and "Dashboard: dashboard Summary view" in out["digest"]
    assert "Goal alpha beta." in out["digest"]
    pane = out["pane"]["pane_id"]
    assert _wait(lambda: _panes(herdr).get(pane, {}).get("focused"))         # the user lands on the report


def test_republish_reuses_the_live_viewer_pane_and_refreshes_it(herdr):
    first = _publish(herdr, REPORT)
    pane, pidfile = first["pane"]["pane_id"], Path(first["file"] + ".viewer.pid")
    pid = _wait(lambda: pidfile.is_file() and pidfile.read_text().strip())
    before = set(_panes(herdr))
    # hand focus back to the agent pane so we can see the second publish take it again
    subprocess.run([herdr["exe"], "pane", "focus", "--pane", pane, "--direction", "left"], env=herdr["env"],
                   capture_output=True)
    second = _publish(herdr, REPORT.replace("alpha beta", "gamma delta"))
    assert second["pane"] == {"ok": True, "pane_id": pane, "reused": True}
    assert set(_panes(herdr)) == before                                       # nothing closed, nothing split
    assert pidfile.read_text().strip() == pid                                 # the very same viewer process
    assert "gamma delta" in Path(second["file"]).read_text()
    assert _wait(lambda: _panes(herdr).get(pane, {}).get("focused"))

    def shows_new_text():
        text = subprocess.run([herdr["exe"], "pane", "read", pane], env=herdr["env"], capture_output=True,
                              text=True).stdout
        return "gamma delta" in text
    assert _wait(shows_new_text)                                              # SIGWINCH made it re-read the file


def test_a_quit_viewer_is_replaced_by_a_fresh_pane(herdr):
    first = _publish(herdr, REPORT)
    pane = first["pane"]["pane_id"]
    pidfile = Path(first["file"] + ".viewer.pid")
    _wait(lambda: pidfile.is_file())
    subprocess.run([herdr["exe"], "pane", "send-keys", pane, "q"], env=herdr["env"], capture_output=True)
    _wait(lambda: pane not in _panes(herdr))                                  # the viewer closes its own pane on quit
    second = _publish(herdr, REPORT)
    assert second["pane"]["ok"] and second["pane"]["pane_id"] != pane and "reused" not in second["pane"]
