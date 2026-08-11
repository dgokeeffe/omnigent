import { useMutation, useMutationState, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import type { NativeModelOption } from "@/lib/types";

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
   * Whether each harness family's launch on this host resolves an
   * AI-Gateway-backed inference config, e.g. `{"claude-native": true,
   * "codex": false}`. Smart Routing's apply layer only works on gateway-backed
   * inference. `null`/absent (or a missing key) means unknown — an older host
   * or server — and must not gate anything away; only an explicit `false` does.
   */
  gateway_inference?: Record<string, boolean> | null;
  /**
   * Advisory runner-capacity snapshot published by the host daemon.
   * `null`/absent means unknown (older host or server, or no report on this
   * replica) — render it as unknown, never as "full". The host's own launch
   * admission is authoritative, so a launch may still be refused with a 429
   * even when this says the host is accepting.
   */
  capacity?: HostCapacity | null;
}

/** How stale an advisory capacity snapshot may be and still be trusted.
 *  Hosts publish on every launch/stop/exit plus a 15 s refresh, so a minute
 *  of silence means "unknown", not "unchanged". */
export const CAPACITY_SNAPSHOT_MAX_AGE_MS = 60_000;

/** Tolerated host/browser clock skew. A snapshot stamped further ahead than
 *  this is not evidence about the present, so it reads as unknown rather than
 *  keeping a stale refusal "fresh" for an extra minute. */
export const CAPACITY_SNAPSHOT_MAX_SKEW_MS = 5_000;

/** Shown instead of `N/limit` when an online host reports no fresh snapshot.
 *  Unknown must read as unknown — never as zero free slots. */
export const CAPACITY_UNKNOWN_LABEL = "capacity unknown";

/** Advisory host runner capacity. Distinct from the CoDA browser-terminal cap
 *  and from a managed lease's durable-session cap — this counts live runner
 *  processes plus in-flight launches on one host daemon. */
export interface HostCapacity {
  /** Live runner processes. */
  active: number;
  /** Launches accepted but not yet registered as active. */
  pending: number;
  /** Configured hard ceiling, or null when the host runs uncapped. */
  limit: number | null;
  /** `limit - (active + pending)`, or null when uncapped (unknown, not 0). */
  available: number | null;
  /** Whether the host would admit another runner at `observed_at`. */
  accepting: boolean;
  /** Why it is refusing: `"host_at_capacity"` or `"memory_pressure"`. */
  reason: string | null;
  /** Whether the host's memory gate is latched. `null`/absent = unknown. */
  pressure?: boolean | null;
  memory_used?: number | null;
  memory_limit?: number | null;
  memory_percent?: number | null;
  memory_high_threshold?: number | null;
  memory_resume_threshold?: number | null;
  reserve_mb?: number | null;
  /** Epoch seconds when the host observed this. Absent = freshness unknown. */
  observed_at?: number | null;
}

/** Whether a snapshot is recent enough to act on (see
 *  {@link CAPACITY_SNAPSHOT_MAX_AGE_MS}). A missing `observed_at` is unknown. */
export function isCapacityFresh(
  capacity: HostCapacity | null | undefined,
  now: number = Date.now(),
): boolean {
  if (!capacity || typeof capacity.observed_at !== "number") return false;
  if (!Number.isFinite(capacity.observed_at)) return false;
  const age = now - capacity.observed_at * 1000;
  return age >= -CAPACITY_SNAPSHOT_MAX_SKEW_MS && age <= CAPACITY_SNAPSHOT_MAX_AGE_MS;
}

/** A host is blocked only when a FRESH snapshot says it is not accepting.
 *  Unknown or stale capacity must never disable a host. */
export function isHostAtCapacity(
  capacity: HostCapacity | null | undefined,
  now: number = Date.now(),
): boolean {
  return isCapacityFresh(capacity, now) && capacity!.accepting === false;
}

