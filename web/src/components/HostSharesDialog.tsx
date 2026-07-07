/**
 * Per-host share management dialog: view, add, and revoke `use`-style
 * grants on a host. Opened from the admin Hosts page; the server gates
 * every call owner/admin/manage, so the same dialog serves an owner if
 * surfaced elsewhere later.
 *
 * Grantees are entered by identity string (the email the auth header
 * carries in header/OIDC mode) — deploys don't necessarily expose a
 * user directory to search, so a plain input is the mode-agnostic UX.
 */

import { useState } from "react";
import { Trash2Icon, UserPlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { showToast } from "@/components/ui/toast";
import {
  type HostGrantLevel,
  useGrantHostPermission,
  useHostPermissions,
  useRevokeHostPermission,
} from "@/hooks/useHostPermissions";

const LEVEL_LABELS: Record<HostGrantLevel, string> = {
  view: "View",
  use: "Use",
  manage: "Manage",
};

const LEVEL_DESCRIPTIONS: Record<HostGrantLevel, string> = {
  view: "sees the host, cannot launch",
  use: "can launch sessions on the host",
  manage: "use + can manage shares",
};

export function HostSharesDialog({
  hostId,
  hostName,
  open,
  onOpenChange,
}: {
  hostId: string;
  hostName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { data: grants, error, isLoading } = useHostPermissions(open ? hostId : null);
  const grant = useGrantHostPermission();
  const revoke = useRevokeHostPermission();
  const [newUser, setNewUser] = useState("");
  const [newLevel, setNewLevel] = useState<HostGrantLevel>("use");
  const pending = grant.isPending || revoke.isPending;

  async function onAdd() {
    const userId = newUser.trim();
    if (!userId) return;
    try {
      await grant.mutateAsync({ hostId, userId, level: newLevel });
      setNewUser("");
      showToast(`Granted ${LEVEL_LABELS[newLevel].toLowerCase()} on ${hostName} to ${userId}.`);
    } catch (e) {
      showToast(`Could not grant access: ${e instanceof Error ? e.message : e}`);
    }
  }

  async function onRevoke(userId: string) {
    try {
      await revoke.mutateAsync({ hostId, userId });
      showToast(`Revoked ${userId}'s access to ${hostName}.`);
    } catch (e) {
      showToast(`Could not revoke access: ${e instanceof Error ? e.message : e}`);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Share {hostName}</DialogTitle>
          <DialogDescription>
            Grant other users access to this host. <b>Use</b> lets them launch their own sessions on
            it; <b>view</b> only makes it visible; <b>manage</b> also lets them administer shares.
            Sharing a host never shares your sessions.
          </DialogDescription>
        </DialogHeader>

        {error !== null && (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          >
            Could not load shares: {error instanceof Error ? error.message : String(error)}
          </div>
        )}

        {isLoading && <div className="text-sm text-muted-foreground">Loading…</div>}

        {grants !== undefined && (
          <div className="flex flex-col gap-1" data-testid="host-share-grant-list">
            {grants.length === 0 && (
              <p className="text-sm text-muted-foreground">Not shared with anyone yet.</p>
            )}
            {grants.map((g) => (
              <div key={g.user_id} className="flex items-center justify-between gap-2 text-sm">
                <span className="min-w-0 break-all">{g.user_id}</span>
                <span className="flex shrink-0 items-center gap-1">
                  <Select
                    value={g.level}
                    onValueChange={(level) =>
                      void grant.mutateAsync({
                        hostId,
                        userId: g.user_id,
                        level: level as HostGrantLevel,
                      })
                    }
                    disabled={pending}
                  >
                    <SelectTrigger className="h-7 w-28 text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {(Object.keys(LEVEL_LABELS) as HostGrantLevel[]).map((level) => (
                        <SelectItem key={level} value={level} className="text-xs">
                          {LEVEL_LABELS[level]}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Button
                    variant="ghost"
                    size="icon"
                    title={`Revoke ${g.user_id}'s access`}
                    onClick={() => void onRevoke(g.user_id)}
                    disabled={pending}
                  >
                    <Trash2Icon className="size-4" />
                  </Button>
                </span>
              </div>
            ))}
          </div>
        )}

        <div className="flex items-center gap-2">
          <Input
            value={newUser}
            onChange={(e) => setNewUser(e.target.value)}
            placeholder="user@example.com"
            className="text-sm"
            disabled={pending}
            onKeyDown={(e) => {
              if (e.key === "Enter") void onAdd();
            }}
            data-testid="host-share-user-input"
          />
          <Select
            value={newLevel}
            onValueChange={(v) => setNewLevel(v as HostGrantLevel)}
            disabled={pending}
          >
            <SelectTrigger className="w-32 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {(Object.keys(LEVEL_LABELS) as HostGrantLevel[]).map((level) => (
                <SelectItem key={level} value={level} className="text-xs">
                  <span className="flex flex-col">
                    <span>{LEVEL_LABELS[level]}</span>
                    <span className="text-[10px] text-muted-foreground">
                      {LEVEL_DESCRIPTIONS[level]}
                    </span>
                  </span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            onClick={() => void onAdd()}
            disabled={pending || newUser.trim() === ""}
            data-testid="host-share-add-button"
          >
            <UserPlusIcon /> Add
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
