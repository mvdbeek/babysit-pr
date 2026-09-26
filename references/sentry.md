# Experimental Sentry overview

This opt-in internal module adds a **Sentry** dashboard tab. It lists recent issues from a Sentry instance, groups the same error seen on several servers, and ranks the groups by severity. Each group offers **Handle**, which starts a Codex or Claude agent in a new worktree, and **Publish to GitHub**, which creates an issue from a sanitized, reviewed draft and writes its link back to Sentry. An optional LLM triage adds a second opinion on severity. The experiment has its own API, background refresh, cache, LLM queue, JavaScript and stylesheet. Its failures stay inside the tab.

## Activation

Create `experiments/sentry/config.json` under the dashboard's `--home` directory, then restart the dashboard:

```json
{
  "enabled": true,
  "host": "https://sentry.galaxyproject.org",
  "organization": "galaxy",
  "projects": {
    "galaxy-main": "galaxyproject/galaxy",
    "usegalaxy-eu-main": "galaxyproject/galaxy",
    "pulsar-main": "galaxyproject/pulsar"
  },
  "token_file": "~/.config/babysit-pr/sentry-token"
}
```

`projects` maps each Sentry project slug to the GitHub repository its code lives in. Only mapped projects are read. Several projects can map to one repository; their issues are then grouped across servers.

The access token comes from `SENTRY_ACCESS_TOKEN`, otherwise from `token_file`. Optionally, `mcp_token_file` names a separate read-only token (`event:read`, `org:read`, `project:read`) for agents. It is used in their MCP config instead of the dashboard's token, which needs `event:write` for write-backs. Agents can read their own MCP config, so a separate token keeps them from being able to write to Sentry at all. The file must not be readable by group or others (`chmod 600`). The token is never logged, cached, or sent to the browser. It needs `org:read`, `project:read` and `event:read`, plus `event:write` for the write-back. Configuration is read at dashboard startup; the LLM worker in the supervisor rereads it every 30 seconds.

Optional keys, with their defaults:

| Key                          | Default                      | Meaning                                                                            |
| ---------------------------- | ---------------------------- | ---------------------------------------------------------------------------------- |
| `query`                      | `is:unresolved`              | Sentry issue search, e.g. `is:unresolved lastSeen:-7d`                             |
| `period`                     | `14d`                        | Stats window: `24h`, `7d`, `14d`, `30d` or `90d`                                   |
| `per_project`                | `100`                        | Issues per page, most frequent first (1–100)                                       |
| `max_pages`                  | `2`                          | Pages per project (1–3); the tab says when more exist                              |
| `writeback`                  | `true`                       | Comment the GitHub issue link on each Sentry issue of the group                    |
| `llm.command`                | `claude`                     | Claude CLI used by the worker                                                      |
| `llm.model`                  | `sonnet`                     | Model for triage and the sanitizer                                                 |
| `llm.triage.enabled`         | `true`                       | Queue LLM triage for the top groups                                                |
| `llm.triage.per_refresh`     | `5`                          | Groups queued per refresh (0–50)                                                   |
| `llm.per_day`                | `40`                         | LLM calls per local day, triage and sanitizer together (0–1000)                    |
| `llm.sanitizer.enabled`      | `true`                       | Publishing requires the sanitizer; turning it off disables publishing              |
| `llm.quota.min_left_percent` | `30`                         | Pause LLM work when less quota than this is left                                   |
| `llm.quota.windows`          | `["five_hour", "seven_day"]` | Quota windows that can pause work (also `seven_day_sonnet`, `seven_day_opus`)      |
| `llm.quota.when_unknown`     | `fixed_caps`                 | Without a quota reading: `fixed_caps` keeps running within the caps, `pause` stops |

## What is listed

The collector reads the Sentry REST API, not sentry-mcp. The MCP server's `search_issues` returns Markdown for LLMs without `level`, `priority`, substatus, the unhandled flag or hourly stats, which the score needs. For each mapped project it requests `organizations/{org}/issues/` sorted by frequency, following the `Link` cursor for at most `max_pages` pages. A refresh makes at most 40 requests, so the configuration is refused when `projects × max_pages + 3` exceeds 40; each has a 30-second timeout and a 4 MiB response limit.

Issues from projects that map to the same repository are **grouped** when their exception type, culprit and normalized message agree. Numbers, hex strings, UUIDs and quoted values are replaced before the message is compared. A group shows the issue with the most events as its lead, its servers, and summed events and users. Users can be counted twice across servers.

