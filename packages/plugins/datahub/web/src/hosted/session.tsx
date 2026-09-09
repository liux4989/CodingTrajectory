import * as React from "react";
import { Cloud, LoaderCircle, LogIn } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { HOSTED_MODE } from "@/hosted/mode";

type HostedConfig = {
  supabase_url: string;
  supabase_anon_key: string;
  horizon_days: number;
  content_scope: "chronicle";
};

type SupabaseSession = {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  user?: { email?: string | null };
};

type HostedAuthValue = {
  hosted: boolean;
  email: string | null;
  signOut: () => Promise<void>;
};

const HostedAuthContext = React.createContext<HostedAuthValue | null>(null);
let hostedAccessToken: string | null = null;

export function datahubFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  if (!HOSTED_MODE) return fetch(input, init);
  const headers = new Headers(init.headers);
  if (hostedAccessToken) headers.set("Authorization", `Bearer ${hostedAccessToken}`);
  return fetch(input, { ...init, headers });
}

export function HostedSessionProvider({ children }: { children: React.ReactNode }) {
  if (!HOSTED_MODE) {
    return (
      <HostedAuthContext.Provider value={{ hosted: false, email: null, signOut: async () => {} }}>
        {children}
      </HostedAuthContext.Provider>
    );
  }
  return <RemoteSessionProvider>{children}</RemoteSessionProvider>;
}

