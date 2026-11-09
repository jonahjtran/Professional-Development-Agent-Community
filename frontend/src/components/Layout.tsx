import { useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { useAttention } from "../hooks";

const SETTINGS = [
  { to: "/models", label: "Models" },
  { to: "/rules", label: "Rules" },
];

/** The profile button and its dropdown. There is no account here, so there is no sign-out. */
function ProfileMenu() {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const { pathname } = useLocation();
  const inSettings = SETTINGS.some((item) => pathname.startsWith(item.to));

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const escape = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  return (
    <div className="profile" ref={box}>
      <button
        type="button"
        className={`avatar${inSettings ? " active" : ""}`}
        aria-label="Profile and settings"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
          <circle cx="12" cy="8" r="4" fill="currentColor" />
          <path d="M4 21c0-4.4 3.6-7 8-7s8 2.6 8 7z" fill="currentColor" />
        </svg>
      </button>
      {open && (
        <div className="menu" role="menu" aria-label="Settings">
          {SETTINGS.map((item) => (
            <Link key={item.to} role="menuitem" to={item.to} onClick={() => setOpen(false)}>
              {item.label}
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

export function Layout() {
  const { questions, todos } = useAttention();
  const waiting = questions.length + todos.length;
  const [askable, setAskable] = useState(
    typeof Notification !== "undefined" && Notification.permission === "default",
  );
  const link = ({ isActive }: { isActive: boolean }) => `nav${isActive ? " active" : ""}`;
  return (
    <>
      <header className="bar">
        <nav className="bar-inner" aria-label="Main">
          <NavLink className="brand" to="/">Agent Studio</NavLink>
          <NavLink className={link} to="/" end>Projects</NavLink>
          <NavLink className={link} to="/inbox">
            Inbox{waiting > 0 && <span id="badge" className={todos.length > 0 ? "alert" : ""}> ({waiting})</span>}
          </NavLink>
          <NavLink className={link} to="/tasks">Tasks</NavLink>
          <NavLink className={link} to="/chat">Chat</NavLink>
          <NavLink className={link} to="/new">New project</NavLink>
          <NavLink className={link} to="/health">Health</NavLink>
          {askable && (
            <button
              id="notify"
              onClick={() => void Notification.requestPermission().then(() => setAskable(false))}
            >
              Enable notifications
            </button>
          )}
          <ProfileMenu />
        </nav>
      </header>
      <main>
        <Outlet />
      </main>
    </>
  );
}
