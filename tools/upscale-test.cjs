#!/usr/bin/env node
/*
 * Tests for the shared pixel-transfer limits:  node tools/upscale-test.cjs
 */
"use strict";

const assert = require("node:assert");
const path = require("node:path");
const Limits = require(path.join(__dirname, "..", "plugin", "limits.js"));

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("the limits match what Photon accepts", () => {
  assert.equal(Limits.MAX_EDGE, 8192);
  assert.equal(Limits.MAX_PIXELS, 16 * 1024 * 1024);
  assert.equal(Limits.megapixels(Limits.MAX_PIXELS), 16);
});

test("checkSource accepts a normal layer and rejects empty or oversized ones", () => {
  Limits.checkSource(1920, 1080);
  Limits.checkSource(4000, 4000); // exactly 16 MP
  assert.throws(() => Limits.checkSource(0, 100), /no raster pixels/);
  assert.throws(() => Limits.checkSource(4000, 5000), /above the 16 MP limit/);
});

test("checkScaled enforces the per-side limit", () => {
  Limits.checkScaled(4096, 4096, 2);
  assert.throws(() => Limits.checkScaled(9000, 100, 3), /above the 8192px per-side limit/);
});

test("checkScaled enforces the total pixel limit", () => {
  Limits.checkScaled(4096, 4096, 2); // exactly 16 MP, legal
  assert.throws(() => Limits.checkScaled(5000, 4000, 2), /above the 16 MP layer limit/);
});

test("checkPadded guards the fallback placement", () => {
  assert.deepEqual(Limits.checkPadded(100, 200), { width: 100, height: 200 });
  assert.throws(() => Limits.checkPadded(9000, 100), /above the plugin limits/);
  assert.throws(() => Limits.checkPadded(5000, 5000), /above the plugin limits/);
});

test("custom limits are honoured (testability)", () => {
  const small = { maxEdge: 100, maxPixels: 1000 };
  assert.throws(() => Limits.checkSource(50, 50, small), /above the/);
  Limits.checkSource(20, 20, small);
  assert.throws(() => Limits.checkScaled(200, 10, 2, small), /per-side/);
  assert.throws(() => Limits.checkPadded(90, 90, small), /above the plugin limits/);
});

test("the megapixel label is percent-free and rounded", () => {
  assert.equal(Limits.megapixels(1048576), 1);
  assert.equal(Limits.megapixels(1.5 * 1048576), 1.5);
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log("  ok   " + name);
    } catch (error) {
      failed += 1;
      console.log("  FAIL " + name + "\n       " + (error && error.message));
    }
  }
  console.log("\n" + (tests.length - failed) + "/" + tests.length + " tests passed");
  process.exit(failed ? 1 : 0);
})();
