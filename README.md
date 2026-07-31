# Rice Price Scraper — Kota Tasikmalaya

Automated daily scraper that pulls rice prices from PIHPS (Bank Indonesia)
for Kota Tasikmalaya and stores them into Supabase.

## How it works

- Runs twice a day via GitHub Actions:
  - **13:00 WIB** — primary attempt
  - **16:00 WIB** — retry attempt, forward-fills the last known price if BI
    still hasn't published today's data (weekends, national holidays, etc.)
- Data is stored in the `rice_prices` table, related to `rice_types` via
  `rice_type_id`.

## Setup

1. Add these repository secrets under **Settings → Secrets and variables → Actions**:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
2. Push to `main` — the workflow at `.github/workflows/scrape-rice-price.yml`
   will pick up the schedule automatically.

## Manual testing

Trigger manually from the **Actions** tab → **Scrape Daily Rice Price** →
**Run workflow**. Choose `allow_fallback = true` to test the retry/forward-fill
behavior, or `false` to test the primary-run behavior.

## Local testing

\`\`\`bash
pip install -r requirements.txt

export SUPABASE_URL="https://xxxxx.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="your-service-role-key"

python scrape_rice_price.py                # primary run behavior
python scrape_rice_price.py --allow-fallback # retry/fallback behavior
\`\`\`

## Related table schema

\`\`\`sql
create table rice_types (
  id smallint primary key,
  name text not null unique
);

create table rice_prices (
  id bigint generated always as identity primary key,
  date date not null,
  rice_type_id smallint references rice_types(id),
  price numeric not null,
  created_at timestamptz default now(),
  unique (date, rice_type_id)
);
\`\`\`