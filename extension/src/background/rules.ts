/**
 * declarativeNetRequest header injection for the WebSocket upgrade.
 *
 * The browser WebSocket API gives no way to set request headers, but the
 * Foundry hosted-agent proxy requires `Authorization: Bearer <entra-token>`
 * on the upgrade. DNR's `modifyHeaders` action runs "before request headers
 * are sent" and supports the `websocket` resource type, so we register a
 * session-scoped rule that stamps the header onto exactly our endpoint.
 *
 * Session rules (not dynamic) are deliberate: they live in memory only, so a
 * bearer token never lands on disk in the ruleset.
 */

const RULE_ID = 1;

/**
 * Installs/replaces the rule that adds the bearer token to upgrades for `wssUrl`.
 * Scoped by urlFilter to our agent endpoint so we never leak the token elsewhere.
 */
export async function installAuthHeaderRule(wssUrl: string, token: string): Promise<void> {
  const { host, pathname } = new URL(wssUrl);

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
          // Pin to host + path so the token is only ever sent to our agent.
          urlFilter: `|wss://${host}${pathname}`,
          resourceTypes: [chrome.declarativeNetRequest.ResourceType.WEBSOCKET],
        },
      },
    ],
  });
}

export async function clearAuthHeaderRule(): Promise<void> {
  await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: [RULE_ID] });
}
