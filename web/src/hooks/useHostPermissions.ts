/**
 * Typed client + hooks for the `/v1/hosts/{id}/permissions` endpoints.
 * Mirrors `omnigent/server/routes/hosts.py` host-sharing handlers.
 * Host levels are strings on the wire ("view" | "use" | "manage";
 * "owner" is effective-only, never grantable).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

export type HostGrantLevel = "view" | "use" | "manage";

export interface HostPermission {
  user_id: string;
  level: HostGrantLevel;
  created_at: number;
  updated_at: number;
  created_by: string | null;
}

async function readErrorDetail(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: string };
    if (body.detail) return body.detail;
  } catch {
    // Non-JSON error body — fall through to the status line.
  }
  return `${res.status} ${res.statusText}`;
}

async function listHostPermissions(hostId: string): Promise<HostPermission[]> {
  const res = await authenticatedFetch(`/v1/hosts/${encodeURIComponent(hostId)}/permissions`);
  if (!res.ok) throw new Error(await readErrorDetail(res));
  const body = (await res.json()) as { permissions: HostPermission[] };
  return body.permissions;
}

async function grantHostPermission(args: {
  hostId: string;
  userId: string;
  level: HostGrantLevel;
}): Promise<void> {
  const res = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(args.hostId)}/permissions/${encodeURIComponent(args.userId)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level: args.level }),
    },
  );
  if (!res.ok) throw new Error(await readErrorDetail(res));
}

async function revokeHostPermission(args: { hostId: string; userId: string }): Promise<void> {
  const res = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(args.hostId)}/permissions/${encodeURIComponent(args.userId)}`,
    { method: "DELETE" },
  );
  if (!res.ok) throw new Error(await readErrorDetail(res));
}

/**
 * Grants on one host. Enabled only while the share panel is open
 * (`hostId` set) — owner/admin/manage gated server-side (403 otherwise).
 */
export function useHostPermissions(hostId: string | null) {
  return useQuery({
    queryKey: ["host-permissions", hostId],
    queryFn: () => listHostPermissions(hostId as string),
    enabled: hostId !== null,
  });
}

export function useGrantHostPermission() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: grantHostPermission,
    onSuccess: (_data, args) => {
      void queryClient.invalidateQueries({ queryKey: ["host-permissions", args.hostId] });
      // A new grant changes what the grantee's /v1/hosts returns.
      void queryClient.invalidateQueries({ queryKey: ["hosts"] });
    },
  });
}

export function useRevokeHostPermission() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: revokeHostPermission,
    onSuccess: (_data, args) => {
      void queryClient.invalidateQueries({ queryKey: ["host-permissions", args.hostId] });
      void queryClient.invalidateQueries({ queryKey: ["hosts"] });
    },
  });
}
