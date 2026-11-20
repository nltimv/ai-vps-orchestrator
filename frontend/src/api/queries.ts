import { keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { del, get, post, put } from "./client";
import type {
  CacheStatus, Catalog, Estimate, GalleryPage, Health, Instance, Job, OffersResponse, SettingsResponse,
  Spending, User,
} from "./types";

export const useMe = () =>
  useQuery({ queryKey: ["me"], queryFn: () => get<User>("/auth/me"), retry: false, staleTime: 60_000 });

export const useAuthStatus = () =>
  useQuery({ queryKey: ["auth-status"], queryFn: () => get<{ needs_setup: boolean }>("/auth/status") });

export const useCatalog = () =>
  useQuery({ queryKey: ["catalog"], queryFn: () => get<Catalog>("/catalog"), staleTime: 5 * 60_000 });

export const useActiveJobs = () =>
  useQuery({ queryKey: ["jobs", "active"], queryFn: () => get<Job[]>("/jobs?active=true&limit=20"), refetchInterval: 15_000 });

export const useRecentJobs = () =>
  useQuery({ queryKey: ["jobs", "recent"], queryFn: () => get<Job[]>("/jobs?limit=12") });

export const useJob = (id: string | undefined) =>
  useQuery({ queryKey: ["job", id], queryFn: () => get<Job>(`/jobs/${id}`), enabled: !!id });

export const useGallery = (kind: string, mine: boolean) =>
  useInfiniteQuery({
    queryKey: ["gallery", kind, mine],
    initialPageParam: "",
    queryFn: ({ pageParam }) => {
      const q = new URLSearchParams({ limit: "30" });
      if (kind !== "all") q.set("kind", kind);
      if (mine) q.set("mine", "true");
      if (pageParam) q.set("cursor", pageParam);
      return get<GalleryPage>(`/gallery?${q}`);
    },
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

export const useMachines = () =>
  useQuery({ queryKey: ["machines"], queryFn: () => get<{ live: Instance[]; recent: Instance[] }>("/machines"), refetchInterval: 20_000 });

export const useOffers = (kind: string, modelId: string, quality: string, preference: string) =>
  useQuery({
    queryKey: ["offers", kind, modelId, quality, preference],
    queryFn: () => {
      const q = new URLSearchParams({ kind, quality, preference });
      if (modelId) q.set("model_id", modelId);
      return get<OffersResponse>(`/machines/offers?${q}`);
    },
    placeholderData: keepPreviousData,
  });

export const useSpending = () =>
  useQuery({ queryKey: ["spending"], queryFn: () => get<Spending>("/spending"), refetchInterval: 60_000 });

export const useSettings = () =>
  useQuery({ queryKey: ["settings"], queryFn: () => get<SettingsResponse>("/settings") });

export const useCacheStatus = () =>
  useQuery({ queryKey: ["cache"], queryFn: () => get<CacheStatus>("/admin/cache"), refetchInterval: 15_000 });

export const useUsers = () =>
  useQuery({
    queryKey: ["users"],
    queryFn: () => get<(User & { is_active: boolean; last_login_at: string | null })[]>("/admin/users"),
  });

export const useHealth = () =>
  useQuery({ queryKey: ["health"], queryFn: () => get<Health>("/health"), refetchInterval: 30_000 });

export function useEstimate(body: unknown, enabled: boolean) {
  return useQuery({
    queryKey: ["estimate", body],
    queryFn: () => post<Estimate>("/estimate", body),
    enabled,
    staleTime: 20_000,
    placeholderData: keepPreviousData,
    retry: false,
  });
}

export function useSaveSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: Record<string, unknown>) => put<{ settings: unknown }>("/settings", patch),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["catalog"] });
    },
  });
}

export function useDeleteJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => del(`/jobs/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["gallery"] }),
  });
}
