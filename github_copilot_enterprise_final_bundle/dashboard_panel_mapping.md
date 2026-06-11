# Dashboard Panel Mapping — Enterprise-Only Standard

## 1. Executive Overview
Purpose: give leadership one place to see adoption, code impact, and billing at a glance.

### Top row
- Enterprise Seat Total → `github_copilot_enterprise_seat_total`
- Active Seats 28d → `github_copilot_enterprise_seat_active_last_28d_total`
- Latest DAU → `github_copilot_daily_active_users`
- Latest MAU → `github_copilot_monthly_active_users`
- AI Credits Cost (range) → `github_copilot_billing_ai_credit_total_net_amount_usd`
- Premium Request Cost (range) → `github_copilot_billing_premium_request_total_net_amount_usd`

### Trends
- Adoption Trend → DAU / WAU / MAU
- Code Impact Trend → added / deleted / suggested lines
- Billing Trend → AI credit net amount + premium request net amount

## 2. Usage & Adoption
Purpose: show how broadly and deeply Copilot is being used.

### KPIs
- Monthly Active Chat Users → `github_copilot_monthly_active_chat_users`
- Monthly Active Agent Users → `github_copilot_monthly_active_agent_users`
- Daily Active CLI Users → `github_copilot_daily_active_cli_users`
- Prompts per Active User → derived from `github_copilot_user_initiated_interaction_count` / DAU
- Acceptance Rate → code acceptance / code generation

### Breakdowns
- Feature Breakdown → `github_copilot_feature_*`
- IDE Breakdown → `github_copilot_ide_*`
- Language Breakdown → `github_copilot_language_feature_*`
- Model Breakdown → `github_copilot_user_model_feature_*`

## 3. Code Generation & Delivery Impact
Purpose: show whether Copilot is driving useful code outcomes.

### KPIs
- Code Generation Activity → `github_copilot_code_generation_activity_count`
- Code Acceptance Activity → `github_copilot_code_acceptance_activity_count`
- Lines Added → `github_copilot_loc_added_sum`
- Lines Deleted → `github_copilot_loc_deleted_sum`
- PRs Created → `github_copilot_pr_total_created`
- PRs Merged → `github_copilot_pr_total_merged`

### Trends
- Generation vs Acceptance
- PR lifecycle
- Median time to merge
- Copilot-authored PR activity

## 4. Billing, Seats, Teams & Cohorts
Purpose: unify financial and organizational visibility.

### Billing
- AI Credit totals and detailed breakdowns
- Premium Request totals and detailed breakdowns
- Billing usage summary

### Seats
- Total seats
- Active seats 7d / 28d / 90d
- Never active seats
- Pending cancellation seats
- Seat plan totals
- Assigning team totals

### Teams / Cohorts
- AI adoption phase totals
- User AI adoption phase
- User-team membership
- Team-level rollups derived from user-team join + user usage

## Guidance
- Do not duplicate a KPI unless one panel is a current snapshot and another is a time trend.
- Keep one overview text panel at the top of each dashboard.
- Keep panel colors aligned with the existing theme.
