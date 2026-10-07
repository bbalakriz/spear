# Enterprise Data Classification and Handling Policy

| | |
|---|---|
| **Document ID** | POLICY-DG-042 |
| **Version** | 4.2 |
| **Status** | Approved |
| **Owner** | Data Governance Office |
| **Effective date** | 2026-01-15 |
| **Next review** | 2027-01-15 |
| **Applies to** | All business units, all environments processing enterprise data |

## 1. Purpose and Scope

This policy defines how all enterprise data is classified, handled, retained, and
monitored across its full lifecycle, from creation through disposal. It applies to
every dataset registered in the enterprise data catalog, every system that stores or
processes classified data, and every employee, contractor, and third party granted
access to it. Compliance with this policy is mandatory and is verified through the
audit program described in section 8.

## 2. Classification Levels

All enterprise data must be classified into one of four levels. Every dataset in the
catalog carries exactly one label, and the label is reviewed at least once per year by
the data owner.

| Level | Definition | Example data | Disclosure risk |
|---|---|---|---|
| **Public** | Approved for unrestricted disclosure | Published pricing, marketing material | None |
| **Internal** | Limited to employees and approved contractors | Org charts, internal runbooks | Low, reputational only |
| **Confidential** | Requires a documented business need to access | CRM records, billing history, vendor agreements | Financial and contractual harm |
| **Restricted** | Requires named approval from the owning department before any new access | Regulated personal data, compensation data, security architecture | Regulatory and legal exposure |

The classification of a dataset is inherited by every derived copy, extract, report,
and non-production clone derived from it, unless a formal reclassification under
section 6 has been approved. Deriving a dataset never lowers its classification by
default.

## 3. Customer Data Classification and Retention

Customer relationship management data, including CRM records, support tickets, and
billing history, is classified as **Confidential**.

- Encryption at rest: AES-256, managed keys rotated at least annually.
- Encryption in transit: TLS 1.2 or higher on every hop, internal and external.
- Retention: seven years from the date of last account activity, after which records
  must be purged or irreversibly anonymized.
- Legal hold: a documented hold suspends purge for the named records only, never the
  whole dataset, and is reviewed every 90 days.
- Cross region export: any export of customer data outside the primary processing
  region requires a signed data transfer assessment and an entry in the transfer register.

## 4. Access Control Model

Access to classified data follows least privilege, granted through role based
entitlements reviewed on the cadence in section 5.

- **Internal** data: granted by default to authenticated employees with a role that
  requires it.
- **Confidential** data: granted only with a documented business need recorded in the
  entitlement system, sponsored by the requester's manager.
- **Restricted** data: granted only with named approval from the owning department's
  data owner, time bound, and recertified on every renewal.

Access entitlements are never inherited through group membership alone for
Confidential and Restricted data: every grant names the human approver who authorized
it, and that name is queryable for the life of the grant.

## 5. Access Logging and Monitoring

Every read or write operation against Confidential or Restricted data must be logged
with the requesting identity, timestamp, operation, and business justification.

- Logs are retained for three years and are immutable once written.
- The data governance team reviews access logs quarterly; the audit team reviews them
  annually and during regulatory examinations.
- Access outside an employee's normal working pattern (unusual hour, unusual volume,
  first time access to a new dataset class) triggers an automated review within 24 hours.
- These logs are the primary evidence used during audits, and their completeness is
  itself an audited control: a dataset found with a logging gap is treated as an
  incident, not a configuration defect.

## 6. Reclassification and Exceptions

Requests to lower the classification of any dataset, or to deviate from any control
in this policy, follow the exception process in POLICY-DG-055: the request names the
dataset, the requested change, the business justification, and the compensating
controls, and it requires approval from a Vice President or above in addition to a
Data Governance Officer whenever the change lowers the classification of Confidential
or Restricted data. No exception may be self-approved by the requesting analyst or
engineer, regardless of seniority.

## 7. Related Documents

- POLICY-DG-055, Data Classification Exception Handling Framework
- DG-RISK-v3, Data Governance Risk Rating Framework
- The retention schedule maintained by the records management office

## 8. Audit and Enforcement

Compliance with this policy is verified by the enterprise audit program. Findings are
rated by severity, tracked to closure in the governance ticketing queue, and reported
to the risk committee. Willful circumvention of this policy is a disciplinary matter.
