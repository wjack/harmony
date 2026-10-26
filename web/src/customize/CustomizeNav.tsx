import { ArrowLeft, Blocks, Handshake, NotebookText, type LucideIcon } from "lucide-react";
import { useNavigate } from "react-router";

import { cx, IconButton } from "../shell/ui";
import { useUnavailable } from "../shell/unavailable";

function NavItem({
  label,
  Icon,
  selected = false,
  onClick,
}: {
  label: string;
  Icon: LucideIcon;
  selected?: boolean;
  onClick?: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        aria-current={selected ? "page" : undefined}
        onClick={onClick}
        className={cx(
          "flex h-8 w-full items-center gap-2.5 rounded-control px-2 text-sm leading-5 text-ink transition-colors duration-100",
          selected ? "bg-hover font-medium" : "hover:bg-hover",
        )}
      >
        <Icon size={16} strokeWidth={1.5} className="shrink-0 text-secondary" aria-hidden />
        <span className="truncate">{label}</span>
      </button>
    </li>
  );
}

/** Customize's second-level sidebar: back, Skills, Connectors and Harmony (UI_REFERENCE.md §6). */
export function CustomizeNav() {
  const navigate = useNavigate();
  const unavailable = useUnavailable();
  return (
    <nav aria-label="Customize" className="hidden w-[244px] shrink-0 flex-col border-r border-border px-2 pt-2.5 lg:flex">
      <div className="flex h-8 items-center gap-1.5">
        <IconButton
          label="Back"
          size="sm"
          onClick={() => {
            void navigate("/");
          }}
        >
          <ArrowLeft size={16} strokeWidth={1.75} />
        </IconButton>
        <span className="text-[15px] leading-5 font-semibold text-ink">Customize</span>
      </div>
      <ul className="mt-3 flex flex-col gap-px">
        <NavItem
          label="Skills"
          Icon={NotebookText}
          onClick={() => {
            unavailable("Skills");
          }}
        />
        <NavItem
          label="Connectors"
          Icon={Blocks}
          onClick={() => {
            unavailable("Connectors");
          }}
        />
        <NavItem label="Harmony" Icon={Handshake} selected />
      </ul>
    </nav>
  );
}
