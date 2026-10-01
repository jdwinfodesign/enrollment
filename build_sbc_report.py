#!/usr/bin/env python3
"""
build_sbc_report.py

COPY of build_database_report.py with four v2 additions (2026-09-29).
The original is untouched. Additions:
  1. state           on each coverage row and each plan
  2. sbc_applicable  on each coverage row (True for medical only)
  3. network_coverage on each common_medical_events row, derived from the
                     in/out-of-network arrays
  4. expanded _report_meta: report_version, gaps (SBC-slot aware),
                     content_dependencies, notes
Output files are named sbc-report-<patient_id>.json.

Original description follows.

Queries a HAPI FHIR server and produces one JSON "database report" per
employee (Patient), containing only what's actually stored in the FHIR
database today.

Gaps in the plan-level data that the schema anticipates but hasn't been
populated yet (excluded services, other covered services, coverage
examples) are included as explicit placeholder objects rather than
silently omitted, so downstream processing can see what's still pending
and where it's supposed to come from ("source": "policy_info").

This report does NOT include any regulatory/boilerplate content
(grievance & appeals rights, COBRA rights, language access services) --
that's DITA-side conditional content, not database content, and is out
of scope here by design.

Usage:
    python build_sbc_report.py
    python build_sbc_report.py --base http://localhost:8080/fhir --out ./reports
"""

import argparse
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

COPAY_TYPE_DISPLAY = {
    "copay": "Copay",
    "copaypct": "Coinsurance",
    "deductible": "Deductible",
    "maxoutofpocket": "Maximum out-of-pocket",
}

COVERAGE_TYPE_CODE_TO_CATEGORY = {
    "EHCPOL": "medical",
    "DENTAL": "dental",
    "VISPOL": "vision",
}


def fetch_all(base, resource_type, params=None):
    """Fetch every page of a FHIR search, following Bundle.link[relation=next]."""
    query = "_count=100"
    if params:
        query += "&" + urllib.parse.urlencode(params)
    url = f"{base}/{resource_type}?{query}"
    resources = []
    while url:
        with urllib.request.urlopen(url) as resp:
            bundle = json.load(resp)
        for entry in bundle.get("entry", []):
            if "resource" in entry:
                resources.append(entry["resource"])
        url = next(
            (link["url"] for link in bundle.get("link", []) if link["relation"] == "next"),
            None,
        )
    return resources


def ref_id(reference):
    """Extract the logical id from a FHIR reference string like 'Organization/alderwood'."""
    if not reference:
        return None
    return reference.split("/")[-1]


def money_str(cost):
    value = cost["value"]
    # Dollar amounts are stored as Quantity with unit "USD" (ISO 4217).
    # "currency" is also accepted for data loaded from an older bundle.
    if "currency" in value or value.get("unit") == "USD":
        amount = value["value"]
        return f"${amount:,.2f}" if isinstance(amount, float) else f"${amount:,}"
    if value.get("unit") == "%":
        return f"{value['value']}%"
    return str(value.get("value"))


def extract_costs(benefit):
    """Split a specificCost.benefit's cost[] entries by network applicability."""
    result = {"in_network": [], "out_of_network": []}
    for cost in benefit.get("cost", []):
        code = cost["type"]["coding"][0]["code"]
        label = COPAY_TYPE_DISPLAY.get(code, code)
        applicability = cost["applicability"]["coding"][0]["code"]
        key = "in_network" if applicability == "in-network" else "out_of_network"
        result[key].append({"type": label, "amount": money_str(cost)})
    return result


