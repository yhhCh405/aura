#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import sys
import time
from urllib.parse import quote, urlparse

import requests


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def log(msg: str, verbose: bool):
    if verbose:
        print(msg, flush=True)


def read_text_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def parse_mr_url(url: str):
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Invalid URL")
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path
    if path.endswith("/"):
        path = path[:-1]
    marker = "/-/merge_requests/"
    idx = path.find(marker)
    if idx == -1:
        raise ValueError("URL does not look like a GitLab merge request URL")
    project_path = path[1:idx]
    mr_iid = path[idx + len(marker):].split("/")[0]
    if not mr_iid.isdigit():
        raise ValueError("Merge request IID not found in URL")
    return base, project_path, int(mr_iid)


def http_json(session: requests.Session, method: str, url: str, **kwargs):
    resp = session.request(method, url, **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} for {url}: {resp.text[:500]}")
    if resp.text:
        return resp.json()
    return None


def get_project(session, api_base, project_path):
    encoded = quote(project_path, safe="")
    return http_json(session, "GET", f"{api_base}/projects/{encoded}")


def get_merge_request(session, api_base, project_id, mr_iid):
    return http_json(session, "GET", f"{api_base}/projects/{project_id}/merge_requests/{mr_iid}")


def get_diff_refs(session, api_base, project_id, mr_iid, mr_json):
    diff_refs = mr_json.get("diff_refs") or {}
    base_sha = diff_refs.get("base_sha")
    start_sha = diff_refs.get("start_sha")
    head_sha = diff_refs.get("head_sha")
    if base_sha and start_sha and head_sha:
        return base_sha, start_sha, head_sha
    versions = http_json(session, "GET", f"{api_base}/projects/{project_id}/merge_requests/{mr_iid}/versions")
    if not versions:
        raise RuntimeError("No diff versions available")
    # pick latest by created_at if present, else first
    def sort_key(v):
        return v.get("created_at", "")
    latest = sorted(versions, key=sort_key, reverse=True)[0]
    return latest["base_commit_sha"], latest["start_commit_sha"], latest["head_commit_sha"]


def get_changes(session, api_base, project_id, mr_iid):
    # Try changes endpoint first
    try:
        return http_json(session, "GET", f"{api_base}/projects/{project_id}/merge_requests/{mr_iid}/changes", params={"unidiff": True})
    except Exception:
        pass

    # Fallback: use latest version diffs
    versions = http_json(session, "GET", f"{api_base}/projects/{project_id}/merge_requests/{mr_iid}/versions")
    if not versions:
        raise RuntimeError("No diff versions available")
    def sort_key(v):
        return v.get("created_at", "")
    latest = sorted(versions, key=sort_key, reverse=True)[0]
    version_id = latest["id"]
    version = http_json(session, "GET", f"{api_base}/projects/{project_id}/merge_requests/{mr_iid}/versions/{version_id}", params={"unidiff": True})
    return {"changes": version.get("diffs", [])}


def list_discussions(session, api_base, project_id, mr_iid):
    discussions = []
    page = 1
    while True:
        resp = session.get(
            f"{api_base}/projects/{project_id}/merge_requests/{mr_iid}/discussions",
            params={"per_page": 100, "page": page},
            timeout=60,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} for discussions: {resp.text[:500]}")
        batch = resp.json() or []
        discussions.extend(batch)
        next_page = resp.headers.get("X-Next-Page")
        if not next_page:
            break
        page = int(next_page)
    return discussions


def has_marker_in_discussions(discussions, marker: str) -> bool:
    for d in discussions or []:
        notes = d.get("notes") or []
        for n in notes:
            body = (n.get("body") or "")
            if marker in body:
                return True
    return False


BOT_MARKER_PREFIX = "[//]: # (aura-review-bot"
# Regex to match the new 'aura' metadata AND the old 'wm-ollama' metadata
BOT_META_RE = re.compile(
    r"(?:<!--\s*(?:wm-ollama|aura)-review-bot\s+|\\?\[//\\?\]: # \((?:wm-ollama|aura)-review-bot\s+)"
    r"anchor=([0-9a-f]{40})\s+id=([0-9a-f]{40})(?:\s*-->|\))",
    re.IGNORECASE
)


