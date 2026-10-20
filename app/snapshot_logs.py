"""
Snapshot Downloads page: what the download script (Download_Video_Mobicam_Ignition.py)
did for every snapshot - read-only, from the tables it logs to (snapshot_db.py):

    snapshot_jobs      one row per initial / recheck / periodic snapshot job
    snapshot_attempts  one row per try inside a job: camera offline, download failed,
                       rejected by a quality gate (with the reasons), or accepted
    trigger_events     no camera matched for a vehicle, trip start / end

Reinke and Image Makers write to the same tables. Rows are shown for this web app's
client: account = SNAPSHOT_ACCOUNT (set by the download script), or - for rows logged
before that column existed - vehicles that appear in this client's trips.
"""
import os

from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file
from sqlalchemy import text

from app import db
from app.routes import login_required

snapshots = Blueprint("snapshots", __name__)

ACCOUNT = os.environ.get("SNAPSHOT_ACCOUNT", "reinke")
TZ = "Africa/Johannesburg"
TRIPS_TABLE = "trips_reinke_logistics_v2"

# Which step a job ended on (most informative first)
STAGE_SQL = """
CASE
  WHEN j.status = 'accepted' THEN 'accepted'
  WHEN j.status = 'in_progress' AND j.finished_at IS NULL THEN 'in_progress'
  WHEN coalesce(s.rejected, 0) > 0 THEN 'quality_gates'
  WHEN coalesce(s.offline, 0) > 0 THEN 'camera_offline'
  WHEN coalesce(s.download_failed, 0) > 0 THEN 'download_failed'
  WHEN coalesce(s.n, 0) = 0 THEN 'not_tried'
  ELSE 'other'
END"""

BASE_SQL = f"""
WITH s AS (
    SELECT job_id, count(*) AS n,
           count(*) FILTER (WHERE outcome = 'accepted') AS accepted,
           count(*) FILTER (WHERE outcome = 'device_unavailable') AS offline,
           count(*) FILTER (WHERE outcome IN ('request_failed', 'download_failed')) AS download_failed,
           count(*) FILTER (WHERE outcome = 'rejected') AS rejected,
           count(*) FILTER (WHERE outcome IN ('gate_error', 'save_failed')) AS errors,
           max(attempted_at) AS last_attempt_at
    FROM snapshot_attempts GROUP BY job_id
), j AS (
    SELECT j.* FROM snapshot_jobs j
    WHERE j.created_at >= :t0 AND j.created_at < :t1
      AND (j.account = :account
           OR (j.account IS NULL AND (j.saved_path ILIKE '%' || :account || '%'
               OR j.asset_name IN (SELECT DISTINCT vehicle FROM {TRIPS_TABLE}))))
)
SELECT j.job_id, j.created_at, j.finished_at, j.asset_name, j.device_name, j.action, j.status,
       j.final_reason, j.saved_path, j.speed_kmh, j.speed_band, j.ignition_status, j.max_retries,
       coalesce(s.n, 0) AS attempts, coalesce(s.accepted, 0) AS accepted_n, coalesce(s.offline, 0) AS offline_n,
       coalesce(s.download_failed, 0) AS download_failed_n, coalesce(s.rejected, 0) AS rejected_n,
       coalesce(s.errors, 0) AS errors_n, s.last_attempt_at,
       {STAGE_SQL} AS stage
FROM j LEFT JOIN s ON s.job_id = j.job_id
"""


def _range():
    """(t0, t1) as SAST dates from ?from=YYYY-MM-DD&to=YYYY-MM-DD (default: today)."""
    f = (request.args.get("from") or "").strip()
    t = (request.args.get("to") or "").strip()
    q = text(f"SELECT (coalesce(CAST(:f AS date), (now() AT TIME ZONE '{TZ}')::date)::timestamp AT TIME ZONE '{TZ}'), "
             f"((coalesce(CAST(:t AS date), (now() AT TIME ZONE '{TZ}')::date) + 1)::timestamp AT TIME ZONE '{TZ}')")
    return db.session.execute(q, {"f": f or None, "t": t or None}).one()


def _params():
    t0, t1 = _range()
    return {"t0": t0, "t1": t1, "account": ACCOUNT}


def _filters():
    where, p = [], {}
    if request.args.get("vehicle"):
        where.append("asset_name = :vehicle")
        p["vehicle"] = request.args["vehicle"]
    if request.args.get("action"):
        where.append("action = :action")
        p["action"] = request.args["action"]
    if request.args.get("stage"):
        where.append("stage = :stage")
        p["stage"] = request.args["stage"]
    return (" WHERE " + " AND ".join(where)) if where else "", p


