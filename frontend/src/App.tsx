import { useQueryClient } from "@tanstack/react-query";
import { Coins, Cpu, Images, LogOut, Moon, Settings as SettingsIcon, Sparkles, Sun } from "lucide-react";
import { useState } from "react";
import { Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";

import { post } from "./api/client";
import { useServerEvents } from "./api/events";
import { useAuthStatus, useGpus, useMe, useSpending } from "./api/queries";
import { cn, money } from "./lib/format";
import { CreatePage } from "./pages/Create";
import { GalleryPage } from "./pages/Gallery";
import { ItemPage } from "./pages/Item";
import { LoginPage } from "./pages/Login";
import { MachinesPage } from "./pages/Machines";
import { SettingsPage } from "./pages/Settings";
import { SpendingPage } from "./pages/Spending";

const NAV = [
  { to: "/", label: "Create", icon: Sparkles, end: true },
  { to: "/gallery", label: "Gallery", icon: Images },
  { to: "/machines", label: "GPUs", icon: Cpu },
  { to: "/spending", label: "Spending", icon: Coins },
  { to: "/settings", label: "Settings", icon: SettingsIcon, admin: true },
];

export function App() {
  const me = useMe();
  const status = useAuthStatus();
  const loc = useLocation();
  useServerEvents(!!me.data);

  if (me.isLoading || status.isLoading) {
    return <div className="flex h-full items-center justify-center text-muted">Loading…</div>;
  }
  if (!me.data) {
    return (
      <Routes>
        <Route path="/invite/:token" element={<LoginPage mode="invite" />} />
        <Route path="*" element={<LoginPage mode={status.data?.needs_setup ? "setup" : "login"} />} />
      </Routes>
    );
  }
  if (loc.pathname.startsWith("/invite/")) return <Navigate to="/" replace />;
  const isAdmin = me.data.role === "admin";
  return (
    <div className="flex h-full">
      <Sidebar isAdmin={isAdmin} name={me.data.display_name} />
      <main className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-7xl p-4 pb-24 md:p-8">
          <Routes>
            <Route path="/" element={<CreatePage />} />
            <Route path="/gallery" element={<GalleryPage />} />
            <Route path="/item/:id" element={<ItemPage />} />
            <Route path="/machines" element={<MachinesPage isAdmin={isAdmin} />} />
            <Route path="/spending" element={<SpendingPage />} />
            <Route path="/settings" element={isAdmin ? <SettingsPage /> : <Navigate to="/" />} />
            <Route path="*" element={<Navigate to="/" />} />
          </Routes>
        </div>
      </main>
      <MobileNav isAdmin={isAdmin} />
    </div>
  );
}

function Sidebar({ isAdmin, name }: { isAdmin: boolean; name: string }) {
  const qc = useQueryClient();
  const machines = useGpus();
  const spending = useSpending();
  const running = machines.data?.workers.filter((w) => w.state !== "stopped").length ?? 0;
  const [dark, setDark] = useState(document.documentElement.classList.contains("dark"));
  const toggleTheme = () => {
    const next = !dark;
    setDark(next);
    document.documentElement.classList.toggle("dark", next);
    localStorage.setItem("aivo-theme", next ? "dark" : "light");
  };
  const logout = async () => {
    await post("/auth/logout");
    qc.clear();
    window.location.href = "/";
  };
  return (
    <aside className="panel hidden w-60 shrink-0 flex-col border-y-0 border-l-0 md:flex">
      <div className="flex items-center gap-2 px-5 py-5">
        <div className="brand-gradient flex size-8 items-center justify-center rounded-lg font-bold text-white">a</div>
        <span className="text-lg font-semibold">aivo</span>
      </div>
      <nav className="flex flex-col gap-1 px-3">
        {NAV.filter((n) => !n.admin || isAdmin).map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.end}
            className={({ isActive }) =>
              cn("flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
                isActive ? "bg-brand-500/15 font-medium text-brand-600 dark:text-brand-300" : "hover:bg-[var(--panel-2)]")
            }
          >
            <n.icon className="size-4" />
            {n.label}
          </NavLink>
        ))}
      </nav>
      <div className="mt-6 px-5 text-xs text-muted">
        <div className="flex items-center gap-2">
          <span className={cn("size-2 rounded-full", running ? "bg-emerald-500 animate-pulse" : "bg-zinc-500")} />
          {running ? `${running} GPU${running > 1 ? "s" : ""} running` : "No GPUs running"}
        </div>
        {spending.data && (
          <div className="mt-2">
            {money(spending.data.compute_usd, 2)} of {money(spending.data.hard_cap_usd, 0)} this month
          </div>
        )}
      </div>
      <div className="mt-auto flex items-center justify-between gap-2 border-t border-base px-4 py-3 text-sm">
        <span className="truncate">{name}</span>
        <div className="flex gap-1">
          <button onClick={toggleTheme} className="rounded p-1.5 hover:bg-[var(--panel-2)]" aria-label="Toggle theme">
            {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
          </button>
          <button onClick={logout} className="rounded p-1.5 hover:bg-[var(--panel-2)]" aria-label="Sign out">
            <LogOut className="size-4" />
          </button>
        </div>
      </div>
    </aside>
  );
}

function MobileNav({ isAdmin }: { isAdmin: boolean }) {
  return (
    <nav className="panel fixed inset-x-0 bottom-0 z-40 flex justify-around border-x-0 border-b-0 py-2 md:hidden">
      {NAV.filter((n) => !n.admin || isAdmin).map((n) => (
        <NavLink key={n.to} to={n.to} end={n.end}
          className={({ isActive }) => cn("flex flex-col items-center gap-0.5 px-2 text-[11px]", isActive ? "text-brand-500" : "text-muted")}>
          <n.icon className="size-5" />
          {n.label}
        </NavLink>
      ))}
    </nav>
  );
}
