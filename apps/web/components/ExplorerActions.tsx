"use client";

import Link from "next/link";
import { ExternalLink } from "lucide-react";

/** Explorer actions of one token or wallet (master §55), built server-side
 * for its own chain (yonixalpha_core.explorer_links): a Solana link is never
 * shown for an EVM token. An action without a confirmed link is shown
 * disabled with the reason, never as a guessed URL. */
const ACTIONS: [string, string][] = [
  ["token", "OPEN TOKEN"], ["transaction", "TRANSACTION"], ["creator", "CREATOR"], ["wallet", "WALLET"],
  ["launchpad", "LAUNCHPAD"], ["dex", "DEX"], ["explorer", "EXPLORER"],
];

export default function ExplorerActions({ links, unavailable, explorerName, only, hide = [] }: {
  links: Record<string, string>;
  unavailable?: Record<string, string>;
  explorerName?: string;
  /** Show only these actions. */
  only?: string[];
  /** Never show these actions (e.g. OPEN TOKEN on the token's own page). */
  hide?: string[];
}) {
  const shown = ACTIONS.filter(([k]) => (!only || only.includes(k)) && !hide.includes(k));
  return (
    <div className="btn-row" role="group" aria-label="Explorer actions">
      {shown.map(([k, label]) => {
        const url = links[k];
        const text = k === "explorer" && explorerName ? `${label} (${explorerName})` : label;
        if (!url) {
          const why = unavailable?.[k];
          if (!why || (why.startsWith("no ") && ["transaction", "wallet", "creator"].includes(k))) return null;
          return <button key={k} type="button" className="btn btn-ghost btn-sm" disabled title={why}>{text}</button>;
        }
        if (url.startsWith("/")) return <Link key={k} className="btn btn-sm" href={url}>{text}</Link>;
        return (
          <a key={k} className="btn btn-ghost btn-sm" href={url} target="_blank" rel="noopener noreferrer" title={url}>
            {text} <ExternalLink size={12} aria-hidden />
          </a>
        );
      })}
    </div>
  );
}