def bot_anchor_id(path: str, line_type: str | None, line: int | None) -> str:
    # Stable identifier for a "location" so we can update-in-place across runs.
    key = f"{path}|{line_type or ''}|{line or 0}".encode("utf-8")
    return hashlib.sha1(key).hexdigest()


def bot_comment_id(path: str, line_type: str | None, line: int | None, body: str) -> str:
    # Stable fingerprint to dedupe across runs (location + normalized body).
    norm = (body or "").strip()
    key = f"{path}|{line_type or ''}|{line or 0}|{norm}".encode("utf-8")
    return hashlib.sha1(key).hexdigest()


def wrap_bot_body(path: str, line_type: str | None, line: int | None, body: str) -> tuple[str, str, str]:
    aid = bot_anchor_id(path, line_type, line)
    cid = bot_comment_id(path, line_type, line, body)
    # Use Markdown link style which is less likely to be stripped than HTML comments
    header = f"[//]: # (aura-review-bot anchor={aid} id={cid})"
    return aid, cid, f"{header}\n{body}\n\n---\n*Gracefully reviewed by Aura*"


def existing_bot_id_to_discussion_id(discussions) -> dict[str, str]:
    out: dict[str, str] = {}
    for d in discussions or []:
        did = d.get("id")
        if not did:
            continue
        notes = d.get("notes") or []
        for n in notes:
            body = (n.get("body") or "")
            m = BOT_META_RE.search(body)
            if m:
                out[m.group(2)] = did
    return out


def existing_bot_meta(discussions) -> dict[str, dict]:
    """
    Returns map[anchor_id] => {id, discussion_id, note_id, body}
    If multiple notes share an anchor, last one wins.
    """
    out: dict[str, dict] = {}
    for d in discussions or []:
        did = d.get("id")
        if not did:
            continue
        notes = d.get("notes") or []
        for n in notes:
            body = (n.get("body") or "")
            m = BOT_META_RE.search(body)
            if not m:
                continue
            aid, cid = m.group(1), m.group(2)
            nid = n.get("id")
            out[aid] = {"id": cid, "discussion_id": did, "note_id": nid, "body": body}
    
    for d in discussions or []:
        did = d.get("id")
        if not did:
            continue
        
        # Check position (top-level or on the first DiffNote)
        pos = d.get("position")
        notes = d.get("notes") or []
        if not pos and notes:
            for n in notes:
                if n.get("position"):
                    pos = n["position"]
                    break
        
        if not pos:
            continue

        path = pos.get("new_path") or pos.get("old_path")
        if not path:
            continue
        
        # Try to map position to our aid keys
        candidates = []
        if pos.get("new_line"):
            candidates.append(bot_anchor_id(path, "new", int(pos["new_line"])))
        if pos.get("old_line"):
            candidates.append(bot_anchor_id(path, "old", int(pos["old_line"])))
            
        for n in notes:
            body = (n.get("body") or "")
            # If we already matched this via regex, skip
            already_matched = False
            for aid in out:
                if out[aid].get("discussion_id") == did:
                    already_matched = True
                    break
            if already_matched:
                continue

            # Fallback: Does it look like a bot comment? 
            # User uses "Commented by bot" or "Generated by AI"
            is_bot = "wm-ollama-review-bot" in body or "Commented by bot" in body or "Generated by AI" in body

            if is_bot:
                for aid_guess in candidates:
                    if aid_guess not in out:
                        out[aid_guess] = {"id": "unknown", "discussion_id": did, "note_id": n.get("id"), "body": body, "is_fallback": True}
    return out


def guess_repo_type(project_path: str) -> str:
    # Heuristic only; can be overridden by --repo-type.
    name = (project_path or "").lower()
    known_packages = ("flutter-core", "inbox", "send-money-mini-app")
    if any(p in name for p in known_packages):
        return "package"
    return "standalone"


