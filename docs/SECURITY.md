# Signal-OS Security

## Authorized use only

**Signal-OS performs OSINT on real, named, private individuals who have not
consented to being investigated.**

That is what the platform does. It correlates usernames, phone numbers, email
addresses, EXIF GPS coordinates, breach records and social profiles into a
profile of a specific human being. In most jurisdictions — GDPR Art. 6(1)(f),
UK DPA 2018 legitimate interests, and the equivalents elsewhere — processing
that data about an identifiable person without a lawful basis is unlawful, and
"it was publicly available" is not by itself a lawful basis.

**Only use this platform for:**

- Investigations you are **legally authorised** to conduct: law enforcement with
  proper authority, court-ordered disclosure, a documented regulatory
  investigation, or a contractual penetration test that explicitly authorises
  OSINT collection.
- Your **own** accounts, infrastructure and property.
- Subjects who have given **informed, specific, revocable consent** — and where
  you can show that consent.

**Never** use it to:

- Harass, stalk, intimidate or surveil a person.
- Compile a dossier on someone for employment, lending, tenancy, insurance or
  any other decision about a human being.
- Aggregate personal data in violation of data-protection law because the
  individual pieces are public.
- Conduct political, journalistic or activist surveillance of individuals.
  Investigative journalists should read the guidance of their own legal desk
  and their newsroom's ethics code first; the tool's capability is not a legal
  opinion.

Operators are responsible for having a lawful basis, a documented scope, and
records showing who authorised each investigation. The audit trail exists to
make that demonstrable.

---

## 1. Threat model

### What we are defending against

| Threat | Control |
|---|---|
| Unauthenticated use of the API | API-key auth, fails closed, failure throttling |
| Abuse by a **legitimate** user (key holder running unlimited investigations) | Per-scope token buckets, auth-failure throttling, audit trail |
| SSRF from the server into its own network | `security.py`: resolved-IP checks, metadata-range blocking, redirect revalidation |
| Path traversal via uploaded filenames or ids | `safe_join`, `sanitize_filename`, id regex validation |
| DNS rebinding (public at validation, private at connect) | Checks applied to **resolved addresses**, re-checked per redirect hop |
| Cloud credential theft via metadata endpoint | `169.254.169.254` blocked; `ALLOW_PRIVATE_NETWORK` documented as the one exception |
| Decompression / oversized-response memory exhaustion | Streaming byte caps on every fetch and read |
| Secret leakage into git history | `detect-private-key` + secret-file hooks in `.pre-commit-config.yaml` |
| A connector's failure taking down a pipeline | Every connector returns `{"error": ...}`, never raises |
| Data retained after a subject exercises erasure | `DELETE /api/cases/{id}` cascades; `STORE_MAX_ROWS` bounds retention |

### What we do **not** defend against

Stated plainly, because a threat model that overstates its own coverage is worse
than none:

- **A compromised host.** If the operator's machine is root-compromised, the
  Tor exit IP, the API keys and the store are all the attacker's.
- **Traffic analysis.** Tor hides the destination, not the pattern. A long
  investigation from one exit node is recognisable to a global observer.
- **Social engineering.** An operator can still talk to people. No technical
  control stops that.
- **Correlated public sources.** We can block internal SSRF; we cannot prevent
  a determined analyst from manually combining public records.
- **Over-collection by the operator.** The platform will investigate whatever
  string you hand it, including a person it has no permission to investigate.
  Scope discipline is a human control.

---

## 2. Authentication

Implemented in `auth.py`; enforced on every guarded route by
`api/deps.py`.

### Scheme

Two equivalent headers:

```
Authorization: Bearer <key>
X-API-Key: <key>
```

```bash
# Single key, full access
SIGNAL_API_KEY=<key>

# Multiple keys with scopes: label:key:scope1,scope2
SIGNAL_API_KEYS=ci:ci-key:read,search ops:ops-key:investigate,read
```

Scopes: `investigate`, `search`, `read`, `write`, `opsec`, `admin`, `*`.

### Properties

- **Constant-time comparison.** `hmac.compare_digest` against **every**
  configured key. The loop does not short-circuit, so neither a match's
  position nor its timing is observable. A non-ASCII key is rejected before
  comparison rather than crashing the check.
- **Fail closed.** Auth enabled with no configured key refuses every request
  with an explicit server-misconfiguration error. It does not wave the request
  through.
- **No secret in logs.** A failed attempt logs the client IP and the path. The
  presented key is never logged — not in full, not truncated, and neither is
  its label.
- **Failure throttling.** 10 failures from one IP within 300 s trips a throttle,
  so the endpoint cannot be used as an online key-guessing oracle.
- **Generic errors.** A missing key and a wrong key produce the same message, so
  the API never confirms that a particular key exists.

### Modes

`AUTH_DISABLED=true` **or** `ENVIRONMENT=development` disables the gate and the
principal becomes `{"id": "anonymous", "scopes": ["*"]}`.

> `ENVIRONMENT=development` disabling auth is convenient and dangerous: a
> production deployment that inherits it has an open OSINT API. Set
> `ENVIRONMENT=production` explicitly, and confirm with
> `curl -s localhost:8766/api/config | jq .env_presence`.

