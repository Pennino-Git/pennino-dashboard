#!/usr/bin/env python3
"""
Fetch active projects from Insightly and generate data.json for the dashboard.
Runs as a GitHub Action every 15 minutes.
Also tracks stage/responsibility changes in activity_log.json.
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
RETRY_DELAY = 10

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
    all_projects = []
    skip = 0
    while True:
        params = {"top": 500, "skip": skip}
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
    try:
        with open("data.json", "r") as f:
            data = json.load(f)
            return {p["id"]: p for p in data.get("projects", [])}
    except Exception:
        return {}

def load_stage_tracking():
    try:
        with open("stage_tracking.json", "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_stage_tracking(tracking):
    with open("stage_tracking.json", "w") as f:
        json.dump(tracking, f, indent=2)

def load_activity_log():
    try:
        with open("activity_log.json", "r") as f:
            return json.load(f)
    except Exception:
        return {"changes": [], "last_updated": ""}

def save_activity_log(log):
    with open("activity_log.json", "w") as f:
        json.dump(log, f, indent=2)

def main():
    if not API_KEY:
        print("ERROR: INSIGHTLY_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    prev_data = load_previous_data()
    prev_clients = {pid: p.get("client", "") for pid, p in prev_data.items() if p.get("client")}
    print(f"Loaded {len(prev_clients)} cached client names from previous run")

    stage_tracking = load_stage_tracking()
    print(f"Loaded {len(stage_tracking)} stage tracking entries")

    activity_log = load_activity_log()
    print(f"Loaded {len(activity_log.get('changes', []))} activity log entries")

    print("Fetching pipeline stages...")
    stages = get_all_stages()
    print(f"  Got {len(stages)} stages across {len(PIPELINES)} pipelines")

    print("Fetching all projects from Insightly...")
    raw_projects = get_all_projects()
    print(f"  Got {len(raw_projects)} total projects")

    if len(raw_projects) == 0 and len(prev_clients) > 0:
        print("\n  Got 0 projects from API but previous data had projects - Insightly may be down.")
        print("   Keeping existing data.json to avoid breaking the dashboard.")
        sys.exit(0)

    try:
        with open("data.json", "r") as f:
            old_data = json.load(f)
        old_count = old_data.get("total", 0)
    except Exception:
        old_count = 0

    active_projects = [p for p in raw_projects
                       if p.get("STATUS", "") in ACTIVE_STATUSES
                       and p.get("PIPELINE_ID") in PIPELINES]
    print(f"  Filtered to {len(active_projects)} active projects")

    if old_count > 20 and len(active_projects) < old_count * 0.5:
        print(f"\n  Active projects dropped from {old_count} to {len(active_projects)} - likely API issue.")
        print("   Keeping existing data.json to avoid breaking the dashboard.")
        sys.exit(0)

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    adelaide = datetime.timezone(datetime.timedelta(hours=9, minutes=30))
    now_adelaide = now_utc.astimezone(adelaide)
    today_str = now_adelaide.strftime("%Y-%m-%d")
    now_iso = now_adelaide.isoformat()

    projects = []
    contact_cache = {}
    new_tracking = {}
    new_changes = []

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

        tags = [t.get("TAG_NAME", "") for t in p.get("TAGS", []) if t.get("TAG_NAME")]
        needs_fieldwork = "FIELD" in tags
        is_premium = "PREMIUM" in tags

        project_id = p["PROJECT_ID"]
        project_key = str(project_id)
        project_name = p.get("PROJECT_NAME", "")

        # Stage tracking
        prev = stage_tracking.get(project_key, {})
        prev_stage_id = prev.get("stage_id")

        if prev_stage_id == stage_id and prev.get("entered"):
            entered_date = prev["entered"]
        else:
            entered_date = today_str

        new_tracking[project_key] = {
            "stage_id": stage_id,
            "entered": entered_date,
            "stage_name": stage_name
        }

        try:
            entered_dt = datetime.datetime.strptime(entered_date, "%Y-%m-%d").date()
            days_in_stage = (now_adelaide.date() - entered_dt).days
        except Exception:
            days_in_stage = 0

        # Activity tracking - detect stage and responsibility changes
        prev_project = prev_data.get(project_id)
        if prev_project:
            prev_stage = prev_project.get("stage_name", "")
            prev_resp = prev_project.get("responsibility", "")
            if prev_stage and prev_stage != stage_name:
                new_changes.append({
                    "type": "stage_change",
                    "project_id": project_id,
                    "project_name": project_name,
                    "tier": tier,
                    "from": prev_stage,
                    "to": stage_name,
                    "timestamp": now_iso
                })
            if prev_resp and prev_resp != responsibility:
                new_changes.append({
                    "type": "responsibility_change",
                    "project_id": project_id,
                    "project_name": project_name,
                    "tier": tier,
                    "from": prev_resp,
                    "to": responsibility,
                    "timestamp": now_iso
                })

        client = prev_clients.get(project_id, "")
        if not client:
            client = get_contact_name(p, contact_cache)

        if (i + 1) % 20 == 0:
            print(f"  Processing: {i+1}/{len(active_projects)}")

        projects.append({
            "id": project_id,
            "name": project_name,
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
            "is_premium": is_premium,
        })

    projects.sort(key=lambda x: ({"T1":1,"T2":2,"T3":3}.get(x["tier"],9), x["name"]))

    output = {"projects": projects, "last_updated": now_iso, "total": len(projects)}

    with open("data.json", "w") as f:
        json.dump(output, f, indent=2)

    save_stage_tracking(new_tracking)

    # Update activity log - prepend new changes, keep last 200
    if new_changes:
        all_changes = new_changes + activity_log.get("changes", [])
        all_changes = all_changes[:200]
        activity_log = {"changes": all_changes, "last_updated": now_iso}
        save_activity_log(activity_log)
        print(f"  Logged {len(new_changes)} activity changes")
    else:
        # Update timestamp even if no changes
        activity_log["last_updated"] = now_iso
        save_activity_log(activity_log)

    with_client = sum(1 for p in projects if p.get("client"))
    unknown_stage = sum(1 for p in projects if p.get("stage_name") == "Unknown")
    with_days = sum(1 for p in projects if p.get("days_in_stage", 0) > 0)
    premium = sum(1 for p in projects if p.get("is_premium"))
    print(f"\n  Generated data.json: {len(projects)} projects, {with_client} clients, {unknown_stage} unknown stages, {with_days} days tracked, {premium} premium")

    # --- Pre-compute tasks.json for dashboard cards + daily digest ---
    print("\nFetching Insightly tasks for dashboard + digest...")
    today_iso = now_adelaide.strftime("%Y-%m-%d")

    # Project ID set for active projects
    active_ids = {p["id"] for p in projects}
    proj_lookup = {p["id"]: p["name"] for p in projects}

    # Fetch ALL incomplete tasks (paginated, max 500/call)
    all_tasks = []
    skip = 0
    while True:
        batch = api_get("Tasks", {"top": 500, "skip": skip})
        if not isinstance(batch, list) or len(batch) == 0:
            break
        all_tasks.extend(batch)
        if len(batch) < 500:
            break
        skip += 500
    print(f"  Fetched {len(all_tasks)} total tasks")

    # Filter: incomplete, linked to active project
    open_tasks = [t for t in all_tasks
                  if not t.get("COMPLETED")
                  and t.get("PROJECT_ID") in active_ids
                  and t.get("STATUS") in ("NOT STARTED", "IN PROGRESS", "DEFERRED", "WAITING")]

    # Build per-project task summary
    by_project = {}
    digest_today = []
    digest_overdue = []

    for t in open_tasks:
        pid = t["PROJECT_ID"]
        due = (t.get("DUE_DATE") or "")[:10]
        title = t.get("TITLE", "")
        status = t.get("STATUS", "")

        is_overdue = due and due < today_iso
        is_due_today = due == today_iso
        is_due_soon = due and not is_overdue and not is_due_today and due <= (now_adelaide + datetime.timedelta(days=7)).strftime("%Y-%m-%d")

        task_entry = {
            "task_id": t.get("TASK_ID"),
            "title": title,
            "due_date": due or None,
            "status": status,
            "overdue": is_overdue,
            "due_today": is_due_today,
            "due_soon": is_due_soon,
        }

        if pid not in by_project:
            by_project[pid] = {"total": 0, "overdue": 0, "due_today": 0, "due_soon": 0, "tasks": []}
        by_project[pid]["total"] += 1
        if is_overdue:
            by_project[pid]["overdue"] += 1
        if is_due_today:
            by_project[pid]["due_today"] += 1
        if is_due_soon:
            by_project[pid]["due_soon"] += 1
        by_project[pid]["tasks"].append(task_entry)

        # Digest lists
        if is_due_today:
            digest_today.append({**task_entry, "project_id": pid, "project_name": proj_lookup.get(pid, "")})
        if is_overdue:
            digest_overdue.append({**task_entry, "project_id": pid, "project_name": proj_lookup.get(pid, "")})

    # Convert project IDs to strings for JSON
    by_project_str = {str(k): v for k, v in by_project.items()}

    tasks_data = {
        "by_project": by_project_str,
        "tasks_due_today": digest_today,
        "tasks_overdue": digest_overdue,
        "summary": {
            "total_open": len(open_tasks),
            "total_overdue": len(digest_overdue),
            "total_due_today": len(digest_today),
            "projects_with_tasks": len(by_project),
        },
        "last_updated": now_iso,
    }

    with open("tasks.json", "w") as f:
        json.dump(tasks_data, f, indent=2)
    print(f"  Saved tasks.json — {len(open_tasks)} open tasks across {len(by_project)} projects")

if __name__ == "__main__":
    main()
