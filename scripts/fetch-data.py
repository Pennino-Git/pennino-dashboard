#!/usr/bin/env python3
"""
Fetch active projects from Insightly and generate data.json for the dashboard.
Runs as a GitHub Action every 15 minutes.
"""
import json, os, sys, urllib.request, urllib.error, urllib.parse, base64, datetime, time

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

MAX_RETRIES = 3
RETRY_DELAY = 10  # seconds

def api_get(endpoint, params=None, retries=MAX_RETRIES):
    url = f"{BASE}/{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    auth = base64.b64encode(f"{API_KEY}:".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth}",
        "Accept": "application/json"
    })
    last_error = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            last_error = e
            code = e.code
            body = e.read().decode()[:200]
            print(f"  API error {code} on {endpoint} (attempt {attempt+1}/{retries}): {body}", file=sys.stderr)
            if code in (429, 500, 502, 503, 504):
                if attempt < retries - 1:
                    wait = RETRY_DELAY * (attempt + 1)
                    print(f"  Retrying in {wait}s...", file=sys.stderr)
                    time.sleep(wait)
                    continue
            return None
        except Exception as e:
            last_error = e
            print(f"  Request error on {endpoint} (attempt {attempt+1}/{retries}): {e}", file=sys.stderr)
            if attempt < retries - 1:
                wait = RETRY_DELAY * (attempt + 1)
                print(f"  Retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            return None
    return None

def get_all_stages():
    """Fetch all pipeline stages via PipelineStages endpoint."""
    result = api_get("PipelineStages")
    stages = {}
    if isinstance(result, list):
        for s in result:
            if s.get("PIPELINE_ID") in PIPELINES:
                stages[s["STAGE_ID"]] = {
                    "name": s["STAGE_NAME"],
                    "order": s.get("STAGE_ORDER", 0),
                    "pipeline_id": s["PIPELINE_ID"]
                }
    return stages

def get_all_projects():
    """Fetch all projects, paginating 500 at a time."""
    all_projects = []
    skip = 0
    while True:
        params = {"top": 500, "skip": skip, "brief": "false"}
        batch = api_get("Projects/Search", params)
        if not batch or not isinstance(batch, list):
            break
        all_projects.extend(batch)
        print(f"  Fetched {len(all_projects)} projects so far...")
        if len(batch) < 500:
            break
        skip += 500
    return all_projects

def get_contact_name(project, contact_cache):
    """Get PM or Client name from project links."""
    links = project.get("LINKS", [])
    pm_link = None
    client_link = None
    
    for link in links:
        obj_type = link.get("LINK_OBJECT_NAME", "")
        if obj_type not in ("Contact", "Organisation"):
            continue
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
    
    obj_type = best.get("LINK_OBJECT_NAME", "")
    obj_id = best.get("LINK_OBJECT_ID")
    if not obj_id:
        return ""
    
    cache_key = f"{obj_type}_{obj_id}"
    if cache_key in contact_cache:
        return contact_cache[cache_key]
    
    name = ""
    if obj_type == "Contact":
        contact = api_get(f"Contacts/{obj_id}")
        if isinstance(contact, dict):
            first = contact.get("FIRST_NAME", "")
            last = contact.get("LAST_NAME", "")
            name = f"{first} {last}".strip()
    elif obj_type == "Organisation":
        org = api_get(f"Organisations/{obj_id}")
        if isinstance(org, dict):
            name = org.get("ORGANISATION_NAME", "")
    
    contact_cache[cache_key] = name
    return name

def load_previous_data():
    """Load previous data.json if it exists to reuse client names."""
    try:
        with open("data.json", "r") as f:
            data = json.load(f)
            return {p["id"]: p.get("client", "") for p in data.get("projects", []) if p.get("client")}
    except Exception:
        return {}

def load_stage_tracking():
    """Load stage_tracking.json — tracks when each project entered its current stage."""
    try:
        with open("stage_tracking.json", "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_stage_tracking(tracking):
    """Save stage_tracking.json."""
    with open("stage_tracking.json", "w") as f:
        json.dump(tracking, f, indent=2)

def main():
    if not API_KEY:
        print("ERROR: INSIGHTLY_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    
    # Load previous client names to avoid unnecessary API calls
    prev_clients = load_previous_data()
    print(f"Loaded {len(prev_clients)} cached client names from previous run")
    
    # Load stage tracking for days-in-stage calculation
    stage_tracking = load_stage_tracking()
    print(f"Loaded {len(stage_tracking)} stage tracking entries")
    
    print("Fetching pipeline stages...")
    stages = get_all_stages()
    print(f"  Got {len(stages)} stages across {len(PIPELINES)} pipelines")
    
    print("Fetching all projects from Insightly...")
    raw_projects = get_all_projects()
    print(f"  Got {len(raw_projects)} total projects")
    
    # SAFETY CHECK: If we got 0 projects but had data before, Insightly is likely down.
    # Don't overwrite good data with empty data.
    if len(raw_projects) == 0 and len(prev_clients) > 0:
        print("\n⚠️  Got 0 projects from API but previous data had projects — Insightly may be down.")
        print("   Keeping existing data.json to avoid breaking the dashboard.")
        sys.exit(0)
    
    # Filter to active only
    active_projects = [p for p in raw_projects 
                       if p.get("STATUS", "") in ACTIVE_STATUSES 
                       and p.get("PIPELINE_ID") in PIPELINES]
    print(f"  Filtered to {len(active_projects)} active projects")
    
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    adelaide = datetime.timezone(datetime.timedelta(hours=9, minutes=30))
    now_adelaide = now_utc.astimezone(adelaide)
    today_str = now_adelaide.strftime("%Y-%m-%d")
    
    projects = []
    contact_cache = {}
    new_tracking = {}
    
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
        
        # Extract tags
        tags = [t.get("TAG_NAME", "") for t in p.get("TAGS", []) if t.get("TAG_NAME")]
        needs_fieldwork = "FIELD" in tags
        
        project_id = p["PROJECT_ID"]
        project_key = str(project_id)
        
        # Stage tracking: detect stage changes and calculate days
        prev = stage_tracking.get(project_key, {})
        prev_stage_id = prev.get("stage_id")
        
        if prev_stage_id == stage_id and prev.get("entered"):
            # Same stage — keep original entered date
            entered_date = prev["entered"]
        else:
            # New project or stage changed — record today
            entered_date = today_str
        
        new_tracking[project_key] = {
            "stage_id": stage_id,
            "entered": entered_date,
            "stage_name": stage_name
        }
        
        # Calculate days in stage
        try:
            entered_dt = datetime.datetime.strptime(entered_date, "%Y-%m-%d").date()
            days_in_stage = (now_adelaide.date() - entered_dt).days
        except Exception:
            days_in_stage = 0
        
        # Try to reuse previous client name, otherwise fetch
        client = prev_clients.get(project_id, "")
        if not client:
            client = get_contact_name(p, contact_cache)
        
        if (i + 1) % 20 == 0:
            print(f"  Processing: {i+1}/{len(active_projects)}")
        
        projects.append({
            "id": project_id,
            "name": p.get("PROJECT_NAME", ""),
            "tier": tier,
            "status": p.get("STATUS", ""),
            "address": address,
            "client": client,
            "link": f"https://crm.na1.insightly.com/details/project/{project_id}",
            "responsibility": responsibility,
            "days_in_stage": days_in_stage,
            "stage_name": stage_name,
            "sa_water_ref": sa_water,
            "stage_order": stage_order,
            "tags": tags,
            "needs_fieldwork": needs_fieldwork,
        })
    
    projects.sort(key=lambda x: ({"T1":1,"T2":2,"T3":3}.get(x["tier"],9), x["name"]))
    
    timestamp = now_adelaide.isoformat()
    
    output = {"projects": projects, "last_updated": timestamp, "total": len(projects)}
    
    with open("data.json", "w") as f:
        json.dump(output, f, indent=2)
    
    # Save stage tracking
    save_stage_tracking(new_tracking)
    
    # Stats
    with_client = sum(1 for p in projects if p.get("client"))
    unknown_stage = sum(1 for p in projects if p.get("stage_name") == "Unknown")
    with_days = sum(1 for p in projects if p.get("days_in_stage", 0) > 0)
    print(f"\n✅ Generated data.json: {len(projects)} projects, {with_client} with client names, {unknown_stage} unknown stages, {with_days} with days tracked")

if __name__ == "__main__":
    main()


