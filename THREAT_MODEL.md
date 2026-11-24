# AidBridge: threat model and evidence

Status: development build, merged into `main` from the branch `financial-pivot` on 4 November 2026. Synthetic and demo data only. Nothing here has been run against real people's records, and nothing has been independently audited. Written on 4 November 2026 for the financial-needs design. In-kind help (asking for an item instead of money) was added on 23 November 2026 and is covered in sections 1, 2.7, 4 and 5. The earlier version of this document described a beneficiary engine, a field-agent role and a handover flow; it is in the git history and at the tag `demo-old-schema`.

AidBridge lets a person post a specific financial need (rent, a medical bill, a school cost). A verifier checks it and it appears on a public board. Supporters pledge parts of the goal and send the money directly to the requester's bank account. Analysts run differentially private counts over the needs. No money passes through the platform. A requester can instead ask for an item (a walker, a heater, bedding): donors offer units and hand them over in person, and the platform never holds the goods either.

## 1. System and design decisions

Stack: FastAPI, PostgreSQL 18, Jinja2 server-rendered pages, Python 3.14. Noise comes from diffprivlib 0.6.6 (it requires scikit-learn below 1.9; versions are pinned in `requirements.txt`).

| Decision | Choice | Why |
|---|---|---|
| Money | No money passes through AidBridge. A donor sends money straight to the requester's bank account. The platform records pledges, the donor's "sent" mark and the requester's "arrived" mark. | The platform never holds or moves funds. It also means it cannot see whether a transfer happened (section 4). |
| A need | Purpose (rent and housing, utilities, food, medical, education, disaster recovery, debt, other). For medical: in Turkey or abroad. For disaster recovery: an event. Province and district, household size, a goal in whole units, a private description, a contact method and detail, the account holder's name and a Turkish IBAN. | A specific, checkable cost. |
| Goal limits | Each purpose has a ceiling in the table `purpose_limits`: 300,000 TRY for most purposes, 1,000,000 TRY for medical in Turkey and for disaster recovery, 2,000,000 EUR for medical abroad. A goal must be at least 100. Euro goals exist only for medical abroad. The values are placeholders chosen by the author. | Keeps goals plausible. Enforced by constraints and a trigger, and checked again in the form. |
| IBAN | The form checks the Turkish IBAN shape and its check digits (ISO 13616, mod 97) and stores it without spaces. The app role cannot change the account holder or the IBAN after posting. | Catches typing mistakes before someone sends money to a wrong account. A mistake is fixed by withdrawing and posting again. The check does not show that the account belongs to the requester. |
| Review | A verifier checks, outside the platform, that the account holder's name matches the requester's identity and sees a supporting document. Approval needs both boxes ticked, writes an event that says so, and marks the payee as verified. The database refuses to publish a need without public text and a verified payee. There is one verifier role and no second review. | Money goes to an account the requester names, so identity and the account are the main fraud check. |
| Public text | Written by the verifier, 10 to 300 characters. Refused if it contains an at-sign, a link, a run of seven or more digits (phone and account numbers), the requester's contact string, part of the account holder's name, or the requester's username. | Only reviewed text reaches the public. |
| Pledges | Partial pledges of at least 10 lira, never more than what is still open. The supporter ticks a notice about withdrawals and refunds and gives a contact that only verifiers see. At most 10 unconfirmed pledges per supporter. A trigger with row locks enforces the floor, the cap, no pledging to your own need, and the limit. A pledge to a euro need is made in lira and stored with the rate used and the euro equivalent. The rate is a placeholder in `settings`. | Many people can give small amounts. The rules sit in the database, so a route bug cannot break them. |
| Pledge states | Pledged, then sent (the donor marks it), then confirmed (the requester says it arrived). The requester can instead say it did not arrive, which makes it disputed, and a verifier settles it as arrived (confirmed) or not arrived (cancelled, and the amount opens again), with a note. A donor can cancel before sending. An unsent pledge expires after 7 days. | Two-sided confirmation without handling money. |
| Expiry | `expire_pledges()` runs when the board, a need page, the pledge form or a supporter's page is loaded. It does not run on a clock. | No scheduler exists yet (section 4). |
| Fulfilment | A need becomes fulfilled when confirmed money leaves less than one minimum pledge open. | A need is not stuck open over a few lira. |
| Withdrawal | A requester can withdraw a pending or published need with a reason of 10 to 500 characters. The reason goes through the same text guard as the public text. Donors who pledged see the reason, and the form says so. Unsent pledges are cancelled. Money already sent stays between donor and requester, and the platform refunds nothing. A fulfilled need cannot be withdrawn. | The donor accepts this notice when pledging. |
| Who sees an account | A donor sees the account holder and IBAN only while their own pledge is pledged, sent or disputed. A requester sees pledges marked sent with amount and date, never the donor's identity or contact on the platform. The money itself is a direct bank transfer, so the requester's bank statement shows the sender's name, and the platform cannot hide that. A verifier sees both contacts on a pledge page. | Keeps strangers apart while letting a verifier help. |
| Public board | Reads only two views. `public_needs` gives: purpose, event, district, province, month, goal in its currency, the verifier's text, confirmed and promised totals, and the donor count. `public_stats` gives seven totals for the front-page lines: open needs, fulfilled needs, the lira confirmed by requesters and the number of donors, plus three item figures (fulfilled item needs, units confirmed by requesters, and the number of item donors), with nothing about any one need or person. Like the money figures, the item figures are exact totals, not noised or suppressed. No sign-in is needed and the board sets no cookie; a session cookie, if the visitor has one, is read only to show their name in the header. A limit of 60 requests a minute per address, a map drawn from our own file with no third-party tiles, and a page per need with a lira estimate for euro goals. | Public by design, so it shows as little as possible. |
| Suggested amount | On request: the amount still open divided by the average number of donors of finished lira needs in the same goal band, rounded up to 10 lira, between the floor and the open amount. Hidden for euro needs and for bands with fewer than 20 finished needs. | Helps a donor choose. Reads aggregates only. |
| Analytics scope | Counts only, in `app/needs_engine.py`. Five fixed groupings on both datasets: need type (9 cells), province (81), status (2), need type by status (18) and goal band (6, lira needs only). On the live dataset only, three more: kind (money or item, 2 cells), item category (10) and item category by status (20). The synthetic dataset has no item data and refuses these. No filters, no means, no ungrouped total. The dataset name must be on a fixed list, and the source view is looked up from it, never taken from input. | Each query shape has a known sensitivity. |
| Analytics privacy unit | Each requester's first 3 needs by id count and the rest are ignored. Money and item needs share those 3 places, so a person with several item needs can push a later money need out of the need type counts. One person changes any released total by at most 3, so Laplace noise uses sensitivity 3 (scale 3/epsilon, which is 30 at epsilon 0.1). A whole table costs one query because a person's needs fall in disjoint cells. | A person can post several needs, so counting needs would not protect the person. |
| Analytics budget | 0.1 per query, 10.0 per dataset, spend-only. Check and spend happen in one transaction with a row lock. Spend first, compute second, release last. A free preview shows cost, cell count and typical error from public facts only. Every query is audited and its noisy cells stored. | Matching accounting. A refusal depends only on the request, so it costs nothing. |
| Analytics sources | `needs_synthetic` reads the view `synth_analytics` (assumed data). `needs_live` reads `needs_analytics` (verified money and item needs on the platform, demo data). Province and status counts include item needs; need type and goal band count money needs only, because item needs have neither. Noisy values below 30 are shown as suppressed. | Suppression is a usability flag, not a protection. |
| Roles | Requester and supporter by sign-up. Verifier, analyst and admin by script. | Nobody can promote themselves. |
| Sessions, passwords, limits | A signed cookie holds only the user id. The role is re-read from the database on every request. Eight-hour expiry, HttpOnly, SameSite=Strict, and a CSRF token on every POST. Passwords use argon2, and unknown usernames still run a dummy hash. Five failed logins per username or 20 per address in 5 minutes lock login. Five sign-ups per address per hour. | The role cannot be forged by editing the cookie. |
| Audit log | Append-only for the app role. It records user, dataset, query type, parameters, epsilon and a SHA-256 hash of the result. The admin sees it, never results. | Shows who asked what. |
| Places | Provinces and districts come from fixed lists (81 provinces, 973 districts) and are validated as a pair. Names are plain ASCII. | Clean place data for the board and the map. |
| In-kind help | A requester can ask for an item instead of money: a type from a fixed catalogue in ten categories (mobility, medical equipment, heating, clothing, bedding, kitchen, hygiene and baby, food, power and communication, other), a quantity of at most 50, whether used items are acceptable, a short specification, a handover method, a place, a private description and a contact. Some types are disabled in the catalogue (for example gas cylinders, generators, oxygen concentrators, hospital beds, diabetic supplies, electric wheelchairs and hearing aids). Review and the public text work as for money needs. The flag `ITEM_NEEDS_OPEN` in `app/items.py` switches item posting on or off. | Items are asked for in plain units and the riskiest types are left out. The platform never holds or moves goods. |
| Item offers | A donor offers a number of units, ticks an acknowledgement and gives a contact. The offer is pledged. A verifier releases the donor's contact to the requester, or declines the offer with a reason. An offer that is still pledged expires after `claim_expiry_days` days, and the clock restarts when the contact is released. A donor can hold only a limited number of unconfirmed offers. Triggers in the database check each offer and every change of its state. | Same idea as pledges, with units instead of lira. |
| Item handover | The donor hands the items over in person and ticks a statement that they did. The requester then confirms receipt or disputes it with a reason of 10 to 450 characters. A verifier settles a dispute either way with a written reason of 10 to 450 characters. A need is fulfilled when confirmed units reach the quantity wanted (a trigger). A handed-over offer never expires by itself; instead, ones the requester has not answered after the unanswered-days threshold are listed for the verifier, who can confirm for the requester or open a dispute. | Two-sided statements. There is no proof, on purpose (section 2.7). |

