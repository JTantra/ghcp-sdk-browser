/**
 * Microsoft Entra token acquisition for the hosted Foundry endpoint.
 *
 * Why this file exists:
 *   The Foundry hosted-agent proxy authenticates the WebSocket upgrade with an
 *   Entra bearer token on the `Authorization` header. The browser WebSocket API
 *   cannot set headers, and the platform explicitly refuses an `authorization`
 *   query parameter. So we acquire a token here and inject it as a header via
 *   declarativeNetRequest (see rules.ts).
 *
 * Flow: authorization code + PKCE through chrome.identity.launchWebAuthFlow.
 * The extension is a public client -- there is no secret, which is why the app
 * registration uses an SPA redirect URI (PKCE mandatory, no implicit grant).
 */

const AUTHORITY = "https://login.microsoftonline.com";
const SCOPE = "https://ai.azure.com/user_impersonation offline_access openid profile";

interface TokenSet {
  accessToken: string;
  refreshToken?: string;
  /** Epoch millis. */
  expiresAt: number;
}

interface AuthConfig {
  clientId: string;
  tenantId: string;
}

let cached: TokenSet | null = null;
/** De-dupes concurrent refreshes so a burst of calls yields one network round-trip. */
let inFlight: Promise<string> | null = null;

function base64UrlEncode(bytes: ArrayBuffer): string {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

async function makePkcePair(): Promise<{ verifier: string; challenge: string }> {
  const random = new Uint8Array(32);
  crypto.getRandomValues(random);
  const verifier = base64UrlEncode(random.buffer);
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return { verifier, challenge: base64UrlEncode(digest) };
}

export async function getAuthConfig(): Promise<AuthConfig | null> {
  const s = await chrome.storage.local.get(["clientId", "tenantId"]);
  if (!s.clientId || !s.tenantId) return null;
  return { clientId: s.clientId, tenantId: s.tenantId };
}

async function loadCached(): Promise<TokenSet | null> {
  if (cached) return cached;
  const s = await chrome.storage.local.get(["tokenSet"]);
  cached = (s.tokenSet as TokenSet | undefined) ?? null;
  return cached;
}

async function store(tokens: TokenSet): Promise<void> {
  cached = tokens;
  await chrome.storage.local.set({ tokenSet: tokens });
}

export async function signOut(): Promise<void> {
  cached = null;
  await chrome.storage.local.remove(["tokenSet"]);
}

/** Trades an authorization code or refresh token for an access token. */
async function redeem(cfg: AuthConfig, body: Record<string, string>): Promise<TokenSet> {
  const res = await fetch(`${AUTHORITY}/${cfg.tenantId}/oauth2/v2.0/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: cfg.clientId, ...body }).toString(),
  });

  const json = await res.json();
  if (!res.ok) {
    throw new Error(`token endpoint ${res.status}: ${json.error_description ?? json.error}`);
  }

  return {
    accessToken: json.access_token,
    refreshToken: json.refresh_token,
    // 60s safety margin so a token never expires mid-upgrade.
    expiresAt: Date.now() + (json.expires_in ?? 3600) * 1000 - 60_000,
  };
}

/** Full interactive sign-in. Opens the Entra consent/login window. */
export async function signIn(): Promise<string> {
  const cfg = await getAuthConfig();
  if (!cfg) throw new Error("set the Client ID and Tenant ID in the side panel first");

  const { verifier, challenge } = await makePkcePair();
  const redirectUri = chrome.identity.getRedirectURL();
  const state = crypto.randomUUID();

  const authUrl =
    `${AUTHORITY}/${cfg.tenantId}/oauth2/v2.0/authorize?` +
    new URLSearchParams({
      client_id: cfg.clientId,
      response_type: "code",
      redirect_uri: redirectUri,
      response_mode: "query",
      scope: SCOPE,
      state,
      code_challenge: challenge,
      code_challenge_method: "S256",
    }).toString();

  const redirect = await chrome.identity.launchWebAuthFlow({ url: authUrl, interactive: true });
  if (!redirect) throw new Error("sign-in was cancelled");

  const params = new URL(redirect).searchParams;
  if (params.get("error")) {
    throw new Error(`${params.get("error")}: ${params.get("error_description") ?? ""}`);
  }
  if (params.get("state") !== state) throw new Error("state mismatch - possible interception");

  const code = params.get("code");
  if (!code) throw new Error("no authorization code in the redirect");

  const tokens = await redeem(cfg, {
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri,
    code_verifier: verifier,
    scope: SCOPE,
  });

  await store(tokens);
  return tokens.accessToken;
}

/**
 * Returns a valid access token, refreshing silently when possible.
 * Throws when interactive sign-in is required -- callers surface that to the UI.
 */
export async function getAccessToken(): Promise<string> {
  if (inFlight) return inFlight;

  inFlight = (async () => {
    const tokens = await loadCached();
    if (tokens && tokens.expiresAt > Date.now()) return tokens.accessToken;

    const cfg = await getAuthConfig();
    if (!cfg) throw new Error("not configured - set Client ID and Tenant ID");

    if (tokens?.refreshToken) {
      try {
        const refreshed = await redeem(cfg, {
          grant_type: "refresh_token",
          refresh_token: tokens.refreshToken,
          scope: SCOPE,
        });
        await store(refreshed);
        return refreshed.accessToken;
      } catch {
        // Refresh token expired or revoked - fall through to interactive.
        await signOut();
      }
    }

    throw new Error("sign-in required");
  })();

  try {
    return await inFlight;
  } finally {
    inFlight = null;
  }
}

export async function isSignedIn(): Promise<boolean> {
  const tokens = await loadCached();
  return !!tokens?.refreshToken || (!!tokens && tokens.expiresAt > Date.now());
}