def extract_plan_costsharing(plan_resource):
    """
    Split InsurancePlan.plan[].specificCost into 'important_questions'
    (overall + prescription-drug categories) and 'common_medical_events'
    (every other category), pulling matching limits/requirement notes
    from InsurancePlan.coverage[].benefit[].
    """
    benefit_notes = {}
    for cov in plan_resource.get("coverage", []):
        for benefit in cov.get("benefit", []):
            code = benefit["type"]["coding"][0]["code"]
            note = {}
            if "requirement" in benefit:
                note["requirement"] = benefit["requirement"]
            if "limit" in benefit:
                limits = []
                for lim in benefit["limit"]:
                    lcode = lim["code"]["coding"][0]
                    limits.append(
                        {
                            "type": lcode.get("display", lcode.get("code")),
                            "value": lim["value"]["value"],
                        }
                    )
                note["limits"] = limits
            benefit_notes[code] = note

    important_questions = []
    common_medical_events = []

    for plan_entry in plan_resource.get("plan", []):
        for sc in plan_entry.get("specificCost", []):
            cat = sc["category"]["coding"][0]
            code = cat["code"]
            row = {"category": code, "label": cat.get("display", code)}
            for benefit in sc.get("benefit", []):
                row.update(extract_costs(benefit))
            row.update(benefit_notes.get(code, {}))
            if code in ("overall", "prescription-drug"):
                important_questions.append(row)
            else:
                common_medical_events.append(row)

    return important_questions, common_medical_events


def add_network_coverage(rows):
    """v2: make covered / not-covered explicit on common_medical_events rows."""
    for row in rows:
        row["network_coverage"] = {
            "in_network": "covered" if row.get("in_network") else "not-covered",
            "out_of_network": "covered" if row.get("out_of_network") else "not-covered",
        }


def state_from_hios(hios_id):
    """HIOS Plan IDs in this demo look like 99101NY0010001: state = chars 5-6."""
    if hios_id and len(hios_id) >= 7 and hios_id[5:7].isalpha():
        return hios_id[5:7].upper()
    return None


def state_for_coverage(patient, group_number, hios_id):
    """Employee state: Patient.address first, then GRP-<ST>-nnnn, then HIOS ID."""
    for addr in patient.get("address", []):
        if addr.get("state"):
            return addr["state"].upper()
    parts = (group_number or "").split("-")
    if len(parts) >= 3 and len(parts[1]) == 2 and parts[1].isalpha():
        return parts[1].upper()
    return state_from_hios(hios_id)


def build_plan_report(plan_resource, org_lookup):
    important_questions, common_medical_events = extract_plan_costsharing(plan_resource)
    owner_id = ref_id(plan_resource.get("ownedBy", {}).get("reference"))
    owner = org_lookup.get(owner_id, {})
    add_network_coverage(common_medical_events)
    hios_id = next((i["value"] for i in plan_resource.get("identifier", [])), None)
    return {
        "name": plan_resource.get("name"),
        "hios_id": hios_id,
        "state": state_from_hios(hios_id),
        "plan_type": plan_resource.get("type", [{}])[0].get("text"),
        "period": plan_resource.get("period"),
        "insurer": owner.get("name"),
        "important_questions": important_questions,
        "common_medical_events": common_medical_events,
        "excluded_services": {
            "status": "tbd",
            "source": "policy_info",
            "note": "Not yet modeled in the FHIR store — see scenario bible Parked Decisions (2026-09-26).",
        },
        "other_covered_services": {
            "status": "tbd",
            "source": "policy_info",
            "note": "Not yet modeled in the FHIR store.",
        },
        "coverage_examples": {
            "status": "approximate",
            "source": "policy_info",
            "note": (
                "Illustrative fictive figures only, not calculator-grade — "
                "flag for talking points if asked how derived. Not currently "
                "stored in the FHIR Bundle."
            ),
        },
    }


