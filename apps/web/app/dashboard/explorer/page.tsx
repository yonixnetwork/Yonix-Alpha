import { redirect } from "next/navigation";

/** The explorer search lives on the Token Explorer page. */
export default function ExplorerIndex() {
  redirect("/dashboard/tokens");
}
