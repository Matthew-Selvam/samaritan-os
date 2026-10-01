import { AppShell } from "@/components/AppShell";

/**
 * app/page.tsx — the single route.
 *
 * All state lives in `components/AppShell.tsx` so this stays a server
 * component: the shell itself is a client component, and nothing else is
 * needed at the route level. Keeping the route server-rendered means the first
 * paint is instant and the dashboard has no client-side hydration waterfall.
 */
export default function Home() {
  return <AppShell />;
}
