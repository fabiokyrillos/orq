"""Stand-in for gh in unit tests: records argv and answers `pr create` with a PR URL."""

import json
import os
import sys

with open(os.environ["FAKE_GH_RECORD"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.com/owner/sandbox/pull/42")
