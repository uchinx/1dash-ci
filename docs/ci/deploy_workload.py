#!/usr/bin/env python3
"""Deploy one v2 workload + poll its Deployment to terminal (MS-05 leg).

Called once per matrix leg with that leg's pushed digest. Posts
POST /api/ci/deploy/workload, then polls GET /api/ci/deployments/:id
every --poll-interval seconds until phase completed/failed/cancelled or
--poll-timeout expires (default 20 minutes, independent of the job's
build timeout).

Retry policy (plan §7):
- Retry with the SAME idempotency key on 409 ERR_IN_FLIGHT (bounded:
  --conflict-retries, --conflict-backoff) and on transient network/5xx
  errors. A retried POST is safe: same key+payload replays the original
  deployment instead of minting a duplicate.
- NEVER retry validation/ownership/type errors (400, 401, 403, 404,
  other 409 codes such as ERR_IDEMPOTENCY_CONFLICT, ERR_REPO_CONFLICT,
  ERR_CI_TYPE_FLIP): fail immediately with the server message.
- NEVER fire a job to "verify" the deployment: success for a job leg
  means the server committed the active image + effective trigger
  config (phase ready = "ready for runs"). The next scheduled run may
  be hours away and happens on the 1dash host, not here.

Auth: ONEDASH_TOKEN env (never logged). The CI credential is only an
HTTP Authorization header — it never becomes a build input.

Stdlib only (no pip install needed at deploy time).
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# 409 codes that are safe to retry with the same idempotency key: the
# server holds another admission for this target; the retry replays or
# joins it. Every other 4xx is a caller-fixable verdict.
RETRYABLE_409_CODES = {"ERR_IN_FLIGHT"}
RETRYABLE_409_CODES = {"ERR_IN_FLIGHT"}

# Substrings (lowercased) identifying an edge bot-protection block page
# (Cloudflare Bot Fight Mode / WAF block) as opposed to a server verdict.
EDGE_BLOCK_MARKERS = (
    "browser's signature",
    "attention required",
    "just a moment",
    "verify you are human",
    "cf-ray",
    "cloudflare",
)


def sniff_edge_block(raw_text):
    lowered = (raw_text or "").lower()
    return any(m in lowered for m in EDGE_BLOCK_MARKERS)


def fail(msg, result=None, result_path=None):
    if result is not None and result_path:
        write_result(result, result_path)
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


def write_result(result, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, sort_keys=True, indent=2)
    os.replace(tmp, path)


def api_call(method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Identifiable UA: the urllib default (Python-urllib/x.y) is
            # blocked by trivial edge rules; a real UA is also what an
            # allowlist exception matches on. Never a browser spoof.
            "User-Agent": "1dash-ci-leg (+https://github.com/uchinx/1dash-ci)",
            # NOTE: this URL names the mirror repo; the source of truth
            # names the private repo and the mirror publish rewrites it.
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode(errors="replace")
        except OSError:
            raw = ""
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"error": f"HTTP {e.code}"}
        if not isinstance(payload, dict) or sniff_edge_block(raw):
            # Edge bot protection (Cloudflare-style block page) sits in
            # front of the server: the request never reached admission.
            # Retrying is pointless — the operator must exempt the CI
            # API paths (WAF skip) or point ONEDASH_URL at an unproxied
            # origin hostname. Say so instead of a bare HTTP code.
            payload = {
                "error": (
                    f"HTTP {e.code}: blocked by edge bot protection in "
                    "front of the 1dash server (request never reached "
                    "admission) — add a WAF skip for /api/ci/* or point "
                    "ONEDASH_URL at an unproxied origin hostname"
                ),
                "edge_block": True,
            }
        return e.code, payload


def classify_post(status, payload):
    """Return 'ok' | 'retry' | 'fatal' for a deploy POST response."""
    if status == 200:
        return "ok"
    if status == 409:
        code = payload.get("code", "")
        if code in RETRYABLE_409_CODES or "in flight" in payload.get("error", "").lower():
            return "retry"
        return "fatal"
    if status in (400, 401, 403, 404):
        return "fatal"
    if 500 <= status <= 599:
        return "retry"
    return "fatal"


def server_message(payload):
    for key in ("error", "message", "detail"):
        if payload.get(key):
            code = payload.get("code", "")
            return f"{code}: {payload[key]}" if code else str(payload[key])
    return json.dumps(payload, sort_keys=True)


def parse_args(argv):
    p = argparse.ArgumentParser(description="deploy one v2 workload and poll to terminal")
    p.add_argument("--server", required=True, help="1dash base URL (https://host, no trailing path)")
    p.add_argument("--repo", required=True, help="canonical owner/repo binding")
    p.add_argument("--service", required=True, help="manifest service key for this leg")
    p.add_argument("--type", required=True, choices=("service", "job"), help="workload type (for result labeling)")
    p.add_argument("--workload-file", required=True, help="workload-<service>.json from parse_manifest.py")
    p.add_argument("--image", required=True, help="pushed image digest ref (name@sha256:...)")
    p.add_argument("--idempotency-key", required=True, help="run-attempt key (reused across HTTP retries)")
    p.add_argument("--poll-interval", type=float, default=5, help="seconds between polls")
    p.add_argument("--poll-timeout", type=float, default=1200, help="explicit poll deadline in seconds (default 20 min)")
    p.add_argument("--conflict-retries", type=int, default=5, help="bounded retries for in-flight conflicts")
    p.add_argument("--conflict-backoff", type=float, default=30, help="seconds between conflict retries")
    p.add_argument("--result-out", required=True, help="path for the per-leg result JSON artifact")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    token = os.environ.get("ONEDASH_TOKEN", "")
    if not token:
        fail("ONEDASH_TOKEN is required (org secret; never logged)", {}, args.result_out)
    try:
        with open(args.workload_file, "r", encoding="utf-8") as f:
            workload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        fail(f"cannot load workload file: {e}", {}, args.result_out)

    server = args.server.rstrip("/")
    body = {
        "repo": args.repo,
        "service": args.service,
        "workload": workload,
        "image": args.image,
        "idempotencyKey": args.idempotency_key,
    }
    result = {
        "service": args.service,
        "type": args.type,
        "image": args.image,
        "phase": "submit-failed",
        "deployment_id": "",
    }

    # --- submit (bounded retries, same key) ---
    deployment_id = ""
    attempts = 1 + max(0, args.conflict_retries)
    for attempt in range(1, attempts + 1):
        try:
            status, payload = api_call("POST", f"{server}/api/ci/deploy/workload", token, body)
        except OSError as e:
            # Transient network loss: safe to retry with the same key.
            if attempt < attempts:
                print(f"deploy POST network error ({e}); retry {attempt}/{attempts} (same key)")
                time.sleep(args.conflict_backoff)
                continue
            fail(f"deploy POST failed after {attempts} attempts: {e}", result, args.result_out)
        verdict = classify_post(status, payload)
        if verdict == "ok":
            try:
                deployment_id = payload["data"]["deployment"]["id"]
            except (KeyError, TypeError):
                fail(f"deploy response missing deployment id: {payload}", result, args.result_out)
            break
        if verdict == "retry":
            if attempt < attempts:
                print(
                    f"deploy deferred ({server_message(payload)}); "
                    f"retry {attempt}/{attempts} with the same idempotency key"
                )
                time.sleep(args.conflict_backoff)
                continue
            fail(
                f"deploy still deferred after {attempts} attempts: {server_message(payload)} "
                f"(deployment may exist server-side; check the dashboard)",
                result,
                args.result_out,
            )
        fail(
            f"deploy rejected ({server_message(payload)}): not retried — "
            "validation/ownership/type errors are never transient",
            result,
            args.result_out,
        )

    result["deployment_id"] = deployment_id
    deploy_url = f"{server}/api/apps (deployment {deployment_id})"
    print(f"submitted {args.service} {args.image} -> deployment {deployment_id}")

    # --- poll to terminal ---
    deadline = time.monotonic() + args.poll_timeout
    while True:
        try:
            status, payload = api_call(
                "GET", f"{server}/api/ci/deployments/{deployment_id}", token
            )
        except OSError as e:
            if time.monotonic() >= deadline:
                break
            print(f"poll network error ({e}); continuing until the deadline")
            time.sleep(args.poll_interval)
            continue
        if status != 200:
            fail(
                f"poll failed ({server_message(payload)}); "
                f"server work may still complete — see {deploy_url}",
                result,
                args.result_out,
            )
        try:
            phase = payload["data"]["phase"]
        except (KeyError, TypeError):
            fail(f"poll response missing phase: {payload}", result, args.result_out)
        # Server terminal-success phase is "completed" (deployments.go:
        # admitted → building → spawning → verifying → cutover →
        # draining → completed; terminal = completed/failed/cancelled).
        # "ready" is the RELEASE status, never a deployment phase —
        # accepted here only as an alias so this script also polls
        # correctly against any server that ever reported it.
        if phase in ("completed", "ready"):
            result["phase"] = "ready"
            if args.type == "job":
                result["note"] = "image + trigger config active; ready for runs (no run fired)"
            write_result(result, args.result_out)
            print(f"{args.service}: ready ({deploy_url})")
            return 0
        if phase in ("failed", "cancelled"):
            try:
                reason = (
                    payload["data"].get("failureReason")
                    or payload["data"].get("failure_reason")
                    or "unknown"
                )
            except (KeyError, AttributeError):
                reason = "unknown"
            result["phase"] = "failed"
            result["failure_reason"] = reason
            fail(f"{args.service}: deploy failed: {reason} ({deploy_url})", result, args.result_out)
        if time.monotonic() >= deadline:
            break
        time.sleep(args.poll_interval)

    result["phase"] = "timeout"
    fail(
        f"{args.service}: poll deadline ({args.poll_timeout:g}s) expired — "
        f"server work may still complete; see {deploy_url}",
        result,
        args.result_out,
    )


if __name__ == "__main__":
    main()
