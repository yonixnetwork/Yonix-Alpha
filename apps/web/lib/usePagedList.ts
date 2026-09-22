"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiGet, ApiError } from "@/lib/api";
import type { Page } from "@/lib/types";

type Filters = Record<string, string | number | boolean | undefined>;

/** The fetch/pagination/error/auth-redirect boilerplate every list page in
 * this dashboard needs identically — extracted here once it was clearly
 * needed by five-plus call sites, not spec'd out in advance.
 */
export function usePagedList<T>(path: string, filters: Filters, limit = 50) {
  const router = useRouter();
  const [data, setData] = useState<Page<T> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const filtersKey = JSON.stringify(filters);

  useEffect(() => {
    setOffset(0);
  }, [path, filtersKey]);

  const load = useCallback(async () => {
    try {
      const result = await apiGet<Page<T>>(path, { ...JSON.parse(filtersKey), limit, offset });
      setData(result);
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.replace("/login");
        return;
      }
      setError("Failed to load data.");
    }
  }, [path, filtersKey, limit, offset, router]);

  useEffect(() => {
    load();
  }, [load]);

  return { data, error, offset, setOffset, limit, reload: load };
}
