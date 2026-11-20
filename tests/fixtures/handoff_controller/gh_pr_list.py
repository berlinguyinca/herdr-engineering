"""A real executable standing in for `gh`: answers only `pr list --json ...`, like the one call the controller makes."""
import json
import sys

if sys.argv[1:3] == ["pr", "list"]:
    print(json.dumps([{"number": 7, "title": "feat: the controller", "headRefName": "feat/handoff-controller"}]))
    sys.exit(0)
sys.stderr.write(f"gh_pr_list: unsupported {sys.argv[1:]!r}\n")
sys.exit(2)
