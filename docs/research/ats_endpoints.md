# ATS public endpoints for direct company career pages

Research date: 2026-10-30. All endpoints marked **VERIFIED** were called live (unauthenticated, ≤1 req/s per host) during this research; shapes below are the field names actually observed. Items marked *unverified* are from prior knowledge / docs and should be re-checked before relying on them.

Difficulty scale: **easy** = one unauthenticated JSON/XML GET returns all jobs; **medium** = pagination or POST/undocumented params, or HTML with stable structure; **hard** = client-rendered, anti-bot, or no stable feed.

---

## Summary table

| ATS | Endpoint | Method | Pagination | Compensation | Difficulty |
|---|---|---|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | GET | none (all jobs) | per-job `?pay_transparency=true` → `pay_input_ranges` | easy |
| Lever | `api.lever.co/v0/postings/{site}?mode=json` (EU: `api.eu.lever.co`) | GET | `skip`/`limit` (optional) | optional `salaryRange` | easy |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true` | GET | none | structured `compensation` | easy |
| Workable | `apply.workable.com/api/v1/widget/accounts/{sub}` (v3 POST for paging) | GET / POST | v3 `nextPage` token | rarely | easy (rate-limits) |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{id}/postings` | GET | `limit`≤100 + `offset` | rarely | easy |
| Recruitee | `{sub}.recruitee.com/api/offers/` | GET | none | `salary` object | easy |
| Personio | `{sub}.jobs.personio.de/xml` (or `.com`) | GET | none | no | easy |
| Teamtailor | `{sub}.teamtailor.com/jobs.json` / `jobs.rss` | GET | none observed | in JSON-LD if set | easy |
| BambooHR | `{sub}.bamboohr.com/careers/list` (+ `/careers/{id}/detail`) | GET | none | sometimes in detail | easy |
| Breezy | `{sub}.breezy.hr/json` | GET | none | `salary` string | easy |
| JazzHR | `{sub}.applytojob.com/apply` (HTML) | GET | HTML | no | medium/hard |
| Gem | `api.gem.com/job_board/v0/{slug}/job_posts/` | GET | none | in content | easy |
| Workday | `{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` | POST | `limit`≤20 + `offset` | rarely (in description) | medium |
| Eightfold | `{host}.eightfold.ai/api/apply/v2/jobs?domain={domain}&start=&num=` | GET | `start`/`num` (10) | no | medium |
| Oracle Recruiting Cloud | `{host}.fa.{dc}.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions` | GET | `limit`/`offset` in finder | no | medium |
| iCIMS | `careers-{x}.icims.com/jobs/search?ss=1&in_iframe=1&pr={n}` (HTML) | GET | HTML pages | no | hard |
| SuccessFactors (RMK) | `jobs.{company}.com/sitemap.xml`, `/search/?q=` (HTML) | GET | `startrow` | no | hard |
| Taleo | `{x}.taleo.net/careersection/...` | JS app / undocumented REST | — | no | hard |
| HRMOS / HERP / Talentio (JP) | `hrmos.co/pages/{co}/jobs`, `herp.careers/v1/{co}`, `open.talentio.com/r/1/c/{co}/...` | GET (HTML) | HTML | no | medium (HTML) |

---

## Greenhouse — **VERIFIED**