/** Short user-facing capacity label, e.g. `"3/10 runners"`, or
 *  {@link CAPACITY_UNKNOWN_LABEL} when capacity is unknown or stale — an
 *  online host must never read as "0 free slots" just because it has not
 *  reported yet. */
export function hostCapacityLabel(
  capacity: HostCapacity | null | undefined,
  now: number = Date.now(),
): string {
  if (!isCapacityFresh(capacity, now)) return CAPACITY_UNKNOWN_LABEL;
  const cap = capacity!;
  const used = cap.active + cap.pending;
  if (cap.limit === null || cap.limit === undefined) {
    return `${used} running · no limit`;
  }
  const suffix =
    cap.accepting === false && cap.reason === "memory_pressure" ? " · paused (memory)" : "";
  return `${used}/${cap.limit} runners${suffix}`;
}

interface HostsResponse {
  hosts: Host[];
}

/** Sanitized manual CoDA target returned by the authenticated server API. */
export interface CodaSandboxOption {
  app_id: string;
  label: string;
  ownership: "unclaimed" | "mine" | "other";
  state: "available" | "full" | "unavailable";
  capacity: { used: number | null; limit: number };
}

async function fetchCodaSandboxes(): Promise<CodaSandboxOption[]> {
  const res = await authenticatedFetch("/v1/sandboxes/coda");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { sandboxes?: CodaSandboxOption[] };
  return body.sandboxes ?? [];
}

/** Fresh advisory choices; authoritative capacity is enforced on create. */
export function useCodaSandboxes(enabled: boolean) {
  return useQuery({
    queryKey: ["coda-sandboxes"],
    queryFn: fetchCodaSandboxes,
    enabled,
    staleTime: 10_000,
    refetchOnWindowFocus: true,
    retry: false,
  });
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
  /** Refetch on every window refocus (paired with `staleTime: 0`) so returning
   *  to the tab is a guaranteed readiness recovery. Only the setup flow needs
   *  this; other consumers keep the 30 s stale window to avoid an app-wide bump
   *  in `/v1/hosts` volume on refocus. */
  refetchOnFocus?: boolean;
}

export function useHosts(options: UseHostsOptions = {}) {
  const enabled = options.enabled ?? true;
  const includeSandbox = options.includeSandbox ?? false;
  const refetchOnFocus = options.refetchOnFocus ?? false;
  return useQuery({
    // Distinct cache key per filtering mode so the picker's filtered
    // list and the header's unfiltered list don't overwrite each other.
    // A bare ["hosts"] invalidation still prefix-matches both.
    queryKey: ["hosts", { includeSandbox }],
    queryFn: () => fetchHosts(includeSandbox),
    enabled,
    // Readiness is pushed live via WS (hosts_changed → invalidate in
    // SessionUpdatesProvider), so the badge normally clears within seconds of
    // `omni setup` finishing. The refocus recovery settles the case that push
    // misses: a user typically runs setup in a terminal with the tab
    // backgrounded, which pauses the interval poll AND is when a reconnect gap
    // can drop the frame. Refetching on refocus makes returning to the tab a
    // guaranteed recovery — paired with staleTime 0 so refocus always refires
    // rather than serving a stale "needs setup" from cache. Scoped to the setup
    // flow via `refetchOnFocus` so the other ~8 consumers don't pay it. The 60 s
    // interval remains the in-tab fallback.
    staleTime: refetchOnFocus ? 0 : 30_000,
    refetchOnWindowFocus: refetchOnFocus,
    refetchInterval: enabled ? 60_000 : false,
  });
}

