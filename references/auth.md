# Dashboard sign-in

The dashboard can require a credential on every API request: a browser session from
Keycloak (OIDC), a scoped key, or a one-time break-glass link. This is Phase 1 of the
rollout. It adds the framework and runs in **report** mode, which refuses nothing:
it counts and logs what **enforce** mode would refuse. Clients (`wt`, voice tools,
sandboxed agents, workers, push devices) move to keys in Phase 2, and enforce mode is
switched on in Phase 3.

The code is `scripts/dashboard_auth.py`. The dashboard hooks it into
`Handler.guarded`, right after the tailnet fence.

## Modes

| `--auth`           | Effect                                                                                                                                                                                                                                                                                                                          |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `report` (default) | On only once `oidc.json` exists, so deploying is a no-op until it is configured. Every request is classified. Requests enforce would refuse are counted and logged, then answered as before                                                                                                                                     |
| `enforce`          | Not usable yet: the dashboard refuses to start in it until Phase 2 and Phase 3 land (below). Once usable, it is always on. A request without a valid credential gets `401 {"login": "/auth/login"}`; a key whose scope does not cover the route gets 403. A broken `oidc.json` stops the dashboard from starting (fails closed) |
| `off`              | No checks                                                                                                                                                                                                                                                                                                                       |

In report mode, a broken `oidc.json` or an unusable auth home is reported on stderr and
auth stays off, so the dashboard keeps serving. An unusable auth home is a symlink, a
directory owned by another user, or an `auth.sqlite` that is a symlink. In enforce mode
either problem stops the dashboard from starting.

**Enforce needs Phase 2 and Phase 3.** Worker, extension and push-device routes still
check their own tokens. Those tokens are stored in the state home
(`worker-tokens.json`, `browser-extension.sqlite`, `dashboard-push.json`), which
Safehouse sandboxes can still write. A Mac sandbox could therefore mint its own
worker token and call `/api/worker/launch` over loopback, whatever enforce says.
Enforce becomes meaningful only once:

- Phase 2 has moved those credentials into the auth store, and
- Phase 3 has taken the state home out of Safehouse's write grants.

Until then, enforce refuses to start. The environment variable
`BABYSIT_AUTH_TEST_ALLOW_OWN_ROUTES=1` overrides this for tests only. `dashboard.py` and `pr_supervisor.py dashboard` both take
`--auth`, `--auth-home DIR` (default `~/.config/babysit-pr-auth`) and
`--oidc-config FILE` (default `AUTH_HOME/oidc.json`). The startup banner prints which
mode is active.

## Files

The auth home `~/.config/babysit-pr-auth/` is created 0700. Its files are 0600:

- `auth.sqlite`: sessions, keys, break-glass links and the would-refuse report. Only SHA-256 digests of cookies, keys and links are stored.
- `admin-key`: the local admin key, for unsandboxed clients.
- `oidc.json`: the OIDC client settings, including the client secret.

The auth home is deliberately outside `~/.local`, which Safehouse grants read-only to
every sandbox, and outside `~/.config/babysit-pr`, whose `github-token` is granted
read-only. No auth secret may ever live under `~/.local`.

## Switching it on

1. Create the Keycloak client (below).
2. Write `~/.config/babysit-pr-auth/oidc.json` and `chmod 600` it:

   ```json
   {
     "issuer": "https://auth.magmahome.org/realms/magmahome",
     "client_id": "babysitter",
     "client_secret": "<from Keycloak → Clients → babysitter → Credentials>",
     "redirect_uris": {
       "babysitter.tailfb45be.ts.net": "https://babysitter.tailfb45be.ts.net/auth/callback",
       "127.0.0.1:8765": "http://127.0.0.1:8765/auth/callback"
     },
     "role": "babysitter-admin"
   }
   ```

   `redirect_uris` maps each Host header to the redirect URI registered for it. The
   URI for a request is looked up from its Host and is never taken from the request
   itself. A Host that is not listed gets 403 at `/auth/login`. Plain `http` is
   accepted only for `127.0.0.1` and `localhost`. `role` defaults to
   `babysitter-admin`.

3. Restart the dashboard (`launchctl kickstart -k`). The banner should read
   `Dashboard auth: report (… OIDC https://auth.magmahome.org/realms/magmahome)`.
