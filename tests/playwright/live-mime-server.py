"""Real ControlPlaneServer with legacy artifacts whose stored media types are hostile (as stored before upload validation)."""
import json
import sys
import threading
import urllib.request

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.repo import ControlPlaneRepo

MIMES = ["application/pdf;a=1,text/html", "application/pdf, text/html", "application/pdf;,text/html"]
BODY = b'<!doctype html><title>t</title><script>document.title="XSS:"+document.domain</script>'

repo, acl = ControlPlaneRepo(), ArtifactAcl()
server = ControlPlaneServer(repo, api_keys={"k": "operator"}, artifact_acl=acl)
threading.Thread(target=server.serve, daemon=True).start()
server.wait_until_ready()
base = f"http://127.0.0.1:{server.port}"
ids = []
for number, mime in enumerate(MIMES):
    # a real upload with a clean type; the stored record is then rewritten to what an old, unvalidated upload left behind
    request = urllib.request.Request(base + f"/api/v1/artifacts?filename=report{number}.pdf&mime=application%2Fpdf", data=BODY,
                                     method="POST", headers={"X-API-Key": "k"})
    aid = json.loads(urllib.request.urlopen(request).read())["artifact_id"]
    repo._artifacts[aid]["mime_type"] = mime
    ids.append(aid)
print(json.dumps({"base": base, "ids": ids, "mimes": MIMES}), flush=True)
sys.stdin.read()
