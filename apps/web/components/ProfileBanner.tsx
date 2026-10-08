"use client";

import { MinusCircle } from "lucide-react";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;

/** System profile banner (yonixalpha_core.system_profile): shown on a page
 * whose chain or feature the profile switches off, so its empty or old
 * data reads as "switched off on purpose", not as a failure. Renders
 * nothing while the chain / feature runs. */
export default function ProfileBanner({ chain, feature }: { chain?: string; feature?: "copy" }) {
  const { data } = useApi<J>("/api/system/profile", undefined, { refreshMs: 300000 });
  if (!data) return null;
  let reason: string | null = null;
  if (chain) reason = data.chains?.[chain]?.reason ?? null;
  if (!reason && feature === "copy" && !data.copy_trading_enabled) reason = data.workers?.["copy-engine"]?.reason ?? "DISABLED";
  if (!reason && !chain && !feature && data.enabled_chains?.length === 1) reason = `DISABLED — ${data.profile} MODE`;
  if (!reason) return null;
  const what = feature === "copy" ? "Copy trading" : chain ? chain.toUpperCase() : "BSC and Robinhood Chain";
  return (
    <div className="notice" role="status">
      <div><MinusCircle size={14} aria-hidden /> <b>{reason}</b></div>
      <div className="small">
        {what} is switched off by the system profile ({data.label}): its worker is not started, nothing new is scanned
        or traded, and new orders are refused. History, settings and open paper positions are kept as they were (not
        managed while switched off). To switch it back on, change {feature === "copy" ? "COPY_TRADING_ENABLED" : "SYSTEM_PROFILE / CHAIN_*"} in
        .env and run scripts/deploy.sh.
      </div>
    </div>
  );
}
