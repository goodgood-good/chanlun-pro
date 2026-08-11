"""Read-only view of the application-owned scheduler jobs."""

from flask import Blueprint, current_app, render_template
from flask_login import login_required


jobs_bp = Blueprint("jobs", __name__)


@jobs_bp.route("/jobs")
@login_required
def jobs():
    scheduler = current_app.extensions.get("scheduler")
    from cl_app import _scheduler_task_snapshot

    jobs_snapshot = _scheduler_task_snapshot(scheduler)
    return render_template("jobs.html", jobs=jobs_snapshot)
