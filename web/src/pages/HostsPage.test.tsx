// Tests for the admin HostsPage (fleet listing).
//
// Browser e2e is impractical (admin-gated — would need a second
// authenticated server), so the surface is pinned here by mocking the
// admin gate (useIsAdmin) and the fleet query (useAdminHosts) directly.
// The server-side contract (?all=true admin gating, fleet fields) is
// pinned by tests/server/integration/test_hosts_api.py.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HostsPage } from "./HostsPage";
import type { AdminHost } from "@/hooks/useAdminHosts";
import * as adminHosts from "@/hooks/useAdminHosts";
import * as isAdminHook from "@/hooks/useIsAdmin";

vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: vi.fn() }));
vi.mock("@/hooks/useAdminHosts", () => ({
  useAdminHosts: vi.fn(),
  useShutdownHost: vi.fn(),
}));

function host(overrides: Partial<AdminHost> = {}): AdminHost {
  return {
    host_id: "host_abc123",
    name: "alice-laptop",
    owner: "alice@example.com",
    status: "online",
    sandbox_provider: null,
    configured_harnesses: { "claude-sdk": true, codex: "needs-auth" },
    created_at: 1_700_000_000,
    last_seen: 1_700_000_100,
    session_count: 3,
    ...overrides,
  };
}

const shutdownMutate = vi.fn();

function mockQuery(result: { data?: AdminHost[]; error?: Error | null; isLoading?: boolean }) {
  vi.mocked(adminHosts.useAdminHosts).mockReturnValue({
    data: result.data,
    error: result.error ?? null,
    isLoading: result.isLoading ?? false,
    refetch: vi.fn(),
  } as unknown as ReturnType<typeof adminHosts.useAdminHosts>);
  vi.mocked(adminHosts.useShutdownHost).mockReturnValue({
    mutateAsync: shutdownMutate,
    isPending: false,
  } as unknown as ReturnType<typeof adminHosts.useShutdownHost>);
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("HostsPage gating", () => {
  it("blocks non-admins with a permission message", () => {
    vi.mocked(isAdminHook.useIsAdmin).mockReturnValue(false);
    mockQuery({ data: undefined });
    render(<HostsPage />);
    expect(screen.getByText("You don't have permission to manage hosts.")).toBeInTheDocument();
    // The fleet query must be disabled for non-admins.
    expect(adminHosts.useAdminHosts).toHaveBeenCalledWith({ enabled: false });
  });
});

describe("HostsPage table", () => {
  it("renders every owner's hosts with owner, status, harnesses, and session count", () => {
    vi.mocked(isAdminHook.useIsAdmin).mockReturnValue(true);
    mockQuery({
      data: [
        host(),
        host({
          host_id: "host_def456",
          name: "bob-laptop",
          owner: "bob@example.com",
          status: "offline",
          session_count: 0,
        }),
      ],
    });
    render(<HostsPage />);

    expect(screen.getByText("alice-laptop")).toBeInTheDocument();
    expect(screen.getByText("bob-laptop")).toBeInTheDocument();
    expect(screen.getByText("alice@example.com")).toBeInTheDocument();
    expect(screen.getByText("bob@example.com")).toBeInTheDocument();
    expect(screen.getByText("Online")).toBeInTheDocument();
    expect(screen.getByText("Offline")).toBeInTheDocument();
    // Harness pills from configured_harnesses keys.
    expect(screen.getAllByText("claude-sdk").length).toBeGreaterThan(0);
    // Session counts.
    expect(screen.getByText("3")).toBeInTheDocument();
  });

  it("renders an empty state when no hosts have connected", () => {
    vi.mocked(isAdminHook.useIsAdmin).mockReturnValue(true);
    mockQuery({ data: [] });
    render(<HostsPage />);
    expect(screen.getByText("No hosts have connected to this server.")).toBeInTheDocument();
  });

  it("shows a load error when the fleet query fails", () => {
    vi.mocked(isAdminHook.useIsAdmin).mockReturnValue(true);
    mockQuery({ data: undefined, error: new Error("403 Forbidden") });
    render(<HostsPage />);
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load hosts.");
  });
});

describe("HostsPage shutdown", () => {
  it("shuts a host down only after the confirm dialog", async () => {
    vi.mocked(isAdminHook.useIsAdmin).mockReturnValue(true);
    mockQuery({ data: [host()] });
    shutdownMutate.mockResolvedValue(undefined);
    render(<HostsPage />);

    fireEvent.click(screen.getByRole("button", { name: /shut down/i }));
    // Dialog open, nothing sent yet — and the bound-session warning shows.
    expect(shutdownMutate).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/3 sessions are bound/)).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Shut down" }));
    await waitFor(() => expect(shutdownMutate).toHaveBeenCalledWith("host_abc123"));
  });

  it("disables the shutdown action for offline and managed-sandbox hosts", () => {
    vi.mocked(isAdminHook.useIsAdmin).mockReturnValue(true);
    mockQuery({
      data: [
        host({ host_id: "host_off", name: "off-box", status: "offline" }),
        host({ host_id: "host_sbx", name: "sbx-box", sandbox_provider: "modal" }),
      ],
    });
    render(<HostsPage />);
    const buttons = screen.getAllByRole("button", { name: /shut down/i });
    expect(buttons).toHaveLength(2);
    for (const b of buttons) expect(b).toBeDisabled();
  });
});