## 2. Threat model

### 2.1 Curious analyst

Goal: learn about individuals from query answers.
- Only counts over eight fixed groupings exist (five on both datasets, three item groupings on the live one). There is no free-form SQL, no filter and no mean. A dataset name must be on a fixed list and nothing from the request reaches SQL text.
- Noise is sized to one person's contribution (3), so one requester's needs, however many, change a table by at most 3. The typical error is about 30 per cell at epsilon 0.1, so small cells are mostly noise.
- Repeating a query averages the noise away. `dev/attack_sim.py` simulates an analyst who repeats a noisy count of a true value of 500 and averages the answers (it uses no database), and the same logic applies here. The budget caps the total: 100 queries per dataset.
- Residual: a total of 10.0 under basic composition is a weak guarantee in absolute terms. It was chosen for utility, and this document does not call it safe.
- Residual: the public board is an exact release of every published need, so while a need is published its province and purpose can be read without any query.
- Residual: the view `needs_analytics` includes household size for the app role, although no grouping uses it. A future grouping on it would need its own review.

### 2.2 Colluding analysts

Goal: pool queries across accounts to get more than one analyst could.
- All analysts draw on one budget per dataset, so extra accounts do not multiply the privacy loss.
- Tested: 20 simultaneous runs by two analysts on a budget for 5 gave exactly 5 answered and 15 refused, the budget spent exactly, and each analyst's audit rows equal to that analyst's answered runs.
- Residual: denial of service. One analyst can spend a whole budget and lock the others out. Per-user caps are not implemented. Combining answers across accounts is argued from composition, not tested.

