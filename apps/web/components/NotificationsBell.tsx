"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { Bell } from "lucide-react";
import { apiPost } from "@/lib/api";
import type { NotificationsPage } from "@/lib/cc";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

export default function NotificationsBell() {
  const [open, setOpen] = useState(false);
  const { data, reload } = useApi<NotificationsPage>("/api/notifications", { limit: 8 }, {
    reloadOn: ["notification.created"],
    refreshMs: 60000,
  });
  const wrap = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    const onClick = (e: MouseEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClick);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onClick);
    };
  }, [open]);

  const unread = data?.unread ?? 0;
  return (
    <div className="bell-wrap" ref={wrap}>
      <button
        className="icon-btn"
        aria-label={`Notifications${unread ? `, ${unread} unread` : ""}`}
        aria-expanded={open}
        aria-haspopup="true"
        onClick={() => setOpen(!open)}
      >
        <Bell size={16} aria-hidden />
        {unread > 0 && <span className="badge">{unread > 99 ? "99+" : unread}</span>}
      </button>
      {open && (
        <div className="bell-menu" role="region" aria-label="Recent notifications">
          <div className="bell-head">
            <b>Notifications</b>
            <button
              className="btn btn-ghost btn-sm"
              disabled={!unread}
              onClick={async () => {
                await apiPost("/api/notifications/read-all");
                reload();
              }}
            >
              Mark all read
            </button>
          </div>
          {!data?.items.length && <div className="muted" style={{ padding: 12 }}>Nothing yet.</div>}
          <ul className="bell-list">
            {data?.items.map((n) => (
              <li key={n.id} className={n.read_at ? "" : "unread"}>
                <span className={`sev sev-${n.severity}`} aria-label={n.severity} />
                <div>
                  <div>{n.title}</div>
                  {n.body && <div className="muted small">{n.body}</div>}
                  <div className="muted small">{formatDate(n.created_at)}</div>
                </div>
              </li>
            ))}
          </ul>
          <Link href="/dashboard/notifications" className="bell-all" onClick={() => setOpen(false)}>
            All notifications &amp; preferences
          </Link>
        </div>
      )}
    </div>
  );
}
