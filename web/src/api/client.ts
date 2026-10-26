import createClient, { type Client } from "openapi-fetch";

import type { paths } from "./schema";

/** Every `/api` path, generated from FastAPI's OpenAPI document (`make gen-api`). */
export type ApiPaths = paths;

export type ApiClient = Client<ApiPaths>;

/**
 * List and snapshot responses carry the highest event `seq` visible in the transaction
 * that produced them. Opening the event stream with `?after=<cursor>` then delivers
 * exactly the events the snapshot doesn't include (ARCHITECTURE.md §8).
 */
export const CURSOR_HEADER = "X-Harmony-Cursor";

export interface ApiClientOptions {
  /** Defaults to the page's origin: the API and the SPA are served from one origin. */
  baseUrl?: string;
  fetch?: (request: Request) => Promise<Response>;
}

export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  return createClient<ApiPaths>({
    baseUrl: options.baseUrl ?? window.location.origin,
    // The session cookie is HttpOnly and same-origin.
    credentials: "same-origin",
    ...(options.fetch ? { fetch: options.fetch } : {}),
  });
}

export function readCursor(response: Response): number | null {
  const raw = response.headers.get(CURSOR_HEADER);
  if (raw === null || !/^\d+$/.test(raw.trim())) return null;
  return Number(raw.trim());
}

/** The typed problem body P1–P2 routes send (`ApiProblem` in the schema). */
export interface ProblemBody {
  detail: string;
  code: string;
  resolved_by?: unknown;
}

function isProblemBody(body: unknown): body is ProblemBody {
  return (
    typeof body === "object" &&
    body !== null &&
    "detail" in body &&
    typeof body.detail === "string" &&
    "code" in body &&
    typeof body.code === "string"
  );
}

/**
 * A non-2xx response, with FastAPI's `detail` when it sent one. For the P1–P2 routes,
 * `problem` is the typed body: `problem.code` says which failure it was
 * (`card_not_pending`, `escalation_not_open`…), so callers can show it inline.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly problem: ProblemBody | null;
  /** The server's `detail` (or the status text), without the status code. */
  readonly detail: string;

  constructor(status: number, message: string, problem: ProblemBody | null = null, detail: string = message) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
    this.detail = detail;
  }

  /** The problem's `code`, or null for a plain FastAPI error. */
  get code(): string | null {
    return this.problem?.code ?? null;
  }

  static from(response: Response, body: unknown): ApiError {
    const detail =
      typeof body === "object" && body !== null && "detail" in body && typeof body.detail === "string"
        ? body.detail
        : response.statusText || "request failed";
    return new ApiError(response.status, `${response.status} ${detail}`, isProblemBody(body) ? body : null, detail);
  }
}

/** Why a request failed, in a sentence fit for a toast ("World is retired."). */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.detail;
  // fetch rejects (a TypeError) only when the request never got a response.
  return "Harmony didn't respond. Check your connection and try again.";
}

interface ApiResult<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/** The body of a successful response; anything else throws `ApiError`. */
export async function unwrap<T>(request: Promise<ApiResult<T>>): Promise<T> {
  const { data, error, response } = await request;
  if (!response.ok || data === undefined) throw ApiError.from(response, error);
  return data;
}
