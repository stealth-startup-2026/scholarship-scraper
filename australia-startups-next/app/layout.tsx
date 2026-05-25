import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Australia Startup Growth Watch",
  description: "A compact dashboard of Australia's top 10 growing startups.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en-AU">
      <body>{children}</body>
    </html>
  );
}
