import { Link } from "react-router";

import { Spark } from "./ui";

export function NotFound() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-6 pb-[12vh] text-center">
      <Spark size={30} />
      <h1 className="font-serif text-[28px] leading-9 tracking-[-0.015em] text-ink">Nothing here</h1>
      <p className="text-sm text-muted">This page doesn&apos;t exist in the Harmony prototype.</p>
      <Link to="/" className="mt-1 text-sm font-medium text-ink underline decoration-border-strong underline-offset-4 hover:decoration-ink">
        Start a new chat
      </Link>
    </div>
  );
}
