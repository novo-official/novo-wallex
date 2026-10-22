"use client";
import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { useApi } from "../lib/useApi";
import { percent, time, tmn } from "../lib/format";

function Tile({ label, value, tone }) {
  return (
    <div className={`tile ${tone || ""}`}>
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  );
}

function Overview() {
  const mode = useSearchParams().get("mode") || "shadow";
  const { data, error } = useApi(`/api/overview?mode=${mode}`);
  if (error) return <p className="bad">خطا در خواندن API: {error}</p>;
  if (!data) return <p>در حال بارگذاری…</p>;
  const s = data.state || {};
  const officer = data.officer;
  const positions = Object.values(data.positions || {});
  const bt = data.backtest?.results || {};
  return (
    <>
      <section className="tiles">
        <Tile label="وضعیت" value={data.running ? "در حال اجرا" : "متوقف"} tone={data.running ? "good" : "bad"} />
        <Tile label="ارزش حساب (تومان)" value={tmn(s.equity)} />
        <Tile label="بازده از شروع" value={percent(s.return)} tone={Number(s.return) < 0 ? "bad" : "good"} />
        <Tile label="معاملات بسته‌شده" value={tmn(data.trades.length)} />
        <Tile
          label="خطاها از آخرین شروع (کل)"
          value={`${tmn(data.errors_since_start)} (${tmn(data.errors)})`}
          tone={data.errors_since_start ? "warn" : ""}
        />
        <Tile
          label="ورودها"
          value={s.halted ? "متوقف (کلید توقف)" : s.entries_halted ? "متوقف موقت" : "مجاز"}
          tone={s.halted || s.entries_halted ? "bad" : "good"}
        />
      </section>

      <section className="card">
        <h2>آخرین تصمیم افسر ریسک (مدل زبانی محلی)</h2>
        {officer ? (
          <>
            <p>
              <b>حالت ریسک:</b> {officer.risk_mode} · <b>نمادهای ممنوع:</b>{" "}
              {(officer.avoid || []).join("، ") || "هیچ"} · <b>معاملات فعال‌سازی:</b>{" "}
              {officer.activity ? "مجاز" : "غیرمجاز"} · {time(officer.decided_at || officer.time)}
            </p>
            <p className="ltr small">{officer.rationale}</p>
            <p className="muted small ltr">{officer.prompt_hash}</p>
          </>
        ) : (
          <p className="muted">هنوز تصمیمی ثبت نشده است.</p>
        )}
      </section>

      <section className="card">
        <h2>پوزیشن‌های باز</h2>
        {positions.length ? (
          <table>
            <thead>
              <tr><th>نماد</th><th>ماژول</th><th>مقدار</th><th>قیمت ورود</th><th>حد ضرر</th><th>حد سود</th><th>زمان</th></tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.symbol}>
                  <td>{p.symbol}</td><td>{p.module}</td><td className="ltr">{p.quantity}</td><td>{tmn(p.entry_price)}</td>
                  <td>{tmn(p.stop)}</td><td>{tmn(p.take_profit)}</td><td>{time(p.opened_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">پوزیشن بازی وجود ندارد.</p>
        )}
      </section>

      <section className="card">
        <h2>معاملات بسته‌شده</h2>
        {data.trades.length ? (
          <table>
            <thead>
              <tr><th>نماد</th><th>ورود</th><th>خروج</th><th>قیمت ورود</th><th>قیمت خروج</th><th>نوع خروج</th><th>سود/زیان (تومان)</th></tr>
            </thead>
            <tbody>
              {data.trades.map((t, i) => (
                <tr key={i}>
                  <td>{t.symbol}</td><td>{time(t.opened)}</td><td>{time(t.closed)}</td><td>{tmn(t.entry_price)}</td>
                  <td>{tmn(t.exit_price)}</td><td>{t.exit_role}</td>
                  <td className={Number(t.pnl_tmn) < 0 ? "bad" : "good"}>{tmn(t.pnl_tmn)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">هنوز معامله‌ای بسته نشده است.</p>
        )}
      </section>

      <section className="card">
        <h2>بک‌تست ۱۴ روزه (کارمزد واقعی اندازه‌گیری‌شده)</h2>
        <table>
          <thead>
            <tr><th>پیکربندی / کارمزد</th><th>بازده</th><th>افت سرمایه</th><th>معامله در روز</th><th>بدترین ۳ روز</th></tr>
          </thead>
          <tbody>
            {Object.entries(bt).map(([k, r]) => (
              <tr key={k}>
                <td className="ltr">{k}</td><td>{percent(r.return)}</td><td>{percent(r.max_drawdown)}</td>
                <td>{Number(r.trades_per_day).toLocaleString("fa-IR", { maximumFractionDigits: 2 })}</td>
                <td>{percent(r.worst_3d)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted small">پراکسی بدون تأیید مدل زبانی؛ گزینه‌ها روی همین داده‌های اخیر انتخاب شده‌اند.</p>
      </section>

      <p className="muted small">
        آخرین ضربان: {time(data.heartbeat)} · پیکربندی: <span className="ltr">{data.last_start?.config_hash}</span>
      </p>
    </>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<p>در حال بارگذاری…</p>}>
      <Overview />
    </Suspense>
  );
}
