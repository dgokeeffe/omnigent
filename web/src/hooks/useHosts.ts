import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

export interface Host {
  host_id: string;
  name: string;
  owner: string;
  status: "online" | "offline";
  /**
   * Sandbox provider backing a server-managed host (e.g. "modal");
   * null for user-connected hosts. Optional because older servers
   * omit the field entirely.
   */
  sandbox_provider?: string | null;
  /**
   * Per-harness readiness reported by the host's last connect, e.g.
   * `{"claude-sdk": true, "codex": "needs-auth"}`. `null`/absent means the
   * host has never reported it (older host build) — unknown, never
   * "nothing configured".
   */
  configured_harnesses?: Record<string, boolean | string> | null;
  /**
   * Whether the current user owns this host. A shared host returned by
   * `/v1/hosts` (granted, not owned) is `false`. Optional/absent on
   * older servers — treat absent as "unknown ownership", which the UI
   * renders the same as owned (no "Shared" badge) for back-compat.
   */
  owned_by_current_user?: boolean;
  /**
   * The current user's effective access on this host: `"view"`,
   * `"use"`, `"manage"`, or `"owner"`. Absent on older servers. A
   * `"view"`-only host is visible but not a valid launch target (a
   * launch needs `use`).
   */
  permission_level?: "view" | "use" | "manage" | "owner" | null;
}

/**
 * Whether a host can be launched on (a runner spawned / workspace
 * browsed). Owner/use/manage can; a `view`-only shared host cannot.
 * Absent `permission_level` (older server) is treated as launchable so
 * the picker keeps working against servers without host sharing.
 */
export function canLaunchOnHost(host: Host): boolean {
  return host.permission_level !== "view";
}

/**
 * Whether a host is shared with the current user (not owned by them).
 * Absent `owned_by_current_user` (older server) is treated as owned.
 */
export function isSharedHost(host: Host): boolean {
  return host.owned_by_current_user === false;
}

interface HostsResponse {
  hosts: Host[];
}

async function fetchHosts(includeSandbox: boolean): Promise<Host[]> {
  const res = await authenticatedFetch("/v1/hosts");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as HostsResponse;
  // Hide server-managed sandbox hosts from every host picker: they
  // are launch targets the server creates on demand (and relaunches
  // at will), not user-connectable machines, so offering them as
  // manual targets is misleading. Hosts from older servers lack the
  // field and are kept. `includeSandbox` opts a caller (the chat-header
  // HostBadge) back into seeing them so it can label sandbox sessions.
  if (includeSandbox) return body.hosts;
  return body.hosts.filter((h) => !h.sandbox_provider);
}

interface UseHostsOptions {
  enabled?: boolean;
  includeSandbox?: boolean;
}

export function useHosts(options: UseHostsOptions = {}) {
  const enabled = options.enabled ?? true;
  const includeSandbox = options.includeSandbox ?? false;
  return useQuery({
    // Distinct cache key per filtering mode so the picker's filtered
    // list and the header's unfiltered list don't overwrite each other.
    // A bare ["hosts"] invalidation still prefix-matches both.
    queryKey: ["hosts", { includeSandbox }],
    queryFn: () => fetchHosts(includeSandbox),
    enabled,
    staleTime: 10_000,
    refetchInterval: enabled ? 10_000 : false,
  });
}
