"""Stand-in for gh in unit tests: records argv and answers the calls orq makes.

`pr create` returns a PR URL. `pr view --json url` fails unless FAKE_GH_PR_EXISTS is set or a PR was created
earlier in this test (tracked in the record file). `pr checks` reports pending once, then pass (counter in the
record file). `pr merge` succeeds and `pr view --json state` then says MERGED.
"""

import json
import os
import sys

RECORD = os.environ["FAKE_GH_RECORD"]
args = sys.argv[1:]

with open(RECORD, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")
with open(RECORD, encoding="utf-8") as handle:
    history = [json.loads(line) for line in handle if line.strip()]

created = any(call[:2] == ["pr", "create"] for call in history[:-1])
merged = any(call[:2] == ["pr", "merge"] for call in history[:-1])
checks_polls = sum(1 for call in history[:-1] if call[:2] == ["pr", "checks"])

if args[:2] == ["pr", "create"]:
    print("https://github.com/owner/sandbox/pull/42")
elif args[:2] == ["pr", "view"] and "url" in args:
    if os.environ.get("FAKE_GH_PR_EXISTS") or created:
        print("https://github.com/owner/sandbox/pull/42")
    else:
        sys.stderr.write("no pull requests found for branch\n")
        sys.exit(1)
elif args[:2] == ["pr", "view"] and "number" in args:
    print("42")
elif args[:2] == ["pr", "view"] and "state" in args:
    print("MERGED" if merged else "OPEN")
elif args[:2] == ["pr", "checks"]:
    if checks_polls == 0:
        sys.stderr.write("no checks reported on the 'orq/add-greeting' branch\n")
        sys.exit(1)
    bucket = "pending" if checks_polls == 1 else "pass"
    print(json.dumps([{"name": "check", "state": "SUCCESS" if bucket == "pass" else "QUEUED", "bucket": bucket,
                       "link": "https://github.com/owner/sandbox/actions/runs/1/job/2"}]))
elif args[:2] == ["pr", "merge"]:
    pass
