import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";

import { post } from "@/api/client";
import { Button, Card, ErrorNote, Field, Input } from "@/components/ui";

export function LoginPage({ mode }: { mode: "login" | "setup" | "invite" }) {
  const qc = useQueryClient();
  const { token } = useParams();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

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
    mode === "login" ? "Sign in to create images, videos and music."
      : mode === "setup" ? "Create the first (admin) account. You can invite others later."
        : "You've been invited. Choose your name and password.";
  return (
    <div className="flex min-h-full items-center justify-center p-4">
      <Card className="w-full max-w-sm p-8">
        <div className="brand-gradient mb-6 flex size-11 items-center justify-center rounded-xl text-xl font-bold text-white">a</div>
        <h1 className="text-2xl font-semibold">{title}</h1>
        <p className="mt-1 mb-6 text-sm text-muted">{sub}</p>
        <form onSubmit={submit} className="flex flex-col gap-4">
          {mode !== "login" && (
            <Field label="Your name"><Input value={name} onChange={(e) => setName(e.target.value)} required autoComplete="name" /></Field>
          )}
          <Field label="Email"><Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email" /></Field>
          <Field label="Password" hint={mode !== "login" ? "at least 10 characters" : undefined}>
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required
              minLength={mode === "login" ? undefined : 10} autoComplete={mode === "login" ? "current-password" : "new-password"} />
          </Field>
          <ErrorNote error={error} />
          <Button type="submit" variant="primary" size="lg" loading={busy}>
            {mode === "login" ? "Sign in" : "Create account"}
          </Button>
        </form>
      </Card>
    </div>
  );
}
