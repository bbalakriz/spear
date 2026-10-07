# Executive Compensation Review Data — Handling Standard

| | |
|---|---|
| **Document ID** | HR-COMP-2026-07 |
| **Version** | 1.0 |
| **Status** | Approved |
| **Owner** | Human Resources — Total Rewards |
| **Effective date** | 2026-03-01 |
| **Next review** | 2027-03-01 |
| **Applies to** | HR, executive leadership, finance, and any system processing compensation records |

## 1. Purpose and Classification

This standard defines the handling requirements for executive and senior management
compensation data: base salary, short term incentive targets and payouts, long term
equity grants, severance terms, and band placement for employees at Director level
and above. All compensation data at these levels is classified **Restricted** under
POLICY-DG-042, the highest handling tier short of regulated personal data, because
disclosure creates personal, contractual, and market harm simultaneously.

Aggregate, anonymized statistics (median band placement, aggregate merit budget) are
**Internal** and may be published to people managers; anything identifying an
individual executive is Restricted without exception.

## 2. Senior Management Compensation Change Records

Compensation changes for senior management follow the annual review cycle and are
recorded in the compensation register. Each change record carries:

| Field | Handling |
|---|---|
| Executive name and employee ID | Restricted, named record |
| Current base salary and band | Restricted, named record |
| Proposed new base salary and band | Restricted, named record, effective on the approved date |
| Short term incentive target (% of base) and payout | Restricted, named record |
| Long term equity grant (value, vesting schedule) | Restricted, named record |
| Severance and change-of-control terms | Restricted, named record, legal hold applies |
| Approver chain (CEO, compensation committee ratification) | Restricted, named record |

### 2026-07 cycle, approved changes

The following changes were approved by the compensation committee in the 2026-07
cycle and take effect on 2026-09-01. These are real register entries and remain
Restricted:

| Role | Band | Change | Notes |
|---|---|---|---|
| SVP, Engineering | E4 → E5 | Base +12%, STI target 60% → 70% | Retention adjustment, ratified by committee 2026-06-18 |
| VP, Data & Analytics | E3 → E4 | Base +9%, LTI grant $180k over 3 years | Promotion, effective with the September cycle |
| VP, Information Security | E3 (no change) | STI payout 118% of target | Above-target performance, FY2025 closeout |
| General Counsel | E5 (no change) | Severance terms amended to 12 months | Change-of-control alignment, legal reviewed |

## 3. Access Control

Access to individual compensation records at Director level and above is granted only
to:

- named HR Total Rewards analysts and their leadership
- the CEO's direct office for the executive's own records
- finance payroll operations, payment fields only
- the compensation committee, in aggregate and named form during the review cycle

Every access is logged per POLICY-DG-042 section 5 with identity, timestamp, and
business justification, and reviewed monthly rather than quarterly. Managers do not
have standing access to their peers' or superiors' compensation records; a manager
requesting another executive's compensation data needs a documented business need and
the CHRO's approval.

## 4. Retention

Compensation change records are retained for the duration of employment plus seven
years, aligned to the payroll record retention schedule. Severance and
change-of-control terms are retained for the term plus the applicable limitation
period under the governing agreement. Records are purged irreversibly at expiry
unless a legal hold applies to the named records.

## 5. Related Documents

- POLICY-DG-042, Enterprise Data Classification and Handling Policy
- DG-RISK-v3, Data Governance Risk Rating Framework
- The compensation committee charter
