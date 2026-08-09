import type * as UseHostsModule from "@/hooks/useHosts";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ResumeWithDirectoryDialog } from "./ResumeWithDirectoryDialog";
import { useHosts } from "@/hooks/useHosts";
import { useDirectorySessions } from "@/hooks/useDirectorySessions";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import { ApiError, getSessionSlim, launchRunner } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";

// Heavy children are exercised by their own tests; stub them so this
// test focuses on the dialog's prefill + bind + fallback logic.
vi.mock("./WorkspacePathField", () => ({
  WorkspacePathField: ({ value, onChange }: { value: string; onChange: (v: string) => void }) => (
    <input
      data-testid="mock-workspace-input"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  ),
}));
vi.mock("./WorkspacePicker", () => ({
  WorkspacePicker: () => <div data-testid="mock-workspace-picker" />,
  isNavigablePath: () => false,
}));
// Partial mock: only useHosts is stubbed. The capacity helpers
// (hostCapacityLabel / isHostAtCapacity) are pure and must stay real, or the
// host rows silently lose their advisory N/limit label and disabled state.
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof UseHostsModule>()),
  useHosts: vi.fn(),
}));
vi.mock("@/hooks/useDirectorySessions", () => ({ useDirectorySessions: vi.fn() }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useRunnerHealthRegistration: vi.fn(),
}));
vi.mock("@/hooks/useRecentWorkspaces", () => ({
  useRecentWorkspaces: () => ({ recent: [], addRecent: vi.fn() }),
}));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  getSessionSlim: vi.fn(),
  launchRunner: vi.fn(),
}));
// Radix Select uses a portal + pointer events that jsdom can't drive, so
// stub it to a native <select>; lets tests switch the selected host.
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
  }: {
    value: string;
    onValueChange: (v: string) => void;
    children: ReactNode;
  }) => (
    <select
      data-testid="mock-host-select"
      value={value}
      onChange={(e) => onValueChange(e.target.value)}
    >
      {children}
    </select>
  ),
  SelectTrigger: ({ children }: { children: ReactNode }) => children,
  SelectValue: () => null,
  SelectContent: ({ children }: { children: ReactNode }) => children,
  // Forward disabled + data-testid so capacity gating is observable here.
  SelectItem: ({
    value,
    children,
    disabled,
    ...rest
  }: {
    value: string;
    children: ReactNode;
    disabled?: boolean;
  }) => (
    <option value={value} disabled={disabled} {...rest}>
      {children}
    </option>
  ),
}));

const useHostsMock = vi.mocked(useHosts);
const useDirectorySessionsMock = vi.mocked(useDirectorySessions);
const useRunnerHealthMock = vi.mocked(useRunnerHealthRegistration);
const getSessionMock = vi.mocked(getSessionSlim);
const launchRunnerMock = vi.mocked(launchRunner);

function sourceSession(over: Partial<Session>): Session {
  return {
    id: "conv_src",
    hostId: "host_src",
    workspace: "/Users/alice/repo",
    gitBranch: null,
    labels: {},
    items: [],
    ...over,
  } as unknown as Session;
}

function renderDialog() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ResumeWithDirectoryDialog
        open
        onOpenChange={() => {}}
        sessionId="conv_clone"
        sourceSessionId="conv_src"
        serverUrl="http://localhost:5173"
      />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useHostsMock.mockReset();
  useDirectorySessionsMock.mockReset();
  useRunnerHealthMock.mockReset();
  getSessionMock.mockReset();
  launchRunnerMock.mockReset();
  useDirectorySessionsMock.mockReturnValue({ data: [] } as unknown as ReturnType<
    typeof useDirectorySessions
  >);
  useRunnerHealthMock.mockReturnValue(new Map());
  launchRunnerMock.mockResolvedValue({ runnerId: "runner_new" });
});

afterEach(() => cleanup());

