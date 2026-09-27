"use client";

import { FlaskConical } from "lucide-react";
import SmokeTestPanel from "@/components/SmokeTestPanel";
import { PageHeader } from "@/components/ui";

export default function SmokeTestPage() {
  return (
    <div>
      <PageHeader title="Live Smoke Test" icon={<FlaskConical size={20} aria-hidden />}
        subtitle="One tiny, fully gated live buy to verify the real execution path. Off unless enabled in the server's .env; the global mode never changes." />
      <SmokeTestPanel />
    </div>
  );
}
