import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SIGNAL-OS // Intelligence Platform",
  description:
    "AI-native multimodal OSINT intelligence operating system — one dashboard, one search bar, one memory system, one entity graph, many agents.",
  applicationName: "SIGNAL-OS",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: "#080c10",
  colorScheme: "dark",
  width: "device-width",
  initialScale: 1,
};

/**
 * Root layout.
 *
 * `h-full overflow-hidden` on `<body>` is what lets the shell own its own
 * scrolling regions: the chrome stays fixed and only the active view scrolls,
 * which is what an analyst wants on a 1280px-to-1920px multi-monitor desk.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full" suppressHydrationWarning>
      <body className="h-full overflow-hidden antialiased">{children}</body>
    </html>
  );
}