- **List:** `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs` — add `?content=true` for description HTML, departments, offices.
- **Board info:** `GET /v1/boards/{token}` → `{name, content}` (company name + blurb; handy for identity checks).
- **Single job:** `GET /v1/boards/{token}/jobs/{id}?pay_transparency=true` → same fields plus **`pay_input_ranges: [{min_cents, max_cents, currency_type, title, blurb}]`** (verified on Tailscale: CAD 203,680–254,600).
- **Also:** `/v1/boards/{token}/departments`, `/v1/boards/{token}/offices` (tree with `jobs` arrays).
- **Pagination:** none; whole board in one response. `meta.total` = count. `content=true` can make big responses (Stripe ≈ 0.5 MB without content, several MB with).
- **Job fields:** `id`, `internal_job_id`, `title`, `location.name`, `updated_at` (ISO), `first_published`, `absolute_url` (apply/view URL), `requisition_id`, `company_name`, `language`, `application_deadline`, `metadata[] {name, value, value_type}` (custom fields such as Employment Type), with content: `content` (HTML-escaped), `departments[] {id,name,parent_id,child_ids}`, `offices[] {id,name,location}`.
- **Compensation:** not in list; use single-job `pay_transparency=true` (only when the company configured pay ranges). Some companies only put pay in the description HTML (`<div class="pay-range">`).
- **Unknown token:** HTTP 404.
- **EU data residency:** some boards are hosted in Greenhouse EU (job ids ending `…101`, links to `s101.recruiting.eu.greenhouse.io`, board pages on `job-boards.eu.greenhouse.io`). `boards-api.eu.greenhouse.io` is NXDOMAIN and the US API returns 404 for these boards (example: Vinted). For now, scrape the company careers site; worth re-checking later for a public EU board API host.
- **Detection:** `boards.greenhouse.io/{token}`, `job-boards.greenhouse.io/{token}`, `boards.greenhouse.io/embed/job_board?for={token}`, `embed/job_board/js?for=`, `<div id="grnhse_app">`, `?gh_jid=` in job URLs on the company site. Tokens aren't always the company name (`doordashusa`, `realtimeboardglobal` = Miro, `gleanwork`, `sourcegraph91`, `traderepublicbank`, `pulumicorporation`).
- **Difficulty:** easy.

## Lever — **VERIFIED**

- **List:** `GET https://api.lever.co/v0/postings/{site}?mode=json`. EU tenants: `https://api.eu.lever.co/v0/postings/{site}?mode=json`.
- **Params:** `skip`, `limit` (paging is optional; without them you get everything), filters `team`, `department`, `location`, `commitment`, `level`, `group=team`.
- **Single:** `GET /v0/postings/{site}/{id}?mode=json`.
- **Fields (verified):** `id`, `text` (title), `categories {team, department, location, commitment, allLocations[]}`, `country`, `workplaceType` (`onsite|remote|hybrid|unspecified`), `createdAt` (epoch **ms**), `hostedUrl`, `applyUrl`, `description`/`descriptionPlain`, `descriptionBody`/`descriptionBodyPlain`, `opening`/`openingPlain`, `lists[] {text, content}` (requirements, responsibilities), `additional`/`additionalPlain`.
- **Compensation:** optional `salaryRange {min, max, currency, interval}` + `salaryDescription(Plain)` when the company enables it. None seen in the samples checked (Spotify, Palantir), so it's usually absent.
- **Unknown site:** 404 `{"ok":false,"error":"Document not found"}`. Some old boards still exist but return `[]` (for example `mistral`, which moved to Ashby).
- **Detection:** `jobs.lever.co/{site}`, `jobs.eu.lever.co/{site}`. Site names can be case-sensitive in links (`Autify`, `Coda`).
- **Difficulty:** easy.

## Ashby — **VERIFIED**

- **List:** `GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true` → `{apiVersion, jobs[]}`.
- **Pagination:** none.
- **Fields (verified):** `id`, `title`, `department`, `team`, `employmentType` (`FullTime`…), `location`, `secondaryLocations[] {location, address}`, `address.postalAddress {addressLocality, addressRegion, addressCountry}`, `isRemote`, `workplaceType` (`OnSite|Remote|Hybrid`), `publishedAt` (ISO), `isListed`, `jobUrl`, `applyUrl`, `descriptionHtml`, `descriptionPlain`, `shouldDisplayCompensationOnJobPostings`, `compensation`.
- **Compensation (best of all ATSes):** `compensation.compensationTierSummary` ("$211.4K – $290.6K • Offers Equity"), `scrapeableCompensationSalarySummary`, `compensationTiers[].components[] {compensationType: Salary|EquityPercentage|Bonus…, interval: "1 YEAR", currencyCode, minValue, maxValue}`, `summaryComponents[]`. Verified on Ramp. Empty arrays when the company doesn't publish pay.
- **Board names:** case-insensitive in practice (`DeepL`, `Linear`), and **can contain dots**: `mistral.ai`, `taxfix.com`, `checkout.com`. Use the exact segment from `jobs.ashbyhq.com/{board}`.
- **Unknown board:** 404. Some boards exist but are empty (Confluent, Astral, Parity).
- **Detection:** `jobs.ashbyhq.com/{board}`, embed script `jobs.ashbyhq.com/{board}/embed`, `?ashby_jid=` on company sites.
- **Difficulty:** easy.

