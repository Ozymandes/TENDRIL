#!/usr/bin/env python3
"""D11 LIVE-freshness certification (host side).

Drives ONE Herdr workspace ("tendril-fresh" by default) through a full
subagent lifecycle and prints per-stage wall-clock latencies measured with
THE WORKTREE'S OWN CODE (never the installed runtime):

  stage A  pi-subagents child transcript appears on disk
  stage B  `herdr api snapshot` reflects the parent state change
           (extension -> herdr; parent turns only — async children are
           invisible to herdr by design, which is WHY the fast cadence
           and the SYNC event exist)
  stage C  `remote-agents snapshot --json` (worktree) carries the child,
           its active states, and its terminal `done`
  stage D  the app-equivalent poll: one full worktree snapshot generation,
           timed per call (p50/max), plus the etag not-modified reply cost

The phone-side stages (Termux RUN_COMMAND dispatch, SSH transport, Android
repository cadence) cannot be measured from the host; LATENCY.md budgets a
warm-socket round trip at ~0.1-0.4 s on top of stage D. The physical-phone
certification adds those numbers.

Usage:
    python3 tests/certify_freshness.py [--workspace-label tendril-fresh]
                                       [--keep]      # keep the workspace
Requires: herdr CLI, a pi agent kind, the pi-subagents extension installed
for pi sessions, and the worktree's bin/remote-agents.

The script creates its OWN workspace (never touches other workspaces),
starts one pi agent in it, and removes the workspace again unless --keep.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REMOTE_AGENTS = os.path.join(ROOT, "bin", "remote-agents")


def ms():
    return time.time() * 1000.0


def sh(args, timeout=15):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def herdr(*args, timeout=15):
    return sh(["herdr", *args], timeout=timeout)


def parent_session_dirs():
    """Directories that hold pi session transcripts for ANY cwd."""
    base = os.path.expanduser("~/.pi/agent/sessions")
    return base


def child_transcripts():
    """Every pi-subagents child transcript currently on disk."""
    base = parent_session_dirs()
    out = set(glob.glob(os.path.join(base, "*", "*", "run-*", "*",
                                     "session.jsonl")))
    out |= set(glob.glob(os.path.join(base, "*", "*", "forks", "*.jsonl")))
    return out


class Certify:
    def __init__(self, label, keep):
        self.label = label
        self.keep = keep
        self.workspace_id = None
        self.pane_id = None

    # ---- herdr lifecycle ------------------------------------------------

    def setup(self):
        r = herdr("workspace", "create", "--cwd", os.path.join(ROOT, "tests"),
                  "--label", self.label, "--no-focus")
        if r.returncode != 0:
            sys.exit(f"workspace create failed: {r.stderr.strip()[:200]}")
        created = json.loads(r.stdout)["result"]
        self.workspace_id = created["workspace"]["workspace_id"]
        self.pane_id = created["root_pane"]["pane_id"]
        r = herdr("agent", "start", "freshness-pi", "--kind", "pi",
                  "--pane", self.pane_id)
        if r.returncode != 0:
            self.teardown()
            sys.exit(f"agent start failed: {r.stderr.strip()[:200]}")
        # let the pi TUI settle to idle
        time.sleep(5)

    def teardown(self):
        if self.workspace_id and not self.keep:
            herdr("workspace", "close", self.workspace_id, timeout=30)

    # ---- observation ----------------------------------------------------

    def herdr_parent_status(self):
        r = herdr("api", "snapshot", timeout=8)
        if r.returncode != 0:
            return None
        try:
            snap = json.loads(r.stdout)["result"]["snapshot"]
        except Exception:
            return None
        for a in snap.get("agents", []):
            if a.get("pane_id") == self.pane_id:
                return a.get("agent_status")
        return None

    def wt_children(self):
        r = sh([REMOTE_AGENTS, "snapshot", "--json"], timeout=15)
        wall = None
        if r.returncode != 0:
            return None, None
        try:
            doc = json.loads(r.stdout)
        except Exception:
            return None, None
        for w in doc.get("workspaces", []):
            for a in w.get("agents", []):
                if a.get("pane_id") == self.pane_id:
                    return a.get("children") or [], doc.get("etag")
        return [], doc.get("etag")

    # ---- the lifecycle --------------------------------------------------

    def run(self):
        t0 = ms()
        stages = {}
        prompt = (
            "Use the subagent tool: launch ONE general-purpose subagent in "
            "the background (async:true) with exactly this task: run this "
            "bash command: echo started > /tmp/tendril-fresh-probe.txt && "
            "sleep 10 && echo done >> /tmp/tendril-fresh-probe.txt . Do not "
            "wait for it. Reply with one word: launched."
        )
        if os.path.exists("/tmp/tendril-fresh-probe.txt"):
            os.unlink("/tmp/tendril-fresh-probe.txt")
        known = child_transcripts()

        r = herdr("agent", "prompt", self.pane_id, prompt)
        if r.returncode != 0:
            sys.exit(f"prompt failed: {r.stderr.strip()[:200]}")
        stages["prompt_sent"] = ms() - t0

        child_file = None
        walls = []
        b_seen = c_seen = c2_seen = c3_seen = False
        deadline = ms() + 180_000
        while ms() < deadline:
            now = ms()
            # A: child transcript on disk
            if child_file is None:
                new = child_transcripts() - known
                if new:
                    child_file = min(new, key=lambda f: os.stat(f).st_mtime)
                    stages["A_child_transcript_s"] = round((now - t0) / 1000, 2)
            # B: herdr reflects the parent working (the extension's report)
            if not b_seen:
                st = self.herdr_parent_status()
                if st and st not in ("idle", "unknown"):
                    b_seen = True
                    stages["B_herdr_parent_working_s"] = round((now - t0) / 1000, 2)
            # C: the worktree snapshot carries the child
            kids, etag = self.wt_children()
            if kids:
                live = [k for k in kids
                        if k.get("status") in ("working", "thinking", "tool")]
                done = [k for k in kids if k.get("status") in ("done", "failed")]
                if live and not c2_seen:
                    c2_seen = True
                    stages["C_child_active_in_snapshot_s"] = round((now - t0) / 1000, 2)
                if done and not c3_seen:
                    c3_seen = True
                    stages["C_child_done_in_snapshot_s"] = round((now - t0) / 1000, 2)
                if (child_file or c2_seen) and not c_seen:
                    c_seen = True
                    stages["C_child_in_snapshot_s"] = round((now - t0) / 1000, 2)
            if c3_seen and self.herdr_parent_status() in ("idle", "done"):
                stages["D_settled_s"] = round((now - t0) / 1000, 2)
                break
            time.sleep(0.2)

        # D: snapshot generation cost + the etag not-modified reply
        t = ms()
        kids, etag = self.wt_children()
        stages["D_snapshot_full_ms"] = round(ms() - t, 1)
        stages["D_snapshot_bytes"] = len(
            sh([REMOTE_AGENTS, "snapshot", "--json"], timeout=15).stdout or "")
        if etag:
            t = ms()
            r = sh([REMOTE_AGENTS, "snapshot", "--if-none-match", etag],
                   timeout=15)
            stages["D_snapshot_notmodified_ms"] = round(ms() - t, 1)
            stages["D_snapshot_notmodified_bytes"] = len(r.stdout or "")
            stages["D_snapshot_notmodified_held"] = (
                json.loads(r.stdout).get("not_modified", False)
                if r.stdout else None)
        return stages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace-label", default="tendril-fresh")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    cert = Certify(args.workspace_label, args.keep)
    try:
        cert.setup()
        out = cert.run()
        print(json.dumps(out, indent=1))
    finally:
        cert.teardown()


if __name__ == "__main__":
    main()
