import type { InfiniteData } from "@tanstack/react-query";
import { keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { del, get, patch, post, put } from "./client";
import type {
  AdminJob, AuthStatus, BulkAction, Catalog, Collection, Estimate, GpusResponse, Health, ItemDetail, ItemsPage, Job, JobLog,
  OffersResponse, SettingsResponse, Spending, User,
} from "./types";

export const useMe = () =>
  useQuery({ queryKey: ["me"], queryFn: () => get<User>("/auth/me"), retry: false, staleTime: 60_000 });

export const useAuthStatus = () =>
  useQuery({ queryKey: ["auth-status"], queryFn: () => get<AuthStatus>("/auth/status") });

export const useCatalog = () =>
  useQuery({ queryKey: ["catalog"], queryFn: () => get<Catalog>("/catalog"), staleTime: 5 * 60_000 });

export const useActiveJobs = () =>
  useQuery({ queryKey: ["jobs", "active"], queryFn: () => get<Job[]>("/jobs?active=true&limit=20"), refetchInterval: 15_000 });

export const useRecentJobs = () =>
  useQuery({ queryKey: ["jobs", "recent"], queryFn: () => get<Job[]>("/jobs?limit=12") });

export const useJob = (id: string | undefined) =>
  useQuery({ queryKey: ["job", id], queryFn: () => get<Job>(`/jobs/${id}`), enabled: !!id });

export interface ItemFilter {
  kind: string; // "all" | Kind
  favorites: boolean;
  collection?: string;
}

/** The user's own gallery, a page at a time (endless scrolling). */
export const useItems = (f: ItemFilter) =>
  useInfiniteQuery({
    queryKey: ["items", f.kind, f.favorites, f.collection ?? ""],
    initialPageParam: "",
    queryFn: ({ pageParam }) => {
      const q = new URLSearchParams({ limit: "40" });
      if (f.kind !== "all") q.set("kind", f.kind);
      if (f.favorites) q.set("favorites", "true");
      if (f.collection) q.set("collection", f.collection);
      if (pageParam) q.set("cursor", pageParam);
      return get<ItemsPage>(`/items?${q}`);
    },
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

export const useItem = (id: string | undefined) =>
  useQuery({ queryKey: ["item", id], queryFn: () => get<ItemDetail>(`/items/${id}`), enabled: !!id });

export const useCollections = () =>
  useQuery({ queryKey: ["collections"], queryFn: () => get<Collection[]>("/collections") });

export const useAdminJobs = (enabled: boolean) =>
  useQuery({ queryKey: ["admin-jobs"], queryFn: () => get<AdminJob[]>("/admin/jobs?limit=30"), enabled, refetchInterval: 15_000 });

/** After changing items: the gallery pages, item pages and collection counts are stale. */
function useGalleryChanged() {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: ["items"] });
    qc.invalidateQueries({ queryKey: ["item"] });
    qc.invalidateQueries({ queryKey: ["collections"] });
  };
}

export function useFavorite() {
  const qc = useQueryClient();
  const changed = useGalleryChanged();
  return useMutation({
    mutationFn: ({ id, favorite }: { id: string; favorite: boolean }) => put(`/items/${id}/favorite`, { favorite }),
    onMutate: ({ id, favorite }) => {
      // the heart fills at once
      qc.setQueriesData<InfiniteData<ItemsPage>>({ queryKey: ["items"] }, (old) =>
        old && { ...old, pages: old.pages.map((p) => ({ ...p, items: p.items.map((i) => (i.id === id ? { ...i, favorite } : i)) })) });
      qc.setQueryData<ItemDetail>(["item", id], (old) => old && { ...old, item: { ...old.item, favorite } });
    },
    onSettled: changed,
  });
}

export function useBulk() {
  const changed = useGalleryChanged();
  return useMutation({
    mutationFn: (b: { ids: string[]; action: BulkAction; collection_id?: string }) => post<{ count: number }>("/items/bulk", b),
    onSettled: changed,
  });
}

export function useDeleteItem() {
  const changed = useGalleryChanged();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => del(`/items/${id}`),
    onSettled: () => { changed(); qc.invalidateQueries({ queryKey: ["jobs"] }); },
  });
}

export function useSetItemCollections() {
  const changed = useGalleryChanged();
  return useMutation({
    mutationFn: ({ id, ids }: { id: string; ids: string[] }) => put(`/items/${id}/collections`, { collection_ids: ids }),
    onSettled: changed,
  });
}

export function useCreateCollection() {
  const changed = useGalleryChanged();
  return useMutation({ mutationFn: (name: string) => post<Collection>("/collections", { name }), onSettled: changed });
}

export function useRenameCollection() {
  const changed = useGalleryChanged();
  return useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => patch<Collection>(`/collections/${id}`, { name }),
    onSettled: changed,
  });
}

export function useDeleteCollection() {
  const changed = useGalleryChanged();
  return useMutation({ mutationFn: (id: string) => del(`/collections/${id}`), onSettled: changed });
}

export const useGpus = () =>
  useQuery({ queryKey: ["machines"], queryFn: () => get<GpusResponse>("/machines"), refetchInterval: 15_000 });

export const useJobLog = (id: string, active: boolean) =>
  useQuery({
    queryKey: ["job-log", id],
    queryFn: () => get<JobLog>(`/jobs/${id}/log`),
    refetchInterval: active ? 2_000 : false,
  });

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


export const useUsers = () =>
  useQuery({
    queryKey: ["users"],
    queryFn: () =>
      get<(User & { is_active: boolean; last_login_at: string | null; sso: boolean; has_password: boolean })[]>(
        "/admin/users",
      ),
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
      // adult content on/off changes what this account may see
      for (const key of ["me", "gallery", "jobs"]) qc.invalidateQueries({ queryKey: [key] });
    },
  });
}
