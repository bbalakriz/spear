# Data Governance Risk Rating Framework

| | |
|---|---|
| **Document ID** | DG-RISK-v3 |
| **Version** | 3.0 |
| **Status** | Approved |
| **Owner** | Data Governance Office |
| **Effective date** | 2025-11-01 |
| **Next review** | 2026-11-01 |
| **Applies to** | All datasets in the enterprise data catalog |

## 1. Risk Tiering Methodology

Every dataset and every classification exception is assigned a risk tier from 1
(lowest) to 4 (highest), scored on three axes:

| Axis | Weight | Scored on |
|---|---|---|
| Sensitivity | 50% | Classification level, regulatory scope, harm from disclosure |
| Volume | 25% | Record count, number of individuals affected |
| Exposure | 25% | Access surface: internal only, partner facing, public facing, regions served |

The composite score maps to a tier: 0.0-1.4 → tier 1, 1.5-2.4 → tier 2, 2.5-3.4 →
tier 3, 3.5-4.0 → tier 4. Customer financial and health data defaults to tier 3 or 4
regardless of the computed score, a dataset may never score below its classification
floor.

## 2. Risk-Based Controls by Tier

| Tier | Required controls (cumulative) | Review cadence |
|---|---|---|
| **1** | Standard encryption, access logging | Annual |
| **2** | Tier 1 + role based entitlement review | Annual |
| **3** | Tier 2 + quarterly access recertification, masked non-production copies, DLP on exports | Quarterly |
| **4** | Tier 3 + named executive sponsor, documented incident response runbook, cross region transfer register entry | Monthly |

The risk tier determines both the review cadence and which approvers must sign off on
any exception, per the thresholds in POLICY-DG-055 section 3.

## 3. Tier Changes

A dataset's tier is re-scored whenever its classification changes, its volume grows
by an order of magnitude, or its exposure surface changes (a new region, a new
partner integration). A tier increase takes effect immediately; a tier decrease
requires a Data Governance Officer's approval and a 30 day observation window.

## 4. Related Documents

- POLICY-DG-042, Enterprise Data Classification and Handling Policy
- POLICY-DG-055, Data Classification Exception Handling Framework