---

## 3. SSRF defence

`backend/security.py` is the most security-critical module in the repository: it
is what stands between an operator-supplied URL and the server's own network.

### Controls

1. **Scheme allowlist.** Only `http` and `https`.
2. **Blocked IP ranges, both families.** Loopback, link-local, private, reserved,
   multicast and CGNAT.
3. **Cloud metadata endpoints blocked explicitly, by address and by hostname** —
   not merely by virtue of being link-local:

   | Address / host | Provider |
   |---|---|
   | `169.254.169.254` | AWS / Azure IMDS |
   | `169.254.170.2` | AWS ECS task metadata |
   | `100.100.100.200` | Alibaba Cloud |
   | `metadata.google.internal`, `metadata.goog`, `metadata` | GCP |

4. **IPv6 handled on its own terms.** `::ffff:127.0.0.1` and 6to4 addresses are
   judged on the **embedded IPv4 address**, not on their v6 properties — an
   IPv4-mapped address is the classic way to smuggle loopback past a v4-only
   check. Teredo, NAT64 and site-local are blocked as well.
5. **Checked on resolved addresses.** The guard resolves the hostname and
   validates the resulting IP, not the hostname string. This closes the
   rebinding hole where a name resolves public during validation and private
   during the actual connection.
6. **Redirects followed manually** with a hop limit, and the check re-runs on
   every hop. A public URL that 302s to `169.254.169.254` is caught.
7. **Hop-by-hop headers and `Authorization` are stripped** when the host changes
   across a redirect (`_strip_hop_secrets`), so a redirect cannot capture a
   credential.
8. **Streaming byte cap** on every read, so a decompression bomb or an endless
   body cannot exhaust memory.
9. **Content-type allowlist** and an honest User-Agent on uploads.

Failures return `{"error": ...}` rather than raising — a crawler must never
take down a pipeline.

### `ALLOW_PRIVATE_NETWORK`

`ALLOW_PRIVATE_NETWORK=true` disables the private-range checks **and** the DNS
resolution they depend on.

It exists because the docker-compose stack resolves `searxng` and `spiderfoot`
as private service names, which the guard would otherwise block.

> **Use it only inside a Compose network you control.** On any normal host —
> especially a cloud VM with an attached instance role — enabling it re-exposes
> the instance metadata endpoint to SSRF, which is a straightforward credential
> theft path. `railway.json` correctly sets it `false`.

---

## 4. Input validation

Request models in `schemas.py` set `extra="forbid"` and cap every field:

| Field class | Cap |
|---|---|
| `input` | 4096 chars |
| `query` | 512 |
| `name` / `location` | 256 |
| `notes` | 8192 |
| `engines` | 20 entries, validated against a known set |
| `tags` | 50 · `models` 12 · `entities` 500 |

Unknown fields are a **422**, not silently dropped — a typo like `input_typey`
is visible instead of riding along unnoticed.

Investigation ids are validated against `^[A-Za-z0-9_-]{8,32}$` before reaching
storage, because they become filenames and dict keys: `../` and shell
metacharacters must not survive.

Uploads are checked for size, content type, magic bytes and sanitised filename.

---

## 5. Rate limiting