def _local(dt):
    if dt is None:
        return None
    return db.session.execute(text(f"SELECT CAST(:d AS timestamptz) AT TIME ZONE '{TZ}'"), {"d": dt}).scalar() \
        .strftime("%Y-%m-%d %H:%M:%S")


def _tables_ready():
    return db.session.execute(text("SELECT to_regclass('snapshot_jobs') IS NOT NULL "
                                   "AND to_regclass('snapshot_attempts') IS NOT NULL")).scalar()


@snapshots.route("/snapshots")
@login_required
def page():
    return render_template("snapshots.html", ready=_tables_ready())


@snapshots.route("/api/snapshots/summary")
@login_required
def summary():
    if not _tables_ready():
        return jsonify({"ready": False})
    p = _params()
    fw, fp = _filters()
    rows = db.session.execute(text(f"SELECT * FROM ({BASE_SQL}) x{fw}"), {**p, **fp}).mappings().all()
    by_stage = {}
    for r in rows:
        by_stage[r["stage"]] = by_stage.get(r["stage"], 0) + 1
    acc = [r for r in rows if r["stage"] == "accepted"]
    tries = sum(r["attempts"] for r in rows)
    out = {
        "ready": True, "jobs": len(rows), "by_stage": by_stage, "attempts": tries,
        "attempt_outcomes": {
            "accepted": sum(r["accepted_n"] for r in rows),
            "camera_offline": sum(r["offline_n"] for r in rows),
            "download_failed": sum(r["download_failed_n"] for r in rows),
            "rejected": sum(r["rejected_n"] for r in rows),
            "errors": sum(r["errors_n"] for r in rows),
        },
        "avg_attempts_to_accept": round(sum(r["attempts"] for r in acc) / len(acc), 1) if acc else None,
        "first_try_accepted": sum(1 for r in acc if r["attempts"] == 1),
    }
    ids = [str(r["job_id"]) for r in rows]
    if ids:
        # Rejection reasons and failed gates (attempt level)
        reasons = db.session.execute(text("""
            SELECT a.failed_gate, regexp_replace(r.reason, '\\(.*$', '') AS reason, count(*) AS n
            FROM snapshot_attempts a CROSS JOIN LATERAL unnest(a.reasons) AS r(reason)
            WHERE a.job_id = ANY(CAST(:ids AS uuid[])) AND a.outcome = 'rejected'
            GROUP BY 1, 2 ORDER BY n DESC LIMIT 20"""), {"ids": ids}).mappings().all()
        out["reasons"] = [dict(r) for r in reasons]
        gates = db.session.execute(text("""
            SELECT coalesce(failed_gate, '-') AS gate, count(*) AS n FROM snapshot_attempts
            WHERE job_id = ANY(CAST(:ids AS uuid[])) AND outcome = 'rejected' GROUP BY 1 ORDER BY n DESC"""),
            {"ids": ids}).mappings().all()
        out["gates"] = [dict(r) for r in gates]
        # Vehicles: camera offline / download failures / gate rejections per vehicle
        veh = {}
        for r in rows:
            v = veh.setdefault(r["asset_name"], {"vehicle": r["asset_name"], "jobs": 0, "accepted": 0,
                                                  "offline_attempts": 0, "download_failed_attempts": 0,
                                                  "rejected_attempts": 0, "attempts": 0})
            v["jobs"] += 1
            v["accepted"] += r["stage"] == "accepted"
            v["offline_attempts"] += r["offline_n"]
            v["download_failed_attempts"] += r["download_failed_n"]
            v["rejected_attempts"] += r["rejected_n"]
            v["attempts"] += r["attempts"]
        out["vehicles"] = sorted(veh.values(), key=lambda v: (v["accepted"] / max(v["jobs"], 1), -v["jobs"]))
        # Camera-offline messages exactly as Surfsight sent them
        offline = db.session.execute(text("""
            SELECT left(coalesce(api_response, '-'), 120) AS message, count(*) AS n FROM snapshot_attempts
            WHERE job_id = ANY(CAST(:ids AS uuid[])) AND outcome = 'device_unavailable'
            GROUP BY 1 ORDER BY n DESC LIMIT 8"""), {"ids": ids}).mappings().all()
        out["offline_messages"] = [dict(r) for r in offline]
    else:
        out.update(reasons=[], gates=[], vehicles=[], offline_messages=[])
    # Vehicles active but with no camera matched (no download possible at all)
    try:
        vf = " AND asset_name = :vehicle" if request.args.get("vehicle") else ""
        out["no_camera_match"] = db.session.execute(text(f"""
            SELECT asset_name, count(*) AS n, max(event_at) AS last_at FROM trigger_events
            WHERE event_type = 'no_device_match' AND event_at >= :t0 AND event_at < :t1{vf}
              AND (account = :account OR (account IS NULL AND asset_name IN
                   (SELECT DISTINCT vehicle FROM {TRIPS_TABLE})))
            GROUP BY 1 ORDER BY n DESC LIMIT 20"""), {**p, "vehicle": request.args.get("vehicle")}).mappings().all()
        out["no_camera_match"] = [{"vehicle": r["asset_name"], "n": r["n"], "last_at": _local(r["last_at"])}
                                  for r in out["no_camera_match"]]
    except Exception:  # noqa: BLE001 - older trigger_events without the account column
        db.session.rollback()
        out["no_camera_match"] = []
    return jsonify(out)