async function fetchHostModelOptions(
  hostId: string,
  harness: string,
): Promise<NativeModelOption[]> {
  const res = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/harnesses/${encodeURIComponent(harness)}/model-options`,
  );
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { models?: NativeModelOption[] };
  return body.models ?? [];
}

/** Model choices available before launch, resolved on the selected host. */
export function useHostModelOptions(hostId: string | null, harness: string, enabled = true) {
  return useQuery({
    queryKey: ["host-model-options", hostId, harness],
    queryFn: () => fetchHostModelOptions(hostId as string, harness),
    enabled: enabled && hostId !== null,
    staleTime: 30_000,
    retry: false,
  });
}

interface InstallHarnessResult {
  object: "harness_install";
  harness: string;
  configured_harnesses: Record<string, boolean | string>;
}

/**
 * Install a missing harness onto a connected host from the UI.
 *
 * POSTs to the flag-gated install endpoint; the server drives the same
 * installer `omni setup` uses and returns the host's refreshed readiness.
 * On success we write that map straight into every cached host list so the
 * "needs setup" badge flips to ready without waiting for the 60 s poll or a
 * reconnect. The caller passes the harness id (e.g. `"codex"`); only ids in the
 * server's `installable_harnesses` set should be offered (see
 * `harnessInstallableOnHost`).
 *
 * Concurrent installs of different harnesses are supported: each `mutate()`
 * call runs independently, and callers track per-harness in-flight state via
 * the call's own `onSettled` (see `HarnessSetupDialog`) rather than the shared
 * observer's `isPending`, which only reflects the latest call.
 */
/** Stable mutation key for harness-install mutations on a host. Lets the setup
 *  dialog read per-harness in-flight state via {@link useInstallingHarnesses}
 *  regardless of which install fired last (a shared observer only remembers the
 *  latest call's callbacks — see the comment in {@link useInstallHarness}). */
export function installHarnessMutationKey(hostId: string): readonly unknown[] {
  return ["install-harness", hostId];
}

export function useInstallHarness(hostId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: installHarnessMutationKey(hostId),
    mutationFn: async (harness: string): Promise<InstallHarnessResult> => {
      const res = await authenticatedFetch(
        `/v1/hosts/${encodeURIComponent(hostId)}/harnesses/${encodeURIComponent(harness)}/install`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
      );
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`;
        try {
          const err = (await res.json()) as { detail?: string };
          if (typeof err.detail === "string" && err.detail) detail = err.detail;
        } catch {
          // Non-JSON error body — keep the status-line detail.
        }
        throw new Error(detail);
      }
      return (await res.json()) as InstallHarnessResult;
    },
    onSuccess: (result) => {
      // Patch the refreshed readiness into every ["hosts", …] cache entry
      // (filtered + unfiltered) so the badge updates immediately. This lives at
      // config level (not the per-call mutate() options) on purpose: config
      // callbacks fire per-mutation from the Mutation object, so a concurrent
      // second install can't orphan the first one's cache patch — unlike the
      // observer's per-call callbacks, which the later mutate() overwrites.
      queryClient.setQueriesData<Host[]>({ queryKey: ["hosts"] }, (hosts) =>
        hosts?.map((h) =>
          h.host_id === hostId ? { ...h, configured_harnesses: result.configured_harnesses } : h,
        ),
      );
    },
  });
}

/**
 * The set of harness ids with an install currently in flight on *hostId*.
 *
 * Reads React Query's global mutation state (filtered to this host's install
 * mutations), so it reflects EVERY pending install regardless of which one
 * fired last. The setup dialog is a single persistent instance sharing one
 * install mutation observer; that observer only remembers the latest call's
 * per-call callbacks, so tracking in-flight installs via a local set fed by
 * mutate()'s onSettled loses any earlier install when a second one starts —
 * leaving the first harness stuck showing "Installing…" forever. Deriving the
 * set from mutation state instead is observer-independent and self-heals.
 */
export function useInstallingHarnesses(hostId: string): ReadonlySet<string> {
  const pending = useMutationState({
    filters: { mutationKey: installHarnessMutationKey(hostId), status: "pending" },
    select: (mutation) => mutation.state.variables as string | undefined,
  });
  return new Set(pending.filter((h): h is string => typeof h === "string"));
}

