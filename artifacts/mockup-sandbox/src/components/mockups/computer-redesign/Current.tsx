import "./_group.css";
import {
  Activity,
  Bot,
  ChevronDown,
  ChevronRight,
  CircleDot,
  Clock3,
  FileCode2,
  Folder,
  GitBranch,
  LayoutGrid,
  MoreHorizontal,
  PanelLeftClose,
  Pencil,
  Plus,
  Search,
  Settings2,
  Sparkles,
  TerminalSquare,
} from "lucide-react";
import { useState } from "react";

type Workspace = { name: string; path: string; unread?: number; chats: string[] };

const workspaces: Workspace[] = [
  { name: "cptr", path: "~/Code/computer", unread: 2, chats: ["Refactor preview shell", "Audit terminal events"] },
  { name: "field-notes", path: "~/Code/field-notes", chats: ["Capture the launch checklist"] },
  { name: "lab-console", path: "~/Code/lab-console", unread: 1, chats: ["Port health dashboard"] },
];

const tabs = [
  { id: "chat", label: "Refactor preview shell", icon: Bot },
  { id: "file", label: "Sidebar.svelte", icon: FileCode2 },
  { id: "terminal", label: "Terminal", icon: TerminalSquare },
];

function WorkspaceRow({
  workspace,
  selected,
  expanded,
  onSelect,
  onToggle,
}: {
  workspace: Workspace;
  selected: boolean;
  expanded: boolean;
  onSelect: () => void;
  onToggle: () => void;
}) {
  return (
    <div className="current-workspace-block">
      <div className={`current-workspace-row ${selected ? "is-selected" : ""}`}>
        <button className="current-chevron" onClick={onToggle} aria-label={`Toggle ${workspace.name} chats`}>
          {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        </button>
        <button className="current-workspace-main" onClick={onSelect} aria-pressed={selected}>
          <Folder size={14} />
          <span className="current-truncate">{workspace.name}</span>
          {workspace.unread ? <span className="current-unread">{workspace.unread}</span> : null}
        </button>
        <button className="current-icon-button current-row-action" aria-label={`Options for ${workspace.name}`}>
          <MoreHorizontal size={13} />
        </button>
        <button className="current-icon-button current-row-action" aria-label={`New chat in ${workspace.name}`}>
          <Pencil size={12} />
        </button>
      </div>
      {selected && expanded ? (
        <div className="current-chat-list">
          {workspace.chats.map((chat, index) => (
            <button key={chat} className={`current-chat-row ${index === 0 ? "is-active" : ""}`}>
              <span className="current-chat-dot">{index === 0 ? <CircleDot size={10} /> : <span />}</span>
              <span className="current-truncate">{chat}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function CurrentTerminal({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <section className={`current-terminal ${open ? "is-open" : "is-closed"}`}>
      <header className="current-terminal-header">
        <div className="current-terminal-title">
          <Activity size={13} />
          <span>Heidi Live Terminal</span>
          <span className="current-terminal-live">LIVE</span>
        </div>
        <div className="current-terminal-actions">
          <button onClick={onToggle}>{open ? "Collapse" : "Expand"}</button>
          <button>Pause</button>
        </div>
      </header>
      {open ? (
        <div className="current-terminal-output">
          <div><span className="current-sequence">001</span><span><b>shell · command started</b><code>pnpm check</code></span></div>
          <div><span className="current-sequence">002</span><span><b>action · read_file</b><code>src/routes/+page.svelte</code></span></div>
          <div><span className="current-sequence">003</span><span><b>agent update · native transcript activity</b><code>Inspecting the workspace layout</code></span></div>
        </div>
      ) : null}
    </section>
  );
}

export function Current() {
  const [activeNav, setActiveNav] = useState("Search");
  const [selectedWorkspace, setSelectedWorkspace] = useState("cptr");
  const [expandedWorkspaces, setExpandedWorkspaces] = useState(new Set(["cptr"]));
  const [activeTab, setActiveTab] = useState("chat");
  const [terminalOpen, setTerminalOpen] = useState(true);
  const [composer, setComposer] = useState("");

  return (
    <div className="computer-redesign-preview current-preview">
      <aside className="current-sidebar">
        <div className="current-brand-row">
          <div className="current-brand"><span className="current-logo">C</span><strong>Computer</strong></div>
          <button className="current-icon-button" aria-label="Collapse sidebar"><PanelLeftClose size={14} /></button>
        </div>
        <nav className="current-nav" aria-label="Computer navigation">
          <button className={activeNav === "Search" ? "is-active" : ""} onClick={() => setActiveNav("Search")}><Search size={14} /><span>Search</span><kbd>⌘K</kbd></button>
          <button className={activeNav === "Automations" ? "is-active" : ""} onClick={() => setActiveNav("Automations")}><Clock3 size={14} /><span>Automations</span></button>
          <button className={activeNav === "FlowDeck" ? "is-active" : ""} onClick={() => setActiveNav("FlowDeck")}><LayoutGrid size={14} /><span>FlowDeck</span></button>
        </nav>
        <div className="current-section-heading"><span>Workspaces</span><button className="current-icon-button" aria-label="Add workspace"><Plus size={14} /></button></div>
        <div className="current-workspace-list">
          {workspaces.map((workspace) => (
            <WorkspaceRow
              key={workspace.name}
              workspace={workspace}
              selected={workspace.name === selectedWorkspace}
              expanded={expandedWorkspaces.has(workspace.name)}
              onSelect={() => setSelectedWorkspace(workspace.name)}
              onToggle={() => setExpandedWorkspaces((previous) => {
                const next = new Set(previous);
                next.has(workspace.name) ? next.delete(workspace.name) : next.add(workspace.name);
                return next;
              })}
            />
          ))}
        </div>
        <div className="current-account"><div className="current-avatar">HD</div><span>Heidi Dang</span><button className="current-icon-button"><Settings2 size={13} /></button></div>
      </aside>
      <main className="current-main">
        <header className="current-topbar">
          <div><span className="current-path">Computer /</span><strong>{selectedWorkspace}</strong></div>
          <div className="current-connection"><span /> connected <span className="current-version">v0.6.4</span></div>
        </header>
        <div className="current-tabs">
          {tabs.map(({ id, label, icon: Icon }) => <button key={id} className={activeTab === id ? "is-active" : ""} onClick={() => setActiveTab(id)}><Icon size={13} /><span>{label}</span>{id !== "chat" ? <span className="current-tab-close">×</span> : null}</button>)}
          <button className="current-icon-button current-add-tab"><Plus size={14} /></button>
        </div>
        <section className="current-chat">
          <div className="current-chat-heading"><div><span className="current-eyebrow">CHAT</span><h1>Refactor preview shell</h1></div><button className="current-icon-button"><MoreHorizontal size={16} /></button></div>
          <div className="current-messages">
            <div className="current-message current-message-user"><div className="current-avatar current-avatar-small">HD</div><div><span className="current-message-meta">Heidi · 10:42</span><p>Can you inspect the current workspace shell and tighten the hierarchy?</p></div></div>
            <div className="current-message current-message-assistant"><div className="current-assistant-mark"><Sparkles size={14} /></div><div><span className="current-message-meta">Computer · working</span><p>I’ll trace the navigation, workspace state, and active terminal affordances before proposing a focused pass.</p><div className="current-tool-card"><GitBranch size={13} /><span>Reading source-grounded layout</span><span className="current-tool-status">done</span></div></div></div>
          </div>
          <CurrentTerminal open={terminalOpen} onToggle={() => setTerminalOpen((value) => !value)} />
          <div className="current-composer-wrap"><textarea value={composer} onChange={(event) => setComposer(event.target.value)} placeholder="Message Computer…" rows={2} /><div className="current-composer-footer"><span>Computer · auto</span><button className="current-send"><Plus size={14} /> Send</button></div></div>
        </section>
      </main>
    </div>
  );
}

export default Current;