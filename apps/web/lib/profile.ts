"use client";

import { useApi } from "@/lib/useApi";

type Profile = {
  profile: string;
  enabled_chains: string[];
  copy_trading_enabled: boolean;
  chains: Record<string, { enabled: boolean; reason: string | null }>;
};

/** The system profile (apps/api GET /system/profile, yonixalpha_core.
 * system_profile): which chains and features run. Pages leave out what is
 * switched off, so the dashboard shows only working parts. Until the profile
 * is known nothing optional is shown (no flash of disabled pages). */
export function useProfile() {
  const { data } = useApi<Profile>("/api/system/profile", undefined, { refreshMs: 300000 });
  const chainOn = (c: string) => !!data?.enabled_chains?.includes(c);
  return {
    loaded: !!data,
    profile: data?.profile,
    chainOn,
    evmOn: chainOn("bsc") || chainOn("robinhood"),
    copyOn: !!data?.copy_trading_enabled,
  };
}

/** Paper account names to the part they belong to (system_profile.account_enabled). */
export function accountOn(p: ReturnType<typeof useProfile>, name: string): boolean {
  if (name.startsWith("evm_copy_")) return p.chainOn(name.slice("evm_copy_".length)) && p.copyOn;
  if (name.startsWith("evm_")) return p.chainOn(name.slice("evm_".length));
  if (name === "copy_solana") return p.copyOn;
  return true;
}
