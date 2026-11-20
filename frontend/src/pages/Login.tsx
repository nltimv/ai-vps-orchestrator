import { useQueryClient } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";
import { useState } from "react";
import { useParams } from "react-router-dom";

import { post } from "@/api/client";
import { useAuthStatus } from "@/api/queries";
import { Button, Card, ErrorNote, Field, Input } from "@/components/ui";

const SSO_ERRORS: Record<string, string> = {
  denied: "Sign-in was cancelled.",
  not_allowed: "Your account isn't allowed to use aivo. Ask an admin to give you access.",
  not_invited: "You need an invite to join. Ask an admin for an invite link.",
  account_disabled: "Your account has been turned off. Ask an admin.",
  email_in_use:
    "An account with this email already exists, but your sign-in provider didn't confirm the address, so it can't be connected automatically. Ask an admin.",
  no_email: "Your sign-in provider didn't share an email address, which aivo needs.",
  session_expired: "Sign-in took too long or was interrupted. Please try again.",
  provider_unreachable: "Couldn't reach the sign-in provider. Please try again in a moment.",
};
const SSO_GENERIC = "Single sign-on isn't working right now. Ask an admin to check the server logs.";

export function LoginPage({ mode }: { mode: "login" | "setup" | "invite" }) {
  const qc = useQueryClient();
  const status = useAuthStatus();
  const { token } = useParams();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const ssoError = new URLSearchParams(window.location.search).get("sso_error");

  const sso = status.data?.sso;
  const passwords = status.data?.password_login ?? true;
  const ssoHref = () => {
    const q = new URLSearchParams({ return_to: "/" });
    if (mode === "invite" && token) q.set("invite", token);
    return `/api/v1/auth/oidc/login?${q}`;
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "login") await post("/auth/login", { email, password });
      else if (mode === "setup") await post("/auth/setup", { email, display_name: name, password });
      else await post("/auth/accept-invite", { token, email, display_name: name, password });
      await qc.invalidateQueries();
      window.history.replaceState(null, "", "/");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const title = mode === "login" ? "Welcome back" : mode === "setup" ? "Set up aivo" : "Join aivo";
  const sub =
    mode === "login"
      ? "Sign in to create images, videos and music."
      : mode === "setup"
        ? sso?.enabled
          ? "Create the first (admin) account — the first person to sign in becomes the admin."
          : "Create the first (admin) account. You can invite others later."
        : "You've been invited. Sign in to accept.";
  return (
    <div className="flex min-h-full items-center justify-center p-4">
      <Card className="w-full max-w-sm p-8">
        <div className="brand-gradient mb-6 flex size-11 items-center justify-center rounded-xl text-xl font-bold text-white">
          a
        </div>
        <h1 className="text-2xl font-semibold">{title}</h1>
        <p className="mb-6 mt-1 text-sm text-muted">{sub}</p>
        {ssoError && (
          <div className="mb-4">
            <ErrorNote error={SSO_ERRORS[ssoError] ?? SSO_GENERIC} />
          </div>
        )}
        {sso?.enabled && (
          <a href={ssoHref()}>
            <Button type="button" variant="primary" size="lg" className="w-full">
              <KeyRound className="size-5" /> Continue with {sso.name}
            </Button>
          </a>
        )}
        {sso?.enabled && passwords && (
          <div className="my-6 flex items-center gap-3 text-xs text-muted">
            <span className="h-px flex-1 bg-[var(--border)]" /> or use a password <span className="h-px flex-1 bg-[var(--border)]" />
          </div>
        )}
        {passwords && (
          <form onSubmit={submit} className="flex flex-col gap-4">
            {mode !== "login" && (
              <Field label="Your name">
                <Input value={name} onChange={(e) => setName(e.target.value)} required autoComplete="name" />
              </Field>
            )}
            <Field label="Email">
              <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email" />
            </Field>
            <Field label="Password" hint={mode !== "login" ? "at least 10 characters" : undefined}>
              <Input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={mode === "login" ? undefined : 10}
                autoComplete={mode === "login" ? "current-password" : "new-password"}
              />
            </Field>
            <ErrorNote error={error} />
            <Button type="submit" variant={sso?.enabled ? "secondary" : "primary"} size="lg" loading={busy}>
              {mode === "login" ? "Sign in" : "Create account"}
            </Button>
          </form>
        )}
        {!passwords && !sso?.enabled && (
          <ErrorNote error="No sign-in method is enabled. Ask the administrator to configure single sign-on." />
        )}
      </Card>
    </div>
  );
}
