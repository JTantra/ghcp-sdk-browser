# ghcp-sdk-browser: new-subscription migration

## Verified target

- Subscription: `bd15c49d-3894-4d25-a9ec-d07c06ada81a` (ASEANInnovationHub_JohandyTantra)
- Tenant: `2270b307-46e9-4e9a-8b44-353d5523f75f`
- Resource group: `jtantra-test-rg`
- Foundry account / project: `jtantra-test-foundry2` / `jtantra-test-foundry2`
- Hosted agent: `ghcp-sdk-browser`, version **5**, status **active**
- Image: `jtantratestacr.azurecr.io/ghcp-sdk-browser/ghcp-sdk-browser-ghcp-newsub:azd-deploy-1788792106`
- Local azd environment: `ghcp-newsub` (selected); `ghcp-poc` is retained unchanged for rollback.

Invocations endpoint:

```text
https://jtantra-test-foundry2.services.ai.azure.com/api/projects/jtantra-test-foundry2/agents/ghcp-sdk-browser/endpoint/protocols/invocations?api-version=v1
```

## Work performed

1. Compared the live source and target. Source version 28 was active; target version 4 was failed with registry authentication errors.
2. Granted `AcrPull` and `Container Registry Repository Reader` on `jtantratestacr` to the actual **project** managed identity `5d2b3887-85df-4949-8d77-3e66e1d415de`. The previously granted **account** identity was different.
3. Published and read back the three application skills: `recovery-playbook`, `report-format`, `risk-scoring`. The unrelated source-project `travel-guide` skill was not part of this app migration.
4. Stored the existing local GitHub credential in target connection `ghcp-browser-secrets` (`CustomKeys`, field `github_token`). The target agent definition contains only `${{connections.ghcp-browser-secrets.credentials.github_token}}`, not the credential. A real Copilot turn verified it resolves at runtime. Credential rotation was not performed.
5. Rebuilt from source with azd into the target registry; read back the deployed version, image, status, skill endpoint, and secret reference.
6. Created a new single-tenant SPA registration and service principal with the old registration's redirect URI and Foundry delegated scope. No tenant-wide consent was granted.
7. Parameterized the Foundry project endpoint in `azure.yaml`; excluded local Copilot state and environment files from deployment packages.
8. Updated extension defaults and built with `tsc --noEmit && vite build`. Checked emitted JavaScript contains only the new endpoint/client/tenant defaults. Kept the manifest public key unchanged to retain the extension ID.

## Verification completed

- Live `mode=info`: `skills_source.source=foundry`, `usable=true`, all three skills listed, no skill-fetch error.
- Telemetry diagnostic reports `application-insights`; delivery/correlation in the portal is not yet verified.
- Existing hosted SSE integration test: **5/5 PASS** (attach, turn started, tool call, separate tool-result POST accepted, turn finished).
- This test uses a **simulated browser tool response**, with a **real hosted agent / Copilot model turn**. It does not prove the installed extension's browser tool execution or interactive Entra sign-in.
- TypeScript/Vite build succeeded; `git diff --check` passed.

## Browser cutover (operator step)

The current extension persists `backendUrl`, `clientId`, `tenantId`, `tokenSet`, and `sessionId` in `chrome.storage.local`. Saved values override the new compiled defaults. **Reloading alone is not enough.** The current Save handler does not clear a token/session from the old tenant.

1. While still configured to the old backend, finish any active work and use **New session** to release the old session and clear its saved ID. Disconnect if needed.
2. **Sign out** to clear the cached access/refresh token.
3. Load/reload the supplied extension build. Its manifest key preserves extension ID `fcgochpbdceoooiilalcpldnlkhlbcie`.
4. In Setup, set and save all three values:
   - Backend: the new invocations endpoint above.
   - Client ID: `cc1880a7-aa8d-49fd-8536-e3042a2fd146`
   - Tenant ID: `2270b307-46e9-4e9a-8b44-353d5523f75f`
5. Sign in with the new-tenant account. Consent may be requested; tenant policy may require an administrator. User access to the new Foundry project is still required.
6. Connect a fresh session; open the local demo at `http://localhost:5055/` and run a browser task. Confirm the displayed tool/skill catalogue and actual page interaction.

Registered SPA redirect URI: `https://fcgochpbdceoooiilalcpldnlkhlbcie.chromiumapp.org/`.

## Deployment precautions

Use the existing target infrastructure. **Do not run `infra/deploy.sh`, `azd provision`, `azd up`, or `azd down` blindly against this shared project.** The old helper provisions resources and writes a raw token setting.

The installed azd/agent extension initially resolved the old default environment during packaging despite `--environment ghcp-newsub`. Selecting the new environment and explicitly passing its target environment variables corrected this. Recheck the printed subscription/project before any future deployment.

azd and az have separate auth. This migration temporarily used `azd config set auth.useAzCliAuth true` with the already authenticated new-tenant Azure CLI account; the previous global setting was restored afterwards. For another deploy, sign azd into the new tenant or deliberately enable Azure CLI authentication again.

## Not migrated / not yet verified

- Browser interactive sign-in and end-to-end browser execution: operator cutover above remains.
- GitHub Actions: verified full gated run [ci-16](https://github.com/JTantra/ghcp-sdk-browser/actions/runs/34137016306), commit `416bbb1`, against the new project. All 10 cases produced responses; zero harness errors; all five aggregate CI gates passed. The dedicated `ghcp-sdk-browser-evals` identity has account-scoped Azure AI Developer and Cognitive Services OpenAI User, plus project-scoped **Foundry Agent Consumer** for endpoint interaction. The first run failed with 403 because neither older role includes `AIServices/endpoints/interact/action`; do not treat that run's empty-response scores as quality results. `NEWSUB_*` repo variables leave the old master configuration untouched. Changes are on `feat/foundry-evals`, not merged to master. The CI client is separate from the extension SPA registration.
- Foundry publishing: `evalrun_a1f201de4a954139bfff27089ce7a83c` under `eval_0351ecdee9204234be4c964152d72a20` was read back as completed with 10 results (6 passed, 4 failed, 0 errored). This per-row roll-up differs from the aggregate CI gate. Existing evaluator conversation/response parsing-fallback warnings remain; do not describe this run as every metric passing.
- Existing test/telemetry/publishing helpers contain old hardcoded defaults; supply explicit target configuration or update each helper before using it. In particular, `scripts/publish_skills.py` defaults to the old project and obtains its token from the current az subscription.
- Historical sessions, chats, evaluations and trace history: not copied.
- Subagent evaluation suite: rerun in GitHub Actions (above). Portal trace correlation is still unverified.
- No source resources, agents, app registrations, or skills were deleted.

## Rollback

The old deployment remains in `jtantra-test-foundry` (old subscription `98517cf5-e3e8-4d6e-9e54-a93d702df3d9`). Browser rollback uses a fresh session and sign-out, followed by:

- Backend: `https://jtantra-test-foundry.services.ai.azure.com/api/projects/jtantra-test-foundry/agents/ghcp-sdk-browser/endpoint/protocols/invocations?api-version=v1`
- Client ID: `509df672-ebb9-4ab5-91d6-5c19996313c1`
- Tenant ID: `92def590-0f4c-43ab-be1e-493d616e2725`

Do not delete the old deployment until the browser cutover and any required eval/telemetry checks pass.
