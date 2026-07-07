import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import type { Host } from "@/hooks/useHosts";

/**
 * A host row from the admin fleet view (`GET /v1/hosts?all=true`).
 * Extends the picker's `Host` shape with the fleet-only fields the
 * server adds when `all=true`.
 */
export interface AdminHost extends Host {
  /** Unix epoch seconds of the host's first registration. */
  created_at: number;
  /** Unix epoch seconds the host was last seen (connect/heartbeat). */
  last_seen: number;
  /** Number of sessions bound to this host. */
  session_count: number;
}

interface AdminHostsResponse {
  hosts: AdminHost[];
}

async function fetchAdminHosts(): Promise<AdminHost[]> {
  const res = await authenticatedFetch("/v1/hosts?all=true");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as AdminHostsResponse;
  return body.hosts;
}

/**
 * Every host connected to the server, across all owners — admin only
 * (the server 403s non-admins; callers gate the UI with `useIsAdmin`).
 * Polls on the same 10s cadence as the host picker so status flips
 * (online/offline) surface without a manual refresh.
 */
export function useAdminHosts(options: { enabled?: boolean } = {}) {
  const enabled = options.enabled ?? true;
  return useQuery({
    queryKey: ["hosts", "admin-all"],
    queryFn: fetchAdminHosts,
    enabled,
    staleTime: 10_000,
    refetchInterval: enabled ? 10_000 : false,
  });
}