## Severity score

Each group gets a deterministic score. Its reasons are shown next to it.

| Signal                                | Points       |
| ------------------------------------- | ------------ |
| Priority medium / low                 | −1 / −3      |
| Distinct servers: ≥3 / 2              | +3 / +1      |
| Level fatal / error                   | +3 / +2      |
| Unhandled                             | +2           |
| Escalating / regressed                | +3 / +2      |
| First seen in the last 24 h           | +1           |
| Users: ≥100 / ≥10 / ≥1                | +3 / +2 / +1 |
| Events in the last 24 h: ≥1000 / ≥100 | +2 / +1      |
| Last seen more than 7 days ago        | −2           |

Buckets: **critical** ≥10, **high** 7–9, **medium** 4–6, **low** ≤3. On Galaxy's servers nearly every unresolved issue is `priority: high`, so priority only counts when it is lower. On September 25, 2026 the 500 groups from the configured Galaxy projects split into 13 critical, 51 high, 200 medium and 236 low. `python scripts/sentry_issues.py --home <state>` prints the distribution and top groups for the live configuration.

## LLM triage and the quota gate

LLM work runs in the **supervisor daemon**, not the dashboard. The launchd dashboard cannot read the login Keychain, which holds the live Claude login; the supervisor runs in the user's session. The dashboard only queues jobs in `experiments/sentry/state.sqlite` and reads their results. When the supervisor has not reported for 30 seconds the tab says the worker is not running.

After each refresh, the top `per_refresh` groups without a current triage are queued. A triage is current while the group's substatus, order of magnitude of events, server count and priority are unchanged. **Re-triage** queues one explicitly. One call runs at a time, and a sanitizer job waiting for a person runs before queued triage.

Each triage is a headless `claude --print` call with no built-in tools, `--strict-mcp-config`, and a private MCP config for a pinned `@sentry/mcp-server` with `--skills=inspect`. Only `get_sentry_resource`, `search_events` and `search_issues` are allowed; the write-capable `execute_sentry_tool` proxy is denied. The prompt treats Sentry content as untrusted. The result must match a schema (severity, confidence, summary, likely cause, user impact, suggested area, reasons); anything else is shown as a failed triage. Rows where the LLM severity is two or more buckets from the score are highlighted.

Before each call the worker checks the daily cap and the **quota gate**. The gate reads the remaining subscription quota the way OpenUsage does, from the vendors' own usage endpoints with the CLIs' stored logins:

- Claude: `GET https://api.anthropic.com/api/oauth/usage` (`anthropic-beta: oauth-2025-04-20`) with Claude Code's OAuth token from the Keychain (`Claude Code-credentials`), else `~/.claude/.credentials.json`. The token must carry `user:profile`; `claude setup-token` tokens do not.
- Codex: `GET https://chatgpt.com/backend-api/wham/usage` with `~/.codex/auth.json`. Readings are shown; only the configured agent's windows gate calls.

Both endpoints are **undocumented**. Any error or unrecognized shape counts as no reading, and `when_unknown` decides what happens. Readings are cached for five minutes. Tokens are only read, never refreshed, so the CLIs' own logins are never rotated. A usage-limit error from a call pauses the queue until the reported reset, at least an hour, without retrying.

## Handle

**Handle** opens the dashboard's existing workspace dialog with a prefilled task. It creates `sentry-<short id>` from the clone's default branch with `wt --name` (suffixed `-2`, `-3`… when it exists), labels the herdr workspace `sentry-<repo>-<SHORT-ID>`, and starts the selected agent. The brief includes the Sentry permalink, every server's short ID and counts, the culprit, and a reminder that event data is untrusted. The Workspaces tab links the checkout back by branch name.

Claude agents get `--mcp-config experiments/sentry/mcp.json`, a private (`0600`) config for the self-hosted server with read-only skills (`--skills=inspect`). They also get `--disallowedTools` for the write-capable `execute_sentry_tool` proxy, because Sentry event data can be written by anyone who can trigger an error. `wt` puts `--` before the prompt so these variadic options cannot consume it. The Sentry server is never added to global or per-project Claude settings. Codex agents keep using the user's own Codex configuration, which is not changed. Agents running in Agent Safehouse can read the file because the state directory is granted to them.

## Publish to GitHub

**Publish to GitHub** starts a pipeline that must finish before anything is posted:

