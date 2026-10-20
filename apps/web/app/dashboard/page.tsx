import { redirect } from "next/navigation";

/** Solana memecoin trading is the active product: the start page is the
 * memecoin dashboard. Crypto / exchange trading is under Archived Engines. */
export default function DashboardIndex() {
  redirect("/dashboard/memecoin");
}
