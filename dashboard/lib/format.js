const fa = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 0 });
const pct = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 2, signDisplay: "exceptZero" });

export const tmn = (v) => (v === null || v === undefined || v === "" ? "—" : fa.format(Number(v)));
export const percent = (v) => (v === null || v === undefined ? "—" : `${pct.format(Number(v) * 100)}٪`);
export const time = (iso) =>
  iso
    ? new Date(iso).toLocaleString("fa-IR", {
        timeZone: "Asia/Tehran",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "—";

export const KIND_LABELS = {
  START: "شروع",
  RESTORE: "بازیابی وضعیت",
  LLM: "تصمیم هوش مصنوعی",
  ENTER: "ورود تأییدشده",
  REJECT: "ردشده",
  SKIP: "رد شده (بدون بررسی)",
  ORDER: "ثبت سفارش",
  REPRICE: "قیمت‌گذاری مجدد",
  CANCEL: "لغو سفارش",
  FILL: "خرید انجام شد",
  EXIT: "خروج",
  STOP: "حد ضرر",
  CLOSE: "بستن معامله",
  ERROR: "خطا",
  HALT_ENTRIES: "توقف ورودها",
  RESUME_ENTRIES: "ازسرگیری ورودها",
  KILL: "کلید توقف",
  BLOCK: "سقف ضرر روزانه",
  RECONCILE: "تطبیق حساب",
  FINAL: "بررسی پایانی",
  DUST: "مانده ناچیز",
};

export const KIND_TONE = {
  ENTER: "good", FILL: "good", CLOSE: "info", REJECT: "warn", SKIP: "warn", CANCEL: "muted",
  ERROR: "bad", HALT_ENTRIES: "bad", KILL: "bad", BLOCK: "bad", STOP: "bad", LLM: "ai",
};
