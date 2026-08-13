// Unit test for the pi-native bridge extension's interrupt / replay logic.
//
// Regression coverage for F18 (SDK_INTEGRATION_BUG_AUDIT.md): an interrupt that
// arrives while Pi is idle used to arm a 30s replay window that aborted the next
// legitimately-started turn. ExtensionContext.abort() is a silent no-op when the
// agent is idle (it does not throw), so the old requestInterrupt() armed the
// window unconditionally and replayPendingInterrupt() then killed the next turn.
//
// This test drives the real extension through its public surface: it registers
// the event handlers with a mock `pi`, and delivers interrupts through the real
// inbox poller (a temp inbox directory). No network is used (postEvent fails
// closed when config has no serverUrl).
//
// Run with: node omnigent/resources/pi_native/omnigent_pi_native_extension.test.js
//
// Manual reproduction of the original bug (for context):
//   1. Start a native Pi session linked to Omnigent and let it go idle.
//   2. Hit "stop"/interrupt while no turn is running (between turns).
//   3. Send a fresh user message within 30 seconds.
//   Before the fix: the fresh turn is aborted immediately at agent_start /
//   turn_start (and tool calls are blocked) before producing output. After the
//   fix: the idle interrupt is dropped and the fresh turn runs normally.

const fs = require("fs");
const os = require("os");
const path = require("path");

const EXT_PATH = path.resolve(__dirname, "omnigent_pi_native_extension.js");

const harnesses = [];

function makeResponse({ status = 200, contentType = "application/json", body = "{}" } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get: (name) => name.toLowerCase() === "content-type" ? contentType : null,
    },
    text: async () => body,
    json: async () => JSON.parse(body),
  };
}

// Build a fresh extension instance with its own temp inbox directory. Each call
// produces independent closure state (activeResponseId, pendingInterruptUntil,
// latestContext, ...).
function makeHarness({ captureEvents = false, existingTools = [] } = {}) {
  const inboxDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-native-inbox-"));
  const configPath = path.join(inboxDir, "config.json");
  // A serverUrl + sessionId make postEvent attempt a real fetch; with a mock
  // global fetch that lets a test capture the posted event bodies. Without
  // them postEvent fails closed (the interrupt tests rely on that).
  const config = captureEvents
    ? { inboxDir, serverUrl: "http://mock", sessionId: "conv_test" }
    : { inboxDir };
  fs.writeFileSync(configPath, JSON.stringify(config));
  process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

  // Capture posted event bodies (status edges etc.) instead of hitting network.
  const postedEvents = [];
  if (captureEvents) {
    global.fetch = async (url, opts) => {
      try {
        if (opts && typeof opts.body === "string" && String(url).endsWith("/events")) {
          postedEvents.push(JSON.parse(opts.body));
        }
      } catch (_err) {}
      return makeResponse({ status: 202, body: '{"queued":false}' });
    };
  }

  const handlers = {};
  const registeredTools = {};
  const pi = {
    on: (name, fn) => {
      handlers[name] = fn;
    },
    registerCommand: () => {},
    registerTool: (tool) => {
      registeredTools[tool.name] = tool;
    },
    getAllTools: () => existingTools,
    sendUserMessage: () => {},
  };

  // Fresh module-function invocation -> fresh closures.
  delete require.cache[EXT_PATH];
  const mod = require(EXT_PATH);
  mod(pi);

  const h = { pi, handlers, inboxDir, postedEvents, registeredTools };
  harnesses.push(h);
  return h;
}

function statusEdges(postedEvents) {
  return postedEvents
    .filter((e) => e && e.type === "external_session_status" && e.data)
    .map((e) => ({ status: e.data.status, responseId: e.data.response_id }));
}

