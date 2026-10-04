"use client";

import { useState } from "react";
import { ErrorNotice, Section } from "@/components/ui";
import { apiPut } from "@/lib/api";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const PROVIDERS: [string, string, string][] = [
  ["honeypot_is_enabled", "Honeypot.is", "BSC buy / sell simulation"],
  ["goplus_enabled", "GoPlus token security", "BSC honeypot / cannot-sell / cannot-buy flags and contract risks"],
];

/** Master §68-69: optional external safety providers for EVM tokens. A flag
 * fails the token; a clean or missing answer never passes it (the on-chain
 * checks stay the primary source). */
export default function ExternalSafety() {
  const { data, error, reload } = useApi<J>("/api/evm/settings");
  const [msg, setMsg] = useState<string | null>(null);
  if (error) return <ErrorNotice error={error} />;
  if (!data) return null;
  const toggle = async (k: string) => {
    try { await apiPut("/api/evm/settings", { [k]: !data.settings[k] }); setMsg("Saved."); reload(); }
    catch (e) { setMsg(String((e as Error).message)); }
  };
  return (
    <Section title="External safety providers (EVM)">
      <p className="muted small">Tertiary sources (master §69): a provider flag fails the token, a clean answer never passes it,
        and a provider that does not answer changes nothing. Robinhood Chain is not covered by either provider. Both are off
        by default.</p>
      <table className="data-table">
        <thead><tr><th>Provider</th><th>What it adds</th><th>State</th><th /></tr></thead>
        <tbody>{PROVIDERS.map(([k, name, what]) => (
          <tr key={k}><td>{name}</td><td className="small">{what}</td>
            <td><span className={data.settings[k] ? "pill pill-ok" : "pill pill-off"}>{data.settings[k] ? "ON" : "OFF"}</span></td>
            <td><button className="btn btn-ghost btn-sm" onClick={() => toggle(k)}>{data.settings[k] ? "Switch off" : "Switch on"}</button></td></tr>
        ))}</tbody>
      </table>
      {msg && <p className="small">{msg}</p>}
    </Section>
  );
}
