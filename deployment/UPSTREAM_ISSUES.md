# Upstream Issues Found During Deployment

Issues discovered while deploying agr_ai_curation with GitHub OAuth on a
standalone host. These are bugs or design gaps in the upstream codebase that we
worked around locally but should be fixed in the main project.

---

## 1. Logout auto-re-login bug

**Component:** frontend

**Summary:**
After a user logs out, they are immediately logged back in automatically. The
logout action appears to have no effect.

**Root Cause:**
The frontend route guard auto-redirects unauthenticated users to
`/api/auth/login`. After logout, the `justLoggedOut` sessionStorage flag is
consumed on the first React render, but the next render sees the user as
unauthenticated and triggers the auto-redirect to login. With GitHub OAuth
(which has no IdP-side logout endpoint), GitHub's session persists and instantly
re-authenticates the user without any prompt.

The backend logout itself is correct -- the auth cookie IS cleared via
`Set-Cookie`. The problem is entirely in the frontend flow. The current design
assumes Cognito/OIDC providers that have IdP logout endpoints capable of
destroying the upstream session.

**Fix Suggestion:**
Show a login page with a button instead of auto-redirecting unauthenticated
users, or persist the `justLoggedOut` flag across multiple renders so the
auto-redirect is suppressed long enough for the user to see the logged-out
state.

**Files Affected:**
- Frontend route guard / auth redirect logic

---

## 2. Logout endpoint required authentication

**Component:** backend

**Summary:**
`POST /api/auth/logout` required a valid auth token via `get_auth_dependency()`.
If the user's token was expired or invalid, the endpoint returned 401 and the
auth cookie was never cleared -- a catch-22 where users with bad tokens could
never log out.

**Root Cause:**
The logout route handler had the auth dependency injected, so FastAPI's
dependency injection rejected the request before the handler body (which clears
the cookie) ever executed.

**Fix/Workaround:**
Removed the auth dependency from the logout endpoint so it always executes and
clears the cookie regardless of token state.

**Files Affected:**
- `backend/src/api/auth.py`

---

## 3. Logout Set-Cookie from fetch() not processed by browser

**Component:** backend

**Summary:**
The logout endpoint's `Set-Cookie` header (to clear the auth cookie) was not
being applied by the browser.

**Root Cause:**
Two compounding issues:

1. The logout endpoint returned a plain Python dict. FastAPI wrapped this in a
   new `JSONResponse`, discarding any `Set-Cookie` headers that had been set on
   the response object.
2. Even after fixing the endpoint to return a `JSONResponse` directly, browsers
   may not process `Set-Cookie` headers from `fetch()` responses when the
   JavaScript immediately performs a synchronous `window.location` redirect
   after the fetch completes.

**Fix/Workaround:**
Added a `GET /api/auth/logout/redirect` endpoint that returns an HTTP 302
redirect with the `Set-Cookie` header inline. The GitHub provider's
`get_logout_url()` returns this endpoint URL so the browser navigates to it
directly (not via fetch), guaranteeing the cookie is cleared before the redirect
to the post-logout destination.

**Files Affected:**
- `backend/src/api/auth.py`
- `backend/src/auth/providers/github.py`

---

## 4. barista_token not passed from JWT claims to user payload

**Component:** backend

**Summary:**
The `barista_token` stored in JWT claims was silently dropped when constructing
the user payload, so downstream code that needed it received `None`.

**Root Cause:**
`_get_user_from_cookie_impl()` built the user payload dict exclusively from
`AuthPrincipal` fields, discarding any extra JWT claims such as `barista_token`.

**Fix/Workaround:**
Added `claims.get("barista_token")` to the user payload dict returned by
`_get_user_from_cookie_impl()`.

**Files Affected:**
- `backend/src/api/auth.py`

---

## 5. docker-compose.production.yml doesn't pass through custom env vars

**Component:** deployment

**Summary:**
New environment variables required by additional auth providers or integrations
are not automatically available to containers.

**Root Cause:**
The backend service's `environment` block in `docker-compose.production.yml`
uses explicit key-value mappings rather than passthrough syntax. Any new
environment variables (e.g. `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`,
`BARISTA_API_TOKEN`) must be manually added to the compose file.

**Fix Suggestion:**
Either document the requirement to update the compose file when adding new
providers, or switch to an `env_file` directive that reads from a `.env` file so
new variables are picked up automatically. At minimum, this is a pattern issue:
every new auth provider or integration that needs env vars also requires a
compose file change.

**Files Affected:**
- `docker-compose.production.yml`

---

## 6. Published frontend image is broken

**Component:** deployment

**Summary:**
The ECR image at `public.ecr.aws/v4p5b7m9/agr-ai-curation-frontend` tagged
`smoke-20260310-final` is an empty Alpine scratch image with no application
content. It cannot be used to run the frontend.

**Root Cause:**
Unknown build/publish issue. The image contains only the base Alpine layer
with no built frontend assets.

**Fix/Workaround:**
Build the frontend from source with `--build-arg VITE_DEV_MODE=false` to
produce a working production image.

**Files Affected:**
- CI/CD pipeline or image publishing workflow
- `frontend/Dockerfile`

---

## 7. Package agent_bundles manifest must be updated for new agents

**Component:** architecture

**Summary:**
Adding a new agent YAML file to the `agents/` directory is not sufficient for
the agent to be active. The agent must also be registered in the package
manifest.

**Root Cause:**
The `system_agent_sync` process on startup reconciles the active agent list
against the `agent_bundles` list in the package manifest. Any agent not listed
in the manifest is deactivated, even if its `agent.yaml` file exists in the
agents directory.

**Fix Suggestion:**
Either auto-discover agents from the filesystem (scan the agents directory) or
document this requirement prominently. The current behavior is surprising
because the agent file can exist and appear valid but silently gets deactivated.

**Files Affected:**
- `packages/alliance/package.yaml` (the `agent_bundles` list)

---

## 8. noctua-py must be in alliance package runtime requirements, not just backend

**Component:** architecture

**Summary:**
Python dependencies needed by package tools must be declared in the package's
own requirements file, not in the backend's requirements.

**Root Cause:**
The package runner executes tools in isolated per-package virtual environments.
Dependencies listed only in `backend/requirements.txt` are not available in the
package venv. For example, `noctua-py` must be in the alliance package's runtime
requirements for tools that import it.

**Fix Suggestion:**
Add `noctua-py` (and any other tool dependencies) to the package runtime
requirements. Document that package tool dependencies go in the package
requirements file, not the backend requirements.

**Files Affected:**
- `packages/alliance/requirements/runtime.txt`
- `backend/requirements.txt` (where the dependency was incorrectly placed)

---

## 9. GROBID cgroup v2 crash

**Component:** PDF extraction (PDFX) deployment

**Summary:**
The `grobid/grobid:0.8.2-crf` Docker image crashes with a
`NullPointerException` on hosts using cgroup v2 (e.g. Ubuntu 24.04).

**Root Cause:**
The JVM's container detection logic fails on cgroup v2 systems, producing a
`NullPointerException` during startup when it tries to read cgroup v1-style
resource limits that do not exist.

**Fix/Workaround:**
Add the following environment variable to the grobid service in the compose
file:

```yaml
environment:
  - JAVA_TOOL_OPTIONS=-XX:-UseContainerSupport
```

This disables the JVM's container resource detection, avoiding the crash.

**Files Affected:**
- PDFX docker-compose file (grobid service definition)
