# Security

## Encryption at rest

**Envelope encryption.** Every stored file gets its own random 256-bit data key. That key is
wrapped with a key-encryption key (KEK) from `AIVO_MEDIA_KEKS`, a Kubernetes Secret or `.env`.

**File format (`AIVOENC1`).** Files are cut into 64 KiB AES-256-GCM chunks.
- Nonce: `chunk_index ‖ final_flag`.
- Associated data: the authenticated header (file id = asset UUID, plaintext size).

This detects tampering, truncation, reordering, appended data and a header swapped in from
another file. A byte range is served by decrypting only the chunks it covers, which is what
makes video seeking work.

**What is encrypted:**
- outputs, thumbnails and posters, and uploaded starting images;
- prompts and lyrics, sealed field by field in the DB.

**Plaintext.** It exists only in RAM on the backend (a tmpfs scratch), and on the RunPod worker
while it works (in `/dev/shm` where that is big enough, otherwise its temporary disk; deleted right
after delivery).

**Key rotation:**
1. Add a new key: `k2:…`.
2. Set `AIVO_MEDIA_ACTIVE_KEY_ID=k2`.
3. Run `aivo rewrap-keys`. Only the wrap section of each file is rewritten.
4. Remove `k1`.

**Backups.** Back up the media volume and the KEK *separately*. Without the KEK, the volume is
unreadable.

## Backend ↔ RunPod workers
**Tickets, not secrets.** RunPod's queue gives each job to a worker. The job's whole input is a ticket:
aivo's address, the job id and a one-time 256-bit token. The worker uses it to talk to aivo over HTTPS
(`/api/v1/worker/jobs/<id>/…`):
- **Storage:** aivo stores only the token's SHA-256 and compares in constant time.
- **Scope:** it opens only that job: its spec, its starting picture, progress, results, done/failed.
- **Lifetime:** it works only while the job waits for or is with a worker. It stops working when the
  job ends, is cancelled or moves to another GPU type.
- **No hints:** a wrong token and an unknown job both get 401.
- **Uploads:** each upload is checked against the SHA-256 the worker sends with it.

**What RunPod holds:**
- **API key:** aivo's RunPod API key stays in the backend.
- **Endpoint settings:** the worker image, aivo's deployment id and the GPU type. No keys or tokens.
- **Jobs:** only the ticket (useless once the job ends) and the result's file names.
- **Prompts and pictures:** never in RunPod's job system. RunPod runs the GPU, so on the worker itself
  aivo trusts RunPod as much as any GPU provider.

**Inside the worker:** ComfyUI listens on `127.0.0.1` only, and the worker strips metadata from
outputs. Hugging Face downloads of gated models use short-lived signed links that aivo resolves with
its own token; workers never hold it.

## Web application
- **Sessions.** argon2id password hashes. Session cookies are opaque tokens (only their
  sha256 is stored), HttpOnly, `SameSite=Lax`, and `Secure` behind TLS.
- **Single sign-on.**
  - OIDC Authorization Code with PKCE, `state` and `nonce`; ID tokens are verified against the
    provider's signing keys.
  - Accounts are only linked by email when the provider marks it verified.
  - Details are in [sso.md](sso.md).
- **CSRF.** Every state-changing API call must carry `X-Requested-With: aivo`. Browsers can't
  send that header cross-site without a CORS preflight, and none is allowed.
- **Headers.** nginx sends a Content-Security-Policy, `X-Frame-Options: DENY`, `nosniff` and
  `Referrer-Policy`.
- **Uploads.** They are re-encoded to PNG, which drops EXIF/GPS metadata, and capped at 25 MB
  and 2048 px.
- **Roles.**
  - Admins can change settings, GPUs and users, and see the worker logs of creations.
  - Members can create, and can see the shared gallery and their own jobs' details.

## RunPod blast radius
- Every endpoint aivo makes carries the deployment id. Cleanup never touches other endpoints.
- The RunPod API key can manage everything in its RunPod account. Keep it in a Secret, and use a
  dedicated RunPod account (or team) if others share yours.
