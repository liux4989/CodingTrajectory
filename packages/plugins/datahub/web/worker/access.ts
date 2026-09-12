import { createRemoteJWKSet, jwtVerify } from "jose";

const MAX_ACCESS_JWT_BYTES = 16_384;
const accessKeySets = new Map<string, ReturnType<typeof createRemoteJWKSet>>();

export function configurationFor(env: Env) {
  try {
    const teamDomain = new URL(env.CF_ACCESS_TEAM_DOMAIN);
    if (
      teamDomain.protocol !== "https:" ||
      !teamDomain.hostname.endsWith(".cloudflareaccess.com") ||
      !env.CF_ACCESS_AUD
    ) {
      return null;
    }
    return {
      teamDomain: teamDomain.origin,
    };
  } catch {
    return null;
  }
}

export async function verifyAccess(request: Request, env: Env): Promise<boolean> {
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

export function withSecurityHeaders(response: Response, workerVersion?: string, cacheControl = "no-store") {
  const headers = new Headers(response.headers);
  if (workerVersion) headers.set("X-CT-Worker-Version", workerVersion);
  headers.set("Content-Security-Policy", [
    "default-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
  ].join("; "));
  headers.set("Cross-Origin-Resource-Policy", "same-origin");
  headers.set("Permissions-Policy", "camera=(), geolocation=(), microphone=()");
  headers.set("Referrer-Policy", "no-referrer");
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("X-Frame-Options", "DENY");
  headers.set("X-Robots-Tag", "noindex, nofollow");
  headers.set("Cache-Control", cacheControl);
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}
