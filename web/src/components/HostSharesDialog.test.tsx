// Tests for the per-host share-management dialog (grant list, add,
// revoke). The permission hooks are mocked directly; the server-side
// contract (owner/admin/manage gating, level validation) is pinned by
// tests/server/integration/test_hosts_api.py.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HostSharesDialog } from "./HostSharesDialog";
import type { HostPermission } from "@/hooks/useHostPermissions";
import * as hostPerms from "@/hooks/useHostPermissions";

vi.mock("@/hooks/useHostPermissions", () => ({
  useHostPermissions: vi.fn(),
  useGrantHostPermission: vi.fn(),
  useRevokeHostPermission: vi.fn(),
}));

const grantMutate = vi.fn();
const revokeMutate = vi.fn();

function mockHooks(result: { data?: HostPermission[]; error?: Error | null }) {
  vi.mocked(hostPerms.useHostPermissions).mockReturnValue({
    data: result.data,
    error: result.error ?? null,
    isLoading: false,
  } as unknown as ReturnType<typeof hostPerms.useHostPermissions>);
  vi.mocked(hostPerms.useGrantHostPermission).mockReturnValue({
    mutateAsync: grantMutate,
    isPending: false,
  } as unknown as ReturnType<typeof hostPerms.useGrantHostPermission>);
  vi.mocked(hostPerms.useRevokeHostPermission).mockReturnValue({
    mutateAsync: revokeMutate,
    isPending: false,
  } as unknown as ReturnType<typeof hostPerms.useRevokeHostPermission>);
}

function grantRow(overrides: Partial<HostPermission> = {}): HostPermission {
  return {
    user_id: "bob@example.com",
    level: "use",
    created_at: 1_700_000_000,
    updated_at: 1_700_000_000,
    created_by: "admin@example.com",
    ...overrides,
  };
}

function renderDialog() {
  return render(
    <HostSharesDialog hostId="host_abc" hostName="alice-laptop" open onOpenChange={() => {}} />,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("HostSharesDialog", () => {
  it("lists existing grants with their level", () => {
    mockHooks({ data: [grantRow()] });
    renderDialog();
    const list = within(screen.getByTestId("host-share-grant-list"));
    expect(list.getByText("bob@example.com")).toBeInTheDocument();
    expect(list.getByText("Use")).toBeInTheDocument();
  });

  it("shows an empty state when the host is unshared", () => {
    mockHooks({ data: [] });
    renderDialog();
    expect(screen.getByText("Not shared with anyone yet.")).toBeInTheDocument();
  });

  it("grants the entered principal at the selected level", async () => {
    mockHooks({ data: [] });
    grantMutate.mockResolvedValue(undefined);
    renderDialog();

    fireEvent.change(screen.getByTestId("host-share-user-input"), {
      target: { value: "carol@example.com" },
    });
    fireEvent.click(screen.getByTestId("host-share-add-button"));
    await waitFor(() =>
      expect(grantMutate).toHaveBeenCalledWith({
        hostId: "host_abc",
        userId: "carol@example.com",
        level: "use",
      }),
    );
  });

  it("disables Add until a principal is entered", () => {
    mockHooks({ data: [] });
    renderDialog();
    expect(screen.getByTestId("host-share-add-button")).toBeDisabled();
  });

  it("revokes a grant from its row action", async () => {
    mockHooks({ data: [grantRow()] });
    revokeMutate.mockResolvedValue(undefined);
    renderDialog();

    fireEvent.click(screen.getByTitle("Revoke bob@example.com's access"));
    await waitFor(() =>
      expect(revokeMutate).toHaveBeenCalledWith({ hostId: "host_abc", userId: "bob@example.com" }),
    );
  });

  it("surfaces a load error (e.g. 403 for a non-manager)", () => {
    mockHooks({ data: undefined, error: new Error("not your host") });
    renderDialog();
    expect(screen.getByRole("alert")).toHaveTextContent("not your host");
  });
});
