# Custom review agents and workflows

Status: draft design, not implemented.

## Problem

Mira's review is a fixed sequence wired into `ReviewEngine._review_diff_internal`. It runs the main chunked review, the security pass, the dependency-overlap pass, the OSV scan and the secrets scan in one `asyncio.gather`, then filters, critiques and posts. An organisation can steer the main pass with rules (global rules, per-repo review context, learned rules, `AGENTS.md` conventions), but it cannot:

1. **Add a reviewer of its own.** Examples:
    1. a security audit driven by a published skill such as [cloudflare/security-audit-skill](https://github.com/cloudflare/security-audit-skill);
    2. an infrastructure reviewer that knows the organisation's CDK and IAM rules;
    3. a compliance check for `payments/**`.
2. **Choose a model and provider for it.** Today every tier shares one provider, endpoint and key.
3. **Decide what runs, in what order, and what a result does.** It can't run a check before the main review, stop the review when a check fails, or only add to the result.
4. **Manage any of this at runtime.** Mira has no place to add, change or remove a reviewer without a redeploy.

## Proposal at a glance

```
                     PR opened / pushed                         @mira agent security-audit
                            │                                              │
                            ▼                                              ▼
   ┌──────────────────────────────────────────────────────────────────────────────────┐
   │ Workflow "secured-pr"  (picked by trigger + repo; built-in default if none)        │
   │                                                                                    │
   │  prepare ─▶ Stage 1 "gate" ───────▶ Stage 2 "review" ──────────────▶ finalize      │
   │  (diff,      ┌───────────────────┐   ┌──────────────┐ ┌───────────┐  (dedupe,      │
   │   filter,    │ agent:            │   │ mira.review  │ │ mira.     │   critique,    │
   │   context)   │ security-audit    │   │ mira.security│ │ walkthrough│  caps, post,  │
   │              │ on_result: block  │   │ mira.deps    │ │ mira.     │   status)      │
   │              └─────────┬─────────┘   │ mira.osv     │ │ overlap   │                │
   │                        │             │ mira.secrets │ └───────────┘                │
   │                        │             └──────────────┘                              │
   │          blocker found?├── yes, on_block: stop ─────────────────────▶ finalize     │
   │                        │   (stage 2 skipped, status FAILED)                        │
   │                        └── no / on_block: continue ─▶ Stage 2                      │
   └──────────────────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
          inline comments + walkthrough (per-step sections) + status + dashboard run
```

## Concepts

1. **Skill**: a directory in the [Agent Skills](https://agentskills.io) layout. It has a `SKILL.md` with `name` and `description` frontmatter and a body, plus optional companion files that the body links to. A skill comes from one of two sources:
    1. `inline`: markdown written in the config or the dashboard.
    2. `git`: a repository URL, a ref and a sub-path, fetched through Mira's existing `RepoFetcher` and cached by resolved commit SHA.
2. **Connection**: a named LLM endpoint (provider, `base_url`, `api_key_env`, region). Agents choose a connection and a model, so several agents can share one key and a key can be rotated in one place.
3. **Agent**: one or more skills, a connection and model, a scope (paths and repos), limits, and output settings.
4. **Step**: one unit of work in a workflow. It is either a **built-in step** (Mira's existing passes, now separately addressable) or an **agent step**.
5. **Workflow**: what Mira does for a trigger. It is an ordered list of stages. A stage holds one or more steps that run in parallel. Each step says what happens to its result: add it to the output, block, or report only.
6. **Run**: one execution of a workflow. It records every step's status, timing, tokens and findings.

## Where configuration lives

Agents, connections and workflows can be defined in **two places**. At runtime the two sets are merged.

1. **Deployment config (YAML)**: `connections`, `agents` and `workflows` sections in the file given to `mira serve --config`. These are the "managed" entries: version-controlled, reproducible, and present from the first start of a new instance.
2. **Dashboard (database)**: admins add, edit, enable, disable and delete agents, connections and workflows at runtime. Changes apply to the next run, with no redeploy.

The rules for the merge:

1. Every entry has a unique name and a `source` of `config` or `dashboard`.
2. **Config entries are read-only in the dashboard**, which shows them with a "managed in config" badge. The one exception is an admin **disable** switch, stored in the database, for switching a misbehaving entry off without a redeploy.
3. A dashboard entry can't take the name of a config entry. If a later deploy adds a config entry with an existing dashboard name, the config entry wins, and the dashboard entry is shown as shadowed until it is renamed.
4. **Export and import**: the dashboard exports all entries as YAML, so a setup built in the UI can be committed and used to start the next instance. Import loads YAML into dashboard entries.
5. **Secrets never go in the database.** A connection names an environment variable (`api_key_env`). The dashboard can only choose variable names from an operator-set allow-list (`connections_allowed_key_envs`), so a dashboard user can't point a connection at an unrelated secret in the environment.
6. These sections are deployment-only. `_strip_deployment_only_settings` removes them from untrusted per-repo `.mira.yaml` files.

## Example configuration

```yaml
connections_allowed_key_envs: [OPENROUTER_API_KEY, SECURITY_AGENT_API_KEY]

connections:
  - name: openrouter
    provider: openai
    base_url: https://openrouter.ai/api/v1
    api_key_env: OPENROUTER_API_KEY
  - name: anthropic-direct
    provider: openai
    base_url: https://api.anthropic.com/v1
    api_key_env: SECURITY_AGENT_API_KEY

agents:
  - name: security-audit
    description: Source-first security review using the Cloudflare skill (guidance mode)
    skills:
      - git:
          url: https://github.com/cloudflare/security-audit-skill
          ref: 3f1c2ab                    # pinned commit; branches refused unless allow_floating_ref
          path: skills/security-audit
      - inline: |
          Services run behind an authenticating proxy; treat /internal/* as authenticated.
    connection: anthropic-direct
    model: claude-opus-5-5
    reasoning_effort: high
    scope:
      include: ["src/**", "lib/**", "infra/**"]
      exclude: ["**/*.test.ts", "docs/**"]
    limits: { max_hops: 12, max_tool_bytes: 200000, timeout_seconds: 600 }
    output: { max_comments: 10, min_severity: warning }

workflows:
  - name: secured-pr
    trigger: pull_request              # pull_request | command | index | schedule
    repos: ["acme-org/*"]
    stages:
      - name: gate
        steps:
          - agent: security-audit
            on_result: block           # append | block | report
            block_when: { min_severity: blocker, status: confirmed }
            on_block: stop             # stop (default) | continue
            on_error: continue         # continue (default) | block
            share_summary: true        # later stages get this step's summary as context
      - name: review
        steps:
          - builtin: mira.review
          - builtin: mira.security
          - builtin: mira.dependencies
          - builtin: mira.osv
          - builtin: mira.secrets
          - builtin: mira.walkthrough
          - builtin: mira.overlap
```

## Workflows

A workflow is picked by trigger and repository: the first workflow whose `trigger` and `repos` match wins. If none match, Mira uses the **built-in default workflow**, which reproduces today's behaviour exactly. An instance with no workflow config therefore behaves the same as before.

### Built-in steps

The existing review is split into steps that can be placed, ordered and gated like agents.

| Step | Today | Notes |
|---|---|---|
| `mira.review` | main chunked review (agentic or one-shot, ensemble) | the only step that produces key issues today; agents can too (see Output) |
| `mira.security` | `security_review_pass` | `review.security_pass`, `security_agentic` |
| `mira.dependencies` | `dependency_review_pass` | runs only when a manifest changes |
| `mira.osv` | `scan_manifest_changes` | deterministic, no LLM |
| `mira.secrets` | `scan_secrets` | deterministic, no LLM |
| `mira.walkthrough` | walkthrough generation | can't block; feeds the walkthrough comment |
| `mira.overlap` | cross-PR overlap detection | can't block; feeds the walkthrough comment |

Every workflow has two fixed bookends that are not steps:

1. **prepare**: fetches the diff, applies `filter`, selects files by priority, builds code context, and posts the "Reviewing…" placeholder.
2. **finalize**: classifies severity, de-duplicates, drops already-posted findings, runs self-critique, applies caps, writes the walkthrough and PR summary, posts, sets the status, and records the run.

The default workflow is one stage holding every built-in step. The existing `review.*` switches still turn the built-in steps on and off. In a custom workflow, a built-in step runs if it is listed, and the matching `review.*` switch is ignored.

```yaml
- name: default-pr
  trigger: pull_request
  stages:
    - name: review
      steps:
        - builtin: mira.review
        - builtin: mira.security       # if review.security_pass
        - builtin: mira.dependencies   # if review.dependency_overlap
        - builtin: mira.osv            # if review.osv_scan
        - builtin: mira.secrets        # if review.secrets_scan
        - builtin: mira.walkthrough    # if review.walkthrough
        - builtin: mira.overlap        # if review.overlap.enabled
```

### Execution

1. Stages run in order. Steps within a stage run in parallel, each with its own LLM client, tool executor and concurrency limit.
2. A step only runs if its scope matches at least one changed file. Otherwise it is recorded as skipped and costs nothing.
3. **`on_result`** decides what a step's findings do:
    1. `append` (default for built-in steps): the findings join the final output.
    2. `block` (default for agent steps): as `append`, and in addition, a finding matching `block_when` marks the step **blocking**.
    3. `report`: the findings are stored and shown in the dashboard and in the walkthrough summary, but never posted inline.
4. **`on_block`** decides what a blocking step does to the rest of the workflow. Both modes are available per step:
    1. `stop` (default): the workflow ends after the current stage. Later stages are skipped. Finalize posts the blocking findings and everything already produced, and sets the status to failed. The walkthrough names the skipped stages. The next push or `@mira review` starts the workflow again. This is cheaper, and the author fixes the blocker first.
    2. `continue`: every stage still runs, and the status is failed at the end. This costs more, but the author sees every finding in one pass.
5. **`on_error`**: by default a failing step is recorded and the workflow continues, as the security pass does today. `block` treats an error like a blocking result, for checks that must pass.
6. **`share_summary`**: a step's summary (and, for agents, a short list of its findings) is passed as extra context to the LLM steps of later stages. Steps in the same stage can't share, because they run at the same time.
7. **Finalize** runs once, after the last stage that ran, over all `append` and `block` findings.

### Triggers

1. `pull_request`: automatic reviews (PR opened, new commits).
2. `command`:
    1. `@mira agent <name>` runs one agent on the whole PR;
    2. `@mira workflow <name>` runs a named workflow;
    3. `@mira agent` with no name lists the available agents.

    Each posts a new top-level comment. Authorisation uses the existing command policy, with new `agent` and `workflow` entries in `CommandsConfig`.
3. `index`: after a full `index_repo`, over the indexing tarball. Output goes to the dashboard only.
4. `schedule`: a periodic whole-repo run (for example, a weekly security audit). Output goes to the dashboard only.

## Mockups

These are rough sketches of what each screen and PR output would contain, not visual designs.

### Dashboard: navigation

```
┌──────────────┬──────────────────────────────────────────────────────────────┐
│ Mira         │                                                              │
│              │                                                              │
│ Overview     │                                                              │
│ Repositories │                                                              │
│ Activity     │                                                              │
│ Rules        │                                                              │
│ ▸ Automation │  ◀── new group                                               │
│    Workflows │                                                              │
│    Agents    │                                                              │
│    Skills    │                                                              │
│    Runs      │                                                              │
│ Settings     │                                                              │
│    Models    │                                                              │
│    Connections ◀── new                                                      │
│    Webhooks  │                                                              │
└──────────────┴──────────────────────────────────────────────────────────────┘
```

### Dashboard: Agents

```
Agents                                                  [Export YAML] [+ New agent]
┌──────────────────┬────────────────────────┬──────────────────┬────────┬──────────┬─────────┐
│ Name             │ Skills                 │ Model            │ Source │ Last run │ Enabled │
├──────────────────┼────────────────────────┼──────────────────┼────────┼──────────┼─────────┤
│ security-audit   │ security-audit@3f1c2ab │ anthropic-direct │ config │ 2h ago ✓ │  [on]   │
│                  │ + 1 inline             │ claude-opus-5-5  │ 🔒     │ 1 block  │         │
├──────────────────┼────────────────────────┼──────────────────┼────────┼──────────┼─────────┤
│ cdk-iam-rules    │ 1 inline               │ openrouter       │ dash-  │ 1d ago ✓ │  [on]   │
│                  │                        │ sonnet-5-5       │ board  │          │         │
└──────────────────┴────────────────────────┴──────────────────┴────────┴──────────┴─────────┘
 🔒 managed in config: read-only here, can only be disabled
```

### Dashboard: edit agent

```
New agent
┌──────────────────────────────────────────────────────────────────────────────┐
│ Name         [cdk-iam-rules                       ]                          │
│ Description  [Reviews CDK stacks against our IAM rules                   ]   │
│                                                                              │
│ Skills                                                        [+ Add skill]  │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │ ◉ Inline  ○ Git repo  ○ Existing skill                                 │  │
│  │ ┌────────────────────────────────────────────────────────────────────┐ │  │
│  │ │ ---                                                                │ │  │
│  │ │ name: cdk-iam                                                      │ │  │
│  │ │ description: IAM least privilege rules for CDK                     │ │  │
│  │ │ ---                                                                │ │  │
│  │ │ 1. No `*` actions or resources on iam.PolicyStatement ...         │ │  │
│  │ └────────────────────────────────────────────────────────────────────┘ │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │ ○ Inline  ◉ Git repo  ○ Existing skill                                 │  │
│  │ URL  [https://github.com/cloudflare/security-audit-skill            ]  │  │
│  │ Ref  [3f1c2ab    ] Path [skills/security-audit ]  [Fetch & preview]    │  │
│  │ ✓ resolved 3f1c2ab · SKILL.md + 14 files · 190 KB                      │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│ Model        Connection [openrouter        ▾]  Model [sonnet-5-5       ▾]    │
│              Reasoning  [medium ▾]                                           │
│ Scope        Include [infra/**, lib/**/*-stack.ts            ]               │
│              Exclude [**/*.test.ts                           ]               │
│              Repos   [acme-org/*                             ]               │
│ Limits       Max hops [12]  Tool bytes [200000]  Timeout [600 s]             │
│ Output       Max comments [10]  Min severity [warning ▾]                     │
│                                                                              │
│                                      [Test on a PR…]   [Cancel]  [Save]      │
└──────────────────────────────────────────────────────────────────────────────┘
 "Test on a PR…" runs the agent on a chosen PR and shows the findings here without posting.
```

### Dashboard: Workflows (pipeline editor)

```
Workflow  secured-pr                    Trigger [pull_request ▾]   Repos [acme-org/*]
──────────────────────────────────────────────────────────────────────────────────────

  ┌─────────┐     ┌─ Stage 1: gate ───────────┐     ┌─ Stage 2: review ──────────┐     ┌──────────┐
  │ prepare │ ──▶ │ ┌───────────────────────┐ │ ──▶ │ ┌──────────┐ ┌───────────┐ │ ──▶ │ finalize │
  │ (fixed) │     │ │ 🤖 security-audit     │ │     │ │ ⚙ review │ │ ⚙ security│ │     │ (fixed)  │
  └─────────┘     │ │ result: block         │ │     │ └──────────┘ └───────────┘ │     └──────────┘
                  │ │ when: confirmed       │ │     │ ┌──────────┐ ┌───────────┐ │
                  │ │       ≥ blocker       │ │     │ │ ⚙ deps   │ │ ⚙ osv     │ │
                  │ │ on block: stop        │ │     │ └──────────┘ └───────────┘ │
                  │ │ shares summary ▶      │ │     │ ┌──────────┐ ┌───────────┐ │
                  │ └───────────────────────┘ │     │ │ ⚙ secrets│ │ ⚙ walk-   │ │
                  │ [+ step]                  │     │ └──────────┘ │  through  │ │
                  └───────────────────────────┘     │ [+ step]     └───────────┘ │
                                                    └────────────────────────────┘
  [+ stage]                                              drag steps to reorder or move

 Step settings (security-audit)
   Result    ○ append   ◉ block   ○ report
   Block when  status [confirmed ▾]  severity ≥ [blocker ▾]
   On block  ◉ stop (skip later stages)   ○ continue (fail at the end)
   On error  ◉ continue   ○ block
   ☑ Share summary with later stages
```

### Dashboard: Runs

```
Runs   repo [acme/payments-api ▾]  workflow [all ▾]  status [all ▾]
┌─────────┬──────────┬────────────┬─────────────┬──────────┬──────────┬────────┐
│ When    │ PR       │ Workflow   │ Trigger     │ Status   │ Findings │ Tokens │
├─────────┼──────────┼────────────┼─────────────┼──────────┼──────────┼────────┤
│ 10:02   │ #26      │ secured-pr │ pull_request│ ✗ blocked│ 1 B 2 W  │ 84k    │
│ 09:15   │ #25      │ secured-pr │ pull_request│ ✓ passed │ 0        │ 61k    │
│ Mon     │ (main)   │ weekly-sec │ schedule    │ ✓ passed │ 3 NV     │ 410k   │
└─────────┴──────────┴────────────┴─────────────┴──────────┴──────────┴────────┘
 B blocker · W warning · NV needs validation
```

```
Run #812 · PR #26 · secured-pr · blocked                        [Open PR] [Re-run]
──────────────────────────────────────────────────────────────────────────────────
 prepare   ✓ 3 s    12 files, 2 skipped (size)
 gate      ✗ 94 s
   🤖 security-audit   ✗ blocked  94 s  52k tok  skill 3f1c2ab  claude-opus-5-5
       ├ B  confirmed  Cross-tenant read in GET /sites/:id      src/sites.ts:41
       ├ W  confirmed  Missing rate limit on /login             src/auth.ts:88
       └ ·  needs validation  proxy auth header trust (not in source)
 review    ⏭ skipped (blocked by security-audit, on_block: stop)
 finalize  ✓ 4 s    posted 2 inline, walkthrough updated, status FAILED
```

### Pull request: walkthrough when a step blocks

```
## Mira PR Walkthrough

> ⛔ Blocked by security-audit: 1 confirmed blocker. The review stage did not run.
> Fix the finding and push, or comment `@mira review` to re-run.

### Workflow secured-pr
1. ✓ prepare: 12 files (2 skipped by size)
2. ✗ gate
    1. security-audit: blocked, 1 blocker, 1 warning, 1 needs validation (94 s)
3. ⏭ review: skipped

### security-audit
Checked the new /sites endpoints for tenant isolation and authentication. ...
1. 🔴 Cross-tenant read in GET /sites/:id (src/sites.ts:41), see inline
2. 🟡 Missing rate limit on /login (src/auth.ts:88), see inline
3. Needs validation: whether the proxy strips client-sent auth headers. Not visible in source.
```

### Pull request: walkthrough when nothing blocks

```
## Mira PR Walkthrough

<summary of the change, as today>

**Confidence: 4/5** ◉◉◉◉○

### Workflow secured-pr
1. ✓ gate: security-audit, no blockers (2 warnings)
2. ✓ review: review, security, dependencies, osv, secrets

### security-audit
2 warnings, see inline. Nothing found off the changed lines.

<key issues, stats and skipped files, as today>
```

### Pull request: inline comment from an agent

```
🟡 Warning · Security · security-audit
**Missing rate limit on /login**

The new handler accepts unlimited attempts per account ...

<suggested fix, as today>
```

### Pull request: commands

```
@mira agent                       →  lists agents: security-audit, cdk-iam-rules
@mira agent security-audit        →  runs one agent on the whole PR, new top-level comment
@mira workflow weekly-sec         →  runs a named workflow on the PR
```

## Agent runtime

### Built-in runtime

The agent runs inside Mira on its agentic loop (`agentic_review_loop`), generalised so that the tools, the hop cap and the submit tool are parameters instead of being hard-coded.

1. **System prompt**: a short Mira frame (what a run is, the trust rules, the output contract), then each skill's frontmatter and `SKILL.md` body, the agent's inline instructions, and any summaries shared by earlier stages.
2. **Progressive disclosure**: companion files are not inlined. The model sees an index of them and loads them with `read_skill_file(skill, path)`. A 190 KB skill therefore costs only its `SKILL.md` until the model needs more.
3. **Code tools**: `read_file` and `grep_repo`, as today. In repo-level runs they read from the indexing tarball instead of making per-file API calls.
4. **Submission**: a `submit_findings` tool with the existing comment schema, extended with:
    1. `category` as free text;
    2. `status`: `confirmed` or `needs_validation`, matching the Cloudflare report schema (only confirmed findings carry a severity);
    3. a `repo_level` flag for findings with no diff line;
    4. `key_issues` and a short `summary`.
5. **Failure handling**: an agent that fails or times out records a failed step and returns no findings. `on_error` decides whether the workflow continues.

The built-in runtime suits skills that work as guidance. The Cloudflare skill supports this explicitly in its "guidance mode": focused reviews with no sub-agents, no files written and no target code executed.

### External runtime

The skill's "full audit mode" assumes a complete agent harness:

1. delegated sub-agents;
2. a writable output directory;
3. Node validators;
4. an OS sandbox with no network.

Mira should not grow those capabilities itself. `runtime: external` on an agent hands the repository tarball and the skill to a separate sandboxed worker, and reads back `findings.json` in the skill's schema. On AWS that would be a one-off ECS task with no egress except the model endpoint, running a headless agent. External agents are allowed only in `schedule`, `index` and `command` workflows, never on every PR.

## Output

1. **Inline comments** carry `source_pass` (`mira.security`, `agent:security-audit`, …), and agent comments show the agent's name in the header. Each agent's `max_comments` applies before the shared noise filter, so one step can't push out another's findings.
2. **Key issues** from any step are kept. Today only the main pass's key issues survive.
3. **Walkthrough**:
    1. a "Workflow" block lists each stage and step with its status;
    2. each agent that ran gets a section with its summary, counts and the findings that could not be anchored to the diff (today those are dropped);
    3. a blocked run puts the reason at the top.

    On Bitbucket, where `<details>` is flattened, the sections are kept short.
4. **Dashboard**: Connections, Agents, Skills, Workflows and Runs pages, as in the mockups.
5. **Status**: the existing single review status reflects the whole workflow, and is failed when a step blocks. A separate status per agent is optional (`status_check: true`).
6. **Outbound webhooks**: new `workflow.completed` and `workflow.blocked` events, with finding counts per step.

## Storage

1. **App database** (global), following the existing `settings` and `global_rules` pattern, with SQLite and Postgres schemas:
    1. `connections`, `agents`, `skills` and `workflows` hold dashboard entries as validated JSON, plus `enabled`, `created_by` and timestamps;
    2. `config_overrides` holds the disable switches for config entries;
    3. `skill_cache` holds fetched skills keyed by `(url, sha, path)`.
2. **Index store** (per repository):
    1. `workflow_runs`: workflow, trigger, PR or commit, status, blocked-by, started and finished times, token usage;
    2. `workflow_steps`: run, stage, step, skill SHAs, model, status, duration, tokens, summary;
    3. `agent_findings`: step, path, line, severity, status, category, title, body, and the posted comment id if one was posted.
3. `review_comments` gains a `source_pass` column.

## Security

1. **Skills are prompts.** A skill can steer the model, so only admins can add one, git refs are pinned to a commit by default, and every step records the skill SHAs it used.
2. **Code under review is untrusted.** Built-in agents only read code and never run it. The external runtime runs only in a sandbox with no network.
3. **Credentials stay in environment variables.** Connections name a variable from an allow-list, and nothing secret is stored in the database.
4. **Cost** is bounded by:
    1. opt-in agents and workflows;
    2. scope matching;
    3. per-step hop, tool-byte and timeout limits;
    4. `on_block: stop`;
    5. token usage recorded per step (today only the main model's usage is counted).

## Delivery

This is one feature, built in our fork and proposed upstream in reviewable pieces. Each piece leaves Mira working, and an instance without workflow config behaves as before.

1. **Workflow engine**: split the built-in review into steps, add the stage runner with prepare and finalize, the default workflow, `on_result` / `on_block` / `on_error` / `share_summary`, and persisted runs and steps. This changes no behaviour on its own.
2. **Agents and skills**: connections, the skill loader and cache, the agent step on the generalised agentic loop, agent output in comments and the walkthrough. First agent: the Cloudflare skill in guidance mode, on one repository.
3. **Dashboard**: Connections, Agents, Skills, Workflows (pipeline editor) and Runs, the config/dashboard merge, export and import, and "Test on a PR".
4. **Triggers**: the `command`, `index` and `schedule` triggers, per-agent statuses and the webhook events.
5. **External runtime** for full audits in a sandboxed worker.

## Out of scope

1. Agents or workflows defined in a reviewed repo's own `.mira.yaml`. The server doesn't read per-repo config today, and letting a PR author add steps would let them choose prompts and models.
2. Agents that change code or open PRs.
3. Branching or conditional logic in workflows beyond scope matching and `block_when`.

## Related fixes found during the design

1. Conventions are stored and read without a platform, so they default to `github`. On Bitbucket, GitLab and Forgejo they are never injected (`indexer._index_conventions`, `engine.py` `get_repo`).
2. Key issues from passes other than the main one are discarded (handled by Output, item 2).
3. Token usage records only the main review model (handled by per-step token recording).
