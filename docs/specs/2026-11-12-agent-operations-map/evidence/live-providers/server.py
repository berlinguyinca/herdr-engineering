import sys, threading, time, os
sys.path.insert(0, os.getcwd())
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
tok = sys.argv[1]
srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0,
    api_keys={"ing-" + tok: "ingest", "op-" + tok: "operator"}, agent_ops_config={"enabled": True})
threading.Thread(target=srv.serve, daemon=True).start()
srv.wait_until_ready()
print(srv.port, flush=True)
time.sleep(3600)
