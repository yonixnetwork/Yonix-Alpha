"use client";

import { useState } from "react";
import { Bot } from "lucide-react";
import { ErrorNotice, Loading, PageHeader, Section, StatePill } from "@/components/ui";
import { apiGet, apiPost } from "@/lib/api";
import type { ConnState } from "@/lib/cc";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface BotRow {
  name: string;
  label: string;
  strategies: string[];
  configured: boolean;
  state: ConnState;
  running?: boolean;
  paused?: boolean;
  position?: Record<string, unknown> | null;
  last_signal?: string | null;
  last_error?: string | null;
  error?: string;
  checked_at?: string;
}

export default function ExternalBotsPage() {
  const { data, error, loading, reload } = useApi<BotRow[]>("/api/external-bots", undefined, { refreshMs: 30000 });
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function close(name: string) {
    const typed = window.prompt(`Close the open position of ${name}? Type the bot name to confirm.`);
    if (typed !== name) return;
    try {
      const r = await apiPost<{ closed: boolean; detail: string }>(`/api/external-bots/${name}/close`, { confirm: name });
      setMessage(`${name}: ${r.closed ? "closed" : "not closed"} — ${r.detail}`);
      reload();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Close failed.");
    }
  }

  async function showConfig(name: string) {
    try {
      setConfig({ bot: name, ...(await apiGet<Record<string, unknown>>(`/api/external-bots/${name}/config`)) });
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Config unavailable.");
    }
  }

  return (
    <div>
      <PageHeader
        title="External Bots"
        icon={<Bot size={20} aria-hidden />}
        subtitle="Your standalone bots, through their control APIs. YonixAlpha refuses LIVE for a strategy while its standalone bot is running or holds a position."
      />
      <ErrorNotice error={error} />
      {message && <div className="notice">{message}</div>}
      {loading && !data && <Loading />}
      {data && (
        <Section title="Control APIs">
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Bot</th>
                  <th>State</th>
                  <th>Running</th>
                  <th>Position</th>
                  <th>Last signal / error</th>
                  <th>Same strategy here</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.map((b) => (
                  <tr key={b.name}>
                    <td>{b.label}</td>
                    <td>
                      <StatePill state={b.state} />
                    </td>
                    <td>{b.configured && b.state === "CONNECTED" ? (b.running ? (b.paused ? "paused" : "yes") : "no") : "—"}</td>
                    <td className="small">{b.position ? JSON.stringify(b.position) : "—"}</td>
                    <td className="muted small">
                      {b.error ?? b.last_error ?? b.last_signal ?? "—"}
                      {b.checked_at && ` · ${formatDate(b.checked_at)}`}
                    </td>
                    <td className="small">{b.strategies.join(", ")}</td>
                    <td>
                      {b.state === "CONNECTED" && (
                        <>
                          <button className="btn btn-ghost btn-sm" onClick={() => showConfig(b.name)}>
                            Config
                          </button>{" "}
                          {b.position && (
                            <button className="btn btn-danger btn-sm" onClick={() => close(b.name)}>
                              Close position
                            </button>
                          )}
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="muted small">
            A bot is NOT CONFIGURED until its URL and token (e.g. META_MUSE_CONTROL_URL / META_MUSE_TOKEN) are set in the server&apos;s .env. Tokens and URLs never reach this page.
          </p>
        </Section>
      )}
      {config && (
        <Section title={`Config — ${String(config.bot)}`}>
          <pre className="small">{JSON.stringify(config, null, 2)}</pre>
        </Section>
      )}
    </div>
  );
}
