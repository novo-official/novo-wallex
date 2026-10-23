"use client";
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useApi } from "../../lib/useApi";
import { KIND_LABELS, KIND_TONE, time, tmn } from "../../lib/format";

const FILTERS = {
  all: null,
  trades: "ENTER,REJECT,SKIP,ORDER,REPRICE,CANCEL,FILL,EXIT,STOP,CLOSE",
  ai: "LLM,ENTER,REJECT",
  risk: "ERROR,HALT_ENTRIES,RESUME_ENTRIES,KILL,BLOCK,RECONCILE,FINAL,STOP",
};
const FILTER_LABELS = { all: "همه", trades: "معاملات", ai: "هوش مصنوعی", risk: "ریسک و خطا" };

function Row({ d }) {
  const [open, setOpen] = useState(false);
  const label = KIND_LABELS[d.kind] || d.kind;
  const who = d.kind === "LLM" ? (d.role === "regime_officer" ? "افسر ریسک" : "عامل معامله") : d.module;
  const summary =
    d.kind === "LLM"
      ? d.role === "regime_officer"
        ? `حالت ${d.risk_mode} · ممنوع: ${(d.avoid || []).join("، ") || "هیچ"}`
        : `${d.symbol}: ${d.approve ? "تأیید" : "رد"} · اندازه ${d.size_fraction == null ? "—" : Number(d.size_fraction).toFixed(2)}`
      : [d.symbol, d.price || d.limit ? `قیمت ${tmn(d.price || d.limit)}` : null, d.role].filter(Boolean).join(" · ");
  return (
    <li className={`event ${KIND_TONE[d.kind] || ""}`} onClick={() => setOpen(!open)}>
      <div className="line">
        <span className="when">{time(d.time)}</span>
        <span className="kind">{label}</span>
        {who && <span className="who">{who}</span>}
        <span className="what">{summary}</span>
      </div>
      {(d.reason || d.risk || d.rationale || d.error) && (
        <div className="why ltr">{d.rationale || d.risk || d.reason || d.error}</div>
      )}
      {open && <pre className="ltr raw">{JSON.stringify(d, null, 2)}</pre>}
    </li>
  );
}

function Timeline() {
  const mode = useSearchParams().get("mode") || "shadow";
  const [filter, setFilter] = useState("all");
  const kinds = FILTERS[filter];
  const { data, error } = useApi(`/api/decisions?mode=${mode}&limit=500${kinds ? `&kind=${kinds}` : ""}`);
  return (
    <>
      <div className="filters">
        {Object.keys(FILTERS).map((f) => (
          <button key={f} className={f === filter ? "on" : ""} onClick={() => setFilter(f)}>
            {FILTER_LABELS[f]}
          </button>
        ))}
        <span className="muted small">{data ? `${tmn(data.count)} رویداد · برای جزئیات کلیک کنید` : ""}</span>
      </div>
      {mode === "replay" && (
        <p className="muted small">
          بازپخش کورکورانه پیکربندی زنده روی ۷ روز تاریخی (۱۵ تا ۲۲ اکتبر ۲۰۲۶): پاسخ‌های واقعی مدل از کش، بدون
          تاریخ و قیمت در ورودی مدل. این داده شادو یا زنده نیست.
        </p>
      )}
      {error && <p className="bad">خطا در خواندن API: {error}</p>}
      {!data && !error && <p>در حال بارگذاری…</p>}
      <ul className="timeline">{data?.results.map((d, i) => <Row key={`${d.time}-${i}`} d={d} />)}</ul>
    </>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<p>در حال بارگذاری…</p>}>
      <Timeline />
    </Suspense>
  );
}