function RemoteSessionProvider({ children }: { children: React.ReactNode }) {
  const [config, setConfig] = React.useState<HostedConfig | null>(null);
  const [session, setSession] = React.useState<SupabaseSession | null>(null);
  const [configError, setConfigError] = React.useState<string | null>(null);
  const [configAttempt, setConfigAttempt] = React.useState(0);

  const acceptSession = React.useCallback((next: SupabaseSession | null) => {
    // The query tree mounts as soon as a session exists. Publish the token
    // synchronously so its first requests cannot race a passive effect.
    hostedAccessToken = next?.access_token ?? null;
    setSession(next);
  }, []);

  React.useEffect(() => {
    const controller = new AbortController();
    setConfigError(null);
    fetch("/api/hosted/config", { signal: controller.signal, headers: { Accept: "application/json" } })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Hosted configuration is unavailable (${response.status}).`);
        const value = (await response.json()) as Partial<HostedConfig>;
        if (
          typeof value.supabase_url !== "string" ||
          typeof value.supabase_anon_key !== "string" ||
          value.content_scope !== "chronicle" ||
          value.horizon_days !== 7
        ) {
          throw new Error("Hosted configuration is invalid.");
        }
        const url = new URL(value.supabase_url);
        if (url.protocol !== "https:") throw new Error("Hosted authentication requires HTTPS.");
        setConfig(value as HostedConfig);
      })
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setConfigError(error instanceof Error ? error.message : "Hosted configuration failed.");
        }
      });
    return () => controller.abort();
  }, [configAttempt]);

  React.useEffect(() => () => {
    hostedAccessToken = null;
  }, []);

  React.useEffect(() => {
    if (!config || !session) return;
    const delay = Math.max(30, session.expires_in - 60) * 1_000;
    const timer = window.setTimeout(() => {
      void refreshSupabaseSession(config, session.refresh_token)
        .then(acceptSession)
        .catch(() => acceptSession(null));
    }, delay);
    return () => window.clearTimeout(timer);
  }, [acceptSession, config, session]);

  const signOut = React.useCallback(async () => {
    const current = session;
    acceptSession(null);
    if (!config || !current) return;
    try {
      await fetch(new URL("/auth/v1/logout", config.supabase_url), {
        method: "POST",
        headers: {
          apikey: config.supabase_anon_key,
          Authorization: `Bearer ${current.access_token}`,
        },
      });
    } catch {
      // The in-memory session is already cleared; remote logout is best effort.
    }
  }, [acceptSession, config, session]);

  if (configError) {
    return (
      <HostedCenteredCard
        title="Remote Datahub is unavailable"
        detail={configError}
        action={<Button onClick={() => setConfigAttempt((value) => value + 1)}>Retry</Button>}
      />
    );
  }
  if (!config) {
    return (
      <HostedCenteredCard
        title="Opening Remote Datahub"
        detail="Loading the Access-protected Chronicle configuration."
        action={<LoaderCircle className="animate-spin text-muted-foreground" aria-label="Loading" />}
      />
    );
  }
  if (!session) {
    return <HostedSignIn config={config} onSession={acceptSession} />;
  }

  return (
    <HostedAuthContext.Provider
      value={{ hosted: true, email: session.user?.email ?? null, signOut }}
    >
      {children}
    </HostedAuthContext.Provider>
  );
}

function HostedSignIn({
  config,
  onSession,
}: {
  config: HostedConfig;
  onSession: (session: SupabaseSession) => void;
}) {
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [pending, setPending] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      const next = await createSupabaseSession(config, email, password);
      setPassword("");
      onSession(next);
    } catch {
      setError("Sign-in failed. Check the email and password for this workspace.");
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="grid min-h-dvh place-items-center bg-background p-4">
      <Card className="w-full max-w-md border-border-soft shadow-popover">
        <CardHeader className="gap-3">
          <div className="grid size-10 place-items-center rounded-lg bg-primary text-primary-foreground">
            <Cloud aria-hidden="true" size={20} />
          </div>
          <div>
            <CardTitle className="font-display">Remote Datahub</CardTitle>
            <CardDescription className="mt-1">
              Cloudflare Access admitted this browser. Sign in to the authorized Supabase workspace to read its seven-day Chronicle snapshot.
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          <form className="grid gap-4" onSubmit={(event) => void submit(event)}>
            <label className="grid gap-1.5 text-body-sm font-medium" htmlFor="hosted-email">
              Email
              <Input
                id="hosted-email"
                name="email"
                type="email"
                autoComplete="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                required
                autoFocus
              />
            </label>
            <label className="grid gap-1.5 text-body-sm font-medium" htmlFor="hosted-password">
              Password
              <Input
                id="hosted-password"
                name="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                required
              />
            </label>
            {error ? <p className="m-0 text-body-sm text-destructive" role="alert">{error}</p> : null}
            <Button type="submit" disabled={pending} className="gap-2">
              {pending ? <LoaderCircle className="animate-spin" aria-hidden="true" /> : <LogIn aria-hidden="true" />}
              {pending ? "Signing in…" : "Sign in"}
            </Button>
            <p className="m-0 text-caption text-muted-foreground">
              Credentials go directly to Supabase Auth. Tokens remain only in this tab's memory and are cleared on reload or sign-out.
            </p>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}

function HostedCenteredCard({
  title,
  detail,
  action,
}: {
  title: string;
  detail: string;
  action: React.ReactNode;
}) {
  return (
    <main className="grid min-h-dvh place-items-center bg-background p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="font-display">{title}</CardTitle>
          <CardDescription>{detail}</CardDescription>
        </CardHeader>
        <CardContent>{action}</CardContent>
      </Card>
    </main>
  );
}

async function createSupabaseSession(config: HostedConfig, email: string, password: string) {
  const url = new URL("/auth/v1/token", config.supabase_url);
  url.searchParams.set("grant_type", "password");
  return requestSupabaseSession(config, url, { email, password });
}

async function refreshSupabaseSession(config: HostedConfig, refreshToken: string) {
  const url = new URL("/auth/v1/token", config.supabase_url);
  url.searchParams.set("grant_type", "refresh_token");
  return requestSupabaseSession(config, url, { refresh_token: refreshToken });
}

async function requestSupabaseSession(
  config: HostedConfig,
  url: URL,
  body: Record<string, string>,
): Promise<SupabaseSession> {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      apikey: config.supabase_anon_key,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error("Supabase sign-in failed");
  const value = (await response.json()) as Partial<SupabaseSession>;
  if (
    typeof value.access_token !== "string" ||
    typeof value.refresh_token !== "string" ||
    typeof value.expires_in !== "number"
  ) {
    throw new Error("Supabase returned an invalid session");
  }
  return value as SupabaseSession;
}

export function useHostedAuth() {
  const value = React.useContext(HostedAuthContext);
  if (!value) throw new Error("useHostedAuth must be used inside HostedSessionProvider");
  return value;
}
