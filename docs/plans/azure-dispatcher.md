# Plan: Azure dispatcher for Quanheim patch notes

**Status:** ready to build, once the prerequisites in [Part A](#part-a-prerequisites-done-by-the-repo-owner) are done.
**For:** a new Claude Code cloud session with the Azure DevOps (ADO) MCP connector and the `Quandru/QuanheimModList` repo.

## Why

The patch notes workflow (`.github/workflows/patchnotes.yml` in this repo) has a `*/5 * * * *` schedule. GitHub runs
free public-repo schedules on a best-effort basis, so in practice it ran every 3–6 hours: a Gale sync at 02:57 UTC
on 31 Oct 2026 waited hours for the next run.

The fix is a small Azure Function that asks GitHub to run the workflow (`workflow_dispatch`) every 5 minutes. It
keeps no logic of its own: all checking, writing and posting stays in this repo. If the function stops, the GitHub
schedule is still there as a slower backup.

It lives in its own ADO project, with Bicep for the Azure resources and an ADO pipeline that deploys both.

## Decisions already made

| Topic | Decision | Why |
|---|---|---|
| Infrastructure as code | **Bicep** | Azure-only, no state backend to host or lock. |
| Hosting | **Flex Consumption** (Linux) | Microsoft's current serverless plan. Linux Consumption retires in 2028. Usage sits well inside the monthly free grant. |
| Language | **Python 3.12** (v2 programming model) | Fastest cold start of the options, and the same language as `tools/patchnotes.py`. |
| Function logic | **Dispatch only** | Nothing to keep in step with the repo. |
| GitHub credential | **Fine-grained PAT**, this repo only, **Actions: Read and write** only | Can start workflow runs and nothing else. GitHub emails before it expires. |
| Where the PAT lives | **Key Vault** (RBAC), read by the function's managed identity through a Key Vault reference | No session, pipeline or log ever sees it. The owner puts it in by hand. |
| Pipeline identity | **ADO service connection with workload identity federation**, scoped to one resource group | No secret, no expiry. Separate from the MCP's app registration. |
| Storage auth | **Managed identity** (`AzureWebJobsStorage__accountName`), shared key access off where Flex allows | No storage keys in app settings. |
| Monitoring | Application Insights on a Log Analytics workspace with a small daily cap, plus one alert rule that emails on dispatch failures | Otherwise a dead dispatcher only shows up as slow patch notes. |

## Target design

```
Azure (resource group rg-quanheim-patchnotes)
┌────────────────────────────────────────────────────────────────────────────┐
│ Function App (Flex Consumption, Python 3.12, system-assigned identity)     │
│   timer "0 */5 * * * *" ──► POST api.github.com/repos/Quandru/              │
│                               QuanheimModList/actions/workflows/            │
│                               patchnotes.yml/dispatches  {"ref":"main"}     │
│   app setting GITHUB_DISPATCH_TOKEN = @Microsoft.KeyVault(...)              │
│                                                                            │
│ Key Vault (RBAC)          secret github-dispatch-token  (set by owner)     │
│ Storage account           host storage + Flex deployment container         │
│ Log Analytics + App Insights   alert rule ──► action group ──► email       │
└────────────────────────────────────────────────────────────────────────────┘
                     ▲ deploys (Bicep + code)
ADO project Quanheim / repo patchnotes-dispatcher / pipeline (service connection, WIF)
```

Role assignments, all made by the Bicep:

| Who | Role | Scope |
|---|---|---|
| Function App identity | Key Vault Secrets User | Key Vault |
| Function App identity | Storage Blob Data Owner (plus the queue/table roles Microsoft's Flex samples assign) | Storage account |
| Owner (`ownerObjectId` parameter) | Key Vault Secrets Officer | Key Vault |

## Part A: prerequisites (done by the repo owner)

Do these before starting the build session. Never paste a token, secret or password into a Claude chat.

### A1. Azure

1. In the personal tenancy's subscription, create the resource group `rg-quanheim-patchnotes` in **UK South**.
   Check Flex Consumption is offered there (`az functionapp list-flexconsumption-locations -o table`). If not,
   pick the nearest region that offers it.
2. Note your own **Object ID** (Entra admin center → Users → you). It's the Bicep's `ownerObjectId`, so you can
   put the GitHub token into Key Vault. Not secret.

### A2. Azure DevOps

1. In the existing ADO org (Entra-backed), create the project **Quanheim** (private).
2. In it, create an **empty** Git repo **patchnotes-dispatcher**: no README and no .gitignore, so the first push is
   clean. The MCP can't create repos.
3. Create the service connection: **Project settings → Service connections → New → Azure Resource Manager →
   Workload identity federation (automatic)**.
   - Scope: **Resource group** `rg-quanheim-patchnotes`
   - Name: **`sc-quanheim-patchnotes`**
   - Leave "Grant access permission to all pipelines" **off**. The first pipeline run asks you to approve it once.
4. That service connection gets **Contributor** on the resource group. The Bicep also creates role assignments,
   so give its service principal one more role:
   - On the service connection, select **Manage Service Principal** and note its name.
   - Resource group → **Access control (IAM) → Add role assignment → Role Based Access Control Administrator**.
     Assign it to that service principal.
   - Recommended: under **Conditions**, constrain it to assigning only *Key Vault Secrets User*, *Key Vault Secrets
     Officer* and the *Storage … Data* roles.

### A3. App registration for the ADO MCP (claude.ai connector)

The remote ADO MCP server (`https://mcp.dev.azure.com/{org}`) signs in with Entra ID. Claude.ai can't register
itself with Entra automatically, so it needs a pre-made app registration. Microsoft documents this for Claude Code
on a local machine, and for Cursor's cloud agents with a web redirect and client secret. This is the cloud
equivalent for claude.ai.

1. Entra admin center → **Enterprise applications**: confirm **Azure DevOps MCP** (app ID
   `2a72489c-aab2-4b65-b93a-a91edccf33b8`) is present in the tenant. If it isn't, follow Microsoft's
   troubleshooting page ("Can't find the Azure DevOps MCP enterprise application in the tenant").
2. **App registrations → New registration**: name `claude-ai-ado-mcp`, single tenant.
3. **Authentication → Add a platform/redirect URI → Web**, and add both:
   - `https://claude.ai/api/mcp/auth_callback`
   - `https://claude.com/api/mcp/auth_callback`
4. **Certificates & secrets → New client secret**. Give it a short expiry (30–90 days; it's only needed for this
   build). Copy the value; it's shown once.
5. **API permissions → Add a permission → APIs my organization uses → Azure DevOps MCP** → delegated permissions.
   Least privilege for this build:
   - read access to projects, code and builds
   - **build: read & execute** (to create and run the pipeline)

   Skip work items, wiki, test plans and Advanced Security. The exact names are in the picker; choose the closest
   matches.
6. **Grant admin consent**.
7. In claude.ai: **Settings → Connectors → Add custom connector**:
   - Name: `Azure DevOps`
   - URL: `https://mcp.dev.azure.com/{org}`
   - **Advanced settings**: OAuth Client ID = the app's Application (client) ID; Client Secret = the value from step 4.

   Then **Connect** and sign in with your Entra account. The connector acts as you, so it can only do what both
   the scopes and your ADO permissions allow.

**If the connector won't connect** (Microsoft lists claude.ai as unsupported for its automatic sign-in, and this
pre-registered route isn't in their docs): fall back to running the build session **locally** in VS Code with the
ADO MCP you already use. Nothing else in this plan changes.

### A4. Git access to ADO for the cloud session

The MCP has no tools to push code, so git needs its own credential.

1. In ADO, create a **PAT** scoped to this one organization with **Code: Read & write** only. Set a 7–30 day expiry.
2. In the Claude environment (session title bar → environment → **Edit**):
   - **Network access:** allow `dev.azure.com` (and `ssh.dev.azure.com` only if you'd rather use SSH).
   - **Credential:** add the PAT under **Network secrets** for host `dev.azure.com` if that section is offered.
     Otherwise add it as the environment variable **`ADO_GIT_PAT`**.
3. Start a **new** session after this, because connectors and environment settings are read at session start.

### A5. GitHub token for the function (after the first deploy)

1. GitHub → **Settings → Developer settings → Fine-grained tokens → Generate**:
   - Resource owner: Quandru. Repository access: **only `QuanheimModList`**.
   - Permissions: **Actions: Read and write** (Metadata: read is added automatically). Nothing else.
   - Expiry: up to a year. GitHub emails before it expires.
2. Once the pipeline has created the Key Vault: Azure portal → Key Vault → **Secrets → Generate/Import**.
   Name **`github-dispatch-token`**, value = the token.

## Part B: build steps (for the Claude session)

Work in order. Each phase ends with a check. Report blockers to the owner instead of working around them, and
never print or ask for secret values.

### B0. Orient

- Read this plan, `README.md` and `.github/workflows/patchnotes.yml` in `Quandru/QuanheimModList`.
- With the ADO MCP: `core_list_projects` to find **Quanheim**, then `repo_repository` → `list`/`get` to confirm
  **patchnotes-dispatcher** exists and is empty.
- Confirm git access: `git ls-remote https://dev.azure.com/{org}/Quanheim/_git/patchnotes-dispatcher`. Use
  `ADO_GIT_PAT` through a credential helper or extra header, never in the remote URL or a committed file.

### B1. Write the repo

```
patchnotes-dispatcher/
├── README.md                  what it is, how to deploy, how to renew the GitHub token
├── azure-pipelines.yml
├── infra/
│   ├── main.bicep
│   └── main.bicepparam        location, baseName, ownerObjectId, alertEmail, githubRepo
└── src/
    ├── function_app.py
    ├── host.json
    └── requirements.txt       azure-functions only
```

**`src/function_app.py`** (about 30 lines, Python v2 model):
- One timer trigger, schedule `0 */5 * * * *`, `run_on_startup=False`, `use_monitor=False`.
- Reads `GITHUB_DISPATCH_TOKEN`, `GITHUB_REPO` (default `Quandru/QuanheimModList`), `GITHUB_WORKFLOW`
  (default `patchnotes.yml`) and `GITHUB_REF` (default `main`).
- `POST https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches` with JSON
  `{"ref": "main"}` and headers:
  - `Authorization: Bearer <token>`
  - `Accept: application/vnd.github+json`
  - `X-GitHub-Api-Version: 2022-11-28`
  - a `User-Agent`

  Use `urllib.request` from the standard library; no `requests`. A `204` is success.
- If the token is missing, or still the unresolved `@Microsoft.KeyVault(...)` string (it is until A5 is done),
  log a **warning** and return. Don't raise, so it doesn't alert before setup is finished.
- Any other non-204 response: log an **error** with the status and GitHub's message (never the token), then
  raise. The alert rule keys off these errors.

**`src/host.json`**: version 2.0. Log level `Warning` by default and `Information` for `Function`. Sampling on.
The aim is a few MB a month.

**`infra/main.bicep`**, following Microsoft's Flex Consumption Bicep samples
(`Azure-Samples/azure-functions-flex-consumption-samples`):
- Names from `baseName` plus `uniqueString(resourceGroup().id)`. Storage and Key Vault have name length limits.
- Log Analytics workspace: `dailyQuotaGb: 1` and 30-day retention. Application Insights on it.
- Storage account: StorageV2, TLS 1.2, no public blob access. `allowSharedKeyAccess: false` if the Flex deployment
  path works with it, otherwise note why not. A blob container for the Flex deployment package.
- Plan: `Microsoft.Web/serverfarms`, sku `FC1` / tier `FlexConsumption`, `reserved: true`.
- Function App: `kind: functionapp,linux`, system-assigned identity, `httpsOnly`.
  - `functionAppConfig.runtime`: `python` / `3.12`.
  - `scaleAndConcurrency`: `instanceMemoryMB: 512`, `maximumInstanceCount` = the minimum allowed. One timer
    needs one instance.
  - Deployment storage: the blob container, authenticated by system-assigned identity.
  - App settings:
    - `AzureWebJobsStorage__accountName`
    - `APPLICATIONINSIGHTS_CONNECTION_STRING`
    - `GITHUB_REPO`, `GITHUB_WORKFLOW`, `GITHUB_REF`
    - `GITHUB_DISPATCH_TOKEN = @Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/github-dispatch-token/)`
      (no version, so it follows rotations).
- Key Vault: RBAC authorization, soft delete on, purge protection off (it's a personal sandbox; say so in a comment).
- The role assignments in the table above, with `guid()` names so redeploys are idempotent.
- Action group (email `alertEmail`), plus a scheduled query alert on App Insights.
  - Condition: any `traces`/`exceptions` from the dispatcher at severity Error or above in the last 30 minutes.
  - Evaluate every 15 minutes, auto-mitigate on.
- Outputs: function app name, Key Vault name, Key Vault URI.

**`azure-pipelines.yml`**:
- Trigger on `main`, for changes under `infra/`, `src/` and `azure-pipelines.yml`. Plus a manual run.
- `pool: vmImage: ubuntu-latest`.
- Variables: `serviceConnection: sc-quanheim-patchnotes`, `resourceGroup: rg-quanheim-patchnotes`.
- **Validate** stage: `AzureCLI@2` runs `az bicep build` and `az deployment group what-if` (the what-if output is
  printed in the log).
- **Deploy** stage:
  - `AzureCLI@2` runs `az deployment group create` with the `.bicepparam` file. Capture its outputs into
    pipeline variables.
  - Package `src/`: `pip install -r requirements.txt --target .python_packages/lib/site-packages`, then zip.
  - Deploy the zip with `AzureFunctionApp@2` (which supports Flex Consumption) or
    `az functionapp deployment source config-zip`. Use whichever works first time on Flex, and note it in the README.
- Pin task major versions. No secrets in the YAML.

Check: `az bicep build` is clean. The pipeline YAML parses. `function_app.py` passes a local unit test with a
mocked HTTP call: 204 succeeds, the unresolved Key Vault string warns, 401 raises.

### B2. First push

`git init -b main`, commit as the owner's choice of author name, push to the ADO repo. Then confirm with
`repo_file` → `list_directory`.

### B3. Create and run the pipeline (MCP)

- `pipelines_write` → `create_pipeline`: name `patchnotes-dispatcher`, repo `patchnotes-dispatcher`, path
  `/azure-pipelines.yml`.
- `pipelines_write` → `run_pipeline`. The first run waits for the owner to **approve the service connection**
  (the "Permit" prompt on the run page); tell them.
- Follow it with `pipelines_run` → `get` and `pipelines_build_log` → `get_content`. Fix and re-push until it's
  green. Expect "Key Vault reference unresolved" warnings in the function logs until A5.

Check: both stages green. Resources exist. The function app shows the `timerDispatch` function.

### B4. Hand over A5, then verify end to end

- Ask the owner to do A5 (create the token, put it in Key Vault). The Key Vault reference picks up within about
  a day. Restarting the Function App picks it up at once.
- Check `https://api.github.com/repos/Quandru/QuanheimModList/actions/runs?event=workflow_dispatch&per_page=5`
  (anonymous works, the repo is public). Expect a new run about every 5 minutes, triggered by the token owner.
- After roughly 30 minutes, confirm the runs are steady and green.

### B5. Tidy the GitHub side (in `Quandru/QuanheimModList`, commit straight to `main`)

- Change the workflow's schedule from `*/5 * * * *` to an hourly backup, `17 * * * *`. Comment that Azure
  dispatches every 5 minutes and the schedule is only a fallback.
- README "Automation" section: one paragraph on the dispatcher, with a link to the ADO repo (it's private, so just
  name it), and add the GitHub token to the list of things that expire.
- Mark this plan **Status: done** with the date, and link the ADO repo name.

### B6. Hand back to the owner

- Delete or let expire the A3 client secret and the A4 ADO PAT. They were only for the build.
- Keep the connector if it's still useful. Otherwise remove it.
- Reminders that remain: the GitHub token's expiry (GitHub emails), and the Claude token's
  (`CLAUDE_CODE_OAUTH_TOKEN_EXPIRES`, existing issue alert).

## Gotchas

- **The workflow's `concurrency` group queues, it doesn't cancel.** Dispatches that arrive while a run is in
  progress (a Claude highlight run takes a minute or two) collapse into one pending run. That's harmless.
- **Key Vault references** cache for up to 24 hours. Restart the app after rotating the token if it can't wait.
- **Flex Consumption** doesn't support every region, and its Bicep shape (`functionAppConfig`) differs from
  classic Consumption. Follow the official samples, not older blog posts.
- **Service connection approval:** the first pipeline run pauses for it. That's expected, not a failure.
- **The GitHub token** must belong to an account with write access to `QuanheimModList`. A dispatch made with it
  shows that user as the run's actor.
- **Costs to watch:** Log Analytics ingestion (capped at 1 GB a day, expected to be a few MB a month), the storage
  account (pence) and Key Vault operations (pence). The function itself sits within the Flex free grant.

## Out of scope

- Moving the patch notes logic into Azure. It stays in GitHub Actions.
- A GitHub App instead of a PAT. Possible later; it swaps a yearly renewal for key handling and more code.
