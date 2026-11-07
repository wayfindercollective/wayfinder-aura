/**
 * Byte-range serving unit tests (no Cloudflare runtime required).
 * Run: node --experimental-strip-types --test src/range.test.ts
 */
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { parseSingleRange, serveObject } from "./range.ts";

describe("parseSingleRange", () => {
  it("reads open, closed and suffix ranges", () => {
    assert.deepEqual(parseSingleRange("bytes=10-", 100), { kind: "range", offset: 10, length: 90 });
    assert.deepEqual(parseSingleRange("bytes=10-19", 100), { kind: "range", offset: 10, length: 10 });
    assert.deepEqual(parseSingleRange("bytes=-5", 100), { kind: "range", offset: 95, length: 5 });
  });

  it("clamps an end past the object", () => {
    assert.deepEqual(parseSingleRange("bytes=90-500", 100), { kind: "range", offset: 90, length: 10 });
    assert.deepEqual(parseSingleRange("bytes=-500", 100), { kind: "range", offset: 0, length: 100 });
  });

  it("calls a start past the end unsatisfiable", () => {
    assert.deepEqual(parseSingleRange("bytes=100-", 100), { kind: "unsatisfiable" });
    assert.deepEqual(parseSingleRange("bytes=0-", 0), { kind: "unsatisfiable" });
    assert.deepEqual(parseSingleRange("bytes=-0", 100), { kind: "unsatisfiable" });
  });

  it("ignores what it does not support", () => {
    for (const header of [null, "", "bytes=0-1,5-6", "items=0-1", "bytes=abc-", "bytes=9-3", "bytes=-"]) {
      assert.deepEqual(parseSingleRange(header, 100), { kind: "full" });
    }
  });
});

const BYTES = new TextEncoder().encode("0123456789");

function bucket(calls: unknown[] = []) {
  const object = (body: Uint8Array) => ({
    size: BYTES.length,
    httpEtag: '"abc"',
    body,
    writeHttpMetadata: (_headers: Headers) => {},
  });
  return {
    async head(key: string) {
      return key === "missing" ? null : object(BYTES);
    },
    async get(key: string, options?: { range?: { offset: number; length: number } }) {
      calls.push(options?.range ?? null);
      if (key === "missing") return null;
      const range = options?.range;
      return object(range ? BYTES.slice(range.offset, range.offset + range.length) : BYTES);
    },
  };
}

function request(method: string, range?: string) {
  return new Request("https://cdn.example/v1/objects/m.bin", {
    method,
    headers: range ? { range } : {},
  });
}

const BASE = { "cache-control": "private, no-store" };

describe("serveObject", () => {
  it("serves the whole object and advertises ranges", async () => {
    const res = await serveObject(request("GET"), bucket() as never, "m.bin", BASE);
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("accept-ranges"), "bytes");
    assert.equal(res.headers.get("content-length"), "10");
    assert.equal(res.headers.get("content-range"), null);
    assert.equal(res.headers.get("cache-control"), "private, no-store");
    assert.equal(await res.text(), "0123456789");
  });

  it("serves the rest of the object as a 206", async () => {
    const calls: unknown[] = [];
    const res = await serveObject(request("GET", "bytes=4-"), bucket(calls) as never, "m.bin", BASE);
    assert.equal(res.status, 206);
    assert.equal(res.headers.get("content-range"), "bytes 4-9/10");
    assert.equal(res.headers.get("content-length"), "6");
    assert.equal(res.headers.get("accept-ranges"), "bytes");
    assert.equal(res.headers.get("etag"), '"abc"');
    assert.deepEqual(calls, [{ offset: 4, length: 6 }]);
    assert.equal(await res.text(), "456789");
  });

  it("answers 416 with the object's size", async () => {
    const calls: unknown[] = [];
    const res = await serveObject(request("GET", "bytes=10-"), bucket(calls) as never, "m.bin", BASE);
    assert.equal(res.status, 416);
    assert.equal(res.headers.get("content-range"), "bytes */10");
    assert.deepEqual(calls, []);
  });

  it("answers a multi-range with the whole object", async () => {
    const res = await serveObject(request("GET", "bytes=0-1,4-5"), bucket() as never, "m.bin", BASE);
    assert.equal(res.status, 200);
    assert.equal(await res.text(), "0123456789");
  });

  it("ignores Range on HEAD", async () => {
    const res = await serveObject(request("HEAD", "bytes=4-"), bucket() as never, "m.bin", BASE);
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("content-length"), "10");
    assert.equal(res.headers.get("accept-ranges"), "bytes");
  });

  it("returns 404 for a missing object, with or without a range", async () => {
    for (const range of [undefined, "bytes=1-"]) {
      const res = await serveObject(request("GET", range), bucket() as never, "missing", BASE);
      assert.equal(res.status, 404);
    }
  });
});
