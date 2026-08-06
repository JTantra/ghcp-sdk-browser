/**
 * declarativeNetRequest header injection for the WebSocket upgrade.
 *
 * The browser WebSocket API gives no way to set request headers, but the
 * Foundry hosted-agent proxy requires `Authorization: Bearer <entra-token>`
 * on the upgrade. DNR's `modifyHeaders` action runs "before request headers
 * are sent" and supports the `websocket` resource type, so we register a
 * session-scoped rule that stamps the header onto exactly our endpoint.
 *
 * Two requirements that are easy to get wrong and both cause a silent 401:
 *  1. `modifyHeaders` needs REAL host permission for the target. The
 *     "declarativeNetRequest" permission alone grants implicit access only to
 *     allow/allowAllRequests/block rules.
 *  2. The host permission must cover the `wss://` scheme, not just `https://`.
 *
 * Session rules (not dynamic) are deliberate: they live in memory only, so a
 * bearer token never lands on disk in the ruleset.
 */

const RULE_ID = 1;

/**
 * Installs/replaces the rule that adds the bearer token to upgrades for `wssUrl`.
 * Scoped by host so the token is only ever sent to our agent endpoint.
 */
export async function installAuthHeaderRule(wssUrl: string, token: string): Promise<void> {
  const { host } = new URL(wssUrl);

  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: [RULE_ID],
    addRules: [
      {
        id: RULE_ID,
        priority: 1,
        action: {
          type: chrome.declarativeNetRequest.RuleActionType.MODIFY_HEADERS,
          requestHeaders: [
            {
              header: "Authorization",
              operation: chrome.declarativeNetRequest.HeaderOperation.SET,
              value: `Bearer ${token}`,
            },
          ],
        },
        condition: {
          // Match on host only. Anchoring the full URL with a scheme prefix and
          // a query string is brittle -- urlFilter has its own syntax where
          // '|' anchors and '?' is literal, which silently fails to match.
          requestDomains: [host],
          resourceTypes: [chrome.declarativeNetRequest.ResourceType.WEBSOCKET],
        },
      },
    ],
  });
}

export async function clearAuthHeaderRule(): Promise<void> {
  await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: [RULE_ID] });
}

/**
 * Reports whether the rule is actually registered and whether the extension
 * really holds host permission for the endpoint. Both are silent failure modes
 * that surface only as an opaque 401 on the upgrade, so we check explicitly and
 * surface the reason instead of letting the operator guess.
 */
export async function diagnoseAuthRule(wssUrl: string): Promise<string[]> {
  const notes: string[] = [];
  const { host, origin } = new URL(wssUrl);

  const rules = await chrome.declarativeNetRequest.getSessionRules();
  const rule = rules.find((r) => r.id === RULE_ID);
  notes.push(rule ? `DNR rule installed for ${host}` : "DNR rule MISSING");

  // https:// is the form Chrome evaluates host permissions against.
  const httpsOrigin = `${origin.replace(/^wss:/, "https:")}/`;
  const granted = await chrome.permissions.contains({ origins: [httpsOrigin] });
  notes.push(
    granted
      ? `host permission granted for ${httpsOrigin}`
      : `host permission MISSING for ${httpsOrigin} -- modifyHeaders will not apply`,
  );

  return notes;
}