### 2.3 Stolen database

Goal: read data from a copy of the database.
- Passwords are argon2 hashes. The session signing key is in `.env`, not in the database, so a copy of the database alone does not allow forging sessions.
- Residual, and the largest one: private data is stored in plain text. That is each need's description, household size, contact and the account holder's name and IBAN, every donor's contact, dispute and withdrawal reasons, and for item needs the specification and every offer's contact. Together with a province and a district these identify people. Field-level encryption is not built. The audit log also reveals what analysts asked.

### 2.4 Admin

Goal: use the admin role to read data or hide activity.
- The admin page shows audit rows (who, what, parameters, epsilon, a 12-character hash prefix) and the budgets, never results. The admin is refused on every analyst page.
- Residual: the admin sees what analysts asked about. Admin, analyst and verifier accounts are created by scripts that need database credentials, because there is no account management page.
- The database owner (`postgres`) is outside the app's controls. The owner can read every row, change the audit log and reset a budget. Append-only is enforced against the app role only.

### 2.5 Who can see what

| Actor | Can see | Cannot see |
|---|---|---|
| Visitor, no account | Published needs: purpose, event, district and province, month, goal, the verifier's text, confirmed and promised totals, donor count. Needs per province on the map. | Pending, rejected, withdrawn and fulfilled needs. Private descriptions, household size, contacts, account details, individual pledges, donors. |
| Requester | Their own needs in full, with status, funding totals, a rejection note and their withdrawal reason. Pledges donors marked sent: amount, date, status. | Other requesters' needs. For money: who a donor is, or a donor's contact. For items the requester does see the donor's contact once a verifier releases it (section 2.7). |
| Supporter | The board, and their own pledges with status. While a pledge is pledged, sent or disputed: the account holder and IBAN. The withdrawal reason of a withdrawn need they pledged to. | Private descriptions, household size, the requester's contact, other supporters' pledges. |
| Verifier | Every need in full including household size, contact, account holder and IBAN, and a summary of the requester's earlier needs. Every pledge page: amount, dates, both contacts, the account, the dispute reason and the pledge's history. | The analytics data. |
| Analyst | Noisy aggregates, and the free preview of a query. | Individual records and needs. |
| Admin | The audit log and the budgets. | Needs, results and contacts, through the application. |
| Database owner | Everything in plain text. Can change the logs and budgets. | Nothing. |

Trust assumptions:
- The verifier is trusted with every private detail, with the payee check, with the public text and with settling disputes. A verifier's decisions are recorded as events, but nothing reviews them.
- The database owner is outside every control in this document.
- The platform cannot see bank transfers. Whether money moved is known only from what the donor, the requester and a verifier say.
- A bank transfer shows each side the other's name. The requester's bank shows the sender, and the donor sees the account holder's name and IBAN. The platform shows the requester no donor identity, but it cannot change what the banks show. A donor who must stay anonymous to the requester cannot use direct transfers.

### 2.6 Matching side: threats

Each entry says what the design does, which test in `dev/` shows it, and what is left over.

**Fake needs and wrong accounts.**
- Design: sign-up is open and a verifier reviews every need before it is public, checking outside the platform that the account holder's name matches the requester's identity and that a supporting document exists. A requester can have 3 pending needs, each address can create 5 accounts an hour, and privileged roles cannot be chosen at sign-up. The IBAN's check digits are verified and payee details cannot change after posting.
- Tested: `test_requesters.py` (the fourth pending need gets 429; 8 simultaneous posts by one requester gave 3 accepted and 5 refused; the sixth sign-up from one address is refused; sign-up as a verifier, admin or analyst is refused; an IBAN with a mistyped digit is refused), `test_verifiers.py` (approval is refused unless both checks are ticked) and `test_schema.py` (payee details cannot be changed after posting, and a need cannot be published without text and a verified payee).
- Left over: the platform cannot check identity, the document, or that the IBAN belongs to the requester. The verifier is the only control. There is no second verifier, and the two ticks record that a check was claimed, not what was seen. A fraudster who passes the check receives donors' money directly and nothing on the platform can reverse it. Many addresses can still flood the queue, and there is no email or phone verification, no CAPTCHA and no password reset.

