# Data Engineering Team Onboarding Guide

| | |
|---|---|
| **Document ID** | ENG-ONBOARD-01 |
| **Version** | 2.1 |
| **Status** | Approved |
| **Owner** | Data Engineering Lead |
| **Effective date** | 2026-02-01 |
| **Next review** | 2027-02-01 |
| **Applies to** | New data engineering hires, internal |

## 1. Laptop and VPN Setup

New data engineering hires receive a laptop pre-imaged with the standard developer
toolchain. Connect to the corporate VPN using the client installed by IT before
requesting access to any internal repository. Your manager will sponsor your access
request in the identity system on day one, and most repository access is granted
within one business day.

If the VPN client fails to connect on first run, this is a known rough edge on newer
laptop hardware: restart the machine once, then re-install the client from the IT
portal before raising a ticket.

## 2. Local Development Environment

Clone the team's monorepo, install the project's dependency manager, and run the
bootstrap script to provision local services. Ask in the team channel if the
bootstrap script fails on first run, this is a known rough edge on newer laptop
hardware and usually resolves with a clean checkout.

## 3. Access to the Knowledge Base and Work Tracker

Day one access covers the enterprise knowledge base (Internal level documents) and
read access to the Data Governance work tracker. Requests against Confidential or
Restricted datasets need a documented business need sponsored by your manager, per
POLICY-DG-042 section 4; most data-governance documentation you need in your first
month is Internal or enterprise-wide and requires no extra approval.

## 4. First Week Checklist

1. VPN connected, monorepo cloned, bootstrap script run clean
2. Identity system access request approved by your manager
3. Team channel and on-call shadow rotation joined
4. Read POLICY-DG-042 (Enterprise Data Classification Policy) before touching any
   dataset outside your team's own scope

## 5. Related Documents

- POLICY-DG-042, Enterprise Data Classification and Handling Policy
- The team runbook, maintained in the monorepo
