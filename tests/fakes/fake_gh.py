"""Stand-in for gh in unit tests: records argv, answers `pr create` with a PR URL; `pr view` fails unless FAKE_GH_PR_EXISTS."""

import json
import os
import sys

with open(os.environ["FAKE_GH_RECORD"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.com/owner/sandbox/pull/42")
elif sys.argv[1:3] == ["pr", "view"]:
    if os.environ.get("FAKE_GH_PR_EXISTS"):
        print("https://github.com/owner/sandbox/pull/42")
    else:
        sys.stderr.write("no pull requests found for branch\n")
        sys.exit(1)