4. Check that sandboxes cannot read the auth home. From inside a Safehouse agent,
   `test -r ~/.config/babysit-pr-auth/oidc.json && echo READABLE || echo denied`
   must print `denied`.
5. Sign in from the header's **Sign in** button.
6. Watch the report (below) until normal use shows no would-refuse entries. Phase 2
   exists to clear them.

To roll back from report mode, rename `oidc.json` or pass `--auth off`, and restart.

## Keycloak client (realm `magmahome`)

**Create client:**

- OpenID Connect, Client ID `babysitter`.
- Client authentication **On**; Authorization Off.
- Flows: Standard **On**. Direct access grants, Implicit, Service accounts, Device grant and CIBA all Off.

**Login settings:**

- Root URL `https://babysitter.tailfb45be.ts.net`, Home URL `/`.
- Valid redirect URIs: `https://babysitter.tailfb45be.ts.net/auth/callback` and `http://127.0.0.1:8765/auth/callback`. For staging, add `http://127.0.0.1:8799/auth/callback` temporarily.
- Valid post logout redirect URIs: `https://babysitter.tailfb45be.ts.net/` and `http://127.0.0.1:8765/`.
- Web origins: empty. The token call is made server-side, so no CORS is needed.

**Advanced:**

- PKCE code challenge method **S256**.
- Under OIDC compatibility modes, set Use refresh tokens **Off**.
- Front-channel logout Off, and no back-channel URL (Keycloak cannot reach the tailnet).
- Keep the realm's default access-token lifespan; the token is used only at sign-in.

**Credentials:** copy the client secret into `oidc.json`.

**Roles:** create the client role `babysitter-admin`. Assign it under Users (or a
group) → Role mapping → filter by clients.

**Client scopes → `babysitter-dedicated`:**

- Add mapper → By configuration → **User Client Role**:
  - Client ID `babysitter`
  - Claim `resource_access.${client_id}.roles`
  - Multivalued On
  - Add to ID token **On**, access token On, introspection On
- On the Scope tab, set **Full scope allowed Off**, so tokens carry only this client's roles.

The default `roles` scope does not put roles in the ID token, which is why the mapper
is needed. Optionally, enable realm-wide Remember Me with a long SSO maximum so that
signing in again on the phone takes one tap.

## Sign-in flow

1. `GET /auth/login?next=/path` makes a random state, nonce and PKCE verifier. They
   are kept in memory, single use, for 10 minutes.
2. The browser is bound to the attempt with a login cookie holding the state:
   - on https, `__Secure-babysit_login` (`Secure`);
   - on plain-http loopback, `babysit_login`.

   Both are `HttpOnly; SameSite=Lax; Path=/auth/; Max-Age=600`, and only the one
   matching the request's scheme is read. The browser is then sent to Keycloak's
   authorization endpoint with `code_challenge_method=S256`.

   `next` is honoured only as an ASCII path on this dashboard: never `//…`, never an
   `/api/` route (some GETs start work), never an `/auth/` route.

   At most 8 pending sign-ins are kept per source address, so a flood of `/auth/login`
   from one source evicts only its own. Source addresses can be forged on loopback, so
   once all 256 slots are held by sources with one sign-in each, a new sign-in is
   refused with 503 rather than evicting one in progress.

3. `GET /auth/callback`:
   - The cookie must equal `state`, and the state must still be pending.
   - The code is exchanged with the client secret (`client_secret_basic`) and the verifier.
   - The ID token is validated:
     - `alg` must be RS256; anything else is refused.
     - Its `kid` must be in the JWKS, and the signature must verify.
     - `iss` must equal the issuer.
     - `aud` must contain the client.
     - `azp` must equal the client when it is present or when there are several audiences.
     - `exp` and `iat` are checked with 60 s of clock skew.
     - `nonce` must match.
   - `resource_access.babysitter.roles` must contain the role. Otherwise the answer is
     403 and no session is created.
   - The access and refresh tokens are discarded.
4. The browser receives the session cookie and is redirected to `next`.

Requests to the identity provider verify TLS, ignore proxy environment variables and
do not follow redirects.

