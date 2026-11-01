import type { Metadata } from "next";
import "./styles.css";

export const metadata: Metadata = {
  title: "OpenBench — NLP Model Benchmarking",
  description: "A local research dashboard for IMDb sentiment models.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
