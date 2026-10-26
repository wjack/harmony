import {
  Check,
  ChevronRight,
  CircleAlert,
  Copy,
  FileText,
  LoaderCircle,
  Radar,
  RotateCcw,
  Search,
  Send,
  Sparkles,
  ThumbsDown,
  ThumbsUp,
  Waypoints,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";

import { IconButton } from "../shell/ui/Button";
import { cx } from "../shell/ui/cx";
import { Spark } from "../shell/ui/Spark";
import { summarize } from "./activity";
import type { ActivityItem } from "./items";
import { Markdown } from "./Markdown";

import "./chat.css";

export function UserMessage({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[min(85%,560px)] rounded-xl bg-bubble px-4 py-[11px] text-[16px] leading-6 break-words whitespace-pre-wrap text-ink">
        {text}
      </div>
    </div>
  );
}

const ACTION_ICON = { size: 16, strokeWidth: 1.6 } as const;

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <IconButton
      label={copied ? "Copied" : "Copy"}
      size="sm"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => {
            setCopied(false);
          }, 1500);
        });
      }}
    >
      {copied ? <Check {...ACTION_ICON} /> : <Copy {...ACTION_ICON} />}
    </IconButton>
  );
}

/** Copy, thumbs up, thumbs down, retry: under a finished reply. */
export function ReplyActions({
  text,
  alwaysVisible,
  onUnavailable,
}: {
  text: string;
  alwaysVisible: boolean;
  onUnavailable: (feature: string) => void;
}) {
  return (
    <div
      className={cx(
        "-ml-1.5 flex items-center gap-0.5 pt-2 transition-opacity",
        !alwaysVisible && "opacity-0 group-hover/reply:opacity-100 focus-within:opacity-100",
      )}
    >
      <CopyButton text={text} />
      <IconButton
        label="Give positive feedback"
        size="sm"
        onClick={() => {
          onUnavailable("Feedback");
        }}
      >
        <ThumbsUp {...ACTION_ICON} />
      </IconButton>
      <IconButton
        label="Give negative feedback"
        size="sm"
        onClick={() => {
          onUnavailable("Feedback");
        }}
      >
        <ThumbsDown {...ACTION_ICON} />
      </IconButton>
      <IconButton
        label="Retry"
        size="sm"
        onClick={() => {
          onUnavailable("Retry");
        }}
      >
        <RotateCcw {...ACTION_ICON} />
      </IconButton>
    </div>
  );
}

export function AssistantMessage({
  markdown,
  actions,
  replyText,
  latest,
  onUnavailable,
}: {
  markdown: string;
  /** The reply ends here: show its action row. */
  actions: boolean;
  /** What Copy copies: the whole reply, not just this block. */
  replyText: string;
  latest: boolean;
  onUnavailable: (feature: string) => void;
}) {
  return (
    <div className="group/reply">
      <Markdown>{markdown}</Markdown>
      {actions && <ReplyActions text={replyText} alwaysVisible={latest} onUnavailable={onUnavailable} />}
    </div>
  );
}

const TOOL_ICONS: Readonly<Record<string, LucideIcon>> = {
  search_network: Radar,
  send_network_request: Send,
  get_request_status: Waypoints,
  search_org_knowledge: Search,
  read_document: FileText,
};

function ActivityStep({ item }: { item: ActivityItem }) {
  if (item.type === "thinking_summary") {
    return (
      <li className="flex gap-2.5">
        <Sparkles size={14} strokeWidth={1.6} className="mt-[3px] shrink-0 text-faint" />
        <p className="text-[13.5px] leading-[1.55] whitespace-pre-wrap text-muted">{item.text}</p>
      </li>
    );
  }
  const Icon = TOOL_ICONS[item.tool] ?? Search;
  return (
    <li className="flex gap-2.5">
      <Icon size={14} strokeWidth={1.6} className="mt-[3px] shrink-0 text-faint" />
      <p className="min-w-0 text-[13.5px] leading-[1.55] text-muted">
        <span className="text-secondary">{item.label}</span>
        {item.detail !== null && item.detail !== "" && <span> · {item.detail}</span>}
        {item.status === "running" && (
          <LoaderCircle size={12} strokeWidth={2} className="ml-1.5 inline animate-spin align-[-1px]" />
        )}
        {item.status === "error" && <span className="ml-1.5 text-danger">failed</span>}
      </p>
    </li>
  );
}

/**
 * "Searched the Harmony network · 14 organizations ›", expandable into the steps.
 * `reply` is the text right after the line, which it shouldn't repeat.
 */
export function ActivityLine({ items, reply = null }: { items: readonly ActivityItem[]; reply?: string | null }) {
  const [open, setOpen] = useState(false);
  const summary = summarize(items, reply);
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => {
          setOpen((value) => !value);
        }}
        className="group/activity -mx-1 inline-flex max-w-full items-center gap-1 rounded-md px-1 py-0.5 text-left text-[14px] leading-6 text-muted transition-colors hover:text-secondary"
      >
        {summary.failed && <CircleAlert size={14} strokeWidth={1.8} className="shrink-0 text-danger" />}
        <span className={cx("truncate", summary.running && "chat-shimmer")}>{summary.text}</span>
        <ChevronRight
          size={14}
          strokeWidth={1.8}
          className={cx("shrink-0 transition-transform duration-150", open && "rotate-90")}
        />
      </button>
      {open && (
        <ol className="mt-1.5 mb-1 ml-[3px] flex flex-col gap-2 border-l border-border py-1 pl-4 animate-fade-in">
          {items.map((item) => (
            <ActivityStep key={item.id} item={item} />
          ))}
        </ol>
      )}
    </div>
  );
}

/** Claude's streaming indicator: the spark alone on a line, turning. */
export function StreamingSpark() {
  return (
    <div className="pt-1" role="status" aria-label="Claude is responding">
      <Spark size={30} animated />
    </div>
  );
}
