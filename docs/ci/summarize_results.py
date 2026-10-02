#!/usr/bin/env python3
"""Render the MS-05 always-run summary from per-leg result artifacts.

Reads every *.json under --results-dir (one per matrix leg, written by
docs/ci/deploy_workload.py or the stale-skip path), appends a markdown
table to --summary-out (the workflow points it at $GITHUB_STEP_SUMMARY),
and exits nonzero when any leg failed, timed out, went stale, or never
reported — partial failure fails overall CI without rolling back
sibling Apps. Job legs that reached ready are labeled "ready for runs".

Stdlib only.
"""

import argparse
import glob
import json
import os
import sys

OK_PHASES = {"ready", "completed"}


def load_results(results_dir):
    paths = sorted(glob.glob(os.path.join(results_dir, "*.json")))
    results, broken = [], []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                results.append((os.path.basename(path), json.load(f)))
        except (OSError, json.JSONDecodeError) as e:
            broken.append((os.path.basename(path), str(e)))
    return results, broken


def short_image(image):
    if "@sha256:" in image:
        name, digest = image.split("@sha256:", 1)
        return f"{name}@sha256:{digest[:12]}"
    return image


def render(results, broken, expected):
    lines = [
        "## 1dash ship summary",
        "",
        "| workload | kind | outcome | image | deployment | detail |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    failed = list(broken)
    seen = set()
    for _, res in results:
        service = res.get("service", "?")
        seen.add(service)
        kind = res.get("type", "?")
        phase = res.get("phase", "missing")
        image = short_image(res.get("image", ""))
        deployment = res.get("deployment_id", "")
        if phase in OK_PHASES:
            outcome = "ready" if kind != "job" else "ready for runs"
            detail = res.get("note", "")
        elif phase == "failed":
            outcome = "failed"
            detail = res.get("failure_reason", "")
            failed.append((service, phase))
        elif phase == "timeout":
            outcome = "timed out (server work may still complete)"
            detail = deployment
            failed.append((service, phase))
        elif phase == "skipped-stale":
            outcome = "skipped (stale — superseded, nothing submitted)"
            detail = res.get("note", "")
            failed.append((service, phase))
        else:
            outcome = f"failed ({phase})"
            detail = res.get("failure_reason", res.get("note", ""))
            failed.append((service, phase))
        lines.append(
            f"| {service} | {kind} | {outcome} | `{image}` | `{deployment}` | {detail} |"
        )
    for name in sorted(set(expected or []) - seen):
        lines.append(f"| {name} | ? | failed (no result reported) | `` | `` | leg never finished |")
        failed.append((name, "missing"))
    for name, err in broken:
        lines.append(f"| {name} | ? | failed (unreadable result) | `` | `` | {err} |")
    lines.append("")
    if failed:
        names = ", ".join(sorted(s for s, _ in failed))
        lines.append(
            f"**Ship failed for: {names}.** Successful siblings stay deployed "
            "(no cross-workload rollback)."
        )
    else:
        lines.append("All workloads shipped.")
    lines.append("")
    return "\n".join(lines), failed


def parse_args(argv):
    p = argparse.ArgumentParser(description="render the MS-05 ship summary")
    p.add_argument("--results-dir", required=True)
    p.add_argument("--summary-out", required=True, help="file to append markdown to")
    p.add_argument("--expected", default="", help="comma-separated services the matrix scheduled")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    results, broken = load_results(args.results_dir)
    expected = [s.strip() for s in args.expected.split(",") if s.strip()]
    markdown, failed = render(results, broken, expected)
    with open(args.summary_out, "a", encoding="utf-8") as f:
        f.write(markdown)
    print(markdown)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
