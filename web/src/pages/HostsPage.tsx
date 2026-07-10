/**
 * Admin hosts management page (``/settings/hosts``). Rendered as a
 * Settings sub-category, following the Members / Policies pattern.
 *
 * Lists EVERY host connected to the server — across all owners — so an
 * operator can audit the fleet: who owns what, what's online, which
 * harnesses each host has configured, and how many sessions are bound
 * to it.
 *
 * Gated on the client by `useIsAdmin` (chrome only) AND on the server
 * by the `?all=true` admin check on `GET /v1/hosts` — the server is
 * what actually enforces.
 */

import { useState } from "react";
import { PowerIcon, RefreshCwIcon, Share2Icon } from "lucide-react";
import { HostSharesDialog } from "@/components/HostSharesDialog";
import { PageScroll } from "@/components/PageScroll";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { showToast } from "@/components/ui/toast";
import { type AdminHost, useAdminHosts, useShutdownHost } from "@/hooks/useAdminHosts";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { cn } from "@/lib/utils";

export function HostsPage() {
  const isAdmin = useIsAdmin();
  const { data: hosts, error, isLoading, refetch } = useAdminHosts({ enabled: isAdmin });
  const shutdown = useShutdownHost();
  const [shutdownCandidate, setShutdownCandidate] = useState<AdminHost | null>(null);
  const [sharesTarget, setSharesTarget] = useState<AdminHost | null>(null);

  async function onConfirmShutdown() {
    if (shutdownCandidate === null) return;
    const target = shutdownCandidate;
    try {
      await shutdown.mutateAsync(target.host_id);
      showToast(`Shutting down ${target.name} — it will show offline shortly.`);
    } catch (e) {
      showToast(`Could not shut down ${target.name}: ${e instanceof Error ? e.message : e}`);
    }
    setShutdownCandidate(null);
  }

  // Non-admin: hard stop. Server would also 403, this is just UX.
  // `useIsAdmin` reports false while identity is still resolving, so a
  // brief flash of this message for an admin is possible but harmless —
  // the query flips it as soon as /v1/me answers.
  if (!isAdmin) {
    return (
      <div className="mx-auto w-full max-w-2xl px-6 py-12">
        <h1 className="mb-2 text-2xl font-semibold">Hosts</h1>
        <p className="text-sm text-muted-foreground">You don't have permission to manage hosts.</p>
      </div>
    );
  }

  return (
    <PageScroll contentClassName="px-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Hosts</h1>
      </div>
      <p className="mb-4 text-sm text-muted-foreground">
        Every host connected to this server, across all owners. Sessions counts how many sessions
        are bound to the host.
      </p>

      {error !== null && (
        <div
          role="alert"
          className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          Could not load hosts. You may not have admin permission, or the server is unreachable.
        </div>
      )}

      {isLoading && (
        <div className="flex min-h-32 items-center justify-center text-sm text-muted-foreground">
          Loading…
        </div>
      )}

      {hosts !== undefined && hosts.length > 0 && (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Host</th>
                <th className="px-3 py-2 font-medium">Owner</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Harnesses</th>
                <th className="px-3 py-2 font-medium">Sessions</th>
                <th className="px-3 py-2 font-medium">Last seen</th>
                <th className="px-3 py-2 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {hosts.map((h) => (
                <HostRow
                  key={h.host_id}
                  host={h}
                  onShutdown={() => setShutdownCandidate(h)}
                  onManageShares={() => setSharesTarget(h)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {hosts !== undefined && hosts.length === 0 && (
        <p className="text-sm text-muted-foreground">No hosts have connected to this server.</p>
      )}

      <div className="mt-3 flex items-center justify-end">
        <Button variant="ghost" size="sm" onClick={() => void refetch()}>
          <RefreshCwIcon /> Refresh
        </Button>
      </div>

      {/* ── Share management ─────────────────────────────────── */}
      {sharesTarget !== null && (
        <HostSharesDialog
          hostId={sharesTarget.host_id}
          hostName={sharesTarget.name}
          open
          onOpenChange={(open) => {
            if (!open) setSharesTarget(null);
          }}
        />
      )}

      {/* ── Shutdown confirmation ────────────────────────────── */}
      <Dialog
        open={shutdownCandidate !== null}
        onOpenChange={(open) => {
          if (shutdown.isPending) return;
          if (!open) setShutdownCandidate(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Shut down {shutdownCandidate?.name}?</DialogTitle>
            <DialogDescription>
              The host daemon will terminate its runners and exit — it will not reconnect until
              someone restarts it on the host machine.
              {shutdownCandidate !== null && shutdownCandidate.session_count > 0 && (
                <>
                  {" "}
                  {shutdownCandidate.session_count} session
                  {shutdownCandidate.session_count === 1 ? " is" : "s are"} bound to this host; any
                  active runs will be interrupted.
                </>
              )}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => setShutdownCandidate(null)}
              disabled={shutdown.isPending}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => void onConfirmShutdown()}
              disabled={shutdown.isPending}
            >
              {shutdown.isPending ? "Shutting down…" : "Shut down"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </PageScroll>
  );
}

function HostRow({
  host,
  onShutdown,
  onManageShares,
}: {
  host: AdminHost;
  onShutdown: () => void;
  onManageShares: () => void;
}) {
  const online = host.status === "online";
  return (
    <tr className="border-t border-border">
      <td className="px-3 py-2 align-middle">
        <div className="font-medium">{host.name}</div>
        <div className="font-mono text-xs text-muted-foreground">{host.host_id}</div>
      </td>
      <td className="px-3 py-2 align-middle">
        <span className="break-all">{host.owner}</span>
        {host.sandbox_provider && (
          <Badge variant="outline" className="ml-2">
            {host.sandbox_provider}
          </Badge>
        )}
      </td>
      <td className="px-3 py-2 align-middle">
        <span className="inline-flex items-center gap-1.5">
          <span
            aria-hidden
            className={cn("size-2 shrink-0 rounded-full", online ? "bg-success" : "bg-destructive")}
          />
          {online ? "Online" : "Offline"}
        </span>
      </td>
      <td className="px-3 py-2 align-middle">
        <HarnessList harnesses={host.configured_harnesses} />
      </td>
      <td className="px-3 py-2 align-middle tabular-nums">{host.session_count}</td>
      <td className="px-3 py-2 align-middle text-muted-foreground">
        {formatEpoch(host.last_seen)}
      </td>
      <td className="px-3 py-2 text-right align-middle">
        <Button
          variant="ghost"
          size="xs"
          title={
            host.sandbox_provider
              ? "Managed sandbox hosts cannot be shared"
              : "Manage who can use this host"
          }
          onClick={onManageShares}
          disabled={Boolean(host.sandbox_provider)}
        >
          <Share2Icon /> Shares
        </Button>
        {/* Offline hosts have no tunnel to signal; managed sandbox hosts
            are torn down by the server's own lifecycle, not this action. */}
        <Button
          variant="ghost"
          size="xs"
          title={
            host.sandbox_provider
              ? "Managed sandbox hosts are terminated automatically"
              : !online
                ? "Host is offline"
                : "Shut down this host"
          }
          onClick={onShutdown}
          disabled={!online || Boolean(host.sandbox_provider)}
        >
          <PowerIcon /> Shut down
        </Button>
      </td>
    </tr>
  );
}

/**
 * Configured-harness pills. `true` renders normally; a string value
 * (e.g. "needs-auth") or `false` renders muted with the state in the
 * tooltip. `null`/absent means the host has never reported harness
 * readiness (older host build) — unknown, not "none".
 */
function HarnessList({ harnesses }: { harnesses: AdminHost["configured_harnesses"] }) {
  if (!harnesses || Object.keys(harnesses).length === 0) {
    return <span className="text-xs text-muted-foreground">Unknown</span>;
  }
  return (
    <div className="flex flex-wrap gap-1">
      {Object.entries(harnesses).map(([name, state]) => (
        <Badge
          key={name}
          variant={state === true ? "secondary" : "outline"}
          className={cn(state !== true && "text-muted-foreground")}
          title={state === true ? name : `${name}: ${state}`}
        >
          {name}
        </Badge>
      ))}
    </div>
  );
}

function formatEpoch(epoch: number): string {
  return new Date(epoch * 1000).toLocaleString();
}
