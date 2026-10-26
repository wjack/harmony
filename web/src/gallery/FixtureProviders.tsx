import { useEffect, useState, type ReactNode } from "react";

import { AppProviders } from "../app/AppProviders";
import type { FixtureOptions, FixtureRoutes } from "../fixtures";
import { FixtureEmitContext } from "./fixtureContext";
import { createFixtureServices, type FixtureServices } from "./fixtureServices";

interface FixtureProvidersProps {
  routes: FixtureRoutes;
  options?: FixtureOptions;
  /** Receives the services, e.g. to `emit` a demo event from a gallery button. */
  onReady?: (services: FixtureServices) => void;
  children: ReactNode;
}

/**
 * Wraps a component in the app's providers over fixture routes, with its own cache
 * and stream. Each gallery section gets one, so sections never share state.
 */
export function FixtureProviders({ routes, options, onReady, children }: FixtureProvidersProps) {
  // Built once per mount: the routes are the section's starting state.
  const [services] = useState(() => createFixtureServices(routes, options));

  useEffect(() => {
    void services.dispatcher.start();
    onReady?.(services);
    return () => {
      services.dispatcher.stop();
    };
  }, [services, onReady]);

  return (
    <AppProviders api={services.api} queryClient={services.queryClient} dispatcher={services.dispatcher}>
      <FixtureEmitContext value={services.emit}>{children}</FixtureEmitContext>
    </AppProviders>
  );
}
