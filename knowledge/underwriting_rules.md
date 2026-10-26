# Synthetic underwriting rules

These rules are invented for the assignment, not actual insurance guidance.
Structured metadata stays beside each rule so the application can enforce it without asking an LLM to calculate thresholds. Every heading is a retrievable document with a stable citation.

## R01 Covered event
<!-- rule: {"kind":"covered_type","claim_types":["*"]} -->
A claim type must appear in the policy's covered_claim_types list. A mismatch requires coverage review. Do not infer covered events from the word homeowners or auto alone.

## R02 Policy period
<!-- rule: {"kind":"active_period","claim_types":["*"]} -->
The loss date must fall within active_from and active_to, inclusive. A missing loss date means coverage cannot be determined, even when the policy is expired at report time.

## R03 Policy limit
<!-- rule: {"kind":"limit","claim_types":["*"]} -->
A claim amount above the policy limit requires coverage review. An amount equal to the limit passes this check but may trigger the near-limit anomaly rule.

## R04 New policy
<!-- rule: {"kind":"early_report","days":7,"claim_types":["*"]} -->
Claims reported within 7 days of policy inception, inclusive, require investigator review regardless of amount.

## R05 Near policy limit
<!-- rule: {"kind":"near_limit","fraction":0.02,"claim_types":["*"]} -->
Claim amounts between 98% and 100% of the policy limit, inclusive, warrant investigator review. This is a review indicator, not proof of fraud.

## R06 Repeated loss
<!-- rule: {"kind":"repeat_loss","days":365,"claim_types":["water_damage","theft","collision","fire"]} -->
A prior claim of the same type on the same policy within the preceding 365 days warrants investigator review. Exclude the current claim ID and future losses. Identify whether earlier findings are still unreviewed.

## R07 Late report
<!-- rule: {"kind":"late_report","days":30,"claim_types":["*"]} -->
A claim reported more than 30 days after the loss requires additional documentation.

## R08 Incomplete intake
<!-- rule: {"kind":"invalid_input","claim_types":["*"]} -->
Missing required fields, duplicate claim IDs, nonpositive amounts, invalid dates and unknown policies require correction before coverage evaluation. Do not infer missing facts from descriptions.
