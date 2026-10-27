import createClient, { type Client, type Middleware } from "openapi-fetch";

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

/** The `type` of each error in FastAPI's 422 body (`string_too_long`…). */
function validationTypes(body: unknown): string[] {
  if (typeof body !== "object" || body === null || !("detail" in body) || !Array.isArray(body.detail)) return [];
  return body.detail.flatMap((error: unknown) =>
    typeof error === "object" && error !== null && "type" in error && typeof error.type === "string" ? [error.type] : [],
  );
}

/**
 * A non-2xx response, with FastAPI's `detail` when it sent one. For the P1–P2 routes,
 * `problem` is the typed body: `problem.code` says which failure it was
 * (`card_not_pending`, `escalation_not_open`…), so callers can show it inline. A 422
 * from FastAPI's own validation carries its error types in `invalid` instead.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly problem: ProblemBody | null;
  /** The server's `detail` (or the status text), without the status code. */
  readonly detail: string;
  /** Why FastAPI rejected the input (`string_too_long`…); empty for any other failure. */
  readonly invalid: readonly string[];

  constructor(
    status: number,
    message: string,
    problem: ProblemBody | null = null,
    detail: string = message,
    invalid: readonly string[] = [],
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
    this.detail = detail;
    this.invalid = invalid;
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
    const problem = isProblemBody(body) ? body : null;
    return new ApiError(response.status, `${response.status} ${detail}`, problem, detail, validationTypes(body));
  }
}

/**
 * The passcode gate refused the request: the visitor never unlocked, locked Harmony in
 * another tab, or the passcode was rotated. Nothing will succeed until they unlock again.
 */
export class PasscodeRequired extends Error {
  constructor() {
    super("passcode required");
    this.name = "PasscodeRequired";
  }
}

/** The gate's 401 (`passcode_required`), as opposed to the API's own (`no_session`…). */
export async function isPasscodeRequired(response: Response): Promise<boolean> {
  if (response.status !== 401) return false;
  try {
    const body: unknown = await response.clone().json();
    return isProblemBody(body) && body.code === "passcode_required";
  } catch {
    return false;
  }
}

/** Calls `lock` on any gate 401. Every request goes through it, mutations included. */
export function lockOnPasscodeRequired(lock: () => void): Middleware {
  return {
    async onResponse({ response }) {
      if (await isPasscodeRequired(response)) lock();
      return undefined;
    },
  };
}

/** The server's passcode page, sending the visitor back to `next` once they unlock. */
export function gatePath(next: string): string {
  return `/api/gate?next=${encodeURIComponent(next)}`;
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
