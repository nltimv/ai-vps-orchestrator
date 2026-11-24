# Single sign-on (OpenID Connect)

aivo can sign people in through your own identity provider. Any standards-compliant OIDC
provider works, for example:

- Keycloak, Authentik, Authelia, Zitadel, Kanidm
- Okta, Microsoft Entra ID, Google

SSO and email + password sign-in can be used together, or SSO alone.

## 1. Register aivo at your identity provider

Create an **OpenID Connect client** (also called an "application" or "relying party"):

| Setting | Value |
|---|---|
| Client type | Confidential (with a client secret) is recommended. Public clients (PKCE only) also work: leave the secret empty. |
| Grant / flow | Authorization Code (PKCE S256 is always used) |
| Redirect URI | `https://<your aivo host>/api/v1/auth/oidc/callback` (local dev: `http://localhost:5173/api/v1/auth/oidc/callback`) |
| Scopes | `openid email profile` (+ a groups scope if your provider needs one, e.g. `groups`) |
| Claims | `email`, `email_verified`, `name`; optionally a groups claim (default name: `groups`) |

## 2. Configure aivo

| Variable (env) | Helm value | Meaning |
|---|---|---|
| `AIVO_OIDC_ISSUER` | `sso.issuer` | Issuer URL. `/.well-known/openid-configuration` must exist below it. |
| `AIVO_OIDC_CLIENT_ID` | `sso.clientId` | Client id. |
| `AIVO_OIDC_CLIENT_SECRET` | `sso.clientSecret` | Client secret (omit for a public client). |
| `AIVO_OIDC_NAME` | `sso.name` | Button text: "Continue with *name*". |
| `AIVO_OIDC_SCOPES` | `sso.scopes` | Default `openid email profile`. |
| `AIVO_PUBLIC_URL` | `publicUrl` | Dashboard URL used to build the redirect URI. Helm defaults it to `https://<ingress.host>`; otherwise it's taken from the request. |
| `AIVO_OIDC_AUTO_PROVISION` | `sso.autoProvision` | `true` (default): anyone your provider lets through gets an account. `false`: invite links only. |
| `AIVO_OIDC_ALLOWED_DOMAINS` | `sso.allowedDomains` | Only these email domains may sign in (comma separated). |
| `AIVO_OIDC_ALLOWED_GROUPS` | `sso.allowedGroups` | Only members of these groups may sign in. |
| `AIVO_OIDC_ADMIN_GROUPS` | `sso.adminGroups` | Members become admins, everyone else members. The admin role then **follows group membership on every sign-in**. |
| `AIVO_OIDC_GROUPS_CLAIM` | `sso.groupsClaim` | Name of the groups claim (default `groups`). Keycloak-style paths like `/aivo-admins` also match `aivo-admins`. |
| `AIVO_OIDC_LINK_BY_EMAIL` | `sso.linkByEmail` | Connect a first SSO sign-in to an existing account with the same email. Only happens when the provider marks the email **verified**. |
| `AIVO_PASSWORD_LOGIN` | `passwordLogin` | `false` turns off email + password sign-in entirely (SSO only). |

Helm example:

```bash
helm upgrade --install aivo oci://ghcr.io/nltimv/charts/ai-vps-orchestrator --version <your version> \
  --reset-then-reuse-values \
  --set sso.enabled=true \
  --set sso.issuer=https://sso.example.com/realms/home \
  --set sso.clientId=aivo --set sso.clientSecret=... \
  --set sso.name="Home SSO" \
  --set 'sso.adminGroups={aivo-admins}' \
  --set passwordLogin=false
```

## Who gets in, and with which role

On every sign-in:

1. **Allow-lists.** If allowed domains or groups are configured, the person must match them.
   - A domain match also requires that the provider doesn't mark the email *unverified*.
   - Members of the admin groups are always allowed.
2. **Known identity.** The identity (issuer + subject) is looked up.
   - If it is already linked, that account is used.
   - Disabled accounts are refused.
3. **First sign-in.** For an identity seen for the first time, the first matching rule wins:
   1. Link to an existing account with the same verified email (if `linkByEmail`).
   2. Use the invite link the person came through. They get the invite's role.
   3. If aivo has no users yet, this person becomes the first admin.
   4. If `autoProvision` is on, create a member account.
   5. Otherwise refuse ("you need an invite").
4. **Admin groups.** If they are configured, the role is set from group membership.

Invites work with SSO too. An admin creates an invite link (Settings → People), and the person
opens it and chooses "Continue with *name*".

Accounts created through SSO have no password. Their name and email are updated from the
provider at each sign-in.

## Security notes

- **Login flow and token checks.**
  - The flow is Authorization Code with PKCE (S256), `state` and `nonce`.
  - The temporary login state lives in a short-lived (10 min), encrypted, HttpOnly cookie scoped
    to `/api/v1/auth/oidc`.
  - The ID token is verified against the provider's published signing keys: signature,
    issuer, audience (+ `azp`), expiry and nonce.
- **Redirects.** After sign-in aivo only redirects to paths on its own site.
- **Email linking.** An existing account is never taken over through an unverified email.
- **Signing out.** It ends the aivo session. It does not sign you out of your identity
  provider.

## Trying it locally without an identity provider

A tiny development-only provider is included:

```bash
uv run python scripts/dev_oidc_provider.py --port 9000
```

```bash
AIVO_OIDC_ISSUER=http://localhost:9000 AIVO_OIDC_CLIENT_ID=aivo AIVO_OIDC_CLIENT_SECRET=dev-secret \
AIVO_OIDC_NAME="Dev SSO" AIVO_OIDC_ADMIN_GROUPS=aivo-admins uv run aivo serve
```

Its sign-in page lets you type any email, name and groups. Never expose it.

## Troubleshooting

Sign-in errors are shown on the login page. The details are in the backend log (`SSO sign-in
refused: ...`).

| Message | Likely cause |
|---|---|
| "isn't working right now" | Wrong issuer or client secret, redirect URI not registered, or clock skew over 60 s |
| "didn't share an email address" | Add the `email` scope or claim mapping |
| "can't be connected automatically" | An account with that email exists, but the provider didn't send `email_verified: true` |
| "isn't allowed" | The allowed domains or groups didn't match. Check that the groups claim is present in the ID token or userinfo. |