## Workable — **VERIFIED**

- **v1 widget (simplest):** `GET https://apply.workable.com/api/v1/widget/accounts/{subdomain}` (`?details=true` adds descriptions) → `{name, description, jobs[]}`. Job fields: `title`, `shortcode`, `code`, `employment_type`, `telecommuting`, `department`, `url`, `shortlink`, `application_url`, `published_on`, `created_at`, `country`, `city`, `state`, `locations[]`, `education`, `experience`, `function`, `industry`, `description` (with details).
- **v3 (paged):** `POST https://apply.workable.com/api/v3/accounts/{sub}/jobs` with body `{"query":"","location":[],"department":[],"worktype":[],"remote":[]}` → `{total, results[], nextPage}`. Resend with `"token": nextPage` for the next page. Result fields: `id, shortcode, title, remote, location, locations, state, published, type, department, workplace, language` (verified on Mercari, total=139).
- **Detail:** `GET https://apply.workable.com/api/v2/accounts/{sub}/jobs/{shortcode}` *(unverified)*.
- **Compensation:** rarely published (sometimes in the description, or a `salary` block in v2 detail).
- **Unknown account:** 404 "Not Found". **Caution:** many generic slugs are real but empty accounts (200 with `jobs: []`). Treat 0 jobs as unresolved.
- **Rate limiting:** got **HTTP 429** at roughly 1 req/1.15 s during bursts. Use ≥2–3 s spacing and back off.
- **Detection:** `apply.workable.com/{sub}`, `{sub}.workable.com`, widget script `workable.com/assets/embed.js`.
- **Difficulty:** easy (mind the rate limit).

## SmartRecruiters — **VERIFIED**

- **List:** `GET https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings?limit=100&offset=0` → `{offset, limit, totalFound, content[]}`. Filters: `q`, `country`, `city`, `department`, `language`.
- **Pagination:** `limit` ≤ 100, `offset`. Loop until `offset >= totalFound`.
- **Fields:** `id`, `uuid`, `name` (title), `refNumber`, `company {identifier, name}`, `releasedDate` (ISO), `location {city, region, country, address, remote, hybrid, fullLocation}`, `industry`, `department {label}`, `function {id, label}` (e.g. `engineering`, useful for eng filtering), `typeOfEmployment`, `experienceLevel`, `ref` (detail URL).
- **Detail:** `GET /v1/companies/{id}/postings/{postingId}` → `jobAd.sections {companyDescription, jobDescription, qualifications, additionalInformation}`, `applyUrl`, `postingUrl` *(shape from docs; list verified)*.
- **Compensation:** rarely (custom fields only).
- **Caution:** an unknown company id returns **200 with `totalFound: 0`**, not 404, so you can't tell missing from empty.
- **Detection:** `jobs.smartrecruiters.com/{Company}`, `careers.smartrecruiters.com/{Company}`.
- **Difficulty:** easy.

## Recruitee — **VERIFIED**

- **List:** `GET https://{sub}.recruitee.com/api/offers/` → `{offers[]}`. Single offer: `/api/offers/{slug}` *(unverified)*.
- **Fields:** `id`, `title`, `slug`, `department`, `location`, `city`, `country`, `remote`, `employment_type_code`, `created_at`, `published_at`, `careers_url`, `careers_apply_url`, `company_name`, `description`, `requirements` (HTML), `salary {min, max, currency, period}` (when set), `tags`.
- **Pagination:** none.
- **Unknown:** 404. **Caution:** generic slugs collide (`prisma.recruitee.com` is a Dutch care organisation, not Prisma Data). Check `company_name` / `careers_url`.
- **Detection:** `{sub}.recruitee.com`, custom domains serving `/o/{slug}` job URLs.
- **Difficulty:** easy.

## Personio — **VERIFIED**

- **Feed:** `GET https://{sub}.jobs.personio.de/xml` (some tenants on `.jobs.personio.com`). Optional `?language=en|de`.
- **Shape:** `<workzag-jobs><position>…</position></workzag-jobs>` with `id`, `subcompany`, `office`, `department`, `recruitingCategory`, `name` (title), `jobDescriptions/jobDescription{name,value}`, `employmentType`, `seniority`, `schedule`, `yearsOfExperience`, `keywords`, `occupation`, `occupationCategory`, `createdAt` (verified: everphone, 1komma5grad).
- **Apply URL:** `https://{sub}.jobs.personio.de/job/{id}`.
- **Pagination:** none. **Compensation:** no.
- **Unknown:** 404. Personio itself returns a valid but empty feed (its own careers site is on Phenom).
- **Detection:** `{sub}.jobs.personio.de|com`.
- **Difficulty:** easy.