describe("ResumeWithDirectoryDialog", () => {
  it("prefills the source host + directory and binds via launchRunner with a branch", async () => {
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host_src", name: "laptop", owner: "me", status: "online" }],
    } as unknown as ReturnType<typeof useHosts>);
    getSessionMock.mockResolvedValue(sourceSession({ workspace: "/Users/alice/repo" }));

    renderDialog();

    // The bind button enables only once the host + directory are
    // prefilled from the source — proves the prefill effects ran.
    const bindBtn = await screen.findByTestId("resume-dir-bind-button");
    await waitFor(() => expect((bindBtn as HTMLButtonElement).disabled).toBe(false));

    // Name a branch so the worktree path is exercised.
    fireEvent.change(screen.getByTestId("resume-dir-branch-input"), {
      target: { value: "feature/login" },
    });
    fireEvent.click(bindBtn);

    // launchRunner gets the clone id, the source workspace, and the
    // branch. A wrong host/workspace here means the prefill didn't seed
    // from the source; a missing git arg means the branch input was dropped.
    await waitFor(() =>
      expect(launchRunnerMock).toHaveBeenCalledWith("host_src", "conv_clone", "/Users/alice/repo", {
        branchName: "feature/login",
        baseBranch: undefined,
      }),
    );
  });

  it("shows advisory N/limit runner capacity on each online host row", async () => {
    // The picker must expose the ACTIVE-RUNNER ceiling (not a browser PTY or
    // managed-lease cap) so a user can see 10/10 before trying to launch.
    useHostsMock.mockReturnValue({
      data: [
        {
          host_id: "host_src",
          name: "laptop",
          owner: "me",
          status: "online",
          capacity: {
            active: 7,
            pending: 1,
            limit: 10,
            available: 2,
            accepting: true,
            reason: null,
            observed_at: Date.now() / 1000,
          },
        },
      ],
    } as unknown as ReturnType<typeof useHosts>);
    getSessionMock.mockResolvedValue(sourceSession({ workspace: "/Users/alice/repo" }));

    renderDialog();

    const label = await screen.findByTestId("resume-dir-host-capacity-host_src");
    // active + pending, because an in-flight launch already holds a slot.
    expect(label.textContent).toBe("8/10 runners");
    const option = screen.getByTestId("resume-dir-host-option-host_src") as HTMLOptionElement;
    expect(option.disabled).toBe(false);
  });

  it("disables a host only when a FRESH snapshot says it is not accepting", async () => {
    useHostsMock.mockReturnValue({
      data: [
        {
          host_id: "host_src",
          name: "laptop",
          owner: "me",
          status: "online",
          capacity: {
            active: 10,
            pending: 0,
            limit: 10,
            available: 0,
            accepting: false,
            reason: "host_at_capacity",
            observed_at: Date.now() / 1000,
          },
        },
      ],
    } as unknown as ReturnType<typeof useHosts>);
    getSessionMock.mockResolvedValue(sourceSession({ workspace: "/Users/alice/repo" }));

    renderDialog();

    const option = (await screen.findByTestId(
      "resume-dir-host-option-host_src",
    )) as HTMLOptionElement;
    expect(option.disabled).toBe(true);
    expect(screen.getByTestId("resume-dir-host-capacity-host_src").textContent).toBe(
      "10/10 runners",
    );
  });

  it("labels unknown capacity as unknown instead of disabling the host", async () => {
    // An older host (or a replica with no report yet) sends no capacity. It
    // must stay selectable and simply show no label — never 0 free slots.
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host_src", name: "laptop", owner: "me", status: "online" }],
    } as unknown as ReturnType<typeof useHosts>);
    getSessionMock.mockResolvedValue(sourceSession({ workspace: "/Users/alice/repo" }));

    renderDialog();

    const option = (await screen.findByTestId(
      "resume-dir-host-option-host_src",
    )) as HTMLOptionElement;
    expect(option.disabled).toBe(false);
    expect(screen.getByTestId("resume-dir-host-capacity-host_src").textContent).toBe(
      "capacity unknown",
    );
  });

  it("surfaces an authoritative 429 race with actionable retry guidance", async () => {
    // The advisory snapshot said "accepting", so the user could select this
    // host; the host's own gate then refused. The dialog must explain the
    // next step rather than showing a bare failure.
    useHostsMock.mockReturnValue({
      data: [
        {
          host_id: "host_src",
          name: "laptop",
          owner: "me",
          status: "online",
          capacity: {
            active: 3,
            pending: 0,
            limit: 10,
            available: 7,
            accepting: true,
            reason: null,
            observed_at: Date.now() / 1000,
          },
        },
      ],
    } as unknown as ReturnType<typeof useHosts>);
    getSessionMock.mockResolvedValue(sourceSession({ workspace: "/Users/alice/repo" }));
    launchRunnerMock.mockRejectedValue(
      new ApiError(
        "This host is at capacity (10 running, 0 starting, limit 10). " +
          "Wait for a session to finish, stop one, or use another host.",
        429,
        "host_at_capacity",
      ),
    );

    renderDialog();
    const bindBtn = await screen.findByTestId("resume-dir-bind-button");
    await waitFor(() => expect((bindBtn as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(bindBtn);

    const error = await screen.findByText(/at capacity/i);
    expect(error.textContent).toContain("use another host");
    expect(error.textContent).toContain("refreshes as sessions finish");
  });

  it("binds the source directory directly when no branch is named", async () => {
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host_src", name: "laptop", owner: "me", status: "online" }],
    } as unknown as ReturnType<typeof useHosts>);
    getSessionMock.mockResolvedValue(sourceSession({ workspace: "/Users/alice/repo" }));

    renderDialog();
    const bindBtn = await screen.findByTestId("resume-dir-bind-button");
    await waitFor(() => expect((bindBtn as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(bindBtn);

    // No git arg (undefined) → server binds the directory directly.
    await waitFor(() =>
      expect(launchRunnerMock).toHaveBeenCalledWith(
        "host_src",
        "conv_clone",
        "/Users/alice/repo",
        undefined,
      ),
    );
  });

  it("shows the CLI reconnect fallback when the source host is offline", async () => {
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host_src", name: "laptop", owner: "me", status: "offline" }],
    } as unknown as ReturnType<typeof useHosts>);
    getSessionMock.mockResolvedValue(sourceSession({}));

    renderDialog();

    // Offline source host → no runner to launch → CLI reconnect, no picker.
    expect(await screen.findByTestId("resume-dir-cli-fallback")).toBeTruthy();
    expect(screen.queryByTestId("resume-dir-host-select")).toBeNull();
    expect(launchRunnerMock).not.toHaveBeenCalled();
  });

  it("warns when the chosen directory differs from the source's", async () => {
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host_src", name: "laptop", owner: "me", status: "online" }],
    } as unknown as ReturnType<typeof useHosts>);
    getSessionMock.mockResolvedValue(sourceSession({ workspace: "/Users/alice/repo" }));

    renderDialog();
    // Prefilled to the source dir → no warning yet.
    await screen.findByTestId("resume-dir-bind-button");
    await waitFor(() =>
      expect((screen.getByTestId("mock-workspace-input") as HTMLInputElement).value).toBe(
        "/Users/alice/repo",
      ),
    );
    expect(screen.queryByTestId("resume-dir-mismatch-warning")).toBeNull();

    // Pick a different directory → mismatch warning appears.
    fireEvent.change(screen.getByTestId("mock-workspace-input"), {
      target: { value: "/Users/alice/other-repo" },
    });
    expect(await screen.findByTestId("resume-dir-mismatch-warning")).toBeTruthy();
  });

  it("warns when a different host is chosen even with the source's path", async () => {
    // Two online hosts: the source's and another. Same path string on a
    // different host is a different machine, so the transcript's file
    // references still won't resolve — the warning must fire.
    useHostsMock.mockReturnValue({
      data: [
        { host_id: "host_src", name: "laptop", owner: "me", status: "online" },
        { host_id: "host_other", name: "desktop", owner: "me", status: "online" },
      ],
    } as unknown as ReturnType<typeof useHosts>);
    getSessionMock.mockResolvedValue(sourceSession({ workspace: "/Users/alice/repo" }));

    renderDialog();
    // Prefilled to the source host + dir → no warning.
    await screen.findByTestId("resume-dir-bind-button");
    await waitFor(() =>
      expect((screen.getByTestId("mock-workspace-input") as HTMLInputElement).value).toBe(
        "/Users/alice/repo",
      ),
    );
    expect(screen.queryByTestId("resume-dir-mismatch-warning")).toBeNull();

    // Switch to a different host WITHOUT changing the directory → mismatch
    // warning fires purely on the host change (the path is unchanged).
    fireEvent.change(screen.getByTestId("mock-host-select"), {
      target: { value: "host_other" },
    });
    expect(await screen.findByTestId("resume-dir-mismatch-warning")).toBeTruthy();
  });

  it("stays in the loading state until the hosts list loads (no CLI-fallback flash)", async () => {
    // Hosts query not resolved yet (data: undefined) while the source IS
    // loaded. Without gating on hostsLoaded, sourceHostOnline would be
    // falsy purely because the list isn't in yet, flashing the CLI
    // reconnect fallback for a source host that may well be online.
    useHostsMock.mockReturnValue({ data: undefined } as unknown as ReturnType<typeof useHosts>);
    getSessionMock.mockResolvedValue(sourceSession({ workspace: "/Users/alice/repo" }));

    renderDialog();

    // The source resolves, but hosts haven't → show loading, NOT the CLI
    // fallback and NOT the picker (host status is unknown, not "offline").
    expect(await screen.findByTestId("resume-dir-loading")).toBeTruthy();
    expect(screen.queryByTestId("resume-dir-cli-fallback")).toBeNull();
    expect(screen.queryByTestId("resume-dir-bind-button")).toBeNull();
  });
});
