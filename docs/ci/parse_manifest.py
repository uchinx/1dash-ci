#!/usr/bin/env python3
"""Parse + validate a v2 1dash.yaml manifest for the MS-05 matrix.

Reads the caller-repo manifest, rejects duplicate YAML keys at every
level (yaml.safe_load would silently keep the last one), validates the
full document — nested fields, not just top-level keys — against
docs/1dash-schema.json, checks each workload's build paths against the
checked-out repo root (absolute / traversal / remote / symlink-escape /
missing), and emits the GitHub matrix plus one workload JSON file per
selected service for the deploy leg to POST verbatim.

Config travels via files/argv, never shell interpolation: the workflow
passes --manifest/--schema/--repo/--out-dir as arguments and consumes
matrix.json + workload-<service>.json from --out-dir.

Exit 0 on success; exit 2 with a `::error::` annotation on any
validation failure (the workflow maps this to a failed check).
Requires: pyyaml, jsonschema (pinned; installed by the workflow).
"""

import argparse
import json
import os
import re
import sys

SERVICE_KEY_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def fail(msg):
    print(f"::error::1dash.yaml: {msg}", file=sys.stderr)
    sys.exit(2)


# --- duplicate-key YAML loading -------------------------------------------

class _DupCheckLoader(__import__("yaml").SafeLoader):
    pass