@snapshots.route("/api/snapshots/jobs")
@login_required
def jobs():
    if not _tables_ready():
        return jsonify({"rows": [], "total": 0, "page": 1, "pages": 1})
    p = _params()
    fw, fp = _filters()
    page_no = max(int(request.args.get("page", 1) or 1), 1)
    per = 50
    total = db.session.execute(text(f"SELECT count(*) FROM ({BASE_SQL}) x{fw}"), {**p, **fp}).scalar()
    rows = db.session.execute(text(f"SELECT * FROM ({BASE_SQL}) x{fw} ORDER BY created_at DESC "
                                   f"LIMIT {per} OFFSET {(page_no - 1) * per}"), {**p, **fp}).mappings().all()
    out = []
    for r in rows:
        out.append({
            "job_id": str(r["job_id"]), "created_at": _local(r["created_at"]),
            "finished_at": _local(r["finished_at"]), "vehicle": r["asset_name"], "action": r["action"],
            "status": r["status"], "stage": r["stage"], "attempts": r["attempts"],
            "offline": r["offline_n"], "download_failed": r["download_failed_n"], "rejected": r["rejected_n"],
            "errors": r["errors_n"], "reason": r["final_reason"], "speed": r["speed_kmh"],
            "ignition": r["ignition_status"],
        })
    return jsonify({"rows": out, "total": total, "page": page_no, "pages": max(1, -(-total // per))})


def _image_path(path):
    """A saved snapshot under this app's data folder (never anything else)."""
    if not path:
        return None
    root = os.path.realpath(os.path.join(os.path.dirname(current_app.root_path), "data"))
    real = os.path.realpath(path)
    if not real.startswith(root + os.sep) or not os.path.isfile(real):
        return None
    return real


@snapshots.route("/api/snapshots/jobs/<job_id>/attempts")
@login_required
def attempts(job_id):
    rows = db.session.execute(text("""
        SELECT attempt_id, attempt_no, attempted_at, duration_ms, outcome, http_status, api_response,
               failed_gate, reasons, metrics, saved_path
        FROM snapshot_attempts WHERE job_id = CAST(:j AS uuid) ORDER BY attempt_no"""), {"j": job_id}).mappings().all()
    out = []
    for r in rows:
        m = r["metrics"] or {}
        out.append({
            "id": r["attempt_id"], "no": r["attempt_no"], "at": _local(r["attempted_at"]),
            "ms": r["duration_ms"], "outcome": r["outcome"], "http": r["http_status"],
            "message": (r["api_response"] or "")[:200] if r["outcome"] != "rejected" else None,
            "gate": r["failed_gate"], "reasons": list(r["reasons"] or []),
            "metrics": {k: m.get(k) for k in ("face_width_px", "face_box", "face_score", "yaw_deg", "pitch_deg",
                                               "eye_blink", "sharpness", "face_luma", "is_night") if k in m},
            "image": f"/api/snapshots/attempts/{r['attempt_id']}/image" if _image_path(r["saved_path"]) else None,
        })
    return jsonify(out)


@snapshots.route("/api/snapshots/attempts/<int:attempt_id>/image")
@login_required
def attempt_image(attempt_id):
    path = db.session.execute(text("SELECT saved_path FROM snapshot_attempts WHERE attempt_id = :i"),
                              {"i": attempt_id}).scalar()
    real = _image_path(path)
    if not real:
        abort(404)
    return send_file(real, mimetype="image/jpeg")


@snapshots.route("/api/snapshots/vehicles")
@login_required
def vehicles():
    if not _tables_ready():
        return jsonify([])
    rows = db.session.execute(text(f"""
        SELECT DISTINCT asset_name FROM snapshot_jobs
        WHERE created_at > now() - interval '30 days' AND asset_name IS NOT NULL
          AND (account = :account OR (account IS NULL AND asset_name IN (SELECT DISTINCT vehicle FROM {TRIPS_TABLE})))
        ORDER BY 1"""), {"account": ACCOUNT}).scalars().all()
    return jsonify(rows)
