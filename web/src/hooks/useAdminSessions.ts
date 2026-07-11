import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

/**
 * A session row from the admin fleet view (`GET /v1/sessions?all=true`).
 *
 * A subset of the server's `SessionListItem` — only the fields the admin
 * Sessions page renders. `owner`, `host_id`, and `runner_id` are what let
 * an operator attribute a session to a user and the host running it.
 */
export interface AdminSession {
  /** Session/conversation id, e.g. `"conv_abc123"`. */
  id: string;
  /** Human-readable title, or `null`/absent when untitled. */
  title?: string | null;
  /** Bound agent's name, e.g. `"research-agent"`. */
  agent_name?: string | null;
  /** Derived lifecycle status (running / waiting / idle / failed / ...). */
  status?: string | null;
  /** Owning user id. `null` when permissions are disabled. */
  owner?: string | null;
  /** Host that launched the runner for this session; `null` for local CLI. */
  host_id?: string | null;
  /** Runner currently bound to the session; `null` when none. */
  runner_id?: string | null;
  /** Unix epoch seconds of creation. */
  created_at: number;
  /** Unix epoch seconds of last update. */
  updated_at: number;
}

interface AdminSessionsResponse {
  data: AdminSession[];
  has_more?: boolean;
}

async function fetchAdminSessions(): Promise<AdminSession[]> {
  // include_archived=true so the fleet view is complete — an operator
  // auditing "everything running" should see archived sessions too.
  // limit=1000 matches the endpoint's ceiling; pagination can be added
  // if a deployment ever exceeds it (the page notes has_more).
  const res = await authenticatedFetch(
    "/v1/sessions?all=true&kind=any&include_archived=true&limit=1000",
  );
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as AdminSessionsResponse;
  return body.data;
}

/**
 * Every session on the server, across all owners — admin only (the server
 * 403s non-admins; callers gate the UI with `useIsAdmin`). Polls on the same
 * 10s cadence as the admin hosts view so status flips surface without a
 * manual refresh.
 */
export function useAdminSessions(options: { enabled?: boolean } = {}) {
  const enabled = options.enabled ?? true;
  return useQuery({
    queryKey: ["sessions", "admin-all"],
    queryFn: fetchAdminSessions,
    enabled,
    staleTime: 10_000,
    refetchInterval: enabled ? 10_000 : false,
  });
}
