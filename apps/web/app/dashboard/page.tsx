import { redirect } from "next/navigation";

/** Regular trading is the active dashboard; the Memecoin overview moved to
 * /dashboard/memecoin (Archived Engines); the Strategy Center is the start page. */
export default function DashboardIndex() {
  redirect("/dashboard/crypto/center");
}
