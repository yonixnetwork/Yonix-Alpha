"use client";

import { Workflow } from "lucide-react";
import ExecutionFunnel from "@/components/ExecutionFunnel";
import { PageHeader } from "@/components/ui";

export default function FunnelPage() {
  return (
    <div>
      <PageHeader title="Execution Funnel" icon={<Workflow size={20} aria-hidden />}
        subtitle="Every stage from observed to position closed, with each token's furthest stage and the exact reason it stopped." />
      <ExecutionFunnel />
    </div>
  );
}
