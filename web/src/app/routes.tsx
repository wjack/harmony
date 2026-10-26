import { Navigate, type RouteObject } from "react-router";

import { AppLayout } from "../shell/AppLayout";
import { ChatsPage } from "../shell/ChatsPage";
import { NotFound } from "../shell/NotFound";
import { OpenEscalation } from "../shell/OpenEscalation";
import { RootLayout } from "../shell/RootLayout";
import { RouteError } from "../shell/RouteError";

/**
 * The SPA's routes (ARCHITECTURE.md §12). The shell owns the frame and mounts each
 * area's pages; areas load on demand, so a visit to one doesn't download the others.
 *
 *   /                     New chat (chat)
 *   /chat/:id             a conversation (chat)
 *   /chats                chat history (shell)
 *   /requests[/:threadId] the Requests inbox (requests); `?escalation=<id>` opens an
 *                         item's thread, for the demo's needs-human chips
 *   /customize/harmony    Harmony settings (customize)
 *   /network              the network view (network)
 *
 * /__gallery, the component gallery, isn't a route: main.tsx renders it instead of
 * this router in development builds, so each section can run a router of its own.
 */
export const routes: RouteObject[] = [
  {
    element: <RootLayout />,
    children: [
      {
        element: <AppLayout />,
        errorElement: <RouteError />,
        children: [
          {
            errorElement: <RouteError />,
            children: [
              { index: true, lazy: async () => ({ Component: (await import("../chat")).NewChatPage }) },
              { path: "chat/:id", lazy: async () => ({ Component: (await import("../chat")).ChatPage }) },
              { path: "chats", element: <ChatsPage /> },
              {
                path: "requests",
                lazy: async () => {
                  const { RequestsPage } = await import("../requests");
                  return {
                    element: (
                      <OpenEscalation>
                        <RequestsPage />
                      </OpenEscalation>
                    ),
                  };
                },
              },
              {
                path: "requests/:threadId",
                lazy: async () => ({ Component: (await import("../requests")).RequestsPage }),
              },
              { path: "customize", element: <Navigate to="/customize/harmony" replace /> },
              {
                path: "customize/harmony",
                lazy: async () => ({ Component: (await import("../customize")).HarmonySettingsPage }),
              },
              { path: "network", lazy: async () => ({ Component: (await import("../network")).NetworkPage }) },
              { path: "*", element: <NotFound /> },
            ],
          },
        ],
      },
    ],
  },
];
