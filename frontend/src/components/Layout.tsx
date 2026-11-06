import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useInbox } from "../hooks";

export function Layout() {
  const inbox = useInbox();
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
            Inbox{inbox.length > 0 && <span id="badge"> ({inbox.length})</span>}
          </NavLink>
          <NavLink className={link} to="/new">New project</NavLink>
          <NavLink className={link} to="/rules">Rules</NavLink>
          <NavLink className={link} to="/health">Health</NavLink>
          {askable && (
            <button
              id="notify"
              onClick={() => void Notification.requestPermission().then(() => setAskable(false))}
            >
              Enable notifications
            </button>
          )}
        </nav>
      </header>
      <main>
        <Outlet />
      </main>
    </>
  );
}
