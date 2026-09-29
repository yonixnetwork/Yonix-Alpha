"use client";

import { useParams } from "next/navigation";
import StrategyPage from "@/components/StrategyPage";

export default function StrategyDetailPage() {
  const { name } = useParams<{ name: string }>();
  return <StrategyPage name={name} />;
}