**Disputes and false claims.**
- Design: the donor marks a pledge sent, the requester confirms or disputes it (a reason of 10 to 450 characters that only verifiers see), and a verifier settles a dispute with a note. Pledges marked sent and unanswered for 14 days are listed for verifiers, which changes nothing by itself. Requesters can answer after withdrawing a need.
- Tested: `test_receipts.py` (40 checks: another requester cannot answer my pledges; the donor's name and contact are not shown to the requester; a confirmed pledge cannot be answered again; a dispute needs a real reason; a verifier settles as arrived or not arrived with a note; settling twice is refused; two verifiers settling one dispute at once gave one winner; the 14-day list; confirming leaves the need fulfilled when less than a minimum pledge is open).
- Left over: the platform cannot see transfers. A donor can mark a pledge sent without sending it, and a requester can say money did not arrive when it did. A verifier decides from two statements and whatever they ask for. A requester can also leave sent pledges unanswered, so totals show as promised until they answer, and only the 14-day list prompts anyone.

**Collecting account details by pledging and cancelling.**
- Design: a donor sees the account holder and IBAN once they pledge, and can cancel before sending. The limit of 10 unconfirmed pledges and the sign-up throttle slow this.
- Tested: `test_supporters.py` (another supporter does not see the account; a cancelled pledge hides it; the limit of 10 holds).
- Left over: one account can pledge, read the details, cancel and repeat across many needs. An IBAN alone does not let anyone take money, but a name, an IBAN, a place and a purpose together identify a household more than the board does. A daily cap on cancellations would need a change to the pledge trigger, which is a schema change that was not made.

**Re-identification from the public text and from withdrawal reasons.**
- Design: the board shows only the fields in section 1. The verifier's text and a requester's withdrawal reason pass the same guard, and the withdrawal form tells the requester that donors will read the reason.
- Tested: `test_verifiers.py` (ten kinds of unsafe text are refused and the need stays pending), `test_withdrawal_reason.py` (eight kinds of unsafe reason are refused and the need is unchanged, the form shows the notice, the old length rule holds), and `test_board.py` (no private description, contact, account detail or review note appears on the board, a need page or the map data).
- Left over: the guard catches contact details, digit runs and the account holder's name, not descriptions. A street, an employer or a distinctive item in a small district can still point at one household, and anyone can read the board. The contact-string check is a substring match, so a paraphrase passes. Withdrawal reasons are free text that every donor who pledged can read, even for 10 lira.

**A careless or malicious verifier.**
- Design: verifiers are created by script. Each decision writes an event. A decision applies only to a pending need and a pledge can only be settled while disputed. A verifier cannot decide a need they posted.
- Tested: `test_verifiers.py` (all other roles are refused on every verifier route; 6 simultaneous decisions by 2 verifiers gave 1 accepted, 5 refused and 1 event; a verifier's own need is refused with 409) and `test_receipts.py` (two verifiers settling one dispute at once).
- Left over: a verifier sees every contact, IBAN and dispute reason, writes the public text and settles disputes. Nothing reviews those decisions, and no page shows the event table, so reviewing them needs database access. Pledge events use a note convention (`pledge <id>: text`) in the event table, which has no pledge column. The verifier's pledge history is filtered by event name so a withdrawal reason cannot fake a line (tested in `test_receipts.py`), but the convention remains.

**Pledge races and over-subscription.**
- Design: one trigger enforces the floor, the goal cap and the limit under row locks. Routes translate its refusals into readable messages.
- Tested: `test_schema.py` (20 simultaneous pledges of 100 against a goal of 1000 gave 10 accepted and a total of exactly 1000.00) and `test_supporters.py` (two simultaneous pledges of 600 on a goal of 1000 gave one accepted and one refused; the form's open-amount check or the trigger may do the refusing, and the status code shows which).
- Left over: none known beyond the weak spot in the test's wording.

**Expiry and stale totals.**
- Design: an overdue unsent pledge stops counting when a page that shows or changes pledges is loaded, and is marked expired.
- Tested: `test_board.py` (an overdue unsent pledge stops counting and is marked expired), `test_supporters.py` (it shows as expired without the account and cannot be marked sent) and `test_schema.py` (`expire_pledges` frees the amount).
- Left over: nothing runs on a clock. If nobody loads those pages, an overdue pledge keeps its amount held, and a public page view writes to the database (it is idempotent).

**Scraping the open board and map.**
- Design: board pages, the map data and need pages share a limit of 60 requests a minute per address. Filters and page numbers are validated and the board sets no cookies (it only reads a session cookie, if there is one, to show the visitor's name).
- Tested: `test_board.py` (the 61st request on any public page gets 429; unknown filters get 400; page numbers are validated).
- Left over: the limit is held in memory, so it resets when the server restarts and is not shared between processes. It uses the connection address, so behind a proxy every visitor looks like one address. Slow scraping stays under the limit. The data is public by design.

**Sign-up abuse and username probing.**
- Design: usernames and passwords are validated, duplicates are refused with a generic message, and sign-ups are throttled per address.
- Tested: `test_requesters.py` (sign-up as a privileged role is refused and creates nothing; five sign-ups are accepted and the sixth gets 429).
- Left over: a refused duplicate still confirms that a name exists. Sign-ups are recorded in the same table as failed logins, so five sign-ups from one address count toward that address's login lockout. That was reasoned from the code and not tested. There is no password change page.

### 2.7 In-kind help (items)

An item need is reviewed like a money need. A donor offers units, a verifier releases the donor's contact to the requester, the two hand the items over in person outside the platform, and each side then says what happened. The platform never holds or moves goods and keeps no proof of a handover. Item posting is switched by the flag `ITEM_NEEDS_OPEN` in `app/items.py`.

**Why there is no photo proof.** A photo step was built and then removed on purpose. A photo shows that someone holds an object, not that it reached the requester or that the two people agreed on anything. Asking for one would make the platform a party to the handover, in the way a courier service is, and would add stored images, retention rules and an upload path to attack, for very little extra evidence. The design now rests on the donor's statement, the requester's statement and a verifier's judgement. That is weaker evidence than a photo pretends to be, and it is the main limit of this feature.

**A donor says items were handed over when they were not, or a requester says they never arrived when they did.**
- Design: the donor ticks a statement that they handed the items over. The requester confirms or disputes it with a reason of 10 to 450 characters, and a verifier settles a dispute either way with a written reason. Every step writes an event. A donor can hold only a limited number of unconfirmed offers, and the verifier's claim page shows the donor's history. Handed-over offers that the requester has not answered after the unanswered-days threshold are listed for verifiers, who can confirm for the requester or open a dispute. A handed-over offer never expires by itself, so a silent requester cannot make it lapse.
- Tested: `test_item_claims.py` (36 checks), `test_item_handover.py` (28) and `test_item_chase.py` (12): who may make each step, the order of states, and the database refusing illegal transitions.
- Left over: the platform cannot tell who is lying. A verifier decides from two statements and whatever they ask for. A dishonest donor can tick "handed over" without handing anything over, and a dishonest requester can keep the items and say they never came. A donor who loses items this way has no remedy on the platform. There is no automatic timeout on handed-over offers (the verifier nudge is a list, not an action), and a requester has no way to chase a donor inside the application.

**Unsafe or unsuitable items.**
- Design: the item type comes from a fixed catalogue, never free text. Types with an obvious risk are disabled in the catalogue (for example gas cylinders, generators, oxygen concentrators, hospital beds, diabetic supplies, electric wheelchairs and hearing aids). The quantity is at most 50, and the requester says whether used items are acceptable.
- Tested: `test_item_posting.py` (42 checks) and `test_item_schema.py` (48): disabled types and bad quantities are refused.
- Left over: nobody inspects an item. Used mobility and medical equipment can be unsafe or unsuitable. Whether giving used medical equipment in Turkey is allowed, or needs conditions, has not been checked. A disabled type is enabled by changing one value in the table `item_types` as the database owner, with no review step.

**Meeting a stranger in person.**
- Design: the requester chooses a handover method when posting, and a donor's contact reaches the requester only when a verifier releases it.
- Left over: the platform does not arrange or supervise the meeting, and it does not check that either person is who they say beyond the verifier's review of the need. An in-person handover carries a physical-safety risk for both people that the platform cannot reduce.

**Contact and detail exposure.**
- Design: the item specification, the private description and every contact stay off the board. A donor's contact is visible to verifiers, and to the requester only after release and only while the offer is pledged, handed over or disputed; once it is confirmed the contact is masked. The requester's contact is never shown to a donor.
- Tested: `test_item_board.py` (27 checks: no private marker on the board or a need page), `test_item_claims.py` and `test_item_handover.py` (who sees which contact, and when it is masked).
- Left over: the application uses one database role (`dp_app`) for everything, so these visibility rules are rules in the route code, not rules the database enforces. A route bug could show a contact to the wrong person, and the database would not stop it. After release the requester holds the donor's contact for good and can copy it. The verifier decides when to release.

**What the public and analysts learn from item needs.**
- The front page shows exact item totals (fulfilled item needs, confirmed units, item donors). While few items are confirmed, one person's single confirmed unit is visible in those numbers, as the lira figures already are. `test_item_stats.py` (11 checks) shows that only confirmed offers count.
- In the analytics, province and status counts now include item needs, and kind and item category groupings exist on the live dataset. The cap of 3 needs per person covers both kinds together. Item cells will stay mostly suppressed until there are many confirmed needs. `test_item_analytics.py` (18 checks) shows the cap, that the synthetic dataset refuses the item groupings, and that need type and goal band ignore item needs.

## 3. Evaluation

The accuracy tables of the earlier version measured the old beneficiary engine on a different dataset and are retired. They are in the git history. The numbers below follow from the noise scale, and `test_analytics_engine.py` confirms the scale (section 5). They were not measured on the data.

At epsilon 0.1 and sensitivity 3 the Laplace scale is 30. The mean absolute error of one cell equals the scale, so it is 30 for any cell, and the relative error is about 30 divided by the cell's true size:

| True count in the cell | Typical error | Relative error |
|---|---|---|
| 100 | 30 | about 30 percent |
| 300 | 30 | about 10 percent |
| 1,000 | 30 | about 3 percent |
| 3,000 | 30 | about 1 percent |

A cell whose true count is 0 shows a value of 30 or more about 18.7 percent of the time (the test measured 0.188), because the suppression threshold equals one noise scale. In the synthetic data, which rests on assumed distributions, the smallest province has about 49 needs before the cap, so at this setting the smallest provinces are mostly noise or suppressed. A budget of 10.0 allows 100 queries per dataset, and any grouping is one query.

The per-query cost and the total were carried over from the earlier design. They were not re-evaluated for this engine, and they sit at the top of the commonly used range.

## 4. Out of scope and known limits

Money and fraud
- The platform records claims, not payments. It cannot see a transfer, so a dispute rests on statements and a verifier's judgement.
- One verifier is the only fraud control. Identity, the supporting document and ownership of the IBAN are checked outside the platform, and nothing records what was seen.
- Payee details cannot be corrected after posting. The only fix is to withdraw and post again.
- There are no refunds. Donors accept this when they pledge.
- The euro rate is a placeholder. Euro goals and pledges are approximate, and the real value depends on the rate when money is sent. The platform stores the rate at the time of pledging.
- Pledging and cancelling can collect account holders' names and IBANs (section 2.6).

Legal and licensing
- Legal fit is unchecked. During the design I read sources suggesting that collecting aid in Turkey needs official permission, including for online campaigns, and that the crowdfunding law covers investment-type platforms. I am not a lawyer and have not confirmed whether a platform where donors pay requesters directly falls under these rules. This must be checked with the supervisor or a lawyer before any real use.
- There is no terms of service, no privacy notice and no way for a person to ask for their data to be removed. Turkey's data protection law (KVKK) has not been assessed.
- Boundary data. The province and district lists come from the OCHA COD-AB Türkiye dataset (CC BY-IGO, via the Humanitarian Data Exchange), credited in the files, the README and under the map. The outline file is not committed. The dataset's methodology says it was shared "for humanitarian use only", and it has not been confirmed that a student prototype is covered. The committed lists of names and codes come from the same dataset, so the same question applies to them.

In-kind help
- The platform cannot see or inspect a handover. There is no photo or other proof (section 2.7). A dispute rests on two statements and a verifier's judgement.
- Used items, above all used medical and mobility equipment, are not checked for safety. The legal rules for giving them have not been checked, and the legal caveat above applies to items as well. A medical detail in an item need would also fall under the data protection law (KVKK), which has not been assessed.
- Nothing tells either side when an offer is made, released or handed over. They find out by opening their pages.
- The front-page item totals are exact, not noised or suppressed.
- Item posting is open (`ITEM_NEEDS_OPEN` is True in `app/items.py`; setting it to False closes the form). The item flow has script tests only (section 5), with no browser check recorded here, and the legal questions above are open.

Data at rest and operations
- Private data is stored in plain text and readable by the database owner (section 2.3). Backups and key management are out of scope.
- No TLS and no `Secure` cookie flag (local http only). A stolen valid cookie works until it expires, and there is no revocation list.
- Rate limits live in memory, per process and per address. Behind a proxy every client would look like one address.
- Anyone can lock a username for 5 minutes by failing logins, and one analyst can exhaust a budget. Slow guessing under 5 attempts per 5 minutes is not blocked.
- Timing is only partly equalised (login). A refusal or a budget-exhausted message depends on state.
- Expiry is lazy (section 2.6), and the event table has no page.
- The interface is English only and was not designed for phones or screen readers. A safety notice nobody can read protects nobody.

Analytics
- The privacy unit is a requester account. One person with several accounts is not covered, and sign-up is only throttled per address.
- The public board is an exact release, so the noisy counts add little protection for needs while they are published.
- Budgets apply per snapshot. Needs change status over time, and a status change between two queries moves a count between cells, which the add/remove guarantee does not cover.
- The synthetic needs table rests on assumed distributions, so accuracy statements say nothing about real needs. `needs_live` holds demo data.
- Suppression at 30 is a usability flag. An empty cell shows a spurious value about 19 percent of the time.
- A euro need uses one of its requester's three places but lands in no goal band, so the band table does not add up to the other tables.
- There is no formal proof of epsilon-differential privacy and no formal audit. Floating-point Laplace sampling has known precision-based leaks (Mironov 2012); diffprivlib documents this and it is accepted here.

Not built
- Notifications to either side, a clock for expiry, email or phone verification, password reset and change, a page for the event table, account management for staff, field-level encryption, hash-chained audit logs, per-user budget caps, a Turkish translation, and a script to clean up the demo accounts.

## 5. Evidence: tested versus argued

Each script was run by the author in their own terminal on PostgreSQL 18 on 3 and 4 November 2026. The item suites were run on 23 November 2026. Every script that touches the database asks for the owner's password with hidden input, refuses to run on a database without the new schema, and creates and removes its own prefixed throwaway accounts. They use throwaway datasets, and each one checks that the real budgets did not move. They test visible text and status codes, not layout or styling.

Tested (a script in the repo shows it):

| Claim | Evidence |
|---|---|
| Role separation holds, and nothing is charged by refused requests | `dev/test_roles.py` (6 checks) and `dev/test_admin.py` (7): every role is refused on pages that belong to another role, analyst and admin open their own, anonymous gets 401, forged CSRF tokens on login and logout get 403, the admin page shows both budgets and only the 12-character hash prefix, and no budget moved |
| Goal limits and field rules are enforced by the database | `dev/test_schema.py` (54 checks): a goal above the ceiling or below 100 is refused and one exactly at the ceiling is accepted, medical needs in Turkey and abroad, euro goals only for medical abroad, a location for medical and an event for disaster recovery |
| The app role has only the grants it should | `dev/test_schema.py`: the app role cannot insert a need as published, change payee details or the goal after posting, delete a need, change settings, or update or delete the audit log. It cannot read the synthetic table directly but can read its view |
| The budget is spend-only | `dev/test_schema.py`: spending works, a decrease is refused, the total cannot be raised and spending cannot exceed it. The real budgets were not touched |
| Publishing and status order are enforced | `dev/test_schema.py`: a need cannot be published without text and a verified payee, publishing works once both are set, and an illegal jump (pending to fulfilled, confirmed to cancelled) is refused |
| Pledge rules are enforced under concurrency | `dev/test_schema.py`: below the floor, without the acknowledgement, on your own need or an unpublished need is refused, a pledge above what is open is refused, the need becomes fulfilled when less than a minimum pledge remains, a supporter holds at most 10 unconfirmed pledges, and 20 simultaneous pledges of 100 against 1000 gave 10 accepted and a total of exactly 1000.00 |
| Withdrawal and expiry behave as designed | `dev/test_schema.py`: a fulfilled need cannot be withdrawn, a reason is required, an unsent pledge is cancelled and a sent one is left in place, a sent pledge on a withdrawn need can still be confirmed, and `expire_pledges` expires an overdue pledge |
| Views expose no private data and derive their totals | `dev/test_schema.py`: `public_needs` lists no private column, `needs_analytics` lists none except household size, `synth_analytics` has the same columns except that `needs_analytics` also has kind and item_category, the board shows only published needs, totals are derived from pledges, the goal band is null for euro needs, and `suggestion_stats` matches an independent calculation for band 4 |
| Posting a need: sign-up limits, validation, the pending limit and withdrawal | `dev/test_requesters.py` (59 checks): other roles and anonymous visitors are refused; privileged sign-up is refused; the sixth sign-up gets 429; purposes, currencies and events; refusals for unknown purposes, provinces, districts, household sizes, goals and IBANs, with the typed text kept and hostile text escaped; the fourth pending need gets 429 and 8 simultaneous posts give 3; funding totals; a valid withdrawal with a reason; another requester gets 404; a rejected need shows the verifier's note |
| Verifiers: access, the two checks, the guarded public text, decisions that race | `dev/test_verifiers.py` (34 checks): all other roles are refused, approval needs both checks, ten kinds of unsafe public text are refused with the need left pending, an approval records the reviewer and an event and the need reaches the board view, a second decision gets 409, a rejection needs a reason the requester sees, forged tokens get 403, a verifier's own need is refused, 6 simultaneous decisions gave 1 winner, and the look-again list shows needs withdrawn after confirmed money |
| The public board shows only published needs and no private data | `dev/test_board.py` (28 checks): private markers never appear on the board, a need page or the map data; pending, rejected, withdrawn and fulfilled needs are not shown and have no page; hostile text is escaped; the board sets no cookies; totals, filters, sorting and paging match the database; map counts equal published needs per province; an overdue pledge stops counting; the 61st request gets 429 |
| Supporters: pledging, account visibility and the limits | `dev/test_supporters.py` (52 checks): other roles are refused; refusals for amounts, the missing tick and bad contacts keep the typed text; a pledge stores the donor's contact and expiry; the account is shown only while the pledge is open and never to another supporter; sent and cancel work once and only for the owner; an overdue pledge shows as expired; a withdrawn need cancels unsent pledges; a donor sees the withdrawal reason only for a need they pledged to; 10 unconfirmed pledges is the limit; hostile text is escaped |
| Receipts and disputes | `dev/test_receipts.py` (40 checks), as listed in section 2.6, including the fulfilment of a need when confirmed money leaves less than a minimum pledge open |
| The suggested amount and when it hides | `dev/test_suggestion.py` (10 checks): the formula, the rounding, the floor and the open amount, hidden for euro needs, below the threshold and when less than the floor is open, and the button fills the field without submitting |
| Error pages for browsers, plain status for everything else | `dev/test_friendly_errors.py` (18 checks): a signed-out browser gets a readable page with status 401 on eight protected routes, and a way back to the board; an unknown public page gives a readable 404; a forged form token gives a readable 403 that says the form expired; without a browser's Accept header the statuses and the JSON are unchanged; a malformed id still gets 422; an error with no readable page (a bad method, 405) is left alone; the readable pages set no cookies |
| Wording and layout from the browser tour | `dev/test_polish.py` (15 checks): plain wording when nothing is confirmed yet, on the board, the need page and the requester's page, and the old wording is gone; the receipt heading appears only while a pledge waits and says so once all are answered; a refused pledge lists each problem on its own line, says once that nothing was saved, and keeps the typed text; the IBAN is shown in one piece; the post form and the review page explain the document check; the analytics preview warns that a cell much smaller than the typical error is mostly noise and that suppressed does not mean empty; the result page shows the friendly dataset name; none of it moves a privacy budget |
| Bank-transfer notices | `dev/test_notices.py` (7 checks): the pledge form tells the donor that their bank shows their name and account to the requester, and the notice stays after a refused pledge; the need page tells a visitor that both sides see each other's name and that the site never holds the money; the post form tells the requester that every donor sees their name and IBAN and they see each donor's name; the platform's own promise that it never gives the requester the donor's contact still holds; no page promises anonymity; the donor's name is not shown on the requester's page |
| The board header follows the session | `dev/test_header.py` (12 checks): a signed-in visitor sees their name, role and a link to their page and no sign-in link; a visitor, a garbage token and an unknown user id see the sign-in links; the board sets no cookie |
| The front-page stats match the tables | `dev/test_stats.py` (13 checks): `public_stats` has one row with exactly seven total columns (four for money, three for items); its open, fulfilled, lira and donor figures equal independent counts on the tables; the board shows them and says they are demo data; the board sets no cookie; the app role cannot write to the view |
| The withdrawal reason is guarded | `dev/test_withdrawal_reason.py` (17 checks): the guard keeps its old messages by default, eight kinds of unsafe reason are refused with the need unchanged and the typed text kept, the notice is on the form, the old length rule holds, a clean reason withdraws, and pending needs get the same guard |
| Needs analytics: the cap, refusals, charging, concurrency and noise scale | `dev/test_analytics_engine.py` (45 checks): a person with 10 needs adds exactly 3 and the first 3 by id count; no cell moves by more than 3 under any of the five groupings; unpublished needs do not use a place; refused queries and unlisted datasets charge nothing; a run charges exactly 0.1, writes one audit row and stores the returned cells; an exhausted budget leaves nothing behind; 20 simultaneous runs by two analysts on a budget for 5 gave 5 answered and 15 refused with each analyst's audit rows matching; the noise scale matches 3/epsilon within 10 percent; an empty cell shows at the expected rate; nothing from 1 to 29 is released; the real budgets were not charged |
| Analyst pages | `dev/test_analytics_routes.py` (22 checks): other roles are refused, previews charge nothing, bad groupings and datasets are refused with nothing charged, forged tokens get 403, a run charges once, another analyst gets 404 on someone's result, the CSV is free, an exhausted budget gives 409 and releases nothing |
| Item needs in the database | `dev/test_item_schema.py` (48 checks): the constraints on item needs and offers, the triggers for offers, state changes and fulfilment, the views, and the app role's grants (it can read the catalogue and cannot delete an offer); `needs_analytics` shows a verified item need as kind item |
| Posting an item need | `dev/test_item_posting.py` (42 checks): the item form, limits, disabled types and the pending limit |
| Verifying item needs | `dev/test_item_verify.py` (26 checks): the verifier's review of item needs, with forged tokens refused |
| Item needs on the board | `dev/test_item_board.py` (27 checks): the board and need pages show the item need and its unit totals, no private markers, and a money need's page is unchanged |
| Offering units, release and decline | `dev/test_item_claims.py` (36 checks): the offer form, who may offer, release or decline by a verifier, cancelling, and what each side can see |
| Handover, confirmation and disputes | `dev/test_item_handover.py` (28 checks): the donor's statement, the requester confirming or disputing, a verifier settling, and the photo and waiver routes no longer existing |
| The verifier nudge | `dev/test_item_chase.py` (12 checks): handed-over offers the requester has not answered are listed, and a verifier can confirm for the requester or open a dispute |
| Front-page item figures | `dev/test_item_stats.py` (12 checks): offered and handed-over units change nothing, confirmed ones add units and donors, a donor counts once, cancelled offers change nothing, the money figures do not move, and the board shows the item line |
| Item needs in the analytics | `dev/test_item_analytics.py` (18 checks): kind and item category groupings work on the live dataset only, fixed cell counts, the cap of 3 across a person's needs, province and status include item needs while need type and goal band do not, and the app role reads the new columns |
| Noise is statistically consistent with epsilon | `dev/test_dp_statistical.py` and `dev/test_engine_dp.py`, rerun on this branch on 4 November 2026. Counts at sensitivity 1: worst excess 2.4 standard errors; sums at sensitivity 500: 3.1; one added person in a grouped cell: 2.6; the unchanged cell: 0.1. The threshold is 4. Three mis-calibrated controls were flagged at 64, 100 and 46. Noise correlation between cells was 0.0030 against a limit of 0.0089. These test the shared noise function, not the needs engine's sensitivity of 3, which the engine test checks more coarsely (the scale within 10 percent). They cannot detect floating-point leaks or test composition |

Run on this branch but not counted in the table above: `dev/test_ratelimit.py` (the login lockout: a correct password during a lockout is refused with 429, and exactly 5 failures were recorded; it prints its expected values and does not assert them). `dev/test_ratelimit_expiry.py` (the lockout ends after 5 minutes) has not yet been run with the wait it needs. `dev/attack_sim.py` is a simulation and `dev/check_failures.py` a helper for the lockout tests. The tests of the retired design (the query builder page, the beneficiary engine's noise and results store, and the old post-a-need and pledge forms) were removed on 4 November 2026 after they failed against this schema. They remain at the tag `demo-old-schema`.

Known weak spots in the tests: the noise checks are statistical and can fail by chance, rarely (the scale check has about 5,000 samples and a 10 percent tolerance chosen by the author). The route-level pledge race test may be settled by the form's check before the trigger is reached. The tests create and remove their own rows, but they should not be run against a database that holds real data.

Argued but not tested:
- Epsilon-differential privacy of the needs engine. The guarantee rests on the cap giving sensitivity 3 and on the Laplace mechanism. The tests show the cap on exact counts and the noise scale, not the guarantee itself.
- A stolen database yields only hashes and plain-text records. Not attempted.
- The audit log is append-only against the app role, which is tested. The owner is outside the claim.
- Sign-ups counting toward the login lockout of the same address.
- The board's rate limit across several server processes or behind a proxy.
- That a transfer really happened. Only the parties' statements and a verifier's decision are recorded.
- The 150-cell guard of the analytics engine, tested only with the limit lowered to 10.

Checked in a browser, not by a test (on this branch, by the author): the analyst page with both budgets, the admin page, and a verifier sign-in; and the requester's "My needs" page for four demo accounts, showing a published need with its funding line and a withdraw option, a pending need, a fully funded need and a withdrawn need with its reason.

Not yet checked in a browser on this branch: the public board and need pages, the map, the pledge form with the suggested amount, my pledges, the requester's receipt buttons, the verifier's queue and pledge pages, and the analytics result pages. The interface has not been restyled, and it has not been looked at on a phone.

Run from a clean clone on 4 November 2026: the branch `financial-pivot` at commit `0fda238` (the same files that `main` now holds, under different commit hashes, because the merge rebased them) was cloned from GitHub into a new folder, installed from `requirements.txt` into a new virtual environment, built into a new database from `schema.sql`, and seeded with the four seed scripts, which reproduced the same figures as the working database (20,194 synthetic needs from 12,000 people; 54 fulfilled, 6 published, 2 pending and 1 withdrawn demo needs; 719 confirmed, 16 sent, 15 pledged and 2 cancelled pledges). The twelve tests listed above then all passed there with the same counts. A started server answered the login, sign-up, board, need and map-data pages, refused an unknown filter with 400, and refused anonymous requests to the requester, verifier and admin pages with 401. Not covered by that run: creating the `dp_app` role (it already existed on the test machine), rebuilding the map outlines from the dataset (the outline file is not committed, so the board answered without it), the browser checks, and the older scripts in section 5 that were run separately.

## 6. Reproducing

`requirements.txt` pins the environment. `schema.sql` builds every table, view, trigger and grant, and `README.md` has the setup steps. Seeds: `seed_users.py` creates the analyst and admin accounts, `seed_verifier.py` a verifier, `seed_needs_synth.py` the synthetic needs, and `seed_demo.py` the demo accounts, needs and pledges. Settings and secrets live in `.env`; copy `.env.example` and fill in the placeholders. The province and district lists are committed. The map outlines are not, and the README explains how to rebuild them. Run a test with `python -m dev.test_board`, for example. Each asks for the database owner's password.