def mr_header_issues(mr: dict, repo_type: str, verbose: bool = False) -> list[str]:
    title = (mr.get("title") or "").strip()
    desc = (mr.get("description") or "").strip()
    
    if verbose:
        log(f"DEBUG: MR description (first 500 chars):\n{repr(desc[:500])}", True)

    issues: list[str] = []

    def has_any(keywords: list[str]) -> bool:
        # Check for keywords anywhere. Be very lenient.
        for k in keywords:
            if re.search(rf"(?im){re.escape(k)}", desc):
                return True
        return False

    # Badge/link requirement
    if "Flutter MR Standard" not in desc and "WM Flutter MR" not in desc:
        issues.append("Missing required MR header badge/link (WM Flutter MR Standard 1.0).")

    # Template presence (lightweight checks; avoid being too strict)
    # Check for "Jira" keyword or a Jira key pattern ECO-123 or just 'cts-'
    has_jira = has_any(["Jira", "Ticket", "JIRA", "CTS-"]) or re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", desc)
    has_peer_test = has_any(["Peer Test", "Testing Guide", "Manual Test", "Implementation Details"])
    # Match both - [ ] and * [ ] and rendered markup if possible
    has_checklist = has_any(["Checklist", "Steps to verify", "Done list"]) or re.search(r"(?im)^\s*[-*]\s*\[\s*[x ]\s*\]\s+", desc)

    if not has_jira:
        issues.append("Missing Jira section/details (e.g. `## Jira` or Ticket Link).")
    if not has_peer_test:
        issues.append("Missing peer test guide (e.g. `## Peer Test Guide`).")
    if not has_checklist:
        issues.append("Missing checklist (e.g. `## Checklist` or checkboxes).")

    if repo_type == "standalone":
        if not re.search(r"(?i)dep-push", desc):
            issues.append("Standalone MR: checklist should mention `dep-push`.")
    if repo_type == "package":
        if not re.search(r"(?i)compatible", desc):
            issues.append("Package MR: checklist should mention `Compatible with current standalone apps`.")
        if not (has_any(["Package", "Repository", "Repo"])):
            issues.append("Package MR: missing package section (e.g. `## Package`).")
        if "Version Impact" not in desc and not re.search(r"(?i)impact", desc):
            issues.append("Package MR: missing `Version Impact` selection.")

    # Help catch missing Jira key in title (only if not found in desc either)
    if not re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", f"{title}\n{desc}"):
        issues.append("MR title or description should include a Jira key like `ECO-123`.")

    return issues


def number_diff(diff_text: str):
    old_line = None
    new_line = None
    numbered = []
    valid_new = set()
    valid_old = set()
    new_type = {}
    old_type = {}
    context_new_to_old = {}
    context_old_to_new = {}

    for raw in diff_text.splitlines():
        line = raw
        if line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                old_line = int(m.group(1))
                new_line = int(m.group(3))
            numbered.append(line)
            continue

        if line.startswith("+++") or line.startswith("---") or line.startswith("diff --git"):
            numbered.append(line)
            continue

        if line.startswith("+"):
            if new_line is not None:
                numbered.append(f"N{new_line} | {line}")
                valid_new.add(new_line)
                new_type[new_line] = "added"
                new_line += 1
            else:
                numbered.append(line)
            continue

        if line.startswith("-"):
            if old_line is not None:
                numbered.append(f"O{old_line} | {line}")
                valid_old.add(old_line)
                old_type[old_line] = "removed"
                old_line += 1
            else:
                numbered.append(line)
            continue

        if line.startswith(" ") or line == "":
            if old_line is not None and new_line is not None:
                numbered.append(f"O{old_line} N{new_line} | {line}")
                valid_old.add(old_line)
                valid_new.add(new_line)
                new_type[new_line] = "context"
                old_type[old_line] = "context"
                context_new_to_old[new_line] = old_line
                context_old_to_new[old_line] = new_line
                old_line += 1
                new_line += 1
            else:
                numbered.append(line)
            continue

        numbered.append(line)

    return "\n".join(numbered), valid_new, valid_old, new_type, old_type, context_new_to_old, context_old_to_new


