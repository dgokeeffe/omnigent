import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Session } from "@/lib/types";
import {
  getSessionSlim,
  listCodaClaims,
  listCodaSandboxOptions,
  releaseCodaClaim,
  releaseSession,
  resumeSession,
} from "@/lib/sessionsApi";
import { CodaLifecycleControls } from "./CodaLifecycleControls";

vi.mock("@/lib/sessionsApi", () => ({
  getSessionSlim: vi.fn(),
  listCodaClaims: vi.fn(),
  listCodaSandboxOptions: vi.fn(),
  releaseCodaClaim: vi.fn(),
  releaseSession: vi.fn(),
  resumeSession: vi.fn(),
}));

const session = {
  id: "session-a",
  agentId: "agent-a",
  agentName: "Agent",
  status: "idle",
  createdAt: 1,
  title: "Session A",
  items: [],
  permissionLevel: 4,
  parentSessionId: null,
  subAgentName: null,
  kind: "default",
  hostId: "host-private",
  detached: false,
} as Session;

function renderControls(value: Session = session) {
  return render(<CodaLifecycleControls session={value} />);
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(listCodaClaims).mockResolvedValue([
    {
      anchorSessionId: "session-a",
      sessions: [
        { id: "session-a", title: "Session A", detached: false },
        { id: "session-b", title: "Session B", detached: false },
      ],
    },
  ]);
  vi.mocked(getSessionSlim).mockResolvedValue(session);
  vi.mocked(listCodaSandboxOptions).mockResolvedValue([
    {
      app_id: "available-target",
      label: "CoDA-Sandbox-1",
      ownership: "mine",
      state: "available",
      capacity: { used: 1, limit: 10 },
    },
    {
      app_id: "private-unavailable-target",
      label: "CoDA-Sandbox-2",
      ownership: "other",
      state: "full",
      capacity: { used: null, limit: 10 },
    },
  ]);
});

describe("CodaLifecycleControls", () => {
  it("shows an accessible destructive warning, every affected session, and cancels", async () => {
    renderControls();
    fireEvent.click(await screen.findByRole("button", { name: "Release CoDA sandbox capacity" }));
    expect(screen.getByRole("dialog", { name: "Release CoDA capacity?" })).toBeInTheDocument();
    expect(
      screen.getByText(/Sandbox files and uncommitted changes will be permanently erased/),
    ).toBeInTheDocument();
    expect(screen.getByTestId("coda-affected-sessions")).toHaveTextContent("Session A");
    expect(screen.getByTestId("coda-affected-sessions")).toHaveTextContent("Session B");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(screen.queryByTestId("coda-release-dialog")).not.toBeInTheDocument(),
    );
    expect(releaseSession).not.toHaveBeenCalled();
    expect(releaseCodaClaim).not.toHaveBeenCalled();
  });

  it("releases one session while preserving the conversation flow", async () => {
    vi.mocked(releaseSession).mockResolvedValue();
    renderControls();
    fireEvent.click(await screen.findByTestId("coda-release-button"));
    fireEvent.click(screen.getByRole("button", { name: "Release this session" }));
    await waitFor(() => expect(releaseSession).toHaveBeenCalledWith("session-a"));
    await waitFor(() =>
      expect(screen.queryByTestId("coda-release-dialog")).not.toBeInTheDocument(),
    );
  });

  it("renders a 409 race as retryable and succeeds on retry", async () => {
    vi.mocked(releaseCodaClaim)
      .mockRejectedValueOnce(new Error("A sandbox session is busy; stop it and retry"))
      .mockResolvedValueOnce();
    renderControls();
    fireEvent.click(await screen.findByTestId("coda-release-button"));
    fireEvent.click(screen.getByRole("button", { name: "Release sandbox (2)" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("stop it and retry");
    fireEvent.click(screen.getByRole("button", { name: "Release sandbox (2)" }));
    await waitFor(() => expect(releaseCodaClaim).toHaveBeenCalledTimes(2));
  });

  it("resumes automatically or in an enabled manual target and disables unavailable choices", async () => {
    vi.mocked(resumeSession).mockResolvedValue();
    renderControls({ ...session, detached: true, hostId: null });
    fireEvent.click(await screen.findByTestId("coda-resume-button"));
    const picker = screen.getByRole("combobox", { name: "CoDA sandbox" });
    const unavailable = await screen.findByRole("option", { name: /CoDA-Sandbox-2/ });
    expect(unavailable).toBeDisabled();
    fireEvent.change(picker, { target: { value: "available-target" } });
    fireEvent.click(screen.getByRole("button", { name: "Resume in fresh sandbox" }));
    await waitFor(() =>
      expect(resumeSession).toHaveBeenCalledWith("session-a", "available-target"),
    );
  });

  it("does not expose owner controls to a read-only viewer", async () => {
    renderControls({ ...session, permissionLevel: 1 });
    await waitFor(() => expect(listCodaClaims).not.toHaveBeenCalled());
    expect(screen.queryByTestId("coda-release-button")).not.toBeInTheDocument();
  });
});