// ctx mock. `idle` may be true/false (exposes isIdle()) or undefined (no isIdle
// method at all, exercising the activeResponseId fallback path).
function makeCtx({ idle } = {}) {
  const ctx = {
    abortCount: 0,
    abort() {
      this.abortCount += 1;
    },
  };
  if (idle !== undefined) ctx.isIdle = () => idle;
  return ctx;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Drop an interrupt into the inbox and wait until the poller has consumed it
// (the poller unlinks the file after invoking handleInterrupt -> requestInterrupt).
async function deliverInterrupt(h) {
  const file = path.join(h.inboxDir, `int-${Date.now()}-${Math.random().toString(36).slice(2)}.json`);
  fs.writeFileSync(file, JSON.stringify({ type: "interrupt" }));
  const deadline = Date.now() + 3000;
  while (fs.existsSync(file)) {
    if (Date.now() > deadline) throw new Error("interrupt file was not consumed by poller");
    await sleep(20);
  }
  // The poller runs requestInterrupt synchronously before unlinking, so by the
  // time the file is gone the interrupt has been processed.
}

function assert(name, cond, detail) {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? "  -- " + detail : ""}`);
  if (!cond) process.exitCode = 1;
}

async function testOmnigentResponseContractAndDegradedStatus() {
  delete require.cache[EXT_PATH];
  const extension = require(EXT_PATH);
  const {
    validateOmnigentJsonResponse,
    fetchOmnigentJson,
    responseFailureClass,
  } = extension._test;

  async function classification(response) {
    try {
      await validateOmnigentJsonResponse(response);
      return "success";
    } catch (error) {
      return responseFailureClass(error);
    }
  }

  assert(
    "200 text/html sign-in response is edge authentication failure",
    (await classification(makeResponse({
      contentType: "text/html; charset=utf-8",
      body: "<!doctype html><title>Databricks sign in</title>",
    }))) === "edge_auth",
  );
  assert(
    "200 application/json object satisfies the response contract",
    (await classification(makeResponse({ body: '{"queued":false}' }))) === "success",
  );
  assert(
    "valid parsed JSON may contain HTML-like user content",
    (await classification(makeResponse({
      body: JSON.stringify({ queued: true, content: "Example: <html> is ordinary tool output" }),
    }))) === "success",
  );
  assert("401 is classified as auth", (await classification(makeResponse({ status: 401 }))) === "auth");
  assert("403 is classified as auth", (await classification(makeResponse({ status: 403 }))) === "auth");
  assert("5xx is classified as server", (await classification(makeResponse({ status: 503 }))) === "server");
  assert(
    "malformed JSON is classified distinctly",
    (await classification(makeResponse({ body: "not-json" }))) === "malformed_json",
  );
  assert(
    "wrong content type is classified distinctly",
    (await classification(makeResponse({ contentType: "text/plain", body: "not json" }))) === "content_type",
  );
  assert(
    "mislabeled sign-in document remains an edge authentication failure",
    (await classification(makeResponse({
      contentType: "application/json",
      body: "<html>Databricks sign in</html>",
    }))) === "edge_auth",
  );
  const largeToolPayload = JSON.stringify({
    content: [{ type: "text", text: "x".repeat(2 * 1_048_576) }],
  });
  let largeToolAccepted = false;
  try {
    await validateOmnigentJsonResponse(
      makeResponse({ body: largeToolPayload }),
      undefined,
      10 * 1_048_576,
    );
    largeToolAccepted = true;
  } catch (_error) {}
  assert(
    "tool-response contract preserves legitimate multi-megabyte output",
    largeToolAccepted,
  );

  const originalFetch = global.fetch;
  global.fetch = async () => ({
    status: 202,
    headers: { get: (name) => name.toLowerCase() === "content-type" ? "application/json" : null },
    text: async () => new Promise(() => {}),
  });
  const stalledStartedAt = Date.now();
  let stalledClass = "success";
  try {
    await fetchOmnigentJson("http://mock/events", { method: "POST" }, undefined, 20);
  } catch (error) {
    stalledClass = responseFailureClass(error);
  } finally {
    global.fetch = originalFetch;
  }
  assert(
    "stalled response body is aborted within the caller budget",
    stalledClass === "transport" && Date.now() - stalledStartedAt < 500,
    JSON.stringify({ stalledClass, elapsedMs: Date.now() - stalledStartedAt }),
  );

  const h = makeHarness({ captureEvents: true });
  const rendered = [];
  const ctx = {
    ui: {
      setTitle() {},
      setStatus(key, value) {
        if (key === "omnigent") rendered.push(value);
      },
    },
  };
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args);
  const sensitive = [
    "Bearer must-not-log",
    "relay-token-must-not-log",
    "Databricks sign in document must-not-log",
  ];
  try {
    global.fetch = async () => {
      throw new Error(sensitive.join(" "));
    };
    for (let i = 0; i < 4; i += 1) {
      await h.handlers.input({ text: `message-${i}` }, ctx);
    }
    assert(
      "transport failure remains fail-open for Pi input handling",
      rendered.length > 0,
      JSON.stringify(rendered),
    );
    assert(
      "chat offline marker appears at the named threshold and remains stable",
      rendered.filter((label) => String(label).includes("chat offline")).length >= 2 &&
        String(rendered.at(-1)).includes("chat offline"),
      JSON.stringify(rendered),
    );
    assert(
      "degraded diagnostic is emitted once per failure episode",
      warnings.length === 1,
      JSON.stringify(warnings),
    );
    const diagnostic = warnings[0] && warnings[0][1];
    assert(
      "diagnostic contains only session id, failure class, and consecutive count",
      diagnostic &&
        JSON.stringify(Object.keys(diagnostic).sort()) ===
          JSON.stringify(["consecutiveCount", "failureClass", "sessionId"]) &&
        diagnostic.failureClass === "transport" &&
        diagnostic.consecutiveCount === 3,
      JSON.stringify(diagnostic),
    );
    const logged = JSON.stringify(warnings);
    assert(
      "diagnostic never logs bearer, relay token, response body, or sign-in document",
      sensitive.every((value) => !logged.includes(value)) && !logged.toLowerCase().includes("<html"),
      logged,
    );

    global.fetch = async () => makeResponse({
      status: 200,
      contentType: "text/html",
      body: "<html>Databricks sign in document must-not-log</html>",
    });
    await h.handlers.input({ text: "edge-failure-1" }, ctx);
    await h.handlers.input({ text: "edge-failure-2" }, ctx);
    await h.handlers.input({ text: "edge-failure-3" }, ctx);
    await h.handlers.input({ text: "edge-failure-4" }, ctx);
    assert(
      "a changed failure class starts one new bounded diagnostic episode",
      warnings.length === 2 && warnings[1][1].failureClass === "edge_auth",
      JSON.stringify(warnings),
    );

    global.fetch = async () => makeResponse({ status: 202, body: '{"queued":false}' });
    await h.handlers.input({ text: "recovered" }, ctx);
    assert(
      "first validated success clears degraded status",
      !String(rendered.at(-1)).includes("chat offline"),
      JSON.stringify(rendered.at(-1)),
    );

    global.fetch = async () => { throw new Error("still private"); };
    await h.handlers.input({ text: "after-success-1" }, ctx);
    await h.handlers.input({ text: "after-success-2" }, ctx);
    assert(
      "validated success resets the consecutive failure streak",
      warnings.length === 2 && !String(rendered.at(-1)).includes("chat offline"),
      JSON.stringify({ warnings, last: rendered.at(-1) }),
    );
  } finally {
    console.warn = originalWarn;
  }

  const patchHarness = makeHarness({ captureEvents: true });
  const patchWarnings = [];
  let patchAttempts = 0;
  console.warn = (...args) => patchWarnings.push(args);
  global.fetch = async (url, options) => {
    if (options && options.method === "PATCH") {
      patchAttempts += 1;
      if (patchAttempts === 1) {
        return makeResponse({
          status: 200,
          contentType: "text/html",
          body: "<html>Databricks sign in private-body</html>",
        });
      }
      return makeResponse({ body: '{"id":"conv_test"}' });
    }
    return makeResponse({ status: 202, body: '{"queued":false}' });
  };
  const patchCtx = {
    sessionManager: { getSessionId: () => "native-private-id" },
    ui: { setTitle() {}, setStatus() {}, notify() {} },
  };
  try {
    await patchHarness.handlers.session_start({}, patchCtx);
    await patchHarness.handlers.agent_start({}, patchCtx);
    assert(
      "external session link retries on a later lifecycle and recovers",
      patchAttempts === 2,
      `patchAttempts=${patchAttempts}`,
    );
    assert(
      "external session link failure emits one sanitized diagnostic",
      patchWarnings.length === 1 &&
        patchWarnings[0][1].failureClass === "edge_auth" &&
        !JSON.stringify(patchWarnings).includes("native-private-id") &&
        !JSON.stringify(patchWarnings).includes("private-body"),
      JSON.stringify(patchWarnings),
    );
  } finally {
    console.warn = originalWarn;
  }
}

async function testIdleInterruptDoesNotPoisonNextTurn() {
  const h = makeHarness();
  const idleCtx = makeCtx({ idle: true });
  await h.handlers.session_start({}, idleCtx);

  await deliverInterrupt(h);

  assert(
    "idle interrupt (isIdle) does not abort the idle context",
    idleCtx.abortCount === 0,
    `abortCount=${idleCtx.abortCount}`,
  );

  // A fresh, legitimate turn starts within the (old) 30s window.
  const turnCtx = makeCtx({ idle: false });
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t1", toolName: "do_thing", input: {} },
    turnCtx,
  );

  assert(
    "fresh turn after idle interrupt is NOT aborted",
    turnCtx.abortCount === 0,
    `abortCount=${turnCtx.abortCount}`,
  );
  assert(
    "fresh turn's tool_call is NOT blocked after idle interrupt",
    !toolResult || toolResult.block !== true,
    JSON.stringify(toolResult),
  );
}

async function testIdleInterruptFallbackNoIsIdle() {
  // No isIdle() on ctx -> requestInterrupt falls back to !activeResponseId.
  // Between turns activeResponseId is null, so this must behave as idle.
  const h = makeHarness();
  const idleCtx = makeCtx({}); // no isIdle method
  await h.handlers.session_start({}, idleCtx);

  await deliverInterrupt(h);

  assert(
    "idle interrupt (activeResponseId fallback) does not arm the window",
    idleCtx.abortCount === 0,
    `abortCount=${idleCtx.abortCount}`,
  );

  const turnCtx = makeCtx({}); // no isIdle method
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t1", toolName: "do_thing", input: {} },
    turnCtx,
  );

  assert(
    "fresh turn after fallback idle interrupt is NOT aborted",
    turnCtx.abortCount === 0,
    `abortCount=${turnCtx.abortCount}`,
  );
  assert(
    "fresh turn's tool_call is NOT blocked (fallback)",
    !toolResult || toolResult.block !== true,
    JSON.stringify(toolResult),
  );
}

async function testMidTurnInterruptStillAborts() {
  // Regression guard: a genuine mid-turn interrupt must still abort and replay.
  const h = makeHarness();
  const turnCtx = makeCtx({ idle: false });
  await h.handlers.session_start({}, turnCtx); // starts the inbox poller
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);

  await deliverInterrupt(h);

  assert(
    "mid-turn interrupt aborts the live turn",
    turnCtx.abortCount >= 1,
    `abortCount=${turnCtx.abortCount}`,
  );

  // Replay must keep aborting within the window and block in-flight tool calls.
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t1", toolName: "do_thing", input: {} },
    turnCtx,
  );
  assert(
    "mid-turn interrupt blocks subsequent tool_call (replay)",
    !!toolResult && toolResult.block === true,
    JSON.stringify(toolResult),
  );
}

async function testAgentLoopInterruptFallbackNoIsIdleBeforeTurnStart() {
  // No isIdle(), and an interrupt lands after agent_start but before
  // turn_start. Older SDKs without isIdle() still need to treat this as part of
  // the live agent loop, not as an idle interrupt to drop.
  const h = makeHarness();
  const turnCtx = makeCtx({}); // no isIdle method
  await h.handlers.session_start({}, turnCtx); // starts the inbox poller
  await h.handlers.agent_start({}, turnCtx);

  await deliverInterrupt(h);

  assert(
    "agent-loop interrupt aborts before turn_start (active loop fallback)",
    turnCtx.abortCount >= 1,
    `abortCount=${turnCtx.abortCount}`,
  );

  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t1", toolName: "do_thing", input: {} },
    turnCtx,
  );
  assert(
    "agent-loop interrupt before turn_start replays to block tool_call",
    !!toolResult && toolResult.block === true,
    JSON.stringify(toolResult),
  );
}

async function testMidTurnInterruptFallbackNoIsIdle() {
  // No isIdle() but an agent loop is active -> must still arm.
  const h = makeHarness();
  const turnCtx = makeCtx({}); // no isIdle method
  await h.handlers.session_start({}, turnCtx); // starts the inbox poller
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);

  await deliverInterrupt(h);

  assert(
    "mid-turn interrupt aborts (activeResponseId fallback)",
    turnCtx.abortCount >= 1,
    `abortCount=${turnCtx.abortCount}`,
  );
}

async function testAgentStartClearsStaleWindow() {
  // Belt-and-suspenders: even if a window is armed during a live turn, a brand
  // new agent loop must start clean and not abort its first tool call.
  const h = makeHarness();
  const turnCtx = makeCtx({ idle: false });
  await h.handlers.session_start({}, turnCtx); // starts the inbox poller
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);
  await deliverInterrupt(h);
  assert(
    "window armed during live turn (precondition)",
    turnCtx.abortCount >= 1,
    `abortCount=${turnCtx.abortCount}`,
  );

  // A new agent loop begins (e.g. the user's next message) within 30s.
  const nextCtx = makeCtx({ idle: false });
  await h.handlers.agent_start({}, nextCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, nextCtx);
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t2", toolName: "do_thing", input: {} },
    nextCtx,
  );

  assert(
    "new agent loop clears stale window (no abort)",
    nextCtx.abortCount === 0,
    `abortCount=${nextCtx.abortCount}`,
  );
  assert(
    "new agent loop's tool_call is NOT blocked",
    !toolResult || toolResult.block !== true,
    JSON.stringify(toolResult),
  );
}

async function testTaskPlanPublishesTodos() {
  const h = makeHarness({ captureEvents: true });
  await h.handlers.session_start({}, {});
  const tool = h.registeredTools.manage_todo_list;
  assert("registers manage_todo_list", !!tool);
  assert(
    "requires a task plan before multi-step work",
    tool.promptGuidelines.some((guideline) =>
      guideline.includes(
        "must call manage_todo_list before using other tools",
      ),
    ),
  );

  const result = await tool.execute("todo-1", {
    operation: "write",
    todoList: [
      {
        id: 1,
        title: "Trace rendering",
        description: "Tracing the shared event path",
        status: "in-progress",
      },
      {
        id: 2,
        title: "Run checks",
        description: "Run focused checks",
        status: "not-started",
      },
    ],
  });
  const event = h.postedEvents.find(
    (item) => item.type === "external_session_todos",
  );
  assert(
    "manage_todo_list publishes the shared todo event",
    JSON.stringify(event && event.data.todos) ===
      JSON.stringify([
        {
          content: "Trace rendering",
          status: "in_progress",
          activeForm: "Tracing the shared event path",
        },
        {
          content: "Run checks",
          status: "pending",
          activeForm: "Run focused checks",
        },
      ]),
    JSON.stringify(event),
  );
  assert(
    "manage_todo_list persists its current list in tool details",
    result.details.todos.length === 2 &&
      result.details.todos[0].status === "in-progress",
    JSON.stringify(result),
  );

  const restored = makeHarness({ captureEvents: true });
  await restored.handlers.session_start(
    {},
    {
      sessionManager: {
        getBranch: () => [
          {
            type: "message",
            message: {
              role: "toolResult",
              toolName: "manage_todo_list",
              details: { todos: result.details.todos },
            },
          },
        ],
      },
    },
  );
  const restoredEvent = restored.postedEvents.find(
    (item) => item.type === "external_session_todos",
  );
  assert(
    "session_start restores and republishes the latest task plan",
    restoredEvent && restoredEvent.data.todos.length === 2,
    JSON.stringify(restoredEvent),
  );
}

async function testExistingTaskToolIsMirroredWithoutConflict() {
  const h = makeHarness({
    captureEvents: true,
    existingTools: [{ name: "manage_todo_list" }],
  });
  await h.handlers.session_start({}, {});
  assert(
    "reuses an existing manage_todo_list tool",
    !h.registeredTools.manage_todo_list,
  );

  await h.handlers.tool_result(
    {
      toolCallId: "external-todo-1",
      toolName: "manage_todo_list",
      details: {
        todos: [
          {
            id: 1,
            title: "Use the shared tool",
            description: "Using the shared task tool",
            status: "in-progress",
          },
        ],
      },
    },
    {},
  );
  const event = h.postedEvents.find(
    (item) => item.type === "external_session_todos",
  );
  assert(
    "mirrors an existing task tool into the shared todo event",
    event && event.data.todos[0].content === "Use the shared tool",
    JSON.stringify(event),
  );
}

// The web store only clears its local "streaming" flag when a turn's `idle`
// status edge carries the same response_id as the `running` edge that opened
// it. A fresh id per edge left the composer stuck queueing until a tab switch
// reset the store. Assert agent_start/agent_end share one id.
async function testRunningIdleShareResponseId() {
  const h = makeHarness({ captureEvents: true });
  const ctx = makeCtx({ idle: false });

  await h.handlers.agent_start({}, ctx);
  await h.handlers.agent_end({ messages: [] }, ctx);

  const edges = statusEdges(h.postedEvents);
  const running = edges.find((e) => e.status === "running");
  const idle = edges.find((e) => e.status === "idle");

  assert(
    "agent_start posts a running edge with a response_id",
    running !== undefined && typeof running.responseId === "string" && running.responseId.length > 0,
    JSON.stringify(running),
  );
  assert(
    "agent_end posts an idle edge with a response_id",
    idle !== undefined && typeof idle.responseId === "string" && idle.responseId.length > 0,
    JSON.stringify(idle),
  );
  assert(
    "running and idle edges share the same response_id",
    running && idle && running.responseId === idle.responseId,
    `running=${running && running.responseId} idle=${idle && idle.responseId}`,
  );

  // A second turn mints a fresh id, still paired across its own running/idle.
  await h.handlers.agent_start({}, ctx);
  await h.handlers.agent_end({ messages: [] }, ctx);
  const edges2 = statusEdges(h.postedEvents);
  const running2 = edges2.filter((e) => e.status === "running");
  const idle2 = edges2.filter((e) => e.status === "idle");
  assert(
    "second turn pairs its own running/idle id and differs from the first",
    running2.length === 2 &&
      idle2.length === 2 &&
      running2[1].responseId === idle2[1].responseId &&
      running2[1].responseId !== running2[0].responseId,
    `turn1=${running2[0].responseId} turn2=${running2[1].responseId}`,
  );
}

(async () => {
  try {
    await testOmnigentResponseContractAndDegradedStatus();
    await testRunningIdleShareResponseId();
    await testTaskPlanPublishesTodos();
    await testExistingTaskToolIsMirroredWithoutConflict();
    await testIdleInterruptDoesNotPoisonNextTurn();
    await testIdleInterruptFallbackNoIsIdle();
    await testMidTurnInterruptStillAborts();
    await testAgentLoopInterruptFallbackNoIsIdleBeforeTurnStart();
    await testMidTurnInterruptFallbackNoIsIdle();
    await testAgentStartClearsStaleWindow();
  } finally {
    for (const h of harnesses) {
      if (h.pi.__omnigentInboxPoller) clearInterval(h.pi.__omnigentInboxPoller);
      try {
        fs.rmSync(h.inboxDir, { recursive: true, force: true });
      } catch (_err) {}
    }
  }
})();