def _no_dup_constructor(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            raise ValueError(f"duplicate key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


def load_yaml_no_dup(path):
    import yaml

    _DupCheckLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup_constructor
    )
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.load(f, Loader=_DupCheckLoader)
    except ValueError as e:
        fail(f"{e} (duplicate keys are rejected so merges fail fast)")
    except yaml.YAMLError as e:
        fail(f"invalid YAML: {e}")


# --- identity --------------------------------------------------------------

def sanitize_repo_name(repo):
    """Mirror ci.SanitizeRepoName: lowercase, runs of disallowed chars
    collapse to one hyphen, edge hyphens trimmed."""
    out = []
    prev_dash = True
    for ch in repo.strip().lower():
        if not ("a" <= ch <= "z" or "0" <= ch <= "9" or ch == "-"):
            ch = "-"
        if ch == "-":
            if not prev_dash:
                out.append("-")
            prev_dash = True
        else:
            out.append(ch)
            prev_dash = False
    return "".join(out).strip("-")


def resolve_app_name(repo, service_key, explicit):
    if explicit:
        return explicit
    base = sanitize_repo_name(repo)
    if not base:
        fail(f"repo {repo!r} sanitizes to an empty name segment")
    return f"{base}-{service_key}"


# --- build path checks (mirror ci.ValidateV2BuildShape/Paths) --------------

def check_build_shape(service_key, build):
    for field in ("context", "dockerfile"):
        p = build.get(field, "")
        if not p:
            fail(f"services[{service_key}].build.{field} must not be empty")
        if p.startswith("-"):
            fail(f"services[{service_key}].build.{field} {p!r} must not look like a CLI flag")
        if "://" in p or p.endswith(".git") or ".git#" in p or ".git@" in p:
            fail(f"services[{service_key}].build.{field} {p!r}: remote contexts are not supported")
        first = p.split("/", 1)[0]
        # Hostname-like first segments (github.com/..., host:path) are
        # remote shorthands — but only with a "/". A bare filename
        # cannot be a host/path shorthand, so conventional names like
        # Dockerfile.remuxer stay legal. Colons are still rejected
        # anywhere; dotted names WITH a slash stay rejected.
        if first != "." and (":" in first or ("." in first and "/" in p)):
            fail(f"services[{service_key}].build.{field} {p!r}: remote contexts are not supported")
        if p.startswith("/"):
            fail(
                f"services[{service_key}].build.{field} {p!r} "
                "must be repo-root-relative, not absolute"
            )
        if ".." in p.split("/"):
            fail(f"services[{service_key}].build.{field} {p!r}: '..' segments are rejected")


def check_build_paths(service_key, build, repo_root):
    root_abs = os.path.abspath(repo_root)
    root_real = os.path.realpath(root_abs)
    for field, want_dir in (("context", True), ("dockerfile", False)):
        rel = build.get(field, "")
        joined = os.path.normpath(os.path.join(root_abs, rel))
        if joined != root_abs and not joined.startswith(root_abs + os.sep):
            fail(f"services[{service_key}].build.{field} {rel!r} escapes the repository")
        if not os.path.lexists(joined):
            fail(f"services[{service_key}].build.{field} {rel!r} does not exist")
        if want_dir and not os.path.isdir(joined):
            fail(f"services[{service_key}].build.{field} {rel!r} is not a directory")
        if not want_dir and os.path.isdir(joined):
            fail(f"services[{service_key}].build.dockerfile {rel!r} is a directory, not a file")
        real = os.path.realpath(joined)
        if real != root_real and not real.startswith(root_real + os.sep):
            fail(
                f"services[{service_key}].build.{field} {rel!r} "
                "escapes the repository via symlink"
            )


# --- main -------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(description="validate a v2 1dash.yaml manifest")
    p.add_argument("--manifest", required=True, help="path to 1dash.yaml in the caller repo")
    p.add_argument("--schema", required=True, help="path to docs/1dash-schema.json at the pinned ref")
    p.add_argument("--repo", required=True, help="canonical owner/repo (for default app names)")
    p.add_argument("--repo-root", default=".", help="checked-out caller repo root for build-path checks")
    p.add_argument("--registry", default="ghcr.io", help="registry host for image base names")
    p.add_argument("--services", default="", help="optional comma-separated service subset (default: all)")
    p.add_argument("--out-dir", required=True, help="directory for matrix.json + workload-<service>.json")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    try:
        with open(args.schema, "r", encoding="utf-8") as f:
            schema = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        fail(f"cannot load schema {args.schema}: {e}")

    doc = load_yaml_no_dup(args.manifest)
    if not isinstance(doc, dict):
        fail("manifest must be a mapping")

    version = doc.get("version")
    if version == 1:
        fail(
            "manifest version is 1: this workflow ships v2 multi-workload manifests only; "
            "v1 single-service repos keep using the docs/ci/ship.yml reference"
        )
    if version != 2:
        fail(f"manifest.version must be 2 (got {version!r})")

    import jsonschema

    try:
        jsonschema.validate(instance=doc, schema=schema)
    except jsonschema.ValidationError as e:
        fail(f"schema validation: {e.message} (at {'/'.join(map(str, e.absolute_path)) or '<root>'})")

    services = doc["services"]
    wanted = [s.strip() for s in args.services.split(",") if s.strip()]
    if wanted:
        unknown = [s for s in wanted if s not in services]
        if unknown:
            fail(f"services selector names unknown workload(s): {', '.join(sorted(unknown))}")
        selected = wanted
    else:
        selected = sorted(services)

    org, _, repo_name = args.repo.partition("/")
    if not org or not repo_name or "/" in repo_name:
        fail(f"--repo must be canonical owner/repo (got {args.repo!r})")
    image_base_prefix = f"{args.registry}/{org.lower()}/{repo_name.lower()}-"

    os.makedirs(args.out_dir, exist_ok=True)
    include = []
    for key in selected:
        if not SERVICE_KEY_RE.match(key):
            fail(f"service key {key!r} must be a lowercase DNS label")
        entry = services[key]
        build = dict(entry.get("build") or {})
        build.setdefault("context", ".")
        build.setdefault("dockerfile", "Dockerfile")
        check_build_shape(key, build)
        check_build_paths(key, build, args.repo_root)
        app_name = resolve_app_name(args.repo, key, (entry.get("name") or "").strip())
        workload_path = os.path.join(args.out_dir, f"workload-{key}.json")
        with open(workload_path, "w", encoding="utf-8") as f:
            json.dump(entry, f, sort_keys=True)
        include.append(
            {
                "service": key,
                "type": entry["type"],
                "name": app_name,
                "context": build["context"],
                "dockerfile": build["dockerfile"],
                "target": build.get("target", ""),
                "image_base": f"{image_base_prefix}{key}",
                "workload_file": workload_path,
            }
        )

    matrix_path = os.path.join(args.out_dir, "matrix.json")
    with open(matrix_path, "w", encoding="utf-8") as f:
        json.dump({"include": include}, f, sort_keys=True)
    print(f"validated {len(include)} workload(s): {', '.join(m['service'] for m in include)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
