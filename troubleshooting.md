Run these queries in Grafana Explore with your **Loki datasource** selected. Set time range to **Last 90 days**. Switch to **Table view** for easier reading.

---

**Step 1 — Confirm base stream exists and has data:**
```logql
{enterprise="sherwin-williams", log_source="copilot_exporter"} | json
```
Expected: log lines appearing with parsed JSON fields. If nothing returns, Loki has no data at all — stop here.

---

**Step 2 — Confirm `ai_credits_used` and `ai_credits_usd` are populated:**
```logql
{enterprise="sherwin-williams", log_source="copilot_exporter"}
| json
| ai_credits_used != ""
| line_format "user={{.user_login}} credits={{.ai_credits_used}} usd={{.ai_credits_usd}}"
```
Expected: lines showing usernames with credit values. If most show `0` that is fine — it means those users used standard models only.

---

**Step 3 — Confirm `used_agent`, `used_chat`, `used_cli` boolean flags exist:**
```logql
{enterprise="sherwin-williams", log_source="copilot_exporter"}
| json
| line_format "user={{.user_login}} chat={{.used_chat}} agent={{.used_agent}} cli={{.used_cli}}"
```
Expected: lines showing true/false values for each flag per user.

---

**Step 4 — Confirm `totals_by_model_feature` array is populated:**
```logql
{enterprise="sherwin-williams", log_source="copilot_exporter"}
| json
| totals_by_model_feature != "[]"
| line_format "user={{.user_login}} models={{.totals_by_model_feature}}"
```
Expected: lines showing the model/feature breakdown array per user. If all show `[]` the model breakdown is not being populated by the GitHub API.

---

**Step 5 — Confirm CLI token fields exist:**
```logql
{enterprise="sherwin-williams", log_source="copilot_exporter"}
| json
| cli_session_count != ""
| line_format "user={{.user_login}} sessions={{.cli_session_count}} output_tokens={{.cli_output_tokens_sum}}"
```
Expected: lines for users who used CLI. If nothing returns, no users have used CLI.

---

**Step 6 — Confirm IDE version detail exists:**
```logql
{enterprise="sherwin-williams", log_source="copilot_exporter"}
| json
| totals_by_ide != "[]"
| line_format "user={{.user_login}} ides={{.totals_by_ide}}"
```
Expected: lines showing the IDE breakdown array including version detail per user.

---

**Step 7 — Confirm chat panel mode breakdown exists:**
```logql
{enterprise="sherwin-williams", log_source="copilot_exporter"}
| json
| chat_panel_agent_mode != ""
| line_format "user={{.user_login}} agent_mode={{.chat_panel_agent_mode}} ask_mode={{.chat_panel_ask_mode}} edit_mode={{.chat_panel_edit_mode}}"
```
Expected: lines showing chat mode counts per user.

---

**Step 8 — Confirm metric queries work (needed for aggregation panels):**

Switch query type to **Metric** in Explore and run:
```logql
sum by (user_login) (
  sum_over_time(
    {enterprise="sherwin-williams", log_source="copilot_exporter"}
    | json
    | unwrap ai_credits_used
    [$__range]
  )
)
```
Expected: a list of users with their total credit spend summed. This confirms LogQL metric queries work against your Loki — required for the spend leaderboard and aggregation panels.

---

**Step 9 — Confirm user count per feature is queryable:**
```logql
sum by (user_login) (
  count_over_time(
    {enterprise="sherwin-williams", log_source="copilot_exporter"}
    | json
    | used_agent="true"
    [$__range]
  )
)
```
Expected: list of users who used agent features with day counts. Confirms boolean field filtering works.

---

**Step 10 — Check how many distinct users are in Loki:**
```logql
count(
  sum by (user_login) (
    count_over_time(
      {enterprise="sherwin-williams", log_source="copilot_exporter"}
      | json
      [$__range]
    )
  )
)
```
Expected: a number close to your total seat count. If significantly lower, some users are missing from Loki — may be the stream limit issue from earlier.

---

**What to report back:**

For each step tell me:
- Returns data / Returns nothing / Returns an error
- For step 4 — whether `totals_by_model_feature` shows actual model names or empty arrays
- For step 8 — whether the metric query returns values or an error
- For step 10 — the approximate user count number

That tells me exactly which of the 5 dashboards I described are buildable with your current data.