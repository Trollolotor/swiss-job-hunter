# ATS parsing research

Reviewed on 2026-10-23. Public endpoint ideas were rewritten into product-owned packages with
synthetic fixtures. No upstream scraper executable code or fixture payloads were copied. Public
APIs change; a repository's successful crawl does not establish complete employer coverage.

| Source | Useful findings | Product decision |
| --- | --- | --- |
| [kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers/tree/6b44a1badc9bfbf5cf176f75265cc5729e520e99) (MIT) | Vendor-specific public endpoints, Workable Markdown, Workday caps and zero sentinels, regional Greenhouse/Lever, SuccessFactors RSS/legacy split, namespaced Teamtailor data. | Use these protocol observations in separate pure parsers. Keep product evidence validation, scopes, partial outcomes and isolated SDK execution. Do not import the framework's HTTP/concurrency/database behavior. |
| [rishilahoti/ashby-job-scraper](https://github.com/rishilahoti/ashby-job-scraper) (MIT plus Commons Clause) | Multi-vendor normalization and a compact dispatcher; hard per-vendor result limits. | Prefer the public Ashby posting API and our existing contracts. Do not copy the stack or treat its Workday/SmartRecruiters limits as complete employer catalogues. |
| [rios-vini/internship-finder](https://github.com/rios-vini/internship-finder) | SAP careers hostname recovery and a feed exceeding 15 MB. | Keep verified destination policies and the existing 5 MB default response cap. Oversized feeds fail explicitly without closure; do not silently truncate or raise limits. Streaming feeds and partition proofs are future work. |
| [orgupdate/Apify-Workable-Job-Scraper](https://github.com/orgupdate/Apify-Workable-Job-Scraper) | A hosted actor wrapper moves scraping and cost outside the application. | Avoid a paid scraping dependency and opaque result/completeness behavior. Use Workable's public widget/Markdown protocol through the existing broker. |
| [leokingzhao/Data_Job_Scraper_With_Django](https://github.com/leokingzhao/Data_Job_Scraper_With_Django) | Fragile portal assumptions and error-to-empty handling; no clear reusable license in the reviewed example. | Do not copy code. Keep RMK and legacy XML distinct, verify observed source structure, and preserve failures instead of returning zero vacancies. |

The first source is the principal protocol reference and is pinned to the reviewed commit. The
other repositories informed tradeoffs, not executable dependencies. Our fixtures contain invented
employers, vacancies, dates and hosts and can be redistributed with this project. Future copied
code or fixtures must receive their original license notice and attribution separately.

The selected implementation optimizes routine public collection: one full feed where available,
separate details only when necessary, existing source-scoped caching, no model generation for a
supported ready protocol, and bounded model repair for structural variants. It does not introduce
anti-bot bypasses, application automation, authenticated APIs or unrestricted browser execution.
