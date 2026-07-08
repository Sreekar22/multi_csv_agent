import type { Metadata } from "next";
import type { ReactNode } from "react";

import "./app.css";

export const metadata: Metadata = {
  title: "Multi-CSV Chat Analytics",
  description: "Chat-based analytics across multiple CSV files",
};

type RootLayoutProps = {
  children: ReactNode;
};

export default function RootLayout({ children }: RootLayoutProps) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
