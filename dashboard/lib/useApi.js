"use client";
import { useEffect, useState } from "react";

export function useApi(path, everyMs = 15000) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let alive = true;
    const load = () =>
      fetch(path, { cache: "no-store" })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((d) => alive && (setData(d), setError(null)))
        .catch((e) => alive && setError(e.message));
    load();
    const id = setInterval(load, everyMs);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [path, everyMs]);
  return { data, error };
}
