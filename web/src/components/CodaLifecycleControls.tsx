import { useCallback, useEffect, useState } from "react";
import { RotateCcwIcon, UnplugIcon } from "lucide-react";

import {
  getSessionSlim,
  listCodaClaims,
  listCodaSandboxOptions,
  releaseCodaClaim,
  releaseSession,
  resumeSession,
  type CodaClaim,
  type CodaSandboxOption,
} from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface CodaLifecycleControlsProps {
  session: Session;
}

/** Owner-only, history-preserving Release and explicit Resume controls. */
export function CodaLifecycleControls({ session }: CodaLifecycleControlsProps) {
  const isOwner = session.permissionLevel == null || session.permissionLevel >= 4;
  const [claims, setClaims] = useState<CodaClaim[]>([]);
  const [detached, setDetached] = useState(session.detached === true);
  const [dialog, setDialog] = useState<"release" | "resume" | null>(null);
  const [sandboxes, setSandboxes] = useState<CodaSandboxOption[]>([]);
  const [sandboxAppId, setSandboxAppId] = useState("");
  const [pending, setPending] = useState<"session" | "sandbox" | "resume" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const claim = claims.find((item) =>
    item.sessions.some((candidate) => candidate.id === session.id),
  );

  const refresh = useCallback(async () => {
    const [nextClaims, snapshot] = await Promise.all([
      listCodaClaims(),
      getSessionSlim(session.id, { refreshState: true }),
    ]);
    setClaims(nextClaims);
    setDetached(snapshot.detached === true);
    window.dispatchEvent(
      new CustomEvent("omnigent:session-lifecycle-changed", { detail: { id: session.id } }),
    );
  }, [session.id]);

  useEffect(() => {
    if (!isOwner) return;
    let active = true;
    void listCodaClaims()
      .then((value) => active && setClaims(value))
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [isOwner]);

  useEffect(() => {
    if (dialog !== "resume") return;
    let active = true;
    void listCodaSandboxOptions()
      .then((value) => active && setSandboxes(value))
      .catch(() => active && setSandboxes([]));
    return () => {
      active = false;
    };
  }, [dialog]);

  if (!isOwner || (!claim && !detached)) return null;

  async function mutate(kind: "session" | "sandbox" | "resume") {
    setPending(kind);
    setError(null);
    try {
      if (kind === "session") await releaseSession(session.id);
      else if (kind === "sandbox") await releaseCodaClaim(session.id);
      else await resumeSession(session.id, sandboxAppId || undefined);
      setDialog(null);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The operation could not be completed");
    } finally {
      setPending(null);
    }
  }

  return (
    <>
      {detached ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => {
            setError(null);
            setDialog("resume");
          }}
          aria-label="Resume session in a fresh CoDA sandbox"
          data-testid="coda-resume-button"
        >
          <RotateCcwIcon className="size-4" />
          Resume
        </Button>
      ) : (
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => {
            setError(null);
            setDialog("release");
          }}
          aria-label="Release CoDA sandbox capacity"
          data-testid="coda-release-button"
        >
          <UnplugIcon className="size-4" />
          Release
        </Button>
      )}

      <Dialog open={dialog === "release"} onOpenChange={(open) => !open && setDialog(null)}>
        <DialogContent data-testid="coda-release-dialog">
          <DialogHeader>
            <DialogTitle>Release CoDA capacity?</DialogTitle>
            <DialogDescription>
              Conversation history, titles, comments, labels, and permissions are kept. Sandbox
              files and uncommitted changes will be permanently erased.
            </DialogDescription>
          </DialogHeader>
          {claim && claim.sessions.length > 1 && (
            <div>
              <p className="mb-2 text-sm font-medium">Sessions on this sandbox</p>
              <ul
                className="max-h-40 list-disc overflow-auto pl-5 text-sm"
                data-testid="coda-affected-sessions"
              >
                {claim.sessions.map((item) => (
                  <li key={item.id}>{item.title || "Untitled session"}</li>
                ))}
              </ul>
            </div>
          )}
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setDialog(null)}
              disabled={pending != null}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={() => void mutate("session")}
              disabled={pending != null}
            >
              {pending === "session" ? "Releasing…" : "Release this session"}
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={() => void mutate("sandbox")}
              disabled={pending != null || !claim}
            >
              {pending === "sandbox"
                ? "Releasing…"
                : `Release sandbox (${claim?.sessions.length ?? 0})`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={dialog === "resume"} onOpenChange={(open) => !open && setDialog(null)}>
        <DialogContent data-testid="coda-resume-dialog">
          <DialogHeader>
            <DialogTitle>Resume in a fresh sandbox</DialogTitle>
            <DialogDescription>
              Your conversation history is retained. A new workspace will be reconstructed; files
              and uncommitted changes from the released workspace cannot be recovered.
            </DialogDescription>
          </DialogHeader>
          <label className="grid gap-2 text-sm" htmlFor="coda-resume-target">
            CoDA sandbox
            <select
              id="coda-resume-target"
              className="h-10 rounded-md border bg-background px-3"
              value={sandboxAppId}
              onChange={(event) => setSandboxAppId(event.target.value)}
              disabled={pending != null}
            >
              <option value="">Automatic (recommended)</option>
              {sandboxes.map((option) => {
                const unavailable = option.state !== "available" || option.ownership === "other";
                return (
                  <option key={option.app_id} value={option.app_id} disabled={unavailable}>
                    {option.label} — {unavailable ? option.state : "available"}
                  </option>
                );
              })}
            </select>
          </label>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setDialog(null)}
              disabled={pending != null}
            >
              Cancel
            </Button>
            <Button type="button" onClick={() => void mutate("resume")} disabled={pending != null}>
              {pending === "resume" ? "Resuming…" : "Resume in fresh sandbox"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
