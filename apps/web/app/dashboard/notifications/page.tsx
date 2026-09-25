"use client";

import { useState } from "react";
import { Bell } from "lucide-react";
import Pagination from "@/components/Pagination";
import { Empty, ErrorNotice, PageHeader, Section } from "@/components/ui";
import { apiPost, apiPut } from "@/lib/api";
import type { NotificationsPage } from "@/lib/cc";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

export default function NotificationsPageView() {
  const [unread, setUnread] = useState(false);
  const [offset, setOffset] = useState(0);
  const list = useApi<NotificationsPage>("/api/notifications", { unread: unread || undefined, limit: 50, offset }, { reloadOn: ["notification.created"] });
  const prefs = useApi<Record<string, { in_app: boolean; telegram: boolean }>>("/api/notifications/prefs");
  const [prefError, setPrefError] = useState<string | null>(null);

  async function toggle(kind: string, telegram: boolean) {
    setPrefError(null);
    try {
      prefs.setData(await apiPut("/api/notifications/prefs", { [kind]: { telegram } }));
    } catch (err) {
      setPrefError(err instanceof Error ? err.message : "Save failed.");
    }
  }

  return (
    <div>
      <PageHeader title="Notifications" icon={<Bell size={20} aria-hidden />}>
        <button
          className="btn btn-ghost btn-sm"
          onClick={async () => {
            await apiPost("/api/notifications/read-all");
            list.reload();
          }}
        >
          Mark all read
        </button>
      </PageHeader>
      <div className="filters">
        <label className="inline-label">
          <input type="checkbox" checked={unread} onChange={(e) => setUnread(e.target.checked)} /> Unread only
        </label>
      </div>
      <ErrorNotice error={list.error} />
      {list.data && list.data.items.length === 0 && <Empty>No notifications.</Empty>}
      <ul className="notif-list">
        {list.data?.items.map((n) => (
          <li key={n.id} className={n.read_at ? "" : "unread"}>
            <span className={`sev sev-${n.severity}`} aria-label={n.severity} />
            <div className="notif-body">
              <div>
                <b>{n.title}</b> <span className="pill pill-off">{n.kind}</span>
              </div>
              {n.body && <div className="muted">{n.body}</div>}
              <div className="muted small">{formatDate(n.created_at)}</div>
            </div>
            {!n.read_at && (
              <button
                className="btn btn-ghost btn-sm"
                onClick={async () => {
                  await apiPost(`/api/notifications/${n.id}/read`);
                  list.reload();
                }}
              >
                Mark read
              </button>
            )}
          </li>
        ))}
      </ul>
      {list.data && <Pagination total={list.data.total} limit={50} offset={offset} onOffsetChange={setOffset} />}
      <Section title="Telegram delivery per kind">
        <div className="muted">Every notification is always stored here. Telegram needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID on the server.</div>
        <ErrorNotice error={prefError || prefs.error} />
        <div className="pref-grid">
          {prefs.data &&
            Object.entries(prefs.data).map(([kind, p]) => (
              <label key={kind} className="pref">
                <input type="checkbox" checked={p.telegram} onChange={(e) => toggle(kind, e.target.checked)} /> {kind.replace(/_/g, " ")}
              </label>
            ))}
        </div>
      </Section>
    </div>
  );
}
