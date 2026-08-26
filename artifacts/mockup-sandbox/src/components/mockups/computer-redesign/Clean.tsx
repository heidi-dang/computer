import "./_group.css";
import {
  Activity,
  ArrowUp,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  CircleDot,
  Clock3,
  Code2,
  Command,
  Copy,
  FileCode2,
  Folder,
  GitBranch,
  Globe2,
  LayoutGrid,
  Menu,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Paperclip,
  Pause,
  Pencil,
  Play,
  Plus,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  TerminalSquare,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";

type Workspace = { name: string; location: string; unread?: number; chats: string[]; tone: string };
type Tab = { id: string; label: string; icon: typeof Bot; dirty?: boolean };

const workspaces: Workspace[] = [
  { name: "cptr", location: "~/Code/computer", unread: 2, chats: ["Refactor preview shell", "Audit terminal events"], tone: "sage" },
  { name: "field-notes", location: "~/Code/field-notes", chats: ["Capture the launch checklist"], tone: "coral" },
  { name: "lab-console", location: "~/Code/lab-console", unread: 1, chats: ["Port health dashboard"], tone: "amber" },
];

const tabs: Tab[] = [
  { id: "chat", label: "Refactor preview shell", icon: Bot },
  { id: "file", label: "Sidebar.svelte", icon: FileCode2, dirty: true },
  { id: "terminal", label: "Terminal", icon: TerminalSquare },
];

const terminalEvents = [
  { number: "001", kind: "shell", title: "command started", detail: "pnpm check", tone: "normal" },
  { number: "002", kind: "file", title: "read_file", detail: "src/routes/+page.svelte", tone: "normal" },
  { number: "003", kind: "agent", title: "native transcript activity", detail: "Tracing workspace state and tab affordances", tone: "agent" },
  { number: "004", kind: "shell", title: "output", detail: "No blocking type errors found", tone: "success" },
];

function TinyLogo() {
  return <span className="clean-logo" aria-hidden="true"><span>C</span></span>;
}

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
    <div className={`clean-workspace-block ${selected ? "is-selected" : ""}`}>
      <div className="clean-workspace-row">
        <button className="clean-workspace-main" onClick={onSelect} aria-pressed={selected}>
          <span className={`clean-workspace-mark ${workspace.tone}`}><Folder size={14} /></span>
          <span className="clean-truncate">{workspace.name}</span>
          {workspace.unread ? <span className="clean-unread">{workspace.unread}</span> : null}
        </button>
        <button className="clean-chevron" onClick={(event) => { event.stopPropagation(); onToggle(); }} aria-label={`Toggle ${workspace.name} chats`} aria-expanded={expanded}>
          {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        </button>
        <button className="clean-row-action" aria-label={`New chat in ${workspace.name}`}><Pencil size={12} /></button>
        <button className="clean-row-action" aria-label={`More options for ${workspace.name}`}><MoreHorizontal size={13} /></button>
      </div>
      {selected && expanded ? (
        <div className="clean-chat-list">
          {workspace.chats.map((chat, index) => (
            <button key={chat} className={`clean-chat-row ${index === 0 ? "is-active" : ""}`}>
              <span className="clean-chat-line">{index === 0 ? <CircleDot size={10} /> : <span className="clean-chat-empty" />}</span>
              <span className="clean-truncate">{chat}</span>
            </button>
          ))}
          <button className="clean-show-more">Show more chats <ChevronRight size={11} /></button>
        </div>
      ) : null}
    </div>
  );
}

function LiveTerminal({
  open,
  follow,
  auditOpen,
  onToggle,
  onFollow,
  onAudit,
}: {
  open: boolean;
  follow: boolean;
  auditOpen: boolean;
  onToggle: () => void;
  onFollow: () => void;
  onAudit: () => void;
}) {
  return (
    <section className={`clean-terminal ${open ? "is-open" : "is-closed"}`} aria-label="Heidi live terminal">
      <header className="clean-terminal-header">
        <div className="clean-terminal-heading">
          <span className="clean-terminal-pulse" />
          <div><strong>Heidi Live Terminal</strong><span>active run · 8f2a0d11</span></div>
        </div>
        <div className="clean-terminal-actions">
          <span className="clean-live-label">LIVE</span>
          <button className={auditOpen ? "is-selected" : ""} onClick={onAudit} aria-pressed={auditOpen} aria-label="Toggle audit report"><ShieldCheck size={14} /></button>
          <button className={follow ? "is-selected" : ""} onClick={onFollow} aria-pressed={follow}>{follow ? <Pause size={13} /> : <Play size={13} />}<span className="clean-terminal-button-label">{follow ? "Following" : "Follow"}</span></button>
          <button onClick={onToggle} aria-expanded={open}>{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}<span className="clean-terminal-button-label">{open ? "Collapse" : "Expand"}</span></button>
        </div>
      </header>
      {open ? (
        <>
          <div className="clean-terminal-output" role="log" aria-live="polite">
            {terminalEvents.map((event) => (
              <div className={`clean-terminal-line ${event.tone}`} key={event.number}>
                <span className="clean-terminal-sequence">{event.number}</span>
                <span className="clean-terminal-kind">{event.kind}</span>
                <span className="clean-terminal-event">{event.title}</span>
                <code>{event.detail}</code>
              </div>
            ))}
          </div>
          {auditOpen ? <div className="clean-audit"><div><ShieldCheck size={14} /><strong>Audit trail</strong><span>4 verified events</span></div><button onClick={onAudit} aria-label="Close audit report"><X size={13} /></button></div> : null}
        </>
      ) : null}
    </section>
  );
}

