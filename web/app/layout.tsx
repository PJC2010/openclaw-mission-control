import type { Metadata, Viewport } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Mission Control",
  description: "Operations dashboard for self-hosted agents",
};

export const viewport: Viewport = {
  themeColor: "#0b0f14",
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="mx-auto w-full max-w-[430px] px-4 pb-10 md:max-w-3xl">
          <header className="flex items-center justify-between py-4">
            <Link href="/" className="flex items-center gap-2 text-lg font-semibold tracking-wide">
              <span
                aria-hidden
                className="inline-block h-2.5 w-2.5 rounded-full"
                style={{ background: "var(--mc-green)", boxShadow: "0 0 8px #2ea04388" }}
              />
              Mission Control
            </Link>
            <nav className="flex gap-4 text-sm" style={{ color: "var(--mc-muted)" }}>
              <Link href="/approvals/" className="hover:text-white">
                Approvals
              </Link>
              <Link href="/runs/" className="hover:text-white">
                Runs
              </Link>
            </nav>
          </header>
          {children}
          <footer
            className="mt-10 text-center text-xs"
            style={{ color: "var(--mc-faint)" }}
          >
            tailnet-only · no funnel · phase 1
          </footer>
        </div>
      </body>
    </html>
  );
}
