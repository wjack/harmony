import type { ComponentProps } from "react";
import ReactMarkdown, { type Components, type ExtraProps } from "react-markdown";
import remarkGfm from "remark-gfm";

import { cx } from "../shell/ui/cx";

import "./chat.css";

const REMARK_PLUGINS = [remarkGfm];

/** react-markdown hands each component its syntax-tree `node`, which isn't a DOM attribute. */
function domProps<T extends ExtraProps>(props: T): Omit<T, "node"> {
  const rest = { ...props };
  delete rest.node;
  return rest;
}

function ExternalLink(props: ComponentProps<"a"> & ExtraProps) {
  return <a {...domProps(props)} target="_blank" rel="noreferrer noopener" />;
}

/**
 * Images never load. The text is written by a model, and a remote image fetches its URL as soon as it renders, which would carry whatever the
 * URL holds out of the member's browser without anyone choosing to send it.
 */
function ImageText({ alt }: ComponentProps<"img"> & ExtraProps) {
  return alt === undefined || alt === "" ? null : <span className="text-muted">[{alt}]</span>;
}

function ScrollingTable(props: ComponentProps<"table"> & ExtraProps) {
  return (
    <div className="scrollbar-quiet overflow-x-auto">
      <table {...domProps(props)} />
    </div>
  );
}

const COMPONENTS: Components = { a: ExternalLink, img: ImageText, table: ScrollingTable };

interface MarkdownProps {
  children: string;
  className?: string;
}

/** Claude's reply body: GitHub-flavored markdown, serif. Raw HTML and images are never rendered. */
export function Markdown({ children, className }: MarkdownProps) {
  return (
    <div className={cx("chat-prose", className)}>
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={COMPONENTS}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