Token bucket per `(principal, scope)`. See
[`API.md` — Rate limiting](API.md#rate-limiting) for the full table.

Scopes exist because a uniform limit is useless: `POST /api/investigate` fans out
to ~10 agents and dozens of outbound calls, while `GET /api/health` is nearly
free. Limiting them together would either break the app or break the health
check.

Two limits to understand before relying on this as a blast-radius control:

- **Buckets are per-process.** With `WORKERS=4` the effective ceiling is 4× the
  configured rate. Horizontal scaling multiplies it until the limiter is moved
  to Redis-backed shared counting.
- **429 is a real risk to legitimate operators.** Six investigations a minute is
  correct for a shared deployment and restrictive for a single analyst doing
  sequential work. Tune per deployment rather than disabling it.

---

## 6. The OPSEC / Tor layer

`backend/opsec.py`. When `OPSEC_ENABLED=true`, outbound HTTP goes through
`TOR_PROXY`, default `socks5h://127.0.0.1:9050`.

`socks5h` matters: it resolves DNS **through Tor**. `socks5://` would leak the
target hostname to the local resolver, which defeats a large part of the point.

`TOR_CONTROL` (`127.0.0.1:9051`) accepts `SIGNAL NEWNYM` to request a new exit
node — exposed as `POST /api/opsec/newcircuit`, and documented in
[`RUNBOOK.md`](RUNBOOK.md#rotate-the-tor-circuit).

### What Tor does and does not do

**Does:** hide the operator's IP from the target; prevent passive correlation of
the source address across unrelated third-party sources; make the investigation
traffic indistinguishable in kind from other Tor traffic.

**Does not:** hide the fact that a Tor client is connecting; prevent a determined
target from correlating on timing or behaviour; protect the operator's *own*
machine, which is where the real data lands; survive an operator who also runs a
browser session against the same subject on their normal connection.

Keep `ControlPort` bound to loopback. Anyone who can reach it can rotate the
egress identity of your host.

`/api/opsec/status` reports the current exit IP so you can confirm the routing
is actually in effect.

---

## 7. Secrets handling

- **Never commit a `.env`.** `.gitignore` covers it; the `no-secret-files` and
  `detect-private-key` pre-commit hooks are the backstop for `git add -f`.
- **`/api/config` never returns secret values.** It reports `env_presence` —
  booleans saying whether a variable is set. Verify with
  `curl -s localhost:8766/api/config | jq .env_presence`.
- **Keys are not logged** — not on success, not on failure, not truncated.
- **Rotate on suspicion, not on cleaning.** Deleting a line from history is not
  a rotation; the key must be considered compromised. Revoke at the provider
  first.

---

## 8. Audit trail

With `AUDIT_LOG=true` (default), every mutating route writes an event through
`store.audit`: the principal id, the action, and the target. Investigation
submission, report export and case deletion are all recorded.

This serves two purposes: it makes a GDPR erasure demonstrable (you can show
what existed and that it was removed), and it is the record an operator presents
when asked to show they had authorisation.

Auditing is best-effort — a store failure does not fail the request. Treat the
trail as evidentiary, not as a transactional guarantee.

---

## 9. GDPR and data retention

### Retention bound

`STORE_MAX_ROWS` (default **5000**) caps each collection, evicting oldest first.
This is a hard ceiling, not a suggestion: a deployment that runs for a year
does not accumulate a year of investigations.

Note that `STORE_MAX_ROWS` bounds *rows*, not subjects. 5000 investigations
concerning one person is still one person's data.

### Erasure path

```bash
curl -X DELETE \
  -H "Authorization: Bearer $SIGNAL_API_KEY" \
  https://signal-os.example.com/api/cases/<case_id>
```

`DELETE /api/cases/{case_id}` removes the case **and every investigation filed
under it** in one transaction, and writes a `case.deleted` audit event.

The cascade is the important part. Deleting only the case row and leaving its
investigations — and the entities and signals inside them — would not be
erasure.

### What deletion does not reach

State this before you promise erasure:

- **Qdrant semantic memory.** VAULT stores entities in Qdrant
  (`QDRANT_COLLECTION`, default `signal_os_memory`). Deleting a case does not
  purge the vector store. Deleting the collection is a manual operation.
- **Agent store directories.** `VAULT_DIR`, `SENTINEL_DIR` and `REPORTS_DIR` hold
  on-disk artefacts — snapshots, generated reports — that are outside the
  database. Remove them by hand for a true purge.
- **Postgres backups.** A backup taken before erasure still contains the rows.
  Erasure propagates forward, not backward.
- **Export artefacts.** Any ZIP a user already downloaded.

A GDPR response that claims completeness without checking those four is a
compliance failure, not a technical one. The full procedure is in
[`RUNBOOK.md` — GDPR erasure](RUNBOOK.md#gdpr-erasure-for-a-case).

### Data protection principles the code supports

- **Purpose limitation.** Every investigation belongs to a `case_id` with a
  name and notes. Articulate the purpose before opening one.
- **Data minimisation.** Store what the report needs. Uploads land in a
  per-case temp directory deleted when the pipeline finishes.
- **Storage limitation.** `STORE_MAX_ROWS` plus the erasure path.
- **Integrity and confidentiality.** Auth, scopes, rate limits, SSRF defence.

---

## 10. Known gaps

Listed because a security document that claims no gaps is not describing this
codebase:

1. **No per-subject retention or deletion.** Data is retained per case, not per
   person. A subject appearing in several cases must be purged from each.
2. **No consent workflow.** The platform has no mechanism to record or verify a
   subject's consent. That bookkeeping is entirely the operator's.
3. **Qdrant and the on-disk agent stores are outside the erasure path** (§9).
4. **Rate-limit state is per-process**, so it is not a global control across
   replicas (§5).
5. **`ALLOW_PRIVATE_NETWORK=true` disables SSRF protection** by design (§3).
6. **No automatic audit-log rotation or export.** The trail grows with
   `STORE_MAX_ROWS` like everything else.
7. **Connector API keys are read from the environment at import time**, so
   rotating one requires a process restart.
8. **No TLS termination in the application.** Terminate at nginx, a load
   balancer or the PaaS; see [`DEPLOY.md`](DEPLOY.md).

---

## 11. Reporting a vulnerability

Report privately to the repository maintainers. Please do not open a public
issue for an authentication bypass, an SSRF hole or a credential leak.

Include: what you found, the version or commit, reproduction steps, and impact.
Give reasonable time for a fix before public disclosure.

---

## Related documents

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — how the layers fit together
- [`API.md`](API.md) — endpoint reference
- [`DEPLOY.md`](DEPLOY.md) — deploying safely
- [`RUNBOOK.md`](RUNBOOK.md) — abuse reports, erasure, circuit rotation
- [`CONTRACTS.md`](CONTRACTS.md) — frozen interface contracts