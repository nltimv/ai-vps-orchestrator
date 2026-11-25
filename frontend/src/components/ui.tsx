import { Loader2, X } from "lucide-react";
import type { ButtonHTMLAttributes, HTMLAttributes, InputHTMLAttributes, ReactNode, TextareaHTMLAttributes } from "react";
import { useEffect } from "react";

import { cn } from "@/lib/format";

type Variant = "primary" | "secondary" | "ghost" | "danger";

export function Button({
  variant = "secondary", size = "md", loading, className, children, ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md" | "lg"; loading?: boolean }) {
  return (
    <button
      {...rest}
      disabled={rest.disabled || loading}
      className={cn(
        "inline-flex items-center justify-center gap-2 rounded-lg font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer",
        size === "sm" && "h-8 px-3 text-sm",
        size === "md" && "h-10 px-4 text-sm",
        size === "lg" && "h-12 px-6 text-base",
        variant === "primary" && "brand-gradient text-white shadow-sm hover:opacity-95",
        variant === "secondary" && "panel hover:bg-[var(--panel-2)]",
        variant === "ghost" && "hover:bg-[var(--panel-2)]",
        variant === "danger" && "bg-red-600 text-white hover:bg-red-700",
        className,
      )}
    >
      {loading && <Loader2 className="size-4 animate-spin" />}
      {children}
    </button>
  );
}

export function Card({ className, children, ...rest }: HTMLAttributes<HTMLDivElement> & { children: ReactNode }) {
  return <div className={cn("panel rounded-xl", className)} {...rest}>{children}</div>;
}

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={cn(
        "panel h-10 w-full rounded-lg px-3 text-sm outline-none focus:border-brand-500 placeholder:text-[var(--muted)]",
        props.className,
      )}
    />
  );
}

export function Textarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      {...props}
      className={cn(
        "panel w-full rounded-lg p-3 text-sm outline-none focus:border-brand-500 placeholder:text-[var(--muted)] resize-y",
        props.className,
      )}
    />
  );
}

export function Label({ children, hint }: { children: ReactNode; hint?: string }) {
  return (
    <div className="mb-2 flex items-baseline justify-between gap-2">
      <span className="text-sm font-medium">{children}</span>
      {hint && <span className="text-xs text-muted">{hint}</span>}
    </div>
  );
}

/** A form field whose caption is a real <label> (clicking it focuses the input; screen readers announce it). */
export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-2 flex items-baseline justify-between gap-2">
        <span className="text-sm font-medium">{label}</span>
        {hint && <span className="text-xs text-muted">{hint}</span>}
      </span>
      {children}
    </label>
  );
}

export function Chips<T extends string>({
  options, value, onChange, allowNone, name,
}: {
  options: { id: T; label: string; hint?: string }[];
  value: T | null;
  onChange: (v: T | null) => void;
  allowNone?: boolean;
  name: string;
}) {
  return (
    <div role="radiogroup" aria-label={name} className="flex flex-wrap gap-2">
      {options.map((o) => {
        const active = value === o.id;
        return (
          <button
            key={o.id}
            type="button"
            role="radio"
            aria-checked={active}
            title={o.hint}
            onClick={() => onChange(active && allowNone ? null : o.id)}
            className={cn(
              "h-9 rounded-full border px-4 text-sm transition-colors cursor-pointer",
              active
                ? "border-brand-500 bg-brand-500/15 text-brand-600 dark:text-brand-300"
                : "border-base hover:bg-[var(--panel-2)]",
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <label className="inline-flex cursor-pointer items-center gap-3 text-sm">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={cn("relative h-6 w-11 rounded-full transition-colors", checked ? "bg-brand-500" : "bg-zinc-400/40")}
      >
        <span className={cn("absolute top-0.5 size-5 rounded-full bg-white shadow transition-all", checked ? "left-5.5" : "left-0.5")} />
      </button>
      {label}
    </label>
  );
}

export function Badge({ children, tone = "neutral", title }: { children: ReactNode; tone?: "neutral" | "good" | "warn" | "bad" | "brand"; title?: string }) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium",
        tone === "neutral" && "bg-zinc-500/15 text-[var(--muted)]",
        tone === "good" && "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
        tone === "warn" && "bg-amber-500/15 text-amber-700 dark:text-amber-400",
        tone === "bad" && "bg-red-500/15 text-red-600 dark:text-red-400",
        tone === "brand" && "bg-brand-500/15 text-brand-600 dark:text-brand-300",
      )}
    >
      {children}
    </span>
  );
}

export function Progress({ value, className }: { value: number; className?: string }) {
  return (
    <div className={cn("h-2 w-full overflow-hidden rounded-full panel-2", className)}>
      <div className="h-full brand-gradient transition-all duration-500" style={{ width: `${Math.round(Math.min(1, Math.max(0, value)) * 100)}%` }} />
    </div>
  );
}

export function Modal({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: string; children: ReactNode }) {
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div role="dialog" aria-label={title} className="panel w-full max-w-lg rounded-xl p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold">{title}</h2>
          <button onClick={onClose} aria-label="Close" className="rounded p-1 hover:bg-[var(--panel-2)]"><X className="size-5" /></button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Empty({ icon, title, children }: { icon: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-base p-12 text-center">
      <div className="text-muted">{icon}</div>
      <div className="font-medium">{title}</div>
      {children && <div className="max-w-md text-sm text-muted">{children}</div>}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof Error ? error.message : String(error);
  return <div className="rounded-lg bg-red-500/10 px-3 py-2 text-sm text-red-600 dark:text-red-400">{msg}</div>;
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-muted">{subtitle}</p>}
      </div>
      {actions}
    </div>
  );
}

export function Stat({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <Card className="p-4">
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <div className="mt-1 text-2xl font-semibold">{value}</div>
      {sub && <div className="mt-1 text-xs text-muted">{sub}</div>}
    </Card>
  );
}
