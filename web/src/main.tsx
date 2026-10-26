import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter } from "react-router";
import { RouterProvider } from "react-router/dom";

import "./index.css";

import { createApiClient } from "./api/client";
import { createQueryClient } from "./api/queryClient";
import { AppProviders } from "./app/AppProviders";
import { routes } from "./app/routes";
import { fetchStreamSnapshot } from "./app/snapshot";
import { RealtimeDispatcher } from "./realtime/dispatcher";

const container = document.getElementById("root");
if (container === null) throw new Error("index.html has no #root element");

// /__gallery (development only) renders over fixtures: no router, session or stream.
if (import.meta.env.DEV && window.location.pathname.startsWith("/__gallery")) {
  void import("./gallery/GalleryApp").then(({ GalleryApp }) => {
    createRoot(container).render(
      <StrictMode>
        <GalleryApp />
      </StrictMode>,
    );
  });
} else {
  startApp(container);
}

function startApp(root: HTMLElement): void {
  const api = createApiClient();
  const queryClient = createQueryClient();
  // One dispatcher, and so one EventSource, per tab. It lives outside React so that
  // StrictMode's double-mounted effects can't open a second stream.
  const dispatcher = new RealtimeDispatcher({
    queryClient,
    snapshot: () => fetchStreamSnapshot(api, queryClient),
  });
  const router = createBrowserRouter(routes);

  createRoot(root).render(
    <StrictMode>
      <AppProviders api={api} queryClient={queryClient} dispatcher={dispatcher}>
        <RouterProvider router={router} />
      </AppProviders>
    </StrictMode>,
  );

  void dispatcher.start();
}