## Teamtailor — **VERIFIED**

- The official API (`api.teamtailor.com/v1`) needs a company API key. **Not usable.**
- **Public career-site feeds:** `GET https://{sub}.teamtailor.com/jobs.json` (JSON Feed 1.1: `items[] {id, title, url, date_published, content_html, _jobposting}`, where `_jobposting` is a full **schema.org JobPosting** object) and `GET /jobs.rss` (RSS: `title, description, pubDate, link, guid, remoteStatus, tt:locations/tt:location{name,city,country}, tt:department, tt:role, tt:division`). Verified on `career.teamtailor.com` and `embarkstudios`.
- Custom domains (e.g. `careers.company.com`) also serve `/jobs.rss` and `/jobs.json`.
- **Pagination:** none observed. **Compensation:** only if present in the JobPosting `baseSalary`.
- **Detection:** `{sub}.teamtailor.com`, `teamtailor-cdn.com` assets, `<meta name="generator" content="Teamtailor">` *(unverified)*.
- **Difficulty:** easy.

## BambooHR — **VERIFIED (shape)**

- **List:** `GET https://{sub}.bamboohr.com/careers/list` → `{meta: {totalCount}, result[]}`. Result fields: `id`, `jobOpeningName`, `departmentId`, `departmentLabel`, `employmentStatusLabel`, `location {city, state}`, `atsLocation {country, state, province, city}`, `isRemote`, `locationType`. Verified envelope on `toggl` (0 open jobs).
- **Detail:** `GET /careers/{id}/detail` → `result.jobOpening {jobOpeningName, description, compensation, datePosted, ...}` *(unverified)*.
- **Unknown subdomain:** 302 redirect to bamboohr.com, so treat a redirect as not found.
- **Detection:** `{sub}.bamboohr.com/careers`, embed `{sub}.bamboohr.com/js/embed.js`.
- **Difficulty:** easy.

## Breezy HR — **VERIFIED**

- **List:** `GET https://{sub}.breezy.hr/json` → array of `{id, friendly_id, name, url, published_date, type{id,name}, location{country{name,id}, state, city, is_remote, name}, department, salary, company{name, logo_url}}`. Verified on `breezy.breezy.hr`.
- **Pagination:** none. **Compensation:** `salary` free-text when set.
- **Detection:** `{sub}.breezy.hr`.
- **Difficulty:** easy.

## JazzHR — **partially verified**

- **Career page:** `https://{sub}.applytojob.com/apply` (HTML list; jobs at `/apply/{code}/{slug}`). Pages are server-rendered with consistent markup, and job pages usually carry JSON-LD `JobPosting`.
- **Legacy XML feed** `https://app.jazz.co/feeds/export/jobs/{sub}` returned **410 Gone** in testing. The real API (`api.resumatorapi.com/v1/jobs?apikey=`) needs a key.
- An inactive account shows the title "JazzHR - Inactive Career Page".
- **Detection:** `{sub}.applytojob.com`, `app.jazz.co`.
- **Difficulty:** medium (HTML scrape + JSON-LD).

## Gem — **VERIFIED**

- **List:** `GET https://api.gem.com/job_board/v0/{slug}/job_posts/` → array in a **Greenhouse-compatible shape** (`absolute_url`, `title`, `content`, `location`, `departments`, `offices`, `updated_at`, …). Verified on Retool (26 jobs) and Groq (39).
- **Detection:** `jobs.gem.com/{slug}`.
- **Difficulty:** easy. You can reuse the Greenhouse parser.

## Workday (myworkdayjobs.com) — **VERIFIED**

