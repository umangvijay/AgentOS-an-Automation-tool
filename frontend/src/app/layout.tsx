import type { Metadata, Viewport } from "next";
import { Inter, Playfair_Display, JetBrains_Mono } from "next/font/google";
import { AppProviders } from "@/components/AppProviders";
import "./globals.css";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });
const playfair = Playfair_Display({ subsets: ["latin"], variable: "--font-playfair", display: "swap" });
const jetbrains = JetBrains_Mono({ subsets: ["latin"], variable: "--font-jetbrains", display: "swap" });

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 5,
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f6f5f1" },
    { media: "(prefers-color-scheme: dark)", color: "#0f0f11" },
  ],
  colorScheme: "light dark",
};

export const metadata: Metadata = {
  title: {
    default: "AgentOS — The Autonomous AI Workspace",
    template: "%s · AgentOS",
  },
  description:
    "AgentOS is the autonomous workspace that builds its own tools. Give it a goal, and it plans, executes, and delivers — building any missing integrations along the way.",
  keywords: ["AI", "autonomous", "workspace", "agents", "MCP", "integrations", "automation"],
  openGraph: {
    title: "AgentOS — The Autonomous AI Workspace",
    description: "AgentOS is the autonomous workspace that builds its own tools.",
    type: "website",
    url: "https://agentos.devpost.local",
    siteName: "AgentOS",
  },
  twitter: {
    card: "summary_large_image",
    title: "AgentOS — The Autonomous AI Workspace",
    description: "AgentOS is the autonomous workspace that builds its own tools.",
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning className={`${inter.variable} ${playfair.variable} ${jetbrains.variable}`}>
      <body className="font-sans">
        {/* First-paint theme: apply before React hydrates to prevent flash */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var t=localStorage.getItem('agentos_theme');if(t==='dark'||t==='light'){document.documentElement.setAttribute('data-theme',t)}}catch(e){}})()`,
          }}
        />
        <AppProviders>
          {children}
        </AppProviders>
      </body>
    </html>
  );
}