def call_ollama(ollama_host, model, messages, temperature=0.2):
    url = f"{ollama_host.rstrip('/')}/api/chat"
    schema = {
        "type": "object",
        "properties": {
            "notes": {"type": "string"},
            "comments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "line_type": {"type": "string", "enum": ["new", "old"]},
                        "line": {"type": "integer"},
                        "body": {"type": "string"}
                    },
                    "required": ["path", "line_type", "line", "body"],
                    "additionalProperties": False
                }
            }
        },
        "required": ["comments"],
        "additionalProperties": False
    }

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "format": schema,
        "options": {
            "temperature": temperature
        }
    }

    resp = requests.post(url, json=payload, timeout=600)
    if resp.status_code >= 400:
        raise RuntimeError(f"Ollama error {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    content = data.get("message", {}).get("content", "")
    if not content:
        return {"comments": []}
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # best-effort extraction
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(content[start:end + 1])
            except Exception:
                pass
        return {"comments": []}


def build_prompt(file_path, diff_numbered, meta):
    flags = f"new_file={meta.get('new_file')}, renamed_file={meta.get('renamed_file')}, deleted_file={meta.get('deleted_file')}, generated_file={meta.get('generated_file')}"
    return (
        f"File: {file_path}\n"
        f"Flags: {flags}\n\n"
        "Numbered diff (use N<line> for added/changed lines, O<line> for removed lines):\n"
        f"{diff_numbered}\n\n"
        "Return JSON only, matching the schema. If no issues, return {\"comments\": []}."
    )


def normalize_comments(raw_comments, file_path, valid_new, valid_old, new_type, old_type, ctx_new_to_old, ctx_old_to_new):
    out = []
    for c in raw_comments:
        if not isinstance(c, dict):
            continue
        path = c.get("path") or file_path
        line_type = c.get("line_type")
        line = c.get("line")
        body = c.get("body", "").strip()
        if not body or line_type not in ("new", "old"):
            continue
        if not isinstance(line, int):
            continue
        if line_type == "new":
            if line not in valid_new:
                continue
        if line_type == "old":
            if line not in valid_old:
                continue
        out.append({
            "path": path,
            "line_type": line_type,
            "line": line,
            "body": body,
            "line_is_context": (line_type == "new" and new_type.get(line) == "context") or (line_type == "old" and old_type.get(line) == "context"),
            "ctx_new_to_old": ctx_new_to_old,
            "ctx_old_to_new": ctx_old_to_new,
        })
    return out


def post_comment(session, api_base, project_id, mr_iid, comment, paths, diff_refs, dry_run=False):
    base_sha, start_sha, head_sha = diff_refs
    new_path, old_path = paths

    # Always wrap with a bot marker (anchor+id) to prevent duplicates and enable updates across runs.
    path = comment.get("path") or new_path or old_path or ""
    _, _, wrapped = wrap_bot_body(path, comment.get("line_type"), comment.get("line"), comment["body"])
    body = wrapped

    payload = {"body": body}

    if comment.get("line_type"):
        position = {
            "position_type": "text",
            "base_sha": base_sha,
            "start_sha": start_sha,
            "head_sha": head_sha,
            "new_path": new_path,
            "old_path": old_path,
        }
        line = comment["line"]
        if comment["line_type"] == "new":
            position["new_line"] = line
            if comment.get("line_is_context"):
                old_line = comment.get("ctx_new_to_old", {}).get(line)
                if old_line:
                    position["old_line"] = old_line
        else:
            position["old_line"] = line
            if comment.get("line_is_context"):
                new_line = comment.get("ctx_old_to_new", {}).get(line)
                if new_line:
                    position["new_line"] = new_line
        payload["position"] = position

    if dry_run:
        print(json.dumps({"would_post": payload}, indent=2))
        return None

    return http_json(session, "POST", f"{api_base}/projects/{project_id}/merge_requests/{mr_iid}/discussions", json=payload)


def resolve_discussion(session, api_base, project_id, mr_iid, discussion_id: str, resolved: bool, dry_run: bool):
    if dry_run:
        print(json.dumps({"would_resolve": {"discussion_id": discussion_id, "resolved": resolved}}, indent=2))
        return None
    # GitLab Docs: PUT /projects/:id/merge_requests/:merge_request_iid/discussions/:discussion_id with resolved boolean
    resp = session.put(
        f"{api_base}/projects/{project_id}/merge_requests/{mr_iid}/discussions/{discussion_id}",
        data={"resolved": "true" if resolved else "false"},
        timeout=60,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} resolving discussion {discussion_id}: {resp.text[:500]}")
    return resp.json()


def update_note_body(session, api_base, project_id, mr_iid, discussion_id: str | None, note_id: int, body: str, dry_run: bool):
    if dry_run:
        print(json.dumps({"would_update_note": {"discussion_id": discussion_id, "note_id": note_id, "body": body}}, indent=2))
        return None
    # Try discussion note endpoint first (more specific).
    if discussion_id:
        url = f"{api_base}/projects/{project_id}/merge_requests/{mr_iid}/discussions/{discussion_id}/notes/{note_id}"
        resp = session.put(url, data={"body": body}, timeout=60)
        if resp.status_code < 400:
            return resp.json()
    # Fallback: MR notes endpoint.
    url2 = f"{api_base}/projects/{project_id}/merge_requests/{mr_iid}/notes/{note_id}"
    resp2 = session.put(url2, data={"body": body}, timeout=60)
    if resp2.status_code >= 400:
        raise RuntimeError(f"HTTP {resp2.status_code} updating note {note_id}: {resp2.text[:500]}")
    return resp2.json()


def main():
    parser = argparse.ArgumentParser(description="GitLab MR code review bot using local Ollama")
    parser.add_argument("mr_url", nargs="?", default=os.getenv("MR_URL"), help="Merge request URL")
    parser.add_argument("--token", default=os.getenv("GITLAB_TOKEN"), help="GitLab PAT (or set GITLAB_TOKEN)")
    parser.add_argument("--model", default=os.getenv("OLLAMA_MODEL", "qwen2.5:7b-16k"), help="Ollama model")
    parser.add_argument("--ollama-host", default=os.getenv("OLLAMA_HOST", "http://localhost:11434"), help="Ollama host")
    parser.add_argument(
        "--rules-file",
        default=os.getenv("MR_RULES_FILE", os.path.join(os.path.dirname(__file__), "rules", "mr_rules_v1.md")),
        help="Path to MR rules markdown to include in the prompt",
    )
    parser.add_argument(
        "--repo-type",
        choices=["auto", "standalone", "package"],
        default=os.getenv("REPO_TYPE", "auto"),
        help="Review rules to enforce (auto guesses from repo name)",
    )
    parser.add_argument("--max-files", type=int, default=200)
    parser.add_argument("--max-comments-per-file", type=int, default=20)
    parser.add_argument("--max-comments-total", type=int, default=200)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--verbose", action="store_true", default=os.getenv("VERBOSE", "false").lower() == "true", help="Print progress logs")
    parser.add_argument("--show-model-notes", action="store_true", default=os.getenv("SHOW_MODEL_NOTES", "false").lower() == "true", help="Print model 'notes' field if returned")
    parser.add_argument(
        "--show-prompts",
        action="store_true",
        help="Print per-file prompt headers (never prints token). Can be noisy.",
    )
    parser.add_argument(
        "--resolve-fixed",
        action="store_true",
        default=os.getenv("RESOLVE_FIXED", "false").lower() == "true",
        help="Resolve previous bot discussions that are no longer flagged on this run",
    )
    parser.add_argument(
        "--update-existing",
        action="store_true",
        default=os.getenv("UPDATE_EXISTING", "true").lower() == "true",
        help="Update existing bot comments in-place when the suggestion changes (default: on)",
    )
    parser.add_argument(
        "--no-update-existing",
        action="store_false",
        dest="update_existing",
        help="Disable in-place updates; post a new comment if the suggestion text changes",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification")
    args = parser.parse_args()

    if not args.token:
        eprint("Missing GitLab token. Set GITLAB_TOKEN or use --token.")
        sys.exit(1)

    if not args.mr_url:
        eprint("Missing Merge Request URL. Provide it as an argument or set MR_URL environment variable.")
        sys.exit(1)

    base, project_path, mr_iid = parse_mr_url(args.mr_url)
    api_base = f"{base}/api/v4"
    log(f"[1/5] Parsed MR URL. Host={base} Project={project_path} MR IID={mr_iid}", args.verbose)

    session = requests.Session()
    session.headers.update({"PRIVATE-TOKEN": args.token})
    if args.insecure:
        session.verify = False

    log("[2/5] Fetching project + MR metadata from GitLab...", args.verbose)
    project = get_project(session, api_base, project_path)
    project_id = project["id"]

    mr = get_merge_request(session, api_base, project_id, mr_iid)
    diff_refs = get_diff_refs(session, api_base, project_id, mr_iid, mr)

    log("[3/5] Fetching MR diffs...", args.verbose)
    changes_resp = get_changes(session, api_base, project_id, mr_iid)
    changes = changes_resp.get("changes", [])

    if not changes:
        eprint("No changes found in MR.")
        return

    total_posted = 0

    rules_text = read_text_file(args.rules_file).strip()
    repo_type = args.repo_type
    if repo_type == "auto":
        repo_type = guess_repo_type(project_path)

    log(f"[4/5] Loaded rules file: {args.rules_file} ({len(rules_text)} chars). Repo type={repo_type}.", args.verbose)
    log(f"[4/5] Using Ollama: host={args.ollama_host} model={args.model}", args.verbose)

    system_msg = (
        "You are Aura, an elegant and insightful code review bot for a Flutter/Dart codebase. "
        "Focus on correctness, bugs, security, performance, and maintainability. "
        "Avoid style-only nitpicks. Be concise, actionable, and charming. "
        "Only comment when there is a clear issue or improvement.\n\n"
        f"Repo type: {repo_type}\n\n"
        "MR Rules (follow and enforce these when applicable):\n"
        f"{rules_text if rules_text else '(no rules file loaded)'}"
    )

    try:
        discussions = list_discussions(session, api_base, project_id, mr_iid)
    except Exception:
        discussions = []

    bot_meta_by_anchor = existing_bot_meta(discussions)
    bot_ids = {v["id"] for v in bot_meta_by_anchor.values() if v.get("id")}
    # Track what the bot thinks is still actionable in this run.
    actionable_ids_this_run: set[str] = set()
    actionable_anchors_this_run: set[str] = set()

    # MR description/header enforcement comment (create or update).
    issues = mr_header_issues(mr, repo_type, verbose=args.verbose)
    header_path = "__mr_header__"
    header_line_type = None
    header_line = None
    header_anchor = bot_anchor_id(header_path, header_line_type, header_line)
    if issues:
        header_body = (
            "MR header/checklist looks incomplete vs MR Rules v1.0.\n\n"
            + "\n".join(f"- {x}" for x in issues)
            + "\n\n"
            "Please update the MR description using the template."
        )
        # Wrap with anchor/id too so it can be updated on rerun.
        _, header_cid, header_warning_wrapped = wrap_bot_body(header_path, header_line_type, header_line, header_body)
        existing = bot_meta_by_anchor.get(header_anchor)
        try:
            if existing and args.update_existing and existing.get("note_id") is not None:
                # Only update if it actually changed.
                if (existing.get("body") or "").strip() != header_warning_wrapped.strip():
                    log("[5/5] Updating existing MR header/template comment...", args.verbose)
                    update_note_body(
                        session,
                        api_base,
                        project_id,
                        mr_iid,
                        existing.get("discussion_id"),
                        int(existing["note_id"]),
                        header_warning_wrapped,
                        dry_run=args.dry_run,
                    )
                    bot_ids.add(header_cid)
            elif not existing:
                log("[5/5] Posting MR header/template enforcement comment...", args.verbose)
                post_comment(
                    session,
                    api_base,
                    project_id,
                    mr_iid,
                    {"body": header_body, "path": header_path},
                    (changes[0].get("new_path") or "", changes[0].get("old_path") or ""),
                    diff_refs,
                    dry_run=args.dry_run,
                )
                total_posted += 1
                bot_ids.add(header_cid)
            
            # CRITICAL: Always mark as actionable so it doesn't get resolved by --resolve-fixed.
            actionable_anchors_this_run.add(header_anchor)
            actionable_ids_this_run.add(header_cid)
        except Exception as ex:
            eprint(f"Failed to upsert MR header comment: {ex}")

    log(f"[5/5] Starting per-file review. Files with diffs={len(changes)}", args.verbose)

    for i, ch in enumerate(changes):
        if i >= args.max_files:
            break
        file_path = ch.get("new_path") or ch.get("old_path") or ""
        diff = ch.get("diff") or ""
        if not diff.strip():
            continue

        log(f"\n[{i+1}/{min(len(changes), args.max_files)}] Reviewing {file_path} (diff chars={len(diff)})", args.verbose)
        numbered_diff, valid_new, valid_old, new_type, old_type, ctx_new_to_old, ctx_old_to_new = number_diff(diff)

        user_msg = build_prompt(file_path, numbered_diff, ch)
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]

        if args.show_prompts:
            # Print only high-level prompt info (diff is already visible in MR; still can be large/noisy).
            log(f"Prompt header: File={file_path} RulesChars={len(rules_text)} NumberedDiffChars={len(numbered_diff)}", True)

        log("Calling Ollama for review...", args.verbose)
        t0 = time.time()
        resp = call_ollama(args.ollama_host, args.model, messages, temperature=0.0)
        dt = time.time() - t0
        log(f"Ollama responded in {dt:.2f}s", args.verbose)
        raw_comments = resp.get("comments", []) if isinstance(resp, dict) else []
        if args.show_model_notes and isinstance(resp, dict):
            notes = (resp.get("notes") or "").strip()
            if notes:
                log("Model notes:\n" + notes, True)

        comments = normalize_comments(
            raw_comments, file_path, valid_new, valid_old, new_type, old_type, ctx_new_to_old, ctx_old_to_new
        )
        if not comments:
            log("No actionable inline comments for this file.", args.verbose)
            continue

        for c0 in comments:
            p0 = c0.get("path") or file_path
            lt0 = c0.get("line_type")
            ln0 = c0.get("line")
            actionable_ids_this_run.add(bot_comment_id(p0, lt0, ln0, c0.get("body", "")))
            actionable_anchors_this_run.add(bot_anchor_id(p0, lt0, ln0))

        comments = comments[: args.max_comments_per_file]
        log(f"Prepared {len(comments)} inline comments to post (after filtering/cap).", args.verbose)

        for c in comments:
            if total_posted >= args.max_comments_total:
                break
            new_path = ch.get("new_path") or ch.get("old_path") or file_path
            old_path = ch.get("old_path") or ch.get("new_path") or file_path
            try:
                # Deduplicate across runs.
                p = c.get("path") or file_path
                lt = c.get("line_type")
                ln = c.get("line")
                cid = bot_comment_id(p, lt, ln, c.get("body", ""))
                aid = bot_anchor_id(p, lt, ln)

                if cid in bot_ids:
                    log(f"Skipping duplicate comment id={cid} at {lt}:{ln}", args.verbose)
                    continue

                existing = bot_meta_by_anchor.get(aid)
                if existing:
                    # Threading Logic:
                    # 1. If existing comment has SAME content (via cid), do nothing (skip).
                    # 2. If existing comment has DIFFERENT content, add a REPLY to the thread.
                    # 3. Do NOT overwrite unless we are strictly in "update-in-place" mode and it's confirmed to be the SAME issue.
                    #    But "update-in-place" on a shifted line is dangerous.
                    
                    prev_body = (existing.get("body") or "").strip()
                    # Check if the new body is already in the thread (scan all notes in that discussion?)
                    # For now, simplistic check against the mapped note.
                    
                    # If we have a robust ID match (cid), use it.
                    if existing.get("id") == cid:
                         log(f"Skipping identical comment id={cid} at {lt}:{ln}", args.verbose)
                         bot_ids.add(cid)
                         continue
                         
                    # If fallback or ID mismatch, check text similarity to avoid spam
                    # Remove footer/header and normalize for comparison
                    def clean_body(b):
                         b = re.sub(BOT_META_RE, "", b)
                         b = b.replace("*Generated by AI*", "").replace("*Commented by bot*", "").strip()
                         b = b.replace("Generated by AI", "").replace("Commented by bot", "").strip()
                         b = b.replace("*Gracefully reviewed by Aura*", "").replace("Gracefully reviewed by Aura", "").strip()
                         # Extremely aggressive normalization: remove all non-alphanumeric and lowercase
                         return re.sub(r'[^a-zA-Z0-9]', '', b).lower()
                    
                    new_clean = clean_body(c.get("body", ""))
                    
                    # Check ALL notes in this discussion for a semantically similar comment
                    discussion_raw = next((d for d in discussions if d.get("id") == existing["discussion_id"]), {})
                    all_notes = discussion_raw.get("notes") or []
                    already_said = False
                    for note in all_notes:
                        note_clean = clean_body(note.get("body") or "")
                        if not new_clean or not note_clean: continue
                        if new_clean in note_clean or note_clean in new_clean:
                            already_said = True
                            break
                    
                    if already_said:
                         log(f"Skipping duplicate suggestion (found in thread) at {p} {lt}:{ln}", args.verbose)
                         bot_ids.add(cid)
                         continue

                    # Calculate the CORRECT wrapped body for this code comment. 
                    # Use a unique variable name to avoid collision with header_warning_wrapped.
                    _, _, comment_wrapped = wrap_bot_body(p, lt, ln, c.get("body", ""))

                    # If different, REPLY to the thread
                    log(f"Posting reply to existing discussion at {p} {lt}:{ln}", args.verbose)
                    
                    # We need to post a new NOTE to the discussion.
                    http_json(
                        session, 
                        "POST", 
                        f"{api_base}/projects/{project_id}/merge_requests/{mr_iid}/discussions/{existing['discussion_id']}/notes", 
                        json={"body": comment_wrapped}
                    )
                    
                    bot_ids.add(cid)
                    total_posted += 1
                    continue
                    
                log(
                    f"Posting comment: {c.get('path', file_path)} {c.get('line_type')}:{c.get('line')}",
                    args.verbose,
                )
                post_comment(
                    session,
                    api_base,
                    project_id,
                    mr_iid,
                    c,
                    (new_path, old_path),
                    diff_refs,
                    dry_run=args.dry_run,
                )
                bot_ids.add(cid)
                total_posted += 1
                time.sleep(0.2)
            except Exception as ex:
                eprint(f"Failed to post comment on {file_path}:{c.get('line')}: {ex}")

        if total_posted >= args.max_comments_total:
            break

    if args.resolve_fixed and bot_ids:
        # Prefer anchor-based resolution so edits don't break the logic.
        fixed_anchors = set(bot_meta_by_anchor.keys()) - actionable_anchors_this_run
        if fixed_anchors:
            log(
                f"Resolving {len(fixed_anchors)} previously-posted bot discussion(s) not flagged this run...",
                args.verbose,
            )
        for aid in sorted(fixed_anchors):
            meta = bot_meta_by_anchor.get(aid) or {}
            did = meta.get("discussion_id")
            if not did:
                continue
            try:
                log(f"Resolving fixed/obsolete bot discussion {did} at anchor {aid}", args.verbose)
                resolve_discussion(session, api_base, project_id, mr_iid, did, True, dry_run=args.dry_run)
            except Exception as ex:
                eprint(f"Failed to resolve discussion {did} for anchor={aid}: {ex}")

    print(f"Done. Posted {total_posted} comments.")


if __name__ == "__main__":
    main()