Discovery is cached for 24 h, and its `issuer` must equal the configured one. A failed
discovery is not retried for 30 s, so a Keycloak outage does not stall every request.

The JWKS is cached. A malformed or short key in it is skipped without affecting the
others. A token signed with an unknown `kid` makes the dashboard fetch the JWKS again,
at most once a minute.

A provider `error` is shown only when it is a plain error code (`[a-z_]{1,40}`);
otherwise the page shows a fixed message.

## Sessions

- The cookie holds 32 random bytes. `auth.sqlite` stores only their SHA-256.
- On the https host, the cookie is `__Host-babysit_session` (`Secure`). On plain-http loopback it is `babysit_session`. Both are `HttpOnly; SameSite=Strict; Path=/`. Only the cookie matching the request's scheme is read.
- Lifetimes on the https host:
  - 7 days idle, extended by use; `last_seen` is written at most every 5 minutes.
  - 30 days absolute.
  - 12 hours for break-glass sessions.
- Lifetimes on plain-http loopback: 1 hour idle and 8 hours absolute, for break-glass sessions too.
- Live session rows are cached for 60 s; a miss is never cached. A revocation from the CLI therefore takes effect within a minute, or at once when the session's idle time is next renewed.
- No refresh tokens are kept. Removing the role takes effect at the next sign-in; `pr_supervisor.py auth sessions revoke --all` makes it immediate.
- **Sign out** in the header POSTs `/auth/logout` (with `X-Babysit-Action: logout`). This deletes the session and clears the cookie. The page then goes to Keycloak's end-session endpoint. No ID token is stored to pass as `id_token_hint`, so Keycloak shows its own logout confirmation page. Confirming there ends the SSO session and returns to the dashboard.

**Loopback is for the mini's own browser only.** Browsers send cookies to every port of
a host, so a plain-http `127.0.0.1` session cookie also goes to any other local server
the same browser visits, such as a dev server or an agent's preview. That is why
loopback sessions are short.

The residual risk: within those 8 hours, a malicious local server visited in the same
browser can read the cookie and use the session. Use the tailnet https host wherever
possible. If you need loopback access without Keycloak, a break-glass link is the
fallback.

SameSite=Strict works because the page shell is public and all data comes from
same-origin `fetch`. A link from Matrix opens `/` without the cookie, and the page's
own requests then carry it.

## Keys and policy

A key is `bsk_<kind>_<43 base64url characters>`. Its prefix routes it and lets
secret scanners spot it. Clients send it as `Authorization: Bearer <key>`. Keys are
stored hashed, with scopes, an optional binding and expiry, `last_used` (written at
most every 5 minutes) and `revoked_at`.

| Credential           | Default scope                                 |
| -------------------- | --------------------------------------------- |
| Session, `admin` key | Everything                                    |
| `read`               | `GET /api/*`                                  |
| `launch`             | `/api/agent/*` (the routes arrive in Phase 2) |
| `worker`             | `POST /api/worker/*`                          |
| `ext`                | `/api/extension`                              |
| `push`               | `POST /api/push-read`, `POST /api/push-seen`  |

A scope is `[GET|POST|OPTIONS ]/path`, or a prefix ending in `*`.
`keys create --scope` can narrow a key, for example to `GET /api/status`. It warns when
a prefix does not end in `/*`, because `/api/push*` also covers `/api/push-subscribe`.

`read` is broad. `GET /api/*` covers:

- agent transcripts and diffs (`workspace-transcript`, `workspace-diff`);
- settings, logs and CI logs;
- the GETs whose `?refresh=1` starts work (`llm-usage`, `attention`, `sentry`, `workspace-overview`).

Give a narrower scope when a client needs less.

**Public**, answering anyone:

- the static shell (any `GET` outside `/api/`);
- `/auth/*`;
- `GET /api/session`.

**Own credential**, still checked by their handlers until Phase 2 moves those credentials into the key store:

- `POST /api/worker/*`: worker tokens;
- `/api/extension`: extension pairing tokens;
- `POST /api/push-read`, `push-seen` and `push-unsubscribe`: push device tokens.

A request target that is not a plain path (`//host/…`, `http://…`) is never public
or own.

