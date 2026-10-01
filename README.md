# 1dash CI (public mirror)

Ships a service repo to 1dash with **two files**: a version-2 `1dash.yaml`
manifest at the repo root, plus a caller workflow at
`<repo>/.github/workflows/ship.yml`. This repo hosts the reusable
workflow, the pinned JSON schema, and the validation/deploy helpers —
nothing else. No secrets live here.

## Use it

Copy [`docs/ci/caller.yml`](docs/ci/caller.yml) to
`<your-repo>/.github/workflows/ship.yml`, keeping the pinned `uses:`
ref and `schema-ref` on the SAME tag, then add a `1dash.yaml`
manifest (see [`docs/ci/1dash.v2.yaml.example`](docs/ci/1dash.v2.yaml.example)).

```yaml
jobs:
  ship:
    uses: uchinx/1dash-ci/.github/workflows/ship.yml@v1.0.12
    with:
      schema-ref: v1.0.12
      services: ${{ inputs.services }}
    secrets: inherit
```

Caller requirements (enforced by GitHub, not by us):

- Push to a protected branch + manual dispatch only. Never add
  `pull_request` — ship legs hold registry + deploy credentials.
- Permissions exactly `contents: read` + `packages: write` +
  `actions: write`.
- `secrets: inherit` (the reusable workflow declares `ONEDASH_URL` /
  `ONEDASH_TOKEN`, plus optional `TOOLS_TOKEN` for private tooling
  repos — not needed here since this mirror is public).
- Your repo's Actions allowlist must permit this repo
  (`uchinx/1dash-ci@*` or broader).

## Source of truth

This repo is a **read-only mirror**, published from the private
`uchinx/1dash` (one tag = one mirrored tag). Do not edit files here.