- **List:** `POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` with headers `Content-Type: application/json`, `Accept: application/json` and body `{"appliedFacets":{}, "limit":20, "offset":0, "searchText":""}`.
- **Response:** `{total, jobPostings[], facets[], userAuthenticated}`. Posting fields: `title`, `externalPath` (`/job/{loc}/{slug}_{reqId}`), `locationsText`, `postedOn` (**relative text** like "Posted 30+ Days Ago" or "Posted Today", not a date), `bulletFields[]` (usually the req id).
- **Pagination:** `limit` **max 20** (limit=50 → HTTP 400). Step `offset`. NVIDIA reports `total: 2000`, which looks like a cap, so for large tenants partition with `searchText` or facets. *Reported but not verified here:* on some tenants `total` is only correct on the first page.
- **Facets:** `jobFamilyGroup`, `workerSubType`, `timeType`, `locationMainGroup`, etc. Pass `{"appliedFacets":{"jobFamilyGroup":["<id>"]}}` to filter to engineering server-side. `searchText:"software engineer"` also works (NVIDIA: 1,787).
- **Detail:** `GET https://{host}/wday/cxs/{tenant}/{site}{externalPath}` → `jobPostingInfo {id, title, jobDescription (HTML), location, postedOn, startDate (ISO), timeType, jobReqId, jobPostingId, country{descriptor}, jobRequisitionLocation, externalUrl, canApply}`, plus `hiringOrganization`, `similarJobs`.
- **Compensation:** none structured. US pay ranges are often in `jobDescription` text.
- **Discovering `{tenant}`, `wd{N}`, `{site}`:** taken from the careers link `https://{tenant}.wd{N}.myworkdayjobs.com/{locale?}/{site}`. A wrong site name gives 404/422 (Akamai guess → 422). Big companies often put a Phenom or custom front-end over Workday (Adobe), so follow "Apply" links to find the tenant.
- **Detection:** `myworkdayjobs.com`, `wd1|wd3|wd5|wd12|wd103|wd105` hosts, `/wday/cxs/` XHRs.
- **Difficulty:** medium. POST, 20 per page, relative dates; be gentle (2,000 jobs = 100 requests).

## Eightfold (PCS) — **VERIFIED**