def sbc_slot_gaps(hios_id):
    """v2: SBC slots the CMS template needs that the FHIR store does not carry."""
    pre = f"plan[{hios_id}]"
    none = "none"
    return [
        {"field": pre + ".common_medical_events[*].deductible_applies",
         "sbc_slot": "What You Will Pay cells (deductible applies / does not apply)",
         "source": none, "status": "no-source",
         "note": "The CMS sample states this per service. The report carries type and amount only."},
        {"field": pre + ".important_questions[*].services_covered_before_deductible",
         "sbc_slot": "Are there services covered before you meet your deductible?",
         "source": none, "status": "no-source"},
        {"field": pre + ".important_questions[*].out_of_pocket_exclusions",
         "sbc_slot": "What is not included in the out-of-pocket limit?",
         "source": none, "status": "no-source"},
        {"field": pre + ".contact",
         "sbc_slot": "Plan contact, Glossary contact and network directory website/phone",
         "source": none, "status": "no-source"},
        {"field": "coverages[*].coverage_tier", "sbc_slot": "Coverage for: (Individual | Family)",
         "source": none, "status": "no-source",
         "note": "Employee-only per scenario bible axiom 4; not carried in the report."},
        {"field": pre + ".minimum_essential_coverage / minimum_value",
         "sbc_slot": "Does this plan provide Minimum Essential Coverage? / meet Minimum Value Standards?",
         "source": none, "status": "no-source"},
        {"field": pre + ".level / market_segment",
         "sbc_slot": "(profiling axes for DITA conditional processing)",
         "source": "scenario-bible", "status": "no-source",
         "note": "Defined in the scenario bible; not present in the FHIR store or this report."},
        {"field": pre + ".common_medical_events[rehabilitation]",
         "sbc_slot": "Rehabilitation services and Habilitation services (two rows)",
         "source": "policy_info", "status": "mismatch",
         "note": "If the store has one merged row, the CMS template still lists two."},
    ]


CONTENT_DEPENDENCIES = [
    "Your Rights to Continue Coverage: state, HHS or DOL agency contacts (varies by state; key on coverages[].state).",
    "Your Grievance and Appeals Rights: contact information (varies by state).",
    "Language Access Services: telephone numbers.",
    "Why This Matters: standardized CMS text (static).",
]


def build_notes(plans_used):
    notes = [
        "All names, plans, identifiers and figures are fictive.",
        "HIOS Plan ID is a pointer to an authoritative external source, not a claim about real-world "
        "issuance (decision 2026-09-28).",
        "In common_medical_events an empty in_network or out_of_network array means not covered; see "
        "network_coverage. In important_questions an empty array means no separate value, which is "
        "not the same thing.",
        "sbc_applicable is false for dental and vision on the assumption that stand-alone dental and "
        "vision coverage is outside the SBC requirement. Confirm before relying on it.",
        "State is taken from Patient.address, else the group number (GRP-<ST>-nnnn), else the HIOS ID.",
    ]
    for hios_id, plan in plans_used.items():
        cats = {r["category"] for r in plan["common_medical_events"]}
        if plan.get("plan_type") == "HMO":
            notes.append(f"plan[{hios_id}]: HMO. Out-of-network care is not covered except emergency services.")
        if not {"pediatric-vision-exam", "pediatric-dental"} & cats:
            notes.append(f"plan[{hios_id}]: children's eye and dental rows are absent, not merely "
                         "not covered. Consumers must handle an absent row.")
        for r in plan["common_medical_events"]:
            if r.get("limits") and not r["in_network"] and not r["out_of_network"]:
                notes.append(f"plan[{hios_id}]: {r['category']} is not covered but still carries a limit "
                             "in the source data. Retained as-is pending review.")
    return notes