/** Payload for {@link useStoreCredential}: an API key, a gateway, or adopt. */
export interface StoreCredentialInput {
  harness: string;
  kind: "key" | "gateway" | "adopt";
  /** The API key / gateway token for `key` / `gateway`; omitted for `adopt`. */
  secret?: string;
  /** Gateway base URL (required for `kind: "gateway"`). */
  base_url?: string;
  /** Family default model id to pin. Accepted by the backend but not yet
   *  surfaced by the v1 form — reserved for a follow-up. */
  default_model?: string;
  /** OpenAI wire protocol (`"chat"` / `"responses"`), gateway/key openai only.
   *  Reserved for a follow-up like {@link default_model}. */
  wire_api?: string;
  /** For `kind: "adopt"`, the host env var to reference. */
  env_var?: string;
}

interface StoreCredentialResult {
  object: "harness_credential";
  harness: string;
  configured_harnesses: Record<string, boolean | string>;
}

/**
 * Write a harness provider credential onto a connected host from the UI.
 *
 * POSTs to the flag-gated credential endpoint; the server forwards the secret
 * to the host daemon, which writes it (keychain + a `providers:` reference) and
 * returns the host's refreshed readiness. On success we patch that map into
 * every cached host list so the harness badge flips (yellow → green) without a
 * reconnect. The secret rides in the request body and is never held server-side.
 */
export function useStoreCredential(hostId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: StoreCredentialInput): Promise<StoreCredentialResult> => {
      const { harness, ...body } = input;
      const res = await authenticatedFetch(
        `/v1/hosts/${encodeURIComponent(hostId)}/harnesses/${encodeURIComponent(harness)}/credential`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`;
        try {
          const err = (await res.json()) as { detail?: string };
          if (typeof err.detail === "string" && err.detail) detail = err.detail;
        } catch {
          // Non-JSON error body — keep the status-line detail.
        }
        throw new Error(detail);
      }
      return (await res.json()) as StoreCredentialResult;
    },
    // This mutation-level onSuccess patches the ["hosts"] cache (badge flip) and
    // invalidates the detect query so a just-adopted credential stops showing.
    // Callers may ALSO pass a call-level onSuccess (toast + close the form);
    // react-query fires both — don't consolidate them, or the cache patch here
    // is lost.
    onSuccess: (result) => {
      queryClient.setQueriesData<Host[]>({ queryKey: ["hosts"] }, (hosts) =>
        hosts?.map((h) =>
          h.host_id === hostId ? { ...h, configured_harnesses: result.configured_harnesses } : h,
        ),
      );
      // A written/adopted credential changes what's adoptable — refetch it.
      void queryClient.invalidateQueries({ queryKey: ["detected-credentials", hostId] });
    },
  });
}

/** A credential already on the host, offered for one-click adopt (non-secret). */
export interface DetectedCredential {
  family: string;
  source: string;
  env_var: string | null;
}

/**
 * Fetch the credentials already present on a host, for the adopt affordance.
 *
 * Hits the flag-gated detect endpoint; the server asks the host daemon for
 * NON-secret descriptors (family + source label + env var name) of adoptable
 * credentials. Enabled only when a host id is given and `enabled` is set (the
 * dialog turns it on only for a harness whose credential the UI can write), so
 * we don't probe hosts for closed dialogs.
 */
export function useDetectedCredentials(hostId: string | null | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ["detected-credentials", hostId],
    queryFn: async (): Promise<DetectedCredential[]> => {
      const res = await authenticatedFetch(
        `/v1/hosts/${encodeURIComponent(hostId ?? "")}/credentials/detected`,
      );
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const body = (await res.json()) as { credentials?: DetectedCredential[] };
      return Array.isArray(body.credentials) ? body.credentials : [];
    },
    enabled: enabled && !!hostId,
    staleTime: 30_000,
  });
}