export function Clean() {
  const [sidebarOpen, setSidebarOpen] = useState(() => typeof window === "undefined" ? true : window.innerWidth > 640);
  const [activeNav, setActiveNav] = useState("Search");
  const [selectedWorkspace, setSelectedWorkspace] = useState("cptr");
  const [expandedWorkspaces, setExpandedWorkspaces] = useState(new Set(["cptr"]));
  const [activeTab, setActiveTab] = useState("chat");
  const [composerFocused, setComposerFocused] = useState(false);
  const [composer, setComposer] = useState("");
  const [terminalOpen, setTerminalOpen] = useState(true);
  const [terminalFollow, setTerminalFollow] = useState(true);
  const [auditOpen, setAuditOpen] = useState(false);
  const [sent, setSent] = useState(false);

  const currentWorkspace = useMemo(
    () => workspaces.find((workspace) => workspace.name === selectedWorkspace) ?? workspaces[0],
    [selectedWorkspace],
  );

  const selectWorkspace = (name: string) => {
    setSelectedWorkspace(name);
    setActiveTab("chat");
  };

  const toggleWorkspace = (name: string) => {
    setExpandedWorkspaces((previous) => {
      const next = new Set(previous);
      next.has(name) ? next.delete(name) : next.add(name);
      return next;
    });
  };

  const submitComposer = () => {
    if (!composer.trim()) return;
    setSent(true);
    setComposer("");
  };

  return (
    <div className="computer-redesign-preview clean-preview">
      <div className="clean-app-shell">
        {sidebarOpen ? <aside className="clean-sidebar">
          <div className="clean-sidebar-top">
            <div className="clean-brand"><TinyLogo /><span>Computer</span><span className="clean-brand-dot" /></div>
            <button className="clean-icon-button" onClick={() => setSidebarOpen(false)} aria-label="Collapse sidebar"><PanelLeftClose size={15} /></button>
          </div>
          <div className="clean-search-box"><Search size={14} /><span>Search anything</span><kbd><Command size={11} />K</kbd></div>
          <nav className="clean-nav" aria-label="Computer navigation">
            {[
              { label: "Search", icon: Search },
              { label: "Automations", icon: Clock3 },
              { label: "FlowDeck", icon: LayoutGrid },
            ].map(({ label, icon: Icon }) => (
              <button key={label} className={activeNav === label ? "is-active" : ""} onClick={() => setActiveNav(label)}>
                <Icon size={15} /><span>{label}</span>{label === "FlowDeck" ? <span className="clean-nav-status"><span />2</span> : null}
              </button>
            ))}
          </nav>
          <div className="clean-workspaces-heading"><div><span className="clean-overline">Workspace</span><span className="clean-count">03</span></div><button className="clean-icon-button" aria-label="Add workspace"><Plus size={15} /></button></div>
          <div className="clean-workspaces-list">
            {workspaces.map((workspace) => <WorkspaceRow key={workspace.name} workspace={workspace} selected={workspace.name === selectedWorkspace} expanded={expandedWorkspaces.has(workspace.name)} onSelect={() => selectWorkspace(workspace.name)} onToggle={() => toggleWorkspace(workspace.name)} />)}
          </div>
          <div className="clean-sidebar-footer">
            <button className="clean-profile"><span className="clean-avatar">HD</span><span><strong>Heidi Dang</strong><small>Personal workspace</small></span><MoreHorizontal size={15} /></button>
            <div className="clean-footer-links"><button><Settings2 size={13} />Settings</button><button><Activity size={13} />System info</button></div>
          </div>
        </aside> : <button className="clean-sidebar-restore" onClick={() => setSidebarOpen(true)} aria-label="Expand sidebar"><PanelLeftOpen size={16} /></button>}

        <main className="clean-main">
          <header className="clean-topbar">
            <div className="clean-breadcrumb"><span>Computer</span><ChevronRight size={13} /><strong>{currentWorkspace.name}</strong><span className="clean-slash">/</span><span>{currentWorkspace.location.replace("~/Code/", "")}</span></div>
            <div className="clean-topbar-actions"><span className="clean-status-chip"><span />Workspace synced</span><button className="clean-topbar-icon" aria-label="Open command menu"><Command size={15} /></button><button className="clean-avatar clean-avatar-top">HD</button></div>
          </header>
          <div className="clean-tab-strip">
            <div className="clean-tabs">
              {tabs.map(({ id, label, icon: Icon, dirty }) => <button key={id} className={`clean-tab ${activeTab === id ? "is-active" : ""}`} onClick={() => setActiveTab(id)}><Icon size={14} /><span>{label}</span>{dirty ? <i /> : null}{id !== "chat" ? <X size={12} className="clean-tab-close" /> : null}</button>)}
              <button className="clean-tab-add" aria-label="Open new tab"><Plus size={15} /></button>
            </div>
            <div className="clean-tab-tools"><button aria-label="Open browser preview"><Globe2 size={14} /></button><button aria-label="Split view"><Code2 size={14} /></button><span /></div>
          </div>

          <section className="clean-workspace-view">
            <div className="clean-chat-column">
              <div className="clean-chat-header">
                <div><span className="clean-overline">Conversation · cptr</span><h1>Refactor preview shell</h1><p>Focused on hierarchy, source fidelity, and a calmer run surface.</p></div>
                <button className="clean-round-button" aria-label="Chat options"><MoreHorizontal size={16} /></button>
              </div>
              <div className="clean-transcript">
                <div className="clean-message clean-message-user">
                  <span className="clean-avatar clean-message-avatar">HD</span>
                  <div className="clean-message-body"><div className="clean-message-meta"><strong>Heidi</strong><span>10:42</span></div><p>Can you inspect the current workspace shell and tighten the hierarchy?</p></div>
                </div>
                <div className="clean-message clean-message-assistant">
                  <span className="clean-assistant-mark"><Sparkles size={15} /></span>
                  <div className="clean-message-body"><div className="clean-message-meta"><strong>Computer</strong><span className="clean-working"><span />working</span></div><p>I’m mapping the parts you already rely on: workspaces, tabs, the chat thread, and Heidi’s live terminal. The redesign keeps them close, but gives each a clear resting place.</p>
                    <div className="clean-work-card"><div className="clean-work-card-icon"><GitBranch size={14} /></div><div><strong>Reading source-grounded layout</strong><span>+page.svelte · Sidebar.svelte · LiveTerminal.svelte</span></div><span className="clean-check"><Check size={13} /></span></div>
                  </div>
                </div>
                {sent ? <div className="clean-message clean-message-user clean-message-new"><span className="clean-avatar clean-message-avatar">HD</span><div className="clean-message-body"><div className="clean-message-meta"><strong>Heidi</strong><span>now</span></div><p>Keep the run details visible, but let the conversation breathe.</p></div></div> : null}
              </div>
              <LiveTerminal open={terminalOpen} follow={terminalFollow} auditOpen={auditOpen} onToggle={() => setTerminalOpen((value) => !value)} onFollow={() => setTerminalFollow((value) => !value)} onAudit={() => setAuditOpen((value) => !value)} />
              <div className={`clean-composer ${composerFocused ? "is-focused" : ""}`}>
                <textarea value={composer} onChange={(event) => setComposer(event.target.value)} onFocus={() => setComposerFocused(true)} onBlur={() => setComposerFocused(false)} onKeyDown={(event) => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) submitComposer(); }} placeholder="Ask Computer to inspect, change, or explain…" rows={2} aria-label="Message Computer" />
                <div className="clean-composer-bar"><div className="clean-composer-tools"><button aria-label="Attach file"><Paperclip size={15} /></button><button className="clean-agent-pill" aria-label="Current agent"><Bot size={13} />Computer<ChevronDown size={12} /></button><span className="clean-composer-hint">⌘ ↵ to send</span></div><button className="clean-send" onClick={submitComposer} aria-label="Send message"><ArrowUp size={15} /></button></div>
              </div>
            </div>
            <aside className="clean-context-rail">
              <div className="clean-rail-label"><span className="clean-overline">Session</span><button aria-label="Copy session id"><Copy size={13} /></button></div>
              <div className="clean-session-id">run_8f2a0d11</div>
              <div className="clean-rail-divider" />
              <div className="clean-rail-stat"><span><TerminalSquare size={14} />Terminal</span><strong>active</strong></div>
              <div className="clean-rail-stat"><span><GitBranch size={14} />Branch</span><strong>main</strong></div>
              <div className="clean-rail-stat"><span><FileCode2 size={14} />Files touched</span><strong>03</strong></div>
              <div className="clean-rail-divider" />
              <div className="clean-rail-label"><span className="clean-overline">Tools in use</span><span className="clean-tool-count">4</span></div>
              <div className="clean-tool-list"><span><Search size={13} />read_file</span><span><Code2 size={13} />inspect_tree</span><span><TerminalSquare size={13} />shell</span><span><ShieldCheck size={13} />audit</span></div>
              <div className="clean-rail-note"><ShieldCheck size={14} /><span><strong>Safe mode</strong><small>Changes need your approval</small></span></div>
            </aside>
          </section>
          <footer className="clean-statusbar"><span><span className="clean-status-dot" />Connected to Computer runtime</span><span className="clean-status-branch"><GitBranch size={12} />main</span><span>UTF-8</span><span>Ln 118, Col 24</span></footer>
        </main>
      </div>
    </div>
  );
}

export default Clean;