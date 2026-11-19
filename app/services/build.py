import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from app.config import dirs, slug
from app.services import events
from app.services import gh_sync, git, github, manifest, models, sources
from app.services.adapters import team_reviewer
from app.services.insights import load as load_insights
from app.services.shell import FAILURES, run, why
from app.services.state import load_state
from app.ui.pages import queue_page, review_page


def user_name(cfg):
    if cfg.get("user_name"):
        return cfg["user_name"]
    for cmd in (["git", "config", "user.name"], ["gh", "api", "user", "-q", ".name // .login"]):
        try:
            name = run(*cmd).split()
            if name:
                return name[0]
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    return "there"


def fetch_diff(pr):
    if git.has_clone(pr["source"]):
        try:
            return git.fetch_diff(pr)
        except FAILURES as e:
            print(f"  {pr['repo']}#{pr['number']}: git diff failed, using the API diff: {why(e)}", file=sys.stderr)
    return github.fetch_api_diff(pr)


def fetch_diffs(prs):
    diffs = {}
    for pr in prs:
        try:
            diffs[pr["repo"], pr["number"]] = fetch_diff(pr)
        except FAILURES as e:
            print(f"  {pr['repo']}#{pr['number']}: failed: {why(e)}", file=sys.stderr)
    return diffs


def build_pr(cfg, pr, diff_text, name):
    d = dirs(cfg)
    s = slug(pr["repo"], pr["number"])
    ins, status = load_insights(cfg, pr["repo"], pr["number"], pr["headRefOid"])
    model = models.review_model(cfg, pr, diff_text, ins, status, name)
    summary = models.pr_summary(model, status)
    (d["pages"] / f"{s}.html").write_text(review_page.render(model))
    (d["pages"] / f"{s}.json").write_text(json.dumps(summary))
    (d["diffs"] / f"{s}.diff").write_text(diff_text)
    return summary


def queue_items(cfg, entries):
    d = dirs(cfg)
    items = []
    for e in entries:
        try:
            summary = json.loads((d["pages"] / f"{e['slug']}.json").read_text())
        except (OSError, ValueError):
            print(f"  {e['slug']}: no page summary; left out of the queue page", file=sys.stderr)
            continue
        items.append(models.queue_item(summary, e["requested_at"], load_state(cfg, e["slug"])))
    return items


IN_PROGRESS_DAYS = 3


def in_progress(cfg, previous, listed):
    """(repo, number) of earlier PRs the search no longer finds that I'm still reviewing: I wrote in a thread
    in the last few days and haven't approved. Posting a comment clears my review request on GitHub."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=IN_PROGRESS_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    keep = []
    for e in previous.values():
        if (e["repo"], e["pr"]) in listed:
            continue
        data = load_state(cfg, e["slug"])
        mine = [m["at"] for t in data.get("threads", []) for m in t["messages"] if m.get("who") == "me"]
        if not data.get("approval") and any(at >= cutoff for at in mine):
            keep.append((e["repo"], e["pr"]))
    return keep


def build(cfg, only=None, source="cli"):
    d = dirs(cfg)
    clones = {}
    prs = []
    previous = {e["slug"]: e for e in manifest.read_manifest(cfg)["prs"] if "slug" in e}
    failed = []
    pending = github.list_pending(cfg, only, failed)
    merged = dict(previous) if only else {}
    if not only:
        # A failed listing (offline after sleep, rate limit) keeps that source's PRs as they were.
        merged.update({s: e for s, e in previous.items() if any(sources.covers(src, e["repo"]) for src in failed)})
        extra = in_progress(cfg, previous, {(p["repo"], p["number"]) for p in pending})
        if extra:
            pending += [p for p in github.list_pending(cfg, extra) if p.get("state", "OPEN") == "OPEN"]
    for pr in pending:
        if pr["repo"] not in clones:
            clones[pr["repo"]] = git.ensure_clone(cfg, pr["source"])
        prs.append(dict(pr, source=clones[pr["repo"]]))
    name = user_name(cfg)
    diffs = fetch_diffs(prs)
    for pr in prs:
        s = slug(pr["repo"], pr["number"])
        if s in previous:
            merged[s] = previous[s]
    ok = [pr for pr in prs if (pr["repo"], pr["number"]) in diffs]
    team_reviewer.sync_briefs(cfg, ok)
    for pr in ok:
        try:
            summary = build_pr(cfg, pr, diffs[pr["repo"], pr["number"]], name)
            try:
                gh_sync.sync(cfg, {"slug": summary["slug"], "repo": pr["repo"], "pr": pr["number"]})
            except Exception as e:
                print(f"  {pr['repo']}#{pr['number']}: comment sync failed: {why(e)}", file=sys.stderr)
            state = load_state(cfg, summary["slug"])
            requested = github.requested_at(cfg, pr)
            merged[summary["slug"]] = manifest.build_entry(cfg, summary, requested, state)
        except Exception as e:
            print(f"  {pr['repo']}#{pr['number']}: build failed: {e}", file=sys.stderr)
            continue
        print(f"  {pr['repo']}#{pr['number']}: {summary['files']} files, insights {summary['insights_status']}")
    entries = list(merged.values())
    (d["pages"] / "index.html").write_text(queue_page.render(models.queue_model(queue_items(cfg, entries), name)))
    written = manifest.write_manifest(cfg, entries)
    events.append(cfg, events.rebuild_event(previous, entries, source, partial=bool(only)))
    return written
