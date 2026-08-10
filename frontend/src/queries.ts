// TanStack Query hooks over the existing endpoints — caching, dedup, and
// interval refetch for free, with zero backend change. The classic dashboard
// hand-rolls setInterval polling; this replaces that with declarative queries.

import { useQuery } from "@tanstack/react-query";
import { apiGet, type JournalRow, type Snapshot, type WhoAmI } from "./api";

export function useWhoAmI() {
  return useQuery({
    queryKey: ["whoami"],
    queryFn: () => apiGet<WhoAmI>("/whoami"),
    staleTime: 60_000,
  });
}

export function useSnapshot() {
  return useQuery({
    queryKey: ["state"],
    queryFn: () => apiGet<Snapshot>("/state"),
    refetchInterval: 5_000,
  });
}

export function useJournal(limit = 50) {
  return useQuery({
    queryKey: ["journal", limit],
    queryFn: () => apiGet<JournalRow[]>(`/journal?limit=${limit}`),
    refetchInterval: 20_000,
  });
}
