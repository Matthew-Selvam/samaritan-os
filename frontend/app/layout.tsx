import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SIGNAL-OS // Intelligence Platform",
  description: "AI-native multimodal OSINT intelligence operating system",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full">
      <body className="h-full overflow-hidden">{children}</body>
    </html>
  );
}
