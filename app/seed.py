"""Seed the database with representative shift risks: python -m app.seed"""

from __future__ import annotations

from app.db import execute, init_db, query, utcnow
from app.models import RiskIn

SAMPLE = [
    RiskIn(title="Tool 4B chamber pressure drift", area="Etch", owner="J. Tan", shift="1",
           severity=5, likelihood=4, downtime_minutes=180, impact_units=2400,
           description="Pressure trending 12% above spec since 03:10; two lots on hold pending recipe verification.",
           action="Recipe re-qual with process eng; hold lots until sign-off", source="MES"),
    RiskIn(title="Deionised water loop conductivity alarm", area="Facilities", owner="", shift="1",
           severity=4, likelihood=4, downtime_minutes=90, impact_units=800,
           description="Loop 2 conductivity above threshold; risk of contamination across wet benches.",
           action="", source="BMS"),
    RiskIn(title="Metrology station 7 calibration overdue", area="Metrology", owner="R. Lim", shift="2",
           severity=3, likelihood=4, downtime_minutes=45, impact_units=300,
           description="Cal window expired 2 days ago; measurement confidence degraded.",
           action="Schedule cal during next idle window", status="mitigating", source="CMMS"),
    RiskIn(title="Short-staffed on night shift maintenance", area="Operations", owner="A. Kaur", shift="4",
           severity=3, likelihood=3, downtime_minutes=0, impact_units=0,
           description="Two technicians on unplanned leave; PM backlog will grow.",
           action="Reallocate from day shift, defer non-critical PMs", source="manual"),
    RiskIn(title="Spare pump inventory below reorder point", area="Supply Chain", owner="M. Rossi", shift="2",
           severity=2, likelihood=3, downtime_minutes=0, impact_units=0,
           description="Only 1 spare dry pump on hand; lead time 6 weeks.",
           action="Raise expedited PO", status="mitigating", source="ERP"),
    RiskIn(title="Recurring photolithography overlay excursion", area="Litho", owner="", shift="1",
           severity=4, likelihood=5, downtime_minutes=210, impact_units=3100,
           description="Third overlay excursion in 48h on scanner 2; root cause unconfirmed.",
           action="Escalate to process engineering; stop-ship pending review", source="SPC"),
]


def main() -> None:
    init_db()
    if query("SELECT 1 FROM risks LIMIT 1"):
        print("Database already contains risks; skipping seed.")
        return
    now = utcnow()
    for r in SAMPLE:
        execute(
            "INSERT INTO risks (title, description, area, source, owner, shift, severity,"
            " likelihood, status, impact_units, downtime_minutes, action, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r.title, r.description, r.area, r.source, r.owner, r.shift, r.severity,
             r.likelihood, r.status, r.impact_units, r.downtime_minutes, r.action, now, now),
        )
    print(f"Seeded {len(SAMPLE)} risks.")


if __name__ == "__main__":
    main()
