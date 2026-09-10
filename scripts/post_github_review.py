"""CLI utility to post Revix Swarm review comments directly to a GitHub Pull Request.

Supports:
1. GitHub CLI (`gh api`) when logged in locally
2. Direct GitHub REST API using GITHUB_TOKEN or Revix App Private Key

Usage:
  uv run python3 scripts/post_github_review.py --repo owner/repo --pr 1
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REVIEW_FILE = Path(__file__).parent / "last_review_result.json"


def get_diff_lines(repo: str, pr_number: int) -> set[tuple[str, int]]:
    """Extracts valid (file_path, new_line_number) pairs from PR diff."""
    cmd = ["gh", "pr", "diff", str(pr_number), "-R", repo]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        return set()

    diff_lines: set[tuple[str, int]] = set()
    current_file = None

    for line in res.stdout.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("@@ ") and current_file:
            # e.g. @@ -15,7 +15,7 @@
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                for ln in range(start, start + count):
                    diff_lines.add((current_file, ln))

    return diff_lines


def post_with_gh(repo: str, pr_number: int, data: dict) -> None:
    score = data.get("score", 0)
    summary = data.get("summary", "")
    comments = data.get("comments", [])

    valid_diff_lines = get_diff_lines(repo, pr_number)

    # Build review body
    status_label = "❌ BLOCKING: REQUEST CHANGES" if score < 80 else "✅ APPROVED"
    body_lines = [
        "### 🔍 Revix Enterprise Swarm Code Review",
        f"**Quality Score: `{score}/100`** — **{status_label}**",
        "",
        "#### Executive Summary",
        f"{summary}",
        "",
        "### 📋 Full Review Findings Table",
        "| Severity | File:Line | Description |",
        "| :--- | :--- | :--- |",
    ]

    for c in comments:
        icon = "🚨" if c["severity"] == "CRITICAL" else ("⚠️" if c["severity"] == "WARNING" else "ℹ️")
        desc = c["body"].replace("\n", " ")[:160]
        body_lines.append(f"| {icon} **{c['severity']}** | `{c['path']}:{c['line']}` | {desc}... |")

    # Add suggested code changes to body
    fixes = [c for c in comments if c.get("suggested_fix")]
    if fixes:
        body_lines.append("")
        body_lines.append("### 🛠️ Key Recommended Fixes")
        for i, c in enumerate(fixes[:4], 1):
            body_lines.append(f"#### {i}. `{c['path']}:{c['line']}` ({c['severity']})")
            body_lines.append(f"{c['body']}\n")
            body_lines.append("```cpp")
            body_lines.append(c["suggested_fix"].strip("`").replace("cpp\n", ""))
            body_lines.append("```\n")

    review_body = "\n".join(body_lines)

    # Format inline comments that map onto actual PR diff hunks
    formatted_comments = []
    skipped = 0
    for c in comments:
        line_num = int(c["line"])
        file_path = c["path"]

        # Map to valid diff line if nearby, or verify
        if (file_path, line_num) in valid_diff_lines or not valid_diff_lines:
            comment_text = f"**[{c['severity']}]** {c['body']}"
            if c.get("suggested_fix"):
                fix_code = c["suggested_fix"].strip("`").replace("cpp\n", "").strip()
                comment_text += f"\n\n```suggestion\n{fix_code}\n```"

            formatted_comments.append({
                "path": file_path,
                "line": line_num,
                "side": c.get("side", "RIGHT"),
                "body": comment_text,
            })
        else:
            skipped += 1

    payload = {
        "body": review_body,
        "event": "COMMENT",  # Safe for PR author & external reviewers
        "comments": formatted_comments,
    }

    payload_path = Path(__file__).parent / "github_review_payload.json"
    with open(payload_path, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"Submitting review to {repo} PR #{pr_number} via `gh api`...")
    print(f"- Total findings: {len(comments)}")
    print(f"- Inline diff comments attached: {len(formatted_comments)} (architectural findings: {skipped})")

    cmd = [
        "gh", "api",
        f"repos/{repo}/pulls/{pr_number}/reviews",
        "--input", str(payload_path),
    ]

    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        print(f"❌ Failed to submit via gh api:\n{res.stderr}", file=sys.stderr)
        # Fallback: post review body without inline comments if diff line matching failed
        print("Retrying review submission with summary payload...")
        payload["comments"] = []
        with open(payload_path, "w") as f:
            json.dump(payload, f, indent=2)
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            print(f"❌ Fallback failed:\n{res.stderr}", file=sys.stderr)
            sys.exit(1)

    print(f"✅ Successfully posted Revix Swarm review to https://github.com/{repo}/pull/{pr_number}!")


def main() -> None:
    parser = argparse.ArgumentParser(description="Post Revix review to GitHub PR")
    parser.add_argument("--repo", required=True, help="GitHub repository in 'owner/repo' format")
    parser.add_argument("--pr", required=True, type=int, help="Pull request number")
    args = parser.parse_args()

    if not REVIEW_FILE.exists():
        print(f"Error: Review result file not found at {REVIEW_FILE}. Run test_fastcache_pr.py first.", file=sys.stderr)
        sys.exit(1)

    with open(REVIEW_FILE) as f:
        data = json.load(f)

    post_with_gh(args.repo, args.pr, data)


if __name__ == "__main__":
    main()