- **List:** `GET https://{company}.eightfold.ai/api/apply/v2/jobs?domain={company-domain}&start=0&num=10` → `{count, positions[]}`. Position fields: `id`, `name`, `posting_name`, `location`, `locations[]`, `department`, `business_unit`, `t_create`, `t_update` (epoch s), `ats_job_id`, `display_job_id`, `type`, `job_description`, `work_location_option`, `location_flexibility`, `canonicalPositionUrl`.
- **Pagination:** `start` / `num` (10 per page observed). Verified on Netflix (count=405).
- Also front-ends some Greenhouse/Workday customers (Twilio's `jobs.twilio.com`).
- **Detection:** `{x}.eightfold.ai`, `app.eightfold.ai` scripts, `/careers?pid=` URLs.
- **Difficulty:** medium.

## Oracle Recruiting Cloud (Fusion HCM "CE" sites) — **VERIFIED**

- **List:** `GET https://{host}.fa.{dc}.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={CX_n},keyword={kw},limit=25,offset=0,sortBy=POSTING_DATES_DESC`.
- **Response:** `items[0] {TotalJobsCount, Offset, Limit, Facets..., requisitionList[]}`. Requisition fields: `Id`, `Title`, `PostedDate`, `PostingEndDate`, `PrimaryLocation`, `PrimaryLocationCountry`, `WorkplaceType`, `JobFamily`, `JobFunction`, `ShortDescriptionStr`, `ExternalQualificationsStr`, `ExternalResponsibilitiesStr`, `secondaryLocations`.
- **Detail:** `recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;Id="{id}",siteNumber={CX_n}` *(unverified)*. Public URL: `https://{host}.fa.{dc}.oraclecloud.com/hcmUI/CandidateExperience/en/sites/{CX_n}/job/{Id}`.
- Verified on Oracle (`eeho.fa.us2.oraclecloud.com`, `CX_45001`, 2,754 jobs). `keyword` matches descriptions too, so it overcounts engineering roles.
- **Detection:** `*.fa.*.oraclecloud.com/hcmUI/CandidateExperience/`.
- **Difficulty:** medium (the URL syntax is awkward, but it's JSON and unauthenticated).

## iCIMS — HARD

- Career portals: `https://careers-{company}.icims.com/jobs/search?ss=1&in_iframe=1&pr={page}` (HTML; `in_iframe=1` gives a lighter page). Job pages: `/jobs/{id}/{slug}/job`, which usually embed schema.org `JobPosting` JSON-LD.
- No public JSON API (the iCIMS API needs credentials). Some companies server-render the iCIMS data on their own site, which is easier: Atlassian's `/company/careers/all-jobs` lists about 349 jobs with iCIMS links.
- Seen at Booking.com (`external-workingatbooking.icims.com`) and Atlassian (campus).
- **Detection:** `*.icims.com`, `icims_content_iframe`.
- **Difficulty:** hard (paged HTML, iframe wrappers, sometimes bot protection).

## SAP SuccessFactors — HARD

- RMK career sites (`jobs.{company}.com`) expose `sitemap.xml` with per-language job sitemaps. **Verified on `jobs.sap.com`**: the sitemap index returns 200, but `/search/?q=` and the RSS `/services/rss/job/` returned a **Cloudflare challenge (403)**. Don't bypass it; use the sitemap URLs plus per-job JSON-LD if those pages are reachable, otherwise skip.
- Classic: `career{N}.successfactors.com/career?company={id}` (JS-heavy HTML).
- **Detection:** `successfactors.com|eu`, `rmkcdn.successfactors.com`, `/talentcommunity/` paths.
- **Difficulty:** hard.

## Taleo (Oracle Taleo Enterprise / Business Edition) — HARD (not verified)

- Enterprise: `https://{x}.taleo.net/careersection/{section}/jobsearch.ftl` is a JS app. An undocumented `POST /careersection/rest/jobboard/searchjobs?lang=en&portal={portalId}` returns JSON but needs a portal id and session-like headers. Business Edition: `https://{x}.tbe.taleo.net/{x}/ats/careers/v2/searchResults?org=…&cws=…` (HTML).
- Being replaced by Oracle Recruiting Cloud at many companies. Prefer ORC when present.
- **Difficulty:** hard. Support last, if ever.

## Japanese ATSes (seen in the JP seed set) — **VERIFIED (HTML)**

- **HRMOS:** `https://hrmos.co/pages/{company}/jobs` lists server-rendered links `/pages/{company}/jobs/{id}` (100 on the first page for Money Forward / Timee, so it paginates). Job pages have JSON-LD *(unverified)*. Unknown → 404.
- **HERP:** `https://herp.careers/v1/{company}` lists server-rendered links `/v1/{company}/{jobId}` (Ubie 44, TIER IV 63, Gaudiy 35). Unknown slugs can return 200 with no jobs.
- **Talentio:** `https://open.talentio.com/r/1/c/{company}/pages/{id}` (SmartHR, LayerX). Linked from the company's own careers site, with no index endpoint found.
- **Difficulty:** medium (HTML, stable selectors, mostly Japanese titles; the eng regex should include エンジニア / 開発).

## Others seen in the wild

- **Deel job boards** (`jobs.deel.com/job-boards/{co}` → `jobs.deel.com/{co}`): Klarna. Client-rendered with no `__NEXT_DATA__` and no public JSON found. Hard.
- **Phenom** (`cdn.phenompeople.com`): a front-end over Workday, Ashby, etc. (Snowflake, Adobe, Personio). Find the backing ATS rather than scraping Phenom.
- **Custom static feeds:** Fly.io `https://fly.io/jobs/feed.xml` (Atom). Always check `/feed.xml`, `/jobs.rss`, `/jobs.json`.
- **Rippling** (`ats.rippling.com/{slug}/jobs`), **Pinpoint** (`{x}.pinpointhq.com/postings.json`), **Comeet**, **Jobvite** (`jobs.jobvite.com/{x}`), **Avature**: not tested. Add on demand.

---

## Detecting the ATS behind a careers URL

Observed hit rate: grepping just the landing-page HTML found a signature for only ~30% of 190 careers pages. Most are client-rendered or sit behind a CDN or WAF (403/429 seen at Revolut, Perplexity, Gusto, DoorDash, Deliveroo, Doctolib, HashiCorp, Confluent, Brex). Use a layered approach:

1. **Follow redirects.** The final host often gives it away (`deno.com/jobs` → `jobs.ashbyhq.com/Deno`; `jobs.booking.com` → iCIMS; `neon.com/careers` → databricks).
2. **Grep HTML, iframes, scripts and links** for these regexes (all used in `detect.py` during research):
   - Greenhouse: `(boards|job-boards)(\.eu)?\.greenhouse\.io/(embed/job_board(/js)?\?for=)?{token}`, `grnhse_app`, `gh_jid=`, `recruiting.eu.greenhouse.io` (EU)
   - Lever: `jobs\.(eu\.)?lever\.co/{site}`
   - Ashby: `jobs\.ashbyhq\.com/{board}` (board may contain dots), `ashby_jid=`
   - Workable: `apply\.workable\.com/{sub}`
   - SmartRecruiters: `(jobs|careers)\.smartrecruiters\.com/{Company}`
   - Recruitee `{sub}\.recruitee\.com`, Personio `{sub}\.jobs\.personio\.(de|com)`, Teamtailor `{sub}\.teamtailor\.com`, BambooHR `{sub}\.bamboohr\.com`, Breezy `{sub}\.breezy\.hr`, JazzHR `{sub}\.applytojob\.com`
   - Workday `{tenant}\.wd\d+\.myworkdayjobs\.com/(xx-XX/)?{site}`; iCIMS `\.icims\.com`; SuccessFactors `successfactors\.(com|eu)`; Taleo `\.taleo\.net`; ORC `\.fa\.\w+\.oraclecloud\.com`; Eightfold `\.eightfold\.ai`; Phenom `phenompeople`; Gem `jobs\.gem\.com/{slug}`; Rippling `ats\.rippling\.com/{slug}`; Deel `jobs\.deel\.com`; HRMOS `hrmos\.co/pages/{co}`; HERP `herp\.careers/v1/{co}`; Talentio `open\.talentio\.com/r/1/c/{co}`
3. **Check the jobs subpage** (`/careers/jobs`, `/open-positions`, `/positions`), since landing pages often lack links (Sourcegraph, Taxfix, LayerX only revealed tokens there).
4. **Slug-probe the easy ATSes** (Greenhouse → Ashby → Lever → Workable → SmartRecruiters → Recruitee → Lever EU → Personio) using variants: company name, name without spaces or with hyphens, domain (`mistral.ai`, `taxfix.com`), `{name}hq`, `{name}inc`.
5. **Validate identity. This step is mandatory.** Slug collisions are real: `recruitee/prisma` (Dutch care org), `greenhouse/axiom` (legal-talent firm), and many empty Workable/Ashby/HERP boards. Checks: company name in `/v1/boards/{token}` name / Workable `name` / SmartRecruiters `company.name` / Recruitee `company_name` / description text; title sanity; reject 0-job boards as unresolved.
6. **Fallbacks for custom pages:**
   - **schema.org `JobPosting` JSON-LD** on job pages (`<script type="application/ld+json">` with `@type: JobPosting`: `title`, `datePosted`, `validThrough`, `employmentType`, `hiringOrganization`, `jobLocation`, `applicantLocationRequirements`, `jobLocationType: TELECOMMUTE`, `baseSalary`, `description`). Google for Jobs requires it, so it's widespread (Teamtailor embeds it in `jobs.json`).
   - **sitemap.xml / robots.txt** `Sitemap:` entries, filtered to URLs matching `/jobs?/|/careers?/|/positions?/` (works for SuccessFactors RMK: `jobs.sap.com/en/jobs/sitemap.xml`).
   - **RSS/Atom/JSON feeds:** `/feed.xml`, `/jobs.rss`, `/jobs.json`, WordPress `/wp-json/` (Carousell is WordPress).
   - **Server-rendered link lists:** count anchors matching `/positions/{uuid}` or `-jid-{n}` (Bolt 191, Wise, Zalando `/en/jobs/{id}-`, Atlassian 349).
   - **Next.js `__NEXT_DATA__`** JSON blobs (Pipedrive, Taxfix, GoTo, LayerX pages had them).

## Politeness and operational notes

- Keep ≤1 req/s per host; Workable needs slower (429 seen). Honour `Retry-After`.
- Cache by ETag/`updated_at`; Greenhouse `content=true` and Ashby boards can be multi-MB (Ashby `mistral.ai` 2.8 MB, `checkout.com` 2.5 MB).
- Don't attempt Cloudflare/Akamai challenge bypass (Revolut, jobs.sap.com search, some careers pages returned 403/429). Mark the company unsupported instead.