**Launch keys** carry a binding: `origin`, `cwd`, `agent`, `docker`, `sandbox`, and
optionally `config_dir`, `claude_account`, `op`, `root` and `depth`. `origin` is
`user` for agents the user starts (New task, or `safe()` in a terminal), and `cron`
or `subtask` for cron runs and the sub-tasks agents start. Phase 2/3 gives `user`
launches looser sub-task limits than `cron` and `subtask` ones, where `pr_workspaces`
applies `SUBTASK_DEPTH` and the per-lineage hourly limit. Launch keys expire after
7 days.

## Break-glass

`pr_supervisor.py auth login-link` prints a one-time link, valid for 10 minutes, for
each host in `oidc.json` plus `http://127.0.0.1:8765` (`--port` changes the port). It
needs no Keycloak.

- The token sits in the URL fragment (`/auth/break-glass#t=…`), so link previews never send it and proxies never log it.
- The page removes it from the address bar and POSTs it with `X-Babysit-Action: break-glass`.
- The result is a session with `via=break-glass`: 12 hours on https, 8 hours on loopback.
- A link works once.

## Report

- Each would-refuse request is counted in `auth.sqlite` by method, route and credential kind (`anonymous`, `invalid`, or a key kind).
  - The route is one of the dashboard's own routes (`dashboard.API_ROUTES`); any other path counts as `/api/(other)` or `/(other)`. Made-up paths therefore cannot grow the table or the log.
  - Query strings are never recorded.
- A line goes to the dashboard's stderr log at most once a minute per route and kind. No key, cookie or query string is ever recorded.
- In enforce mode the same counter records actual refusals.

The report shows in three places:

- `/api/status` has `auth: {mode, would_deny}`.
- The header's session button shows the count in report mode.
- `GET /api/auth-report` and `pr_supervisor.py auth report` list the rows. `auth report --reset` starts the count afresh, for example after a Phase 2 client migrates.

## CLI

Every `auth` command refuses to run inside a Seatbelt sandbox: the auth home is the
trust root.

```sh
pr_supervisor.py auth status                        # files present, live keys and sessions, report
pr_supervisor.py auth admin-key [--rotate]          # AUTH_HOME/admin-key (0600); rotate revokes the old one
pr_supervisor.py auth keys list [--all]
pr_supervisor.py auth keys create --kind read [--scope 'GET /api/status'] [--expires-days 30] [--label voice]
pr_supervisor.py auth keys revoke k_…
pr_supervisor.py auth sessions list
pr_supervisor.py auth sessions revoke <id> | --all
pr_supervisor.py auth login-link [--port 8765]
pr_supervisor.py auth launch-key --agent claude --cwd "$PWD" [--docker] [--origin user|cron|subtask]
pr_supervisor.py auth report [--reset]
```

`--auth-home DIR` goes right after `auth`. A key is printed only once, by
`keys create` and `launch-key`.

## Staging against the real Keycloak

Use a throwaway dashboard, never the live one on 8765:

```sh
BABYSIT_AUTH_TEST_ALLOW_OWN_ROUTES=1 \
pr_supervisor.py --home "$(mktemp -d)" dashboard --port 8799 --auth enforce \
  --auth-home "$(mktemp -d)" --oidc-config ~/staging-oidc.json
```

The override is acceptable here only because the state home is a throwaway one.
Without it, use `--auth report`.

The staging `oidc.json` maps `127.0.0.1:8799` to `http://127.0.0.1:8799/auth/callback`,
which must be registered temporarily in Keycloak.

## Next phases

- **Phase 2:**
  - `wt` sends the admin key file as a Bearer token.
  - `/api/agent/*` arrives, with launch keys minted per launch and typed into panes by file, never by value.
  - Voice tools get `read` keys.
  - Push device tokens move into the store hashed, as `push` keys.
  - Worker tokens become `worker` keys.
  - With those three moved, the own-credential list is empty, and enforce no longer refuses to start.
  - A `safe()` zshrc snippet runs `auth launch-key`.
- **Phase 3:**
  - Switch to `--auth enforce`.
  - Drop `~/.local/state/babysit-pr` from Safehouse's read-write grants.
  - Narrow its `~/.local` read grant.
  - Raise sub-task limits for `origin: user` launches.
- **Phase 4:** a filtering herdr socket per launch. Separately, Collie goes behind Keycloak.
