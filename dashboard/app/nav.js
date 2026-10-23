"use client";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";

function Inner() {
  const path = usePathname();
  const params = useSearchParams();
  const router = useRouter();
  const mode = params.get("mode") || "shadow";
  const q = `?mode=${mode}`;
  return (
    <nav>
      <Link className={path === "/" ? "on" : ""} href={`/${q}`}>نمای کلی</Link>
      <Link className={path === "/decisions" ? "on" : ""} href={`/decisions${q}`}>خط زمانی تصمیم‌ها</Link>
      <select value={mode} onChange={(e) => router.push(`${path}?mode=${e.target.value}`)} aria-label="حالت">
        <option value="shadow">شبیه‌سازی (شادو)</option>
        <option value="live">زنده</option>
        <option value="replay">بازپخش تاریخی ۷ روزه (کورکورانه)</option>
      </select>
    </nav>
  );
}

export default function Nav() {
  return (
    <Suspense fallback={null}>
      <Inner />
    </Suspense>
  );
}
