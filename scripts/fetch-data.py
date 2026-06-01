#!/usr/bin/env python3
"""
Fetch active projects from Insightly and generate data.json for the dashboard.
Runs as a GitHub Action every 15 minutes.
"""
import json, os, sys, urllib.request, urllib.error, urllib.parse, base64, datetime

API_KEY = os.environ.get("INSIGHTLY_API_KEY", "")
BASE = "https://api.na1.insightly.com/v3.1"

ACTIVE_STATUSES = {"IN PROGRESS", "NOT STARTED", "DEFERRED"}

PIPELINES = {
    934369: {"name": "T1 Land Survey", "tier": "T1"},
    934372: {"name": "T2 Land Division & Realignment", "tier": "T2"},
    1152108: {"name": "Land Division v2", "tier": "T2"},
    783292: {"name": "APA Project", "tier": "T3"},
}

STAGE_RESPONSIBILITY = {
    "Prepare Project": "AR (Alex)", "Field Work Scheduled": "DP (Damiano)",
    "Prepare Plan": "TR (Tristan)", "Send Plan to Client": "DP (Damiano)",
    "Invoice": "AR (Alex)",
    "1. Concept Planning": "AR (Alex)", "Concept Planning": "AR (Alex)",
    "2. PlanSA Lodgement": "Council/Authority", "PlanSA Lodgement": "Council/Authority",
    "3. Authority Assessment": "Council/Authority", "Authority Assessment": "Council/Authority",
    "4. Conditions & Compliance": "Client", "Conditions & Compliance": "Client",
    "5. Certified Survey": "DP (Damiano)", "Certified Survey": "DP (Damiano)",
    "6. Land Division Certificate Lodged with PlanSA": "Council/Authority",
    "Land Division Certificate Lodged with PlanSA": "Council/Authority",
    "7. SCAP Application": "SCAP/DAC", "SCAP Application": "SCAP/DAC",
    "8. Final Authority Approvals": "SCAP/DAC", "Final Authority Approvals": "SCAP/DAC",
    "9. LTO Lodgement": "LTO", "LTO Lodgement": "LTO",
    "10. LTO Approval": "LTO", "LTO Approval": "LTO",
    "11. Complete": "AR (Alex)", "Complete": "AR (Alex)",
    "Search & DBYD": "AR (Alex)", "Plan Drawing & Check": "TR (Tristan)",
    "Plan to Client": "DP (Damiano)", "Further Work Required": "Client",
    "Invoice & Close": "AR (Alex)",
}

def api_get(endpoint, params=None):
    url = f"{BASE}/{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    auth = base64.b64encode(f"{API_KEY}:".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth}",
        "Accept": "application/json"
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"API error {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"Request error: {e}", file=sys.stderr)
        return []

def get_all_projects():
    """Fetch all projects, paginating 500 at a time."""
    all_projects = []
    skip = 0
    while True:
        params = {"top": 500, "skip": skip, "brief": "false"}
        batch = api_get("Projects/Search", params)
        if not batch:
            break
        all_projects.extend(batch)
        print(f"  Fetched {len(all_projects)} projects so far...")
        if len(batch) < 500:
            break
        skip += 500
    return all_projects

def get_pipeline_stages():
    stages = {}
    for pid in PIPELINES:
        data = api_get(f"Pipelines/{pid}")
        if data and "STAGES" in data:
            for s in data["STAGES"]:
                stages[s["STAGE_ID"]] = {
                    "name": s["STAGE_NAME"],
                    "order": s.get("STAGE_ORDER", 0),
                    "pipeline_id": pid
                }
    return stages

def get_contact_name(project):
    links = project.get("LINKS", [])
    pm_link = None
    client_link = None
    for link in links:
        role = (link.get("ROLE") or "").strip().lower()
        if any(skip in role for skip in ["site contact", "conveyancer", "planner", "council", "contractor"]):
            continue
        if any(pm in role for pm in ["project manager", "pm"]):
            pm_link = link
        elif "client" in role:
            client_link = link
    best = pm_link or client_link
    if not best:
        return ""
    contact_id = best.get("CONTACT_ID")
    if not contact_id:
        org_id = best.get("ORGANISATION_ID")
        if org_id:
            org = api_get(f"Organisations/{org_id}")
            return org.get("ORGANISATION_NAME", "") if org else ""
        return ""
    contact = api_get(f"Contacts/{contact_id}")
    if contact:
        first = contact.get("FIRST_NAME", "")
        last = contact.get("LAST_NAME", "")
        return f"{first} {last}".strip()
    return ""

def main():
    if not API_KEY:
        print("ERROR: INSIGHTLY_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    
    print("Fetching pipeline stages...")
    stages = get_pipeline_stages()
    print(f"  Got {len(stages)} stages")
    
    print("Fetching all projects from Insightly...")
    raw_projects = get_all_projects()
    print(f"  Got {len(raw_projects)} total projects")
    
    # CLIENT-SIDE FILTER: only active statuses + known pipelines
    active_projects = [p for p in raw_projects 
                       if p.get("STATUS", "") in ACTIVE_STATUSES 
                       and p.get("PIPELINE_ID") in PIPELINES]
    print(f"  Filtered to {len(active_projects)} active projects (statuses: {', '.join(ACTIVE_STATUSES)})")
    
    projects = []
    contact_cache = {}
    
    for i, p in enumerate(active_projects):
        pid = p.get("PIPELINE_ID")
        tier = PIPELINES[pid]["tier"]
        stage_id = p.get("STAGE_ID")
        stage_info = stages.get(stage_id, {})
        stage_name = stage_info.get("name", "Unknown")
        stage_order = stage_info.get("order", 0)
        
        address = ""
        sa_water = ""
        responsibility = ""
        for cf in p.get("CUSTOMFIELDS", []):
            fid = cf.get("FIELD_NAME", "")
            val = cf.get("FIELD_VALUE", "") or ""
            if fid == "PROJECT_FIELD_3": address = val
            elif fid == "PROJECT_FIELD_6": sa_water = val
            elif fid == "Current_Responsibility__c": responsibility = val
        
        if not responsibility:
            responsibility = STAGE_RESPONSIBILITY.get(stage_name, "")
        
        project_id = p["PROJECT_ID"]
        if project_id not in contact_cache:
            try:
                contact_cache[project_id] = get_contact_name(p)
            except Exception:
                contact_cache[project_id] = ""
        
        if (i + 1) % 20 == 0:
            print(f"  Processing contacts: {i+1}/{len(active_projects)}")
        
        projects.append({
            "id": project_id,
            "name": p.get("PROJECT_NAME", ""),
            "tier": tier,
            "status": p.get("STATUS", ""),
            "address": address,
            "client": contact_cache[project_id],
            "link": f"https://crm.na1.insightly.com/details/project/{project_id}",
            "responsibility": responsibility,
            "days_in_stage": 0,
            "stage_name": stage_name,
            "sa_water_ref": sa_water,
            "stage_order": stage_order,
        })
    
    projects.sort(key=lambda x: ({"T1":1,"T2":2,"T3":3}.get(x["tier"],9), x["name"]))
    
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    adelaide = datetime.timezone(datetime.timedelta(hours=9, minutes=30))
    timestamp = now_utc.astimezone(adelaide).isoformat()
    
    output = {"projects": projects, "last_updated": timestamp, "total": len(projects)}
    
    with open("data.json", "w") as f:
        json.dump(output, f, indent=2)
    
    print(f"\n✅ Generated data.json: {len(projects)} active projects at {timestamp}")

if __name__ == "__main__":
    main()
