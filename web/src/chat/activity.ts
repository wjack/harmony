/**
 * The one-line summary of a turn's tool use and thinking, Claude style: one step reads
 * as its label and outcome ("Searched the Harmony network · 2 possible fits of 14 ›");
 * several name what was searched ("Searched the network for 2 needs and your records")
 * and then the other steps, a repeated one named once with its count. The tools lead,
 * since they are what Claude did on the network; a step with none says what it
 * thought, unless the reply after it opens with the same words.
 */
import type { ActivityItem } from "./items";
import type { ToolActivityItem } from "./types";

export interface ActivitySummary {
  text: string;
  running: boolean;
  failed: boolean;
}

const MAX_JOINED = 2;

/** The searches the summary names together ("Searched the network … and your records"). */
const SEARCHES: ReadonlySet<string> = new Set(["search_network", "search_org_knowledge"]);

function toolLine(item: ToolActivityItem): string {
  return item.outcome === null || item.outcome === "" ? item.label : `${item.label} · ${item.outcome}`;
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

/**
 * "Searched the network for 2 needs and your records": the network searches, by what
 * they looked for ("twice" when it was the same need again), with the organization's
 * own records when Claude searched those too. Null without a network search.
 */
function searchPhrase(tools: readonly ToolActivityItem[]): string | null {
  const network = tools.filter((tool) => tool.tool === "search_network");
  if (network.length === 0) return null;
  const needs = new Set(network.map((tool) => tool.detail?.trim() ?? ""));
  const distinct = needs.size === network.length && !needs.has("");
  const count = network.length > 1 && distinct ? ` for ${String(network.length)} needs` : times(network.length);
  const records = tools.some((tool) => tool.tool === "search_org_knowledge") ? " and your records" : "";
  return `Searched the network${count}${records}`;
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

  const search = searchPhrase(tools);
  const others = search === null ? tools : tools.filter((tool) => !SEARCHES.has(tool.tool));
  const phrases: Step[] = [
    ...(search === null ? [] : [{ label: search, count: 1 }]),
    ...steps(others).map((step) => ({ label: `${step.label}${times(step.count)}`, count: step.count })),
  ];
  const shown = phrases
    .slice(0, MAX_JOINED)
    .map((phrase, index) => (index === 0 ? phrase.label : lowerFirst(phrase.label)))
    .join(", ");
  const more = phrases.slice(MAX_JOINED).reduce((sum, phrase) => sum + phrase.count, 0);
  return {
    text: more > 0 ? `${shown} and ${String(more)} more ${more === 1 ? "step" : "steps"}` : shown,
    running: false,
    failed,
  };
}
