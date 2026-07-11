/**
 * Admin sessions fleet view (``/settings/sessions``). Rendered as a
 * Settings sub-category, following the Hosts / Members / Policies pattern.
 *
 * Lists EVERY session on the server — across all owners — so an operator
 * can audit the fleet: who owns each session, which host/runner it's
 * bound to, and its current status. Complements the Hosts page, which
 * shows only a per-host session count.
 *
 * Gated on the client by `useIsAdmin` (chrome only) AND on the server by
 * the `?all=true` admin check on `GET /v1/sessions` — the server is what
 * actually enforces.
 */

import { RefreshCwIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { type AdminSession, useAdminSessions } from "@/hooks/useAdminSessions";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { cn } from "@/lib/utils";

export function SessionsPage() {
  const isAdmin = useIsAdmin();
  const { data: sessions, error, isLoading, refetch } = useAdminSessions({ enabled: isAdmin });

  // Non-admin: hard stop. Server would also 403, this is just UX.
  // `useIsAdmin` reports false while identity is still resolving, so a
  // brief flash of this message for an admin is possible but harmless —
  // the query flips it as soon as /v1/me answers.
  if (!isAdmin) {
    return (
      <div className="mx-auto w-full max-w-2xl px-6 py-12">
        <h1 className="mb-2 text-2xl font-semibold">Sessions</h1>
        <p className="text-sm text-muted-foreground">
          You don't have permission to view all sessions.
        </p>
      </div>
    );
  }

  return (
    <PageScroll contentClassName="px-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Sessions</h1>
      </div>
      <p className="mb-4 text-sm text-muted-foreground">
        Every session on this server, across all owners. Owner and Host show who the session
        belongs to and where its runner is bound.
      </p>

      {error !== null && (
        <div
          role="alert"
          className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          Could not load sessions. You may not have admin permission, or the server is unreachable.
        </div>
      )}

      {isLoading && (
        <div className="flex min-h-32 items-center justify-center text-sm text-muted-foreground">
          Loading…
        </div>
      )}

      {sessions !== undefined && sessions.length > 0 && (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Session</th>
                <th className="px-3 py-2 font-medium">Owner</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Host</th>
                <th className="px-3 py-2 font-medium">Runner</th>
                <th className="px-3 py-2 font-medium">Updated</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <SessionRow key={s.id} session={s} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {sessions !== undefined && sessions.length === 0 && (
        <p className="text-sm text-muted-foreground">No sessions exist on this server.</p>
      )}

      <div className="mt-3 flex items-center justify-end">
        <Button variant="ghost" size="sm" onClick={() => void refetch()}>
          <RefreshCwIcon /> Refresh
        </Button>
      </div>
    </PageScroll>
  );
}

function SessionRow({ session }: { session: AdminSession }) {
  return (
    <tr className="border-t border-border">
      <td className="px-3 py-2 align-middle">
        <div className="font-medium">{session.title || "(untitled)"}</div>
        <div className="font-mono text-xs text-muted-foreground">{session.id}</div>
        {session.agent_name && (
          <div className="text-xs text-muted-foreground">{session.agent_name}</div>
        )}
      </td>
      <td className="px-3 py-2 align-middle">
        <span className="break-all">{session.owner ?? "—"}</span>
      </td>
      <td className="px-3 py-2 align-middle">
        <StatusBadge status={session.status} />
      </td>
      <td className="px-3 py-2 align-middle">
        {session.host_id ? (
          <span className="break-all font-mono text-xs">{session.host_id}</span>
        ) : (
          <span className="text-xs text-muted-foreground">local</span>
        )}
      </td>
      <td className="px-3 py-2 align-middle">
        {session.runner_id ? (
          <span className="break-all font-mono text-xs">{session.runner_id}</span>
        ) : (
          <span className="text-xs text-muted-foreground">—</span>
        )}
      </td>
      <td className="px-3 py-2 align-middle text-muted-foreground">
        {formatEpoch(session.updated_at)}
      </td>
    </tr>
  );
}

/**
 * Status pill. "running"/"waiting" are the live states worth highlighting;
 * everything else (idle, failed, absent) renders muted so the eye lands on
 * what's actually consuming a runner.
 */
function StatusBadge({ status }: { status: AdminSession["status"] }) {
  if (!status) return <span className="text-xs text-muted-foreground">—</span>;
  const live = status === "running" || status === "waiting";
  return (
    <Badge
      variant={live ? "secondary" : "outline"}
      className={cn(!live && "text-muted-foreground")}
    >
      {status}
    </Badge>
  );
}

function formatEpoch(epoch: number): string {
  return new Date(epoch * 1000).toLocaleString();
}