def build_employee_report(patient, coverages, plan_lookup, org_lookup):
    name = patient.get("name", [{}])[0]
    full_name = " ".join(name.get("given", []) + [name.get("family", "")]).strip()

    coverage_rows = []
    plans_used = {}
    gaps = []

    for cov in coverages:
        cov_type_code = cov["type"]["coding"][0]["code"]
        category = COVERAGE_TYPE_CODE_TO_CATEGORY.get(cov_type_code, cov_type_code)

        classes = cov.get("class", [])
        plan_class = next((c for c in classes if c["type"]["coding"][0]["code"] == "plan"), {})
        group_class = next((c for c in classes if c["type"]["coding"][0]["code"] == "group"), {})
        hios_id = plan_class.get("value")

        payor_id = ref_id(cov.get("payor", [{}])[0].get("reference"))
        policyholder_id = ref_id(cov.get("policyHolder", {}).get("reference"))

        coverage_rows.append(
            {
                "category": category,
                "subscriber_id": cov.get("subscriberId"),
                "group_number": group_class.get("value"),
                "employer": group_class.get("name") or org_lookup.get(policyholder_id, {}).get("name"),
                "state": state_for_coverage(patient, group_class.get("value"), hios_id),
                "insurer": org_lookup.get(payor_id, {}).get("name"),
                "plan_hios_id": hios_id,
                "plan_name": plan_class.get("name"),
                "effective_date": cov.get("period", {}).get("start"),
                "sbc_applicable": category == "medical",
            }
        )

        if hios_id and hios_id not in plans_used:
            plan_resource = plan_lookup.get(hios_id)
            if plan_resource:
                plans_used[hios_id] = build_plan_report(plan_resource, org_lookup)
            else:
                gaps.append(
                    {
                        "field": f"plan[{hios_id}]",
                        "source": "policy_info",
                        "note": (
                            f"Coverage references plan HIOS ID {hios_id} but no "
                            "matching InsurancePlan was found in the FHIR store."
                        ),
                    }
                )

    for hios_id, plan_report in plans_used.items():
        for field, slot in (
            ("excluded_services", "Services Your Plan Generally Does NOT Cover"),
            ("other_covered_services", "Other Covered Services"),
            ("coverage_examples", "Coverage Examples (three sample events)"),
        ):
            info = plan_report[field]
            gaps.append(
                {
                    "field": f"plan[{hios_id}].{field}",
                    "sbc_slot": slot,
                    "source": info["source"],
                    "status": info["status"],
                }
            )
        gaps.extend(sbc_slot_gaps(hios_id))

    return {
        "employee": {
            "id": patient["id"],
            "name": full_name,
            "gender": patient.get("gender"),
            "birth_date": patient.get("birthDate"),
        },
        "coverages": coverage_rows,
        "plans": plans_used,
        "_report_meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "report_version": "2",
            "generated_from": "HAPI FHIR server (database report only — no regulatory/DITA content included)",
            "gaps": gaps,
            "content_dependencies": CONTENT_DEPENDENCIES,
            "notes": build_notes(plans_used),
        },
    }


def main():
    parser = argparse.ArgumentParser(
        description="Build per-employee database reports from a HAPI FHIR server."
    )
    parser.add_argument("--base", default="http://localhost:8080/fhir", help="FHIR server base URL")
    parser.add_argument("--out", default="./reports", help="Output directory for JSON reports")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching from {args.base} ...")
    patients = fetch_all(args.base, "Patient")
    plans = fetch_all(args.base, "InsurancePlan")
    orgs = fetch_all(args.base, "Organization")

    plan_lookup = {}
    for plan in plans:
        for ident in plan.get("identifier", []):
            plan_lookup[ident["value"]] = plan

    org_lookup = {org["id"]: org for org in orgs}

    print(f"Found {len(patients)} patients, {len(plans)} plans, {len(orgs)} organizations.")

    for patient in patients:
        coverages = fetch_all(args.base, "Coverage", {"patient": f"Patient/{patient['id']}"})
        report = build_employee_report(patient, coverages, plan_lookup, org_lookup)
        out_path = out_dir / f"sbc-report-{patient['id']}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        n_gaps = len(report["_report_meta"]["gaps"])
        print(f"  wrote {out_path}  ({len(coverages)} coverages, {n_gaps} gaps flagged)")


if __name__ == "__main__":
    main()
