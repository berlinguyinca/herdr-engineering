"""A real stand-in for a headless model CLI: reads the prompt on stdin, saves it, prints an answer.

usage: briefing_writer.py MODE PROMPT_OUT   (MODE: ok | fenced | garbage | sleep)
"""
import json
import sys
import time

mode, prompt_out = sys.argv[1], sys.argv[2]
prompt = sys.stdin.read()
with open(prompt_out, "w", encoding="utf-8") as handle:
    handle.write(prompt)
briefing = {
    "goal": "Ship the handoff controller",
    "completed": ["wrote the drain"],
    "remaining": ["write the takeover"],
    "decisions": ["poll the store"],
    "next_commands": ["pytest tests/test_handoff_controller_briefing.py"],
    "resume_point": "start at briefing.py",
    "pull_requests": ["https://github.com/example/repo/pull/7", "not-a-url"],
    "ignored_key": "dropped",
}
if mode == "ok":
    print(json.dumps(briefing))
elif mode == "fenced":
    print("Here is the briefing:\n```json\n" + json.dumps({"remaining": "just one string"}) + "\n```\nDone.")
elif mode == "garbage":
    print("I could not write a briefing, sorry.")
elif mode == "sleep":
    time.sleep(30)
