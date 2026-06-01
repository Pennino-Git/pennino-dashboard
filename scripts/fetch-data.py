#!/usr/bin/env python3
"""
Fetch active projects from Insightly and generate data.json for the dashboard.
Runs as a GitHub Action every 15 minutes during business hours.
"""
import json, os, sys, urllib.request, urllib.error, base64, datetime

API_KEY = os.environ.get("INSIGHTLY_API_KEY", "")
BASE = "https://api.na1.insightly.com/v3.1"

# Pipeline IDs
PIPELINES = {
    934369: {"name": "T1 Land Survey", "tier": "T1"},
    934372: {"name": "T2 Land Division & Realignment", "tier": "T2"},
    1152108: {"name": "Land Division v2", "tier": "T2"},
    783292: {"name": "APA Project", "tier": "T3"},
}

# Stage -> Responsibility mapping
STAGE_RESPONSIBILITY = {
    # T1 Land Survey
    "Prepare Project": "AR (Alex)",
    "Field Work Scheduled": "DP (Damiano)",
    "Prepare Plan": "TR (Tristan)",
    "Send Plan to Client": "DP (Damiano)",
    "Invoice": "AR (Alex)",
    # Land Division v2
    "1. Concept Planning": "AR (Alex)",
    "2. PlanSA Lodgement": "Council/Authority",
    "3. Authority Assessment": "Council/Authority",
    "4. Conditions & Compliance": "Client",
    "5. Certified Survey": "DP (Damiano)",
    "6. Land Division Certificate Lodged with PlanSA": "Council/Authority",
    "7. SCAP Application": "SCAP/DAC",
    "8. Final Authority Approvals": "SCAP/DAC",
    "9. LTO Lodgement": "LTO",
    "10. LTO Approval": "LTO",
    "11. Complete": "AR (Alex)",
    # T2 legacy
    "Concept Planning": "AR (Alex)",
    "PlanSA Lodgement": "Council/Authority",
    "Authority Assessment": "Council/Authority",
    "Conditions & Compliance": "Client",
    "Certified Survey": "DP (Damiano)",
    "Land Division Certificate Lodged with PlanSA": "Council/Authority",
    "SCAP Application": "SCAP/DAC",
    "Final Authority Approvals": "SCAP/DAC",
    "LTO Lodgement": "LTO",
    "LTO Approval": "LTO",
    "Complete": "AR (Alex)",
    # T3 APA Project
    "Search & DBYD": "AR (Alex)",
    "Plan Drawing & Check": "TR (Tristan)",
    "Plan to Client": "DP (Damiano)",
    "Further Work Required": "Client",
    "Invoice & Close": "AR (Alex)",
}

# Smart filter stage keywords
SMART_FILTERS = {
    "Fieldwork": ["Field Work"],
    "Drafting": ["Prepare Plan", "Plan Drawing", "Concept"],
    "With Authority": ["Authority", "PlanSA", "SCAP", "LTO", "Council"],
    "On Hold": [],  # checked via status
    "New": ["Prepare Project", "Search", "Concept Planning"],
    "PlanSA": ["PlanSA"],
    "LTO": ["LTO"],
}

def api_get(endpoint, params=None):
    url = f"{BASE}/{endpoint}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    
    auth = base64.b64encode(f"{API_KEY}:".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth}",
        "Accept": "application/json"
    })
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"API error {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
        return []

def get_all_active_projects():
    """Fetch all active projects (IN PROGRESS, NOT STARTED, DEFERRED)"""
    all_projects = []
    for status in ["IN PROGRESS", "NOT STARTED", "DEFERRED"]:
        skip = 0
        while True:
            params = {
                "status": status,
                "top": 500,
                "skip": skip,
                "brief": "false"
            }
            batch = api_get("Projects/Search", params)
            if not batch:
                break
            all_projects.extend(batch)
            if len(batch) < 500:
                break
            skip += 500
    return all_projects

def get_pipeline_stages():
    """Fetch all pipeline stage details"""
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
    """Get PM name first, then Client, skip Site Contact/Conveyancer etc."""
    links = project.get("LINKS", [])
    
    pm_link = None
    client_link = None
    
    for link in links:
        role = (link.get("ROLE") or "").strip()
        role_lower = role.lower()
        
        # Skip non-relevant roles
        if any(skip in role_lower for skip in ["site contact", "conveyancer", "planner", "council", "contractor"]):
            continue
        
        if any(pm in role_lower for pm in ["project manager", "pm"]):
            pm_link = link
        elif "client" in role_lower:
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
    print(f"  Got {len(stages)} stages across {len(PIPELINES)} pipelines")
    
    print("Fetching active projects...")
    raw_projects = get_all_active_projects()
    print(f"  Got {len(raw_projects)} active projects")
    
    # Process projects
    projects = []
    contact_cache = {}
    
    for p in raw_projects:
        pid = p.get("PIPELINE_ID")
        if pid not in PIPELINES:
            continue
        
        tier = PIPELINES[pid]["tier"]
        stage_id = p.get("STAGE_ID")
        stage_info = stages.get(stage_id, {})
        stage_name = stage_info.get("name", "Unknown")
        stage_order = stage_info.get("order", 0)
        
        # Get custom fields
        address = ""
        sa_water = ""
        responsibility = ""
        custom_fields = p.get("CUSTOMFIELDS", [])
        for cf in custom_fields:
            fid = cf.get("FIELD_NAME", "")
            val = cf.get("FIELD_VALUE", "") or ""
            if fid == "PROJECT_FIELD_3":
                address = val
            elif fid == "PROJECT_FIELD_6":
                sa_water = val
            elif fid == "Current_Responsibility__c":
                responsibility = val
        
        # If no custom responsibility, derive from stage
        if not responsibility:
            responsibility = STAGE_RESPONSIBILITY.get(stage_name, "")
        
        # Get contact name (cache to avoid repeated API calls)
        project_id = p["PROJECT_ID"]
        if project_id not in contact_cache:
            try:
                contact_cache[project_id] = get_contact_name(p)
            except Exception:
                contact_cache[project_id] = ""
        client = contact_cache[project_id]
        
        projects.append({
            "id": project_id,
            "name": p.get("PROJECT_NAME", ""),
            "tier": tier,
            "status": p.get("STATUS", ""),
            "address": address,
            "client": client,
            "link": f"https://crm.na1.insightly.com/details/project/{project_id}",
            "responsibility": responsibility,
            "days_in_stage": 0,  # Will be populated by stage tracking
            "stage_name": stage_name,
            "sa_water_ref": sa_water,
            "stage_order": stage_order,
        })
    
    # Sort by tier then name
    projects.sort(key=lambda x: ({"T1":1,"T2":2,"T3":3}.get(x["tier"],9), x["name"]))
    
    # Generate timestamp in Adelaide time
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    adelaide_offset = datetime.timezone(datetime.timedelta(hours=9, minutes=30))
    now_adelaide = now_utc.astimezone(adelaide_offset)
    timestamp = now_adelaide.isoformat()
    
    output = {
        "projects": projects,
        "last_updated": timestamp,
        "total": len(projects)
    }
    
    with open("data.json", "w") as f:
        json.dump(output, f, indent=2)
    
    print(f"\nGenerated data.json: {len(projects)} projects")
    print(f"Timestamp: {timestamp}")

if __name__ == "__main__":
    main()
