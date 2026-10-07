# Data Classification Exception Handling Framework

| | |
|---|---|
| **Document ID** | POLICY-DG-055 |
| **Version** | 3.1 |
| **Status** | Approved |
| **Owner** | Data Governance Office |
| **Effective date** | 2026-01-15 |
| **Next review** | 2027-01-15 |
| **Applies to** | All classification exceptions, all business units |

## 1. Purpose

This framework defines how a business unit requests, reviews, and approves a
deviation from the default classification or controls defined in POLICY-DG-042. It
exists so that exceptions are explicit, time bound, traceable to a named human
approver, and reviewed on a fixed cadence rather than accumulating silently.

## 2. Requesting a Classification Exception

Business units that need to deviate from the default classification in POLICY-DG-042
file an exception request through the governance ticketing queue. The request must
state:

1. the dataset, by catalog identifier
2. the requested classification change (from level, to level)
3. the business justification
4. the proposed compensating controls
5. the requested expiry date, never longer than 12 months

Exception requests are tracked in the DATA_GOV governance project and remain in an
`in-review` state until a governance officer signs off. An exception that is approved
carries its ticket reference, its expiry, and its approver on the dataset's catalog
entry for its whole life.

## 3. Approval Thresholds

| Exception type | Required approvers |
|---|---|
| Lowers classification of Confidential or Restricted data | Vice President or above, **plus** a Data Governance Officer |
| Adds compensating controls without lowering classification | Data Governance Officer alone |
| Extends an existing exception past its expiry | Same approvers as the original exception |
| Emergency temporary access (max 7 days) | Data Governance Officer, ratified by the risk committee at the next sitting |

No exception may be self-approved by the requesting analyst or engineer, regardless
of seniority. A self-approval is a conflict of interest and is void: the request
routes to the next governance officer outside the requester's reporting line.

## 4. Compensating Controls

An exception that lowers classification must be offset by controls that reduce the
residual risk to at or below the risk tier of the original classification, per
DG-RISK-v3. Accepted compensating controls include, non exhaustively:

- field level masking or tokenization of the sensitive attributes
- row level access filtering scoped to the requesting team only
- shortened retention with verified purge
- enhanced access logging with per read alerting
- a named executive sponsor accepting the residual risk in writing

## 5. Review and Expiry

Every exception carries an expiry date. At expiry the exception lapses automatically
and the dataset returns to its default classification; continuing the exception
requires a new request. The governance team reviews the open exception register
quarterly, and every exception is re-evaluated whenever the underlying dataset's risk
tier changes.

## 6. Related Documents

- POLICY-DG-042, Enterprise Data Classification and Handling Policy
- DG-RISK-v3, Data Governance Risk Rating Framework
