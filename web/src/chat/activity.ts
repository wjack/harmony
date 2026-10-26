/**
 * The one-line summary of a turn's tool use and thinking, Claude style: "Searched the
 * Harmony network · 14 organizations ›" for one step, "Searched the Harmony network
 * twice, checked request status ›" for several, a repeated step named once with its
 * count. The tools lead, since they are what Claude did on the network; a step with
 * none says what it thought, unless the reply after it opens with the same words.
 */
import type { ActivityItem } from "./items";
import type { ToolActivityItem } from "./types";

export interface ActivitySummary {
  text: string;
  running: boolean;
  failed: boolean;
}

const MAX_JOINED = 2;

function toolLine(item: ToolActivityItem): string {
  return item.detail === null || item.detail === "" ? item.label : `${item.label} · ${item.detail}`;
}

interface Step {
  label: string;
  count: number;
}

/** The tools' labels in the order first used, each with how many times it ran. */
function steps(tools: readonly ToolActivityItem[]): Step[] {
  const byLabel = new Map<string, Step>();
  for (const tool of tools) {
    const step = byLabel.get(tool.label);
    if (step === undefined) byLabel.set(tool.label, { label: tool.label, count: 1 });
    else step.count += 1;
  }
  return [...byLabel.values()];
}

function times(count: number): string {
  if (count === 1) return "";
  return count === 2 ? " twice" : ` ${String(count)} times`;
}

function lowerFirst(text: string): string {
  return text.length > 0 ? `${text.charAt(0).toLowerCase()}${text.slice(1)}` : text;
}

function firstSentence(text: string): string {
  const line = text.trim().split("\n")[0] ?? "";
  const match = /^(.+?[.!?])(\s|$)/.exec(line);
  return (match?.[1] ?? line).replace(/\.$/, "");
}

/** `reply`: the text that follows the line, if any. */
export function summarize(items: readonly ActivityItem[], reply: string | null = null): ActivitySummary {
  const tools = items.filter((item): item is ToolActivityItem => item.type === "tool_activity");
  const running = tools.find((tool) => tool.status === "running");
  const failed = tools.some((tool) => tool.status === "error");
  if (running !== undefined) return { text: running.label, running: true, failed };

  const [first, ...rest] = tools;
  if (first === undefined) {
    const thinking = items.findLast((item) => item.type === "thinking_summary");
    const thought = thinking === undefined ? "" : firstSentence(thinking.text);
    const repeated = thought === "" || reply?.trimStart().startsWith(thought) === true;
    return { text: repeated ? "Thought it through" : thought, running: false, failed };
  }
  if (rest.length === 0) return { text: toolLine(first), running: false, failed };

  const grouped = steps(tools);
  const shown = grouped
    .slice(0, MAX_JOINED)
    .map((step, index) => `${index === 0 ? step.label : lowerFirst(step.label)}${times(step.count)}`)
    .join(", ");
  const more = grouped.slice(MAX_JOINED).reduce((sum, step) => sum + step.count, 0);
  return {
    text: more > 0 ? `${shown} and ${more} more ${more === 1 ? "step" : "steps"}` : shown,
    running: false,
    failed,
  };
}
