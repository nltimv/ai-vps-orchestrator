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

**Plaintext.** It exists only in RAM: a tmpfs scratch on the backend, and tmpfs job
directories on the GPU machine.

**Key rotation:**
1. Add a new key: `k2:…`.
2. Set `AIVO_MEDIA_ACTIVE_KEY_ID=k2`.
3. Run `aivo rewrap-keys`. Only the wrap section of each file is rewritten.
4. Remove `k1`.

**Backups.** Back up the media volume and the KEK *separately*. Without the KEK, the volume is
unreadable.

## Backend ↔ GPU machine
**Push model.** The backend calls the machine, never the other way round. The backend holds the
cloud token, the KEK and the DB, so it is never exposed.

**Per-machine trust:**
- **Certificates.** Each machine gets a throwaway CA and a leaf certificate for
  `i-<instance-uuid>.worker.aivo.invalid`. The CA's private key is discarded at once. The
  backend pins that CA and sends the synthetic name as SNI. If a destroyed machine's public IP
  is reused by someone else, the handshake fails, and no prompt or token is ever sent there.
- **Tokens.** A 256-bit bearer token is checked in constant time. user_data holds only its
  hash. The token is rotated on first contact, and the backend stores it encrypted.
- **Inside the machine:**
  - Containers cannot reach the cloud metadata service (iptables `DOCKER-USER` rule).
  - ComfyUI listens on `127.0.0.1` only.
  - The agent strips metadata from outputs.
- **Network.**
  - A per-deployment Cloud Firewall limits port 443 to the backend's egress IP.
  - A throwaway SSH key is registered so DigitalOcean never emails a root password.
    No private key is kept.

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
  - Admins can change settings, machines, cache and users.
  - Members can create, and can see the shared gallery and their own jobs' details.

## Cloud blast radius
- Every resource is tagged with the deployment id. Sweeps never touch other deployments.
- Destroys are plain droplet deletes, never "with associated resources", so cache disks
  survive.
- The optional watchdog token can delete *any* droplet in its account. Use it only inside a
  dedicated team.
