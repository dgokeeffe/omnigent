import { describe, expect, it } from "vitest";

import {
  CAPACITY_SNAPSHOT_MAX_AGE_MS,
  CAPACITY_SNAPSHOT_MAX_SKEW_MS,
  CAPACITY_UNKNOWN_LABEL,
  hostCapacityLabel,
  isCapacityFresh,
  isHostAtCapacity,
  type HostCapacity,
} from "./useHosts";

const NOW = 1_700_000_000_000;
const FRESH_AT = NOW / 1000;

function capacity(overrides: Partial<HostCapacity> = {}): HostCapacity {
  return {
    active: 3,
    pending: 0,
    limit: 10,
    available: 7,
    accepting: true,
    reason: null,
    observed_at: FRESH_AT,
    ...overrides,
  };
}

describe("isCapacityFresh", () => {
  it("treats absent capacity as unknown", () => {
    expect(isCapacityFresh(null, NOW)).toBe(false);
    expect(isCapacityFresh(undefined, NOW)).toBe(false);
  });

  it("treats a missing or non-numeric observed_at as unknown", () => {
    expect(isCapacityFresh(capacity({ observed_at: null }), NOW)).toBe(false);
    expect(
      isCapacityFresh({ ...capacity(), observed_at: "soon" } as unknown as HostCapacity, NOW),
    ).toBe(false);
  });

  it("accepts a snapshot inside the freshness window and rejects older ones", () => {
    expect(isCapacityFresh(capacity(), NOW)).toBe(true);
    const edge = NOW + CAPACITY_SNAPSHOT_MAX_AGE_MS;
    expect(isCapacityFresh(capacity(), edge)).toBe(true);
    expect(isCapacityFresh(capacity(), edge + 1)).toBe(false);
  });
});

describe("isHostAtCapacity", () => {
  it("only blocks on a fresh not-accepting snapshot", () => {
    expect(isHostAtCapacity(capacity({ accepting: false }), NOW)).toBe(true);
    expect(isHostAtCapacity(capacity({ accepting: true }), NOW)).toBe(false);
  });

  it("never blocks on unknown or stale capacity", () => {
    // A partially-upgraded fleet must stay usable: unknown is not "full".
    expect(isHostAtCapacity(null, NOW)).toBe(false);
    expect(isHostAtCapacity(capacity({ accepting: false, observed_at: null }), NOW)).toBe(false);
    const stale = NOW + CAPACITY_SNAPSHOT_MAX_AGE_MS + 1;
    expect(isHostAtCapacity(capacity({ accepting: false }), stale)).toBe(false);
  });
});

describe("hostCapacityLabel", () => {
  it("renders used/limit including in-flight launches", () => {
    // Pending launches already consume a slot, so they must be counted or
    // the label disagrees with the host's own admission arithmetic.
    expect(hostCapacityLabel(capacity({ active: 8, pending: 2 }), NOW)).toBe("10/10 runners");
    expect(hostCapacityLabel(capacity({ active: 0, pending: 0 }), NOW)).toBe("0/10 runners");
  });

  it("labels an uncapped host as having no limit rather than 0 free", () => {
    expect(hostCapacityLabel(capacity({ limit: null, available: null, active: 4 }), NOW)).toBe(
      "4 running · no limit",
    );
  });

  it("calls out a memory-pressure pause separately from the count cap", () => {
    expect(
      hostCapacityLabel(capacity({ active: 2, accepting: false, reason: "memory_pressure" }), NOW),
    ).toBe("2/10 runners · paused (memory)");
    expect(
      hostCapacityLabel(
        capacity({ active: 10, pending: 0, accepting: false, reason: "host_at_capacity" }),
        NOW,
      ),
    ).toBe("10/10 runners");
  });

  it("labels unknown or stale capacity as unknown, never as zero free slots", () => {
    // An online host that has not reported must not read as "0/10".
    expect(hostCapacityLabel(null, NOW)).toBe(CAPACITY_UNKNOWN_LABEL);
    expect(hostCapacityLabel(capacity({ observed_at: null }), NOW)).toBe(CAPACITY_UNKNOWN_LABEL);
    expect(hostCapacityLabel(capacity(), NOW + CAPACITY_SNAPSHOT_MAX_AGE_MS + 1)).toBe(
      CAPACITY_UNKNOWN_LABEL,
    );
  });

  it("rejects a future-stamped snapshot beyond the tolerated skew", () => {
    // A badly-skewed host must not keep a stale refusal "fresh" for an extra
    // minute by stamping the future.
    const withinSkew = NOW + CAPACITY_SNAPSHOT_MAX_SKEW_MS;
    expect(isCapacityFresh(capacity({ observed_at: withinSkew / 1000 }), NOW)).toBe(true);
    const beyondSkew = withinSkew + 1000;
    expect(isCapacityFresh(capacity({ observed_at: beyondSkew / 1000 }), NOW)).toBe(false);
    expect(
      isHostAtCapacity(capacity({ accepting: false, observed_at: beyondSkew / 1000 }), NOW),
    ).toBe(false);
    expect(hostCapacityLabel(capacity({ observed_at: beyondSkew / 1000 }), NOW)).toBe(
      CAPACITY_UNKNOWN_LABEL,
    );
  });

  it("treats a non-finite observed_at as unknown", () => {
    expect(isCapacityFresh(capacity({ observed_at: Number.NaN }), NOW)).toBe(false);
    expect(isCapacityFresh(capacity({ observed_at: Number.POSITIVE_INFINITY }), NOW)).toBe(false);
  });
});
