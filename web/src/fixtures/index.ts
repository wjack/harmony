/**
 * A fake API for building screens without a backend: the gallery (/__gallery), tests
 * and screenshots. Routes are keyed like the OpenAPI paths ("GET /api/inbox",
 * "POST /api/cards/{card_id}/approve"), so a fixture names exactly the route it fakes,
 * and a body is type-checked against the generated schema.
 *
 * Each area keeps its own fixtures (fixtures/chat.ts, fixtures/requests.ts, ...) and
 * merges them over `shellRoutes()` from fixtures/shell.ts.
 */
import { createApiClient, CURSOR_HEADER, type ApiClient, type ApiPaths } from "../api/client";

type Method = "get" | "post" | "put" | "delete";

/** "GET /api/inbox", for every method and path the schema declares. */
export type RouteKey = {
  [P in keyof ApiPaths]: {
    [M in Method]: ApiPaths[P][M] extends { responses: unknown } ? `${Uppercase<M>} ${P}` : never;
  }[Method];
}[keyof ApiPaths];

type SuccessBody<Op> = Op extends { responses: { 200: { content: { "application/json": infer B } } } }
  ? B
  : Op extends { responses: { 201: { content: { "application/json": infer B } } } }
    ? B
    : unknown;

/** The 200 body type of a route key. */
export type FixtureBody<K extends RouteKey> = K extends `${infer M} ${infer P}`
  ? P extends keyof ApiPaths
    ? Lowercase<M> extends keyof ApiPaths[P]
      ? SuccessBody<ApiPaths[P][Lowercase<M>]>
      : never
    : never
  : never;

export interface FixtureRequest {
  method: string;
  path: string;
  /** Path parameters by their template name (`{card_id}` → `params.card_id`). */
  params: Record<string, string>;
  query: URLSearchParams;
  /** The parsed JSON body, if any. */
  body: unknown;
}

/** A handler may return the body, or a `Response` for errors and headers. */
export type FixtureHandler<K extends RouteKey> = (
  request: FixtureRequest,
) => FixtureBody<K> | Response | Promise<FixtureBody<K> | Response>;

export type FixtureRoutes = { [K in RouteKey]?: FixtureBody<K> | FixtureHandler<K> };

export interface FixtureOptions {
  /** The `X-Harmony-Cursor` every response carries. */
  cursor?: number;
  /** Simulated latency, to see loading states. */
  latencyMs?: number;
}

/** A JSON response, e.g. `problem(409, "escalation_not_open", "…")` for an error path. */
export function json(body: unknown, status = 200, cursor = 1): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", [CURSOR_HEADER]: String(cursor) },
  });
}

/** A typed `ApiProblem` error response. */
export function problem(status: number, code: string, detail: string, extra: Record<string, unknown> = {}): Response {
  return json({ detail, code, ...extra }, status);
}

function matchTemplate(template: string, path: string): Record<string, string> | null {
  const names: string[] = [];
  const pattern = template.replace(/\{([^}]+)\}/g, (_, name: string) => {
    names.push(name);
    return "([^/]+)";
  });
  const match = new RegExp(`^${pattern}$`).exec(path);
  if (match === null) return null;
  return Object.fromEntries(names.map((name, index) => [name, decodeURIComponent(match[index + 1] ?? "")]));
}

async function readBody(request: Request): Promise<unknown> {
  const text = await request.text();
  if (text === "") return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

/** A `fetch` that serves `routes`; anything else is a 404 `not_found` problem. */
export function fixtureFetch(routes: FixtureRoutes, options: FixtureOptions = {}) {
  const cursor = options.cursor ?? 1;
  const entries = Object.entries<unknown>(routes);
  // Exact paths win over templates, so "/api/threads/{thread_id}/workspace" isn't
  // shadowed by a shorter template.
  entries.sort(([a], [b]) => Number(a.includes("{")) - Number(b.includes("{")));

  return async (request: Request): Promise<Response> => {
    const url = new URL(request.url);
    if (options.latencyMs !== undefined) {
      await new Promise((resolve) => setTimeout(resolve, options.latencyMs));
    }
    for (const [key, value] of entries) {
      const [method = "", template = ""] = key.split(" ");
      if (method !== request.method) continue;
      const params = matchTemplate(template, url.pathname);
      if (params === null) continue;
      if (typeof value !== "function") return json(value, 200, cursor);
      const handler = value as (request: FixtureRequest) => unknown;
      const result = await handler({
        method: request.method,
        path: url.pathname,
        params,
        query: url.searchParams,
        body: await readBody(request),
      });
      return result instanceof Response ? result : json(result, 200, cursor);
    }
    return problem(404, "not_found", `no fixture for ${request.method} ${url.pathname}`);
  };
}

/** A typed API client over fixture routes. */
export function createFixtureApi(routes: FixtureRoutes, options: FixtureOptions = {}): ApiClient {
  return createApiClient({ baseUrl: "http://fixtures.harmony.test", fetch: fixtureFetch(routes, options) });
}