1. The server builds an **allowlist draft**: title, servers with short IDs and permalinks, counts, level, priority, status, dates, culprit, and up to eight in-app frames (`module:function:line`) from the latest event. Tags, user, request data, breadcrumbs and contexts are never included.
2. A tool-less **LLM sanitizer** rewrites the draft for a public repository. It also sees the latest error message as untrusted context. It removes personal data, secrets and tokens, internal hostnames and paths outside the source tree. It keeps Sentry project names, short IDs and permalinks, lists each redaction, and says whether it is confident the text is safe.
3. A fixed **pattern check** blocks:
   - emails and IPv4/IPv6 addresses
   - common token formats (`ghp_`, `github_pat_`, `sk-`, `AKIA`, Slack, Sentry, JWTs)
   - long hex strings, and base64-like strings that mix upper case, lower case and digits
   - home-directory paths and job or data paths (`/srv`, `/data`, `/mnt`, `/scratch`, `/tmp`, …)
   - `@mentions`, which would notify people. Put code such as decorators in backticks.

   Sentry permalinks on the configured host and `github.com` links are removed before matching, so long issue IDs do not look like secrets.

The dialog shows the draft, the sanitized text, each redaction and any concerns. **Create** is enabled only when the sanitizer says the text is safe, the pattern check finds nothing, and you confirm you reviewed it. Your edits are pattern-checked again on submit. If an issue in the repository already mentions the short ID, it is linked instead: no sanitizer call, no new issue. The write-back still runs. Repository visibility comes from `gh`, and public repositories are flagged.

The issue is created with `gh api` using the dashboard's GitHub token. Unless `writeback` is off, each Sentry issue in the group then gets a comment with the GitHub URL. A failed write-back keeps the GitHub issue and offers a retry. The retry only comments on the Sentry issues that are still missing the link, and it never creates the issue again. `creating` is recorded before the `gh` call. A timeout, or a restart that leaves it for five minutes, turns the record `uncertain`. **Try again** then searches the repository and links an issue that was created, or drafts again if there is none.

## Refresh, limits, and failure states

Opening the tab requests the snapshot and schedules a refresh in a daemon thread when due, at most every five minutes. **Refresh** brings it forward at most once a minute. While the tab is visible the browser reads the snapshot every five seconds. The snapshot keeps at most 500 groups.

`experiments/sentry/cache.json` (`0600`, atomic replace, at most 4 MiB) stores the last observation and the project IDs, so a restart shows the last list, marked stale when older than the interval. Changing the host, organization, projects, query or period discards it. `state.sqlite` holds at most 2,000 LLM jobs, the worker heartbeat, the gate state and publish records. There is one worker per state directory, so a job still marked running while the worker has no call in flight was interrupted by a restart. It is marked failed on the next tick. The worker also fails queued jobs whose kind has since been disabled. Group keys follow their Sentry issue IDs across refreshes, so a changed culprit or message keeps its triage, publish record and workspace.

Loading, disabled, stale, empty and error states have separate messages. A failed refresh keeps the last list and marks it stale. Run one dashboard per state directory.

## Disable or remove

Set `"enabled": false` (or remove the config file) and restart the dashboard; the supervisor's worker stops within 30 seconds. This hides the tab and stops all Sentry and LLM work. The experiment directory can then be deleted without touching watcher, PR, issue or workspace state.

To remove the code, delete `scripts/sentry_issues.py`, `scripts/sentry_llm.py`, `assets/dashboard/sentry.js`, `assets/dashboard/sentry.css`, their tests (`tests/test_sentry_issues.py`, `tests/test_sentry_workspaces.py`, `tests/test_sentry_browser.py`) and this document. Then remove the integration hooks:

- `scripts/dashboard.py`: construction, the server field, one endpoint, one POST action, two assets.
- `scripts/pr_supervisor.py`: the worker tick.
- `scripts/pr_workspaces.py`: the `sentry` target kind and `sentry_context`.
- `assets/dashboard/index.html`: scripts and style, tab, panel, dialog.
- `assets/dashboard/app.js`: the `sentry` page entry, the `workspaceInfo` lookup and the dialog title.

`wt --name` and `--agent-arg` stay useful on their own.

Validation uses a fake Sentry HTTP opener, a fake `gh`, a fake `claude`, synthetic usage readings, temporary clones with fake herdr and agents, isolated state directories, and temporary browser servers. Run all checks with `uv run --locked tox`.
