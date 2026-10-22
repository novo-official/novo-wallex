import "./globals.css";
import Nav from "./nav";

export const metadata = { title: "نوو | معامله‌گر هوشمند والکس" };

export default function RootLayout({ children }) {
  return (
    <html lang="fa" dir="rtl">
      <body>
        <header className="top">
          <div className="brand">
            تیم نوو <span>· موتور معاملاتی هوشمند والکس</span>
          </div>
          <Nav />
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
