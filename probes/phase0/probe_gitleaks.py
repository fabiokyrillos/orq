"""Phase 0 probe for gitleaks: staged scan with a synthetic secret, then a clean scan."""

from __future__ import annotations

import json
import secrets
import shutil
import string

from _common import OUT, SCRATCH, git, header, run

GITLEAKS = shutil.which("gitleaks") or "gitleaks"


def main() -> None:
    res = run("g0-version", [GITLEAKS, "version"], SCRATCH)
    header(res)
    print("  version:", res.stdout.strip(), " path:", GITLEAKS)

    # Synthetic token shaped like a GitHub PAT. Generated at runtime, never committed, never pushed.
    alphabet = string.ascii_letters + string.digits
    fake = "ghp_" + "".join(secrets.choice(alphabet) for _ in range(36))
    leak = SCRATCH / "leaky_config.py"
    leak.write_text(f'GITHUB_TOKEN = "{fake}"\n', encoding="utf-8")
    git("add", "leaky_config.py", cwd=SCRATCH)

    report = OUT / "g1-leaks.json"
    report.unlink(missing_ok=True)
    res = run(
        "g1-staged-with-secret",
        [GITLEAKS, "git", "--pre-commit", "--staged", "--redact", "--no-banner",
         "--report-format", "json", "--report-path", str(report), str(SCRATCH)],
        SCRATCH,
    )
    header(res)
    if report.exists():
        findings = json.loads(report.read_text(encoding="utf-8"))
        print("  findings:", [(f.get("RuleID"), f.get("File"), f.get("StartLine"), f.get("Secret")) for f in findings])

    git("rm", "--cached", "-q", "leaky_config.py", cwd=SCRATCH)
    leak.unlink()
    (SCRATCH / "clean.txt").write_text("nothing secret\n", encoding="utf-8")
    git("add", "clean.txt", cwd=SCRATCH)
    res = run(
        "g2-staged-clean",
        [GITLEAKS, "git", "--pre-commit", "--staged", "--redact", "--no-banner", str(SCRATCH)],
        SCRATCH,
    )
    header(res)
    git("rm", "--cached", "-q", "clean.txt", cwd=SCRATCH)
    (SCRATCH / "clean.txt").unlink()

    res = run(
        "g3-commit-range",
        [GITLEAKS, "git", "--log-opts=HEAD~1..HEAD", "--redact", "--no-banner", str(SCRATCH)],
        SCRATCH,
    )
    header(res)


if __name__ == "__main__":
    main()
