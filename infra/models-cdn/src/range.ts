/**
 * Single byte-range serving for model objects, so an interrupted download can
 * fetch only what it is missing. Callers authorize the request first; nothing
 * here looks at credentials.
 */

export type ParsedRange =
  | { kind: "full" }
  | { kind: "unsatisfiable" }
  | { kind: "range"; offset: number; length: number };

/**
 * Anything other than one well-formed byte range (multiple ranges, another
 * unit, garbage) is "full": RFC 9110 lets a server ignore Range and answer 200.
 */
export function parseSingleRange(header: string | null, size: number): ParsedRange {
  const match = /^bytes=(\d*)-(\d*)$/.exec((header || "").trim());
  if (!match || (!match[1] && !match[2])) return { kind: "full" };

  if (!match[1]) {
    const suffix = Number(match[2]);
    if (suffix === 0 || size === 0) return { kind: "unsatisfiable" };
    const length = Math.min(suffix, size);
    return { kind: "range", offset: size - length, length };
  }

  const offset = Number(match[1]);
  const end = match[2] ? Number(match[2]) : size - 1;
  if (end < offset && match[2]) return { kind: "full" };
  if (offset >= size) return { kind: "unsatisfiable" };
  return { kind: "range", offset, length: Math.min(end, size - 1) - offset + 1 };
}

type StoredObject = {
  size: number;
  httpEtag: string;
  writeHttpMetadata(headers: Headers): void;
};

export type ObjectBucket = {
  head(key: string): Promise<StoredObject | null>;
  get(
    key: string,
    options?: { range: { offset: number; length: number } },
  ): Promise<(StoredObject & { body: ReadableStream | Uint8Array }) | null>;
};

function notFound(key: string): Response {
  return new Response(JSON.stringify({ error: "object_not_found", key }), {
    status: 404,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

function responseHeaders(
  obj: StoredObject,
  extra: Record<string, string>,
  contentLength: number,
): Headers {
  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  headers.set("etag", obj.httpEtag);
  headers.set("accept-ranges", "bytes");
  for (const [name, value] of Object.entries(extra)) headers.set(name, value);
  headers.set("content-length", String(contentLength));
  return headers;
}

export async function serveObject(
  request: Request,
  bucket: ObjectBucket,
  key: string,
  extraHeaders: Record<string, string>,
): Promise<Response> {
  const rangeHeader = request.method === "GET" ? request.headers.get("range") : null;

  if (rangeHeader) {
    const head = await bucket.head(key);
    if (!head) return notFound(key);
    const range = parseSingleRange(rangeHeader, head.size);
    if (range.kind === "unsatisfiable") {
      return new Response(null, {
        status: 416,
        headers: { "content-range": `bytes */${head.size}`, "accept-ranges": "bytes" },
      });
    }
    if (range.kind === "range") {
      const part = await bucket.get(key, {
        range: { offset: range.offset, length: range.length },
      });
      if (!part) return notFound(key);
      const headers = responseHeaders(part, extraHeaders, range.length);
      headers.set(
        "content-range",
        `bytes ${range.offset}-${range.offset + range.length - 1}/${head.size}`,
      );
      return new Response(part.body as BodyInit, { status: 206, headers });
    }
  }

  const obj = await bucket.get(key);
  if (!obj) return notFound(key);
  const headers = responseHeaders(obj, extraHeaders, obj.size);
  if (request.method === "HEAD") {
    return new Response(null, { status: 200, headers });
  }
  return new Response(obj.body as BodyInit, { status: 200, headers });
}
