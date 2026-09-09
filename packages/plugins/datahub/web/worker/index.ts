import { createRemoteJWKSet, jwtVerify } from "jose";

const API_PATHS = new Set([
  "/api/datahub/snapshot",
  "/api/datahub/changes",
  "/api/projects",
  "/api/sessions",
  "/api/sessions/graph",
  "/api/sessions/tree",
  "/api/sessions/items",
]);
const MAX_ACCESS_JWT_BYTES = 16_384;
const accessKeySets = new Map<string, ReturnType<typeof createRemoteJWKSet>>();

export default {
  async fetch(request, env): Promise<Response> {
    const configuration = configurationFor(env);
    if (!configuration) return jsonError(503, "Remote Datahub is not configured.");

    const authenticated = await verifyAccess(request, env);
    if (!authenticated) return jsonError(403, "Cloudflare Access authentication required.");

    const url = new URL(request.url);
    if (url.pathname === "/api/hosted/config") {
      if (request.method !== "GET") return jsonError(404, "Not found.");
      return withSecurityHeaders(
        Response.json({
          supabase_url: configuration.supabaseUrl,
          supabase_anon_key: env.CT_SUPABASE_ANON_KEY,
          horizon_days: 7,
          content_scope: "chronicle",
        }),
        configuration.supabaseOrigin,
        true,
      );
    }

    if (url.pathname.startsWith("/api/")) {
      if (request.method !== "GET" || !API_PATHS.has(url.pathname)) {
        return withSecurityHeaders(jsonError(404, "Not found."), configuration.supabaseOrigin, true);
      }
      const forwarded = requestForFacade(request);
      const response = await env.DATAHUB_FACADE.fetch(forwarded);
      return withSecurityHeaders(response, configuration.supabaseOrigin, true);
    }

    if (request.method !== "GET" && request.method !== "HEAD") {
      return withSecurityHeaders(jsonError(404, "Not found."), configuration.supabaseOrigin, false);
    }
    const response = await env.ASSETS.fetch(request);
    return withSecurityHeaders(response, configuration.supabaseOrigin, false, url.pathname);
  },
} satisfies ExportedHandler<Env>;

function configurationFor(env: Env) {
  try {
    const supabaseUrl = new URL(env.CT_SUPABASE_URL);
    const teamDomain = new URL(env.CF_ACCESS_TEAM_DOMAIN);
    if (
      supabaseUrl.protocol !== "https:" ||
      teamDomain.protocol !== "https:" ||
      !teamDomain.hostname.endsWith(".cloudflareaccess.com") ||
      !env.CT_SUPABASE_ANON_KEY ||
      env.CT_SUPABASE_ANON_KEY.length > 8_192 ||
      !env.CF_ACCESS_AUD
    ) {
      return null;
    }
    return {
      supabaseUrl: supabaseUrl.origin,
      supabaseOrigin: supabaseUrl.origin,
      teamDomain: teamDomain.origin,
    };
  } catch {
    return null;
  }
}

async function verifyAccess(request: Request, env: Env): Promise<boolean> {
  const token = request.headers.get("cf-access-jwt-assertion") ?? "";
  if (!token || token.length > MAX_ACCESS_JWT_BYTES) return false;
  const configuration = configurationFor(env);
  if (!configuration) return false;
  try {
    let jwks = accessKeySets.get(configuration.teamDomain);
    if (!jwks) {
      jwks = createRemoteJWKSet(
        new URL(`${configuration.teamDomain}/cdn-cgi/access/certs`),
      );
      accessKeySets.set(configuration.teamDomain, jwks);
    }
    const { payload, protectedHeader } = await jwtVerify(token, jwks, {
      algorithms: ["RS256"],
      issuer: configuration.teamDomain,
      audience: env.CF_ACCESS_AUD,
    });
    return protectedHeader.alg === "RS256" && typeof payload.email === "string";
  } catch {
    return false;
  }
}

function requestForFacade(request: Request) {
  const headers = new Headers(request.headers);
  for (const name of [
    "cf-access-jwt-assertion",
    "cf-access-authenticated-user-email",
    "cf-connecting-ip",
    "cookie",
    "x-forwarded-for",
    "x-real-ip",
  ]) {
    headers.delete(name);
  }
  headers.set("Accept", "application/json");
  return new Request(request, { headers });
}

function jsonError(status: number, message: string) {
  return Response.json({ error: { message } }, { status });
}

function withSecurityHeaders(
  response: Response,
  supabaseOrigin: string,
  api: boolean,
  pathname = "",
) {
  const headers = new Headers(response.headers);
  headers.set("Content-Security-Policy", [
    "default-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    `connect-src 'self' ${supabaseOrigin}`,
  ].join("; "));
  headers.set("Cross-Origin-Resource-Policy", "same-origin");
  headers.set("Permissions-Policy", "camera=(), geolocation=(), microphone=()");
  headers.set("Referrer-Policy", "no-referrer");
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("X-Frame-Options", "DENY");
  headers.set("X-Robots-Tag", "noindex, nofollow");
  if (api) headers.set("Cache-Control", "no-store");
  else if (pathname.startsWith("/assets/")) {
    headers.set("Cache-Control", "public, max-age=31536000, immutable");
  } else {
    headers.set("Cache-Control", "no-cache");
  }
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}
