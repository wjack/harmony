import { isRouteErrorResponse, useRouteError } from "react-router";

import { Button, Spark } from "./ui";

/** A page that failed to load or render: shown in place of the page, inside the shell. */
export function RouteError() {
  const error = useRouteError();
  const detail = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : error instanceof Error
      ? error.message
      : null;
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-6 pb-[12vh] text-center">
      <Spark size={28} />
      <h1 className="font-serif text-[26px] leading-9 tracking-[-0.015em] text-ink">Something went wrong</h1>
      <p className="max-w-[460px] text-sm leading-6 text-muted">
        This page hit an error. Reloading usually fixes it; your chats and requests are safe.
      </p>
      {detail !== null && (
        <pre className="max-w-[560px] overflow-x-auto rounded-control bg-bubble px-3 py-2 font-mono text-xs text-secondary">
          {detail}
        </pre>
      )}
      <Button
        variant="primary"
        className="mt-1"
        onClick={() => {
          window.location.reload();
        }}
      >
        Reload
      </Button>
    </div>
  );
}
