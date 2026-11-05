# Review workflows

A workflow decides which review passes Mira runs for a pull request, in what order, and what their findings do. Without any workflow configured, Mira runs its default pipeline: every enabled pass in parallel, every finding posted.

## Configuring

Workflows live in the deployment config (`mira serve --config`), never in a repository's `.mira.yaml`.

```yaml
workflows:
  - name: secured-pr
    trigger: pull_request          # the only trigger so far
    repos: ["acme/*"]              # owner/repo globs; empty matches every repository
    stages:
      - name: gate
        steps:
          - builtin: mira.security
            on_result: block
            block_when: { min_severity: blocker }
            on_block: stop
            share_summary: true
          - builtin: mira.secrets
            on_result: block
      - name: review
        steps:
          - builtin: mira.review
          - builtin: mira.dependencies
          - builtin: mira.osv
          - builtin: mira.walkthrough
          - builtin: mira.overlap
```

The first enabled workflow whose `trigger` and `repos` match is used. If none match, the default workflow runs.

## Steps

| Step | What it runs |
|---|---|
| `mira.review` | The main chunked review (agentic tools, ensemble, rules and conventions). |
| `mira.security` | The dedicated security pass (`llm.security_model`). |
| `mira.dependencies` | The duplicate-dependency check, when a manifest changed. |
| `mira.osv` | The OSV.dev CVE check of changed manifests. |
| `mira.secrets` | The hardcoded-secrets scan of added lines. |
| `mira.walkthrough` | The walkthrough comment. |
| `mira.overlap` | Detection of other open PRs touching the same files. |

`mira.walkthrough` and `mira.overlap` only feed the walkthrough comment. They run alongside the whole workflow wherever they are listed (the walkthrough is posted early), and they can only `append`. Leaving one out of a workflow turns it off.

A step listed in a workflow runs even if its `review.*` switch is off. The switches only shape the default workflow.

## What a step's findings do

1. `on_result`:
    1. `append` (default): findings go through the usual severity rules, noise filter, de-duplication and self-critique, and are posted.
    2. `block`: as `append`, and findings at or above `block_when.min_severity` that pass self-critique make the step **blocking**. A blocked review fails the check run (or build status), and the walkthrough opens with the reason.
    3. `report`: findings are listed in the walkthrough but not posted as comments.
2. `on_block`: `stop` (default) skips the later stages; `continue` runs them and fails at the end.
3. `on_error`: `continue` (default) records the failure and carries on; `block` treats it as blocking.
4. `share_summary`: the step's summary and top findings are added as context to the main review in later stages.

The next push, or `@mira review`, runs the workflow again from the first stage.
