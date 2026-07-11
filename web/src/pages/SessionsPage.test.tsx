// Tests for the admin SessionsPage (fleet listing).
//
// Browser e2e is impractical (admin-gated), so the surface is pinned here
// by mocking the admin gate (useIsAdmin) and the fleet query
// (useAdminSessions) directly. The server-side contract (?all=true admin
// gating, owner disclosure) is pinned by
// tests/server/integration/test_sessions_permissions.py.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionsPage } from "./SessionsPage";
import type { AdminSession } from "@/hooks/useAdminSessions";
import * as adminSessions from "@/hooks/useAdminSessions";
import * as isAdminHook from "@/hooks/useIsAdmin";

vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: vi.fn() }));
vi.mock("@/hooks/useAdminSessions", () => ({ useAdminSessions: vi.fn() }));

function session(overrides: Partial<AdminSession> = {}): AdminSession {
  return {
    id: "conv_abc123",
    title: "Refactor auth",
    agent_name: "claude-sdk",
    status: "running",
    owner: "alice@example.com",
    host_id: "host_abc123",
    runner_id: "rnr_xyz",
    created_at: 1_700_000_000,
    updated_at: 1_700_000_100,
    ...overrides,
  };
}

function mockQuery(result: {
  data?: AdminSession[];
  error?: Error | null;
  isLoading?: boolean;
}) {
  vi.mocked(adminSessions.useAdminSessions).mockReturnValue({
    data: result.data,
    error: result.error ?? null,
    isLoading: result.isLoading ?? false,
    refetch: vi.fn(),
  } as unknown as ReturnType<typeof adminSessions.useAdminSessions>);
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SessionsPage gating", () => {
  it("blocks non-admins with a permission message", () => {
    vi.mocked(isAdminHook.useIsAdmin).mockReturnValue(false);
    mockQuery({ data: undefined });
    render(<SessionsPage />);
    expect(
      screen.getByText("You don't have permission to view all sessions."),
    ).toBeInTheDocument();
    // The fleet query must be disabled for non-admins.
    expect(adminSessions.useAdminSessions).toHaveBeenCalledWith({ enabled: false });
  });
});

describe("SessionsPage table", () => {
  it("renders every owner's sessions with owner, status, host, and runner", () => {
    vi.mocked(isAdminHook.useIsAdmin).mockReturnValue(true);
    mockQuery({
      data: [
        session(),
        session({
          id: "conv_def456",
          title: "Local script",
          owner: "bob@example.com",
          status: "idle",
          host_id: null,
          runner_id: null,
        }),
      ],
    });
    render(<SessionsPage />);

    expect(screen.getByText("Refactor auth")).toBeInTheDocument();
    expect(screen.getByText("Local script")).toBeInTheDocument();
    expect(screen.getByText("alice@example.com")).toBeInTheDocument();
    expect(screen.getByText("bob@example.com")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByText("idle")).toBeInTheDocument();
    // Host id shown for the bound session; "local" for the CLI one.
    expect(screen.getByText("host_abc123")).toBeInTheDocument();
    expect(screen.getByText("local")).toBeInTheDocument();
    expect(screen.getByText("rnr_xyz")).toBeInTheDocument();
  });

  it("shows an untitled placeholder when a session has no title", () => {
    vi.mocked(isAdminHook.useIsAdmin).mockReturnValue(true);
    mockQuery({ data: [session({ title: null })] });
    render(<SessionsPage />);
    expect(screen.getByText("(untitled)")).toBeInTheDocument();
  });

  it("renders an empty state when no sessions exist", () => {
    vi.mocked(isAdminHook.useIsAdmin).mockReturnValue(true);
    mockQuery({ data: [] });
    render(<SessionsPage />);
    expect(screen.getByText("No sessions exist on this server.")).toBeInTheDocument();
  });

  it("shows a load error when the fleet query fails", () => {
    vi.mocked(isAdminHook.useIsAdmin).mockReturnValue(true);
    mockQuery({ data: undefined, error: new Error("403 Forbidden") });
    render(<SessionsPage />);
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load sessions.");
  });
});
