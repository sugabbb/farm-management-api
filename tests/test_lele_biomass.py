"""Lele per-fish weight, real ADG, biomass and sell-date projection.

Unit tests cover the pure math; the rest go through the real API and PostgreSQL (see
tests/conftest.py). Every group created here has a code starting with ``BIOMASS-TEST`` and is
removed, with its samplings and audit events, after each test.
"""

import os
import re
import threading
import warnings
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select
from sqlalchemy.exc import SAWarning
from sqlalchemy.orm import Session

import app.features.farm as farm_api
import app.features.lele_biomass_jobs as biomass_jobs
from app.db.models import (
    AuditEvent,
    DailyRecord,
    FeedType,
    LeleBiomassChartDate,
    LeleBiomassJob,
    LeleBiomassSnapshot,
    LeleSampling,
    LeleSamplingResult,
    LivestockGroup,
    PopulationAdjustment,
    User,
)
from app.db.session import engine
from app.features.common import today_local
from app.features.lele_biomass_jobs import drain
from app.features.lele_biomass_queue import enqueue
from app.features.lele_biomass_service import (
    average_weight_g,
    biomass_kg,
    chart_dates,
    daily_gain_g,
    line_values,
    project_sell_date,
    rebuild_snapshot,
)
from app.main import app
from app.scripts.backfill_lele_sampling_results import backfill
from app.scripts.backfill_lele_sampling_results import main as backfill_main
from app.scripts.lele_biomass_worker import main as worker_main

CODE_PREFIX = "BIOMASS-TEST"
TODAY = today_local()


def days_ago(days: int) -> date:
    return TODAY - timedelta(days=days)


# --- Pure math ----------------------------------------------------------------------------------


def test_average_weight_is_total_of_the_three_batches_over_thirty_fish() -> None:
    # The example from the requirement: 10 fish weigh 0.300, 0.350 and 0.400 kg.
    assert average_weight_g(Decimal("0.300") + Decimal("0.350") + Decimal("0.400"), 30) == Decimal("35.000")
    assert average_weight_g(Decimal("1.000"), 30) == Decimal("33.333")
    assert average_weight_g(Decimal("0.001"), 30) == Decimal("0.033")
    with pytest.raises(ValueError):
        average_weight_g(Decimal("1"), 0)


def test_daily_gain_and_biomass() -> None:
    assert daily_gain_g(Decimal("35"), Decimal("50"), 14) == Decimal("1.071")
    assert daily_gain_g(Decimal("50"), Decimal("45"), 10) == Decimal("-0.500")
    assert biomass_kg(900, Decimal("75")) == Decimal("67.500")
    assert biomass_kg(0, Decimal("75")) == Decimal("0.000")
    with pytest.raises(ValueError):
        daily_gain_g(Decimal("1"), Decimal("2"), 0)


def test_projection_rounds_days_up_to_reach_the_target() -> None:
    sampled_on = date(2026, 1, 1)
    # (142 - 35) / 1.3 = 82.3 days -> 83 days.
    projection = project_sell_date(Decimal("35"), sampled_on, Decimal("1.3"), Decimal("142"))
    assert projection is not None
    assert projection.days_to_target == 83
    assert projection.projected_sell_date == date(2026, 3, 25)
    # Exact division does not add a day.
    exact = project_sell_date(Decimal("140.7"), sampled_on, Decimal("1.3"), Decimal("142"))
    assert exact is not None and exact.days_to_target == 1


def test_projection_when_target_reached_or_not_growing() -> None:
    sampled_on = date(2026, 1, 1)
    reached = project_sell_date(Decimal("142"), sampled_on, Decimal("1.3"), Decimal("142"))
    assert reached is not None and (reached.days_to_target, reached.projected_sell_date) == (0, sampled_on)
    assert project_sell_date(Decimal("100"), sampled_on, Decimal("0"), Decimal("142")) is None
    assert project_sell_date(Decimal("100"), sampled_on, Decimal("-0.5"), Decimal("142")) is None


def test_chart_dates_are_weekly_up_to_the_end_plus_sampling_dates() -> None:
    start = date(2026, 1, 1)
    dates = chart_dates(start, date(2026, 1, 20), {date(2026, 1, 15), date(2026, 1, 10), date(2026, 2, 1)})
    # Weekly dates up to the end, the end itself (the harvest date), and the samplings in between;
    # Jan 15 is already a weekly date and Feb 1 is after the end.
    assert dates == [
        date(2026, 1, 1),
        date(2026, 1, 8),
        date(2026, 1, 10),
        date(2026, 1, 15),
        date(2026, 1, 20),
    ]
    assert chart_dates(start, start, set()) == [start]
    assert chart_dates(start, date(2026, 1, 15), set()) == [date(2026, 1, 1), date(2026, 1, 8), date(2026, 1, 15)]


def test_line_values_start_at_the_anchor_and_may_pass_the_target() -> None:
    dates = [date(2026, 1, 1) + timedelta(days=7 * week) for week in range(4)]
    # From 100 g on Jan 8 at 10 g/day; nothing before the anchor, and no stop at 142 g.
    assert line_values(dates, date(2026, 1, 8), Decimal("100"), Decimal("10")) == {
        date(2026, 1, 8): Decimal("100.000"),
        date(2026, 1, 15): Decimal("170.000"),
        date(2026, 1, 22): Decimal("240.000"),
    }
    # A shrinking weight is drawn as measured until it would reach zero.
    assert line_values(dates, date(2026, 1, 1), Decimal("20"), Decimal("-1.5")) == {
        date(2026, 1, 1): Decimal("20.000"),
        date(2026, 1, 8): Decimal("9.500"),
    }


# --- Database / API fixtures --------------------------------------------------------------------


def wipe_biomass_data(db: Session) -> None:
    group_ids = select(LivestockGroup.id).where(LivestockGroup.code.startswith(CODE_PREFIX))
    sampling_ids = select(LeleSampling.id).where(LeleSampling.group_id.in_(group_ids))
    db.execute(
        delete(AuditEvent).where(
            AuditEvent.entity_type.in_(
                ("lele_samplings", "livestock_groups", "population_adjustments", "daily_records")
            )
        )
    )
    db.execute(delete(DailyRecord).where(DailyRecord.group_id.in_(group_ids)))  # feed usages cascade
    db.execute(delete(FeedType).where(FeedType.name.startswith(CODE_PREFIX)))
    db.execute(delete(LeleSamplingResult).where(LeleSamplingResult.sampling_id.in_(sampling_ids)))
    db.execute(delete(LeleSampling).where(LeleSampling.group_id.in_(group_ids)))  # measurements cascade
    db.execute(delete(PopulationAdjustment).where(PopulationAdjustment.group_id.in_(group_ids)))
    # Jobs and the biomass read model cascade with the group.
    db.execute(delete(LivestockGroup).where(LivestockGroup.code.startswith(CODE_PREFIX)))
    db.commit()


@pytest.fixture
def biomass_db(db: Session) -> Iterator[Session]:
    wipe_biomass_data(db)
    try:
        yield db
    finally:
        db.rollback()
        wipe_biomass_data(db)


@pytest.fixture
def client(biomass_db: Session, actor: User, actor_password: str) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        login = test_client.post(
            "/api/v1/auth/login",
            headers={"X-PBB-API-Key": os.environ["API_KEY"]},
            json={"email": actor.email, "password": actor_password},
        )
        assert login.status_code == 200
        test_client.headers.update(
            {
                "X-PBB-API-Key": os.environ["API_KEY"],
                "Authorization": f"Bearer {login.json()['data']['access_token']}",
            }
        )
        yield test_client


def make_group(
    db: Session,
    suffix: str,
    *,
    species: str = "LELE",
    start: int = 60,
    population: int = 1000,
    initial_weight: str | None = None,
) -> LivestockGroup:
    group = LivestockGroup(
        species=species,
        code=f"{CODE_PREFIX}-{suffix}",
        name=f"Biomass test {suffix}",
        start_date=days_ago(start),
        initial_population=population,
        initial_weight_g=Decimal(initial_weight) if initial_weight else None,
        location="Kolam 1",
        status="ACTIVE",
    )
    db.add(group)
    db.flush()
    rebuild(db, group, "group_created")  # as the API does when it creates a group
    return group


def rebuild(db: Session, group: LivestockGroup, reason: str) -> None:
    """Queues a rebuild after a change made directly in the database, and runs the queue."""
    enqueue(db, group, reason)
    db.commit()
    drain()
    db.expire_all()


def queued(db: Session) -> list[tuple[int, str]]:
    db.expire_all()
    return [(row.group_id, row.reason) for row in db.scalars(select(LeleBiomassJob).order_by(LeleBiomassJob.id))]


def sample(
    client: TestClient, group: LivestockGroup, day: date, weights: tuple[str, str, str], sampling_id: int | None = None
) -> dict[str, object]:
    payload = {
        "group_id": group.id,
        "sampling_date": day.isoformat(),
        "measurements": [
            {"sequence": index, "total_weight_kg": weight} for index, weight in enumerate(weights, start=1)
        ],
    }
    if sampling_id is None:
        response = client.post("/api/v1/lele-samplings", json=payload)
        assert response.status_code == 201, response.json()
    else:
        response = client.patch(f"/api/v1/lele-samplings/{sampling_id}", json=payload)
        assert response.status_code == 200, response.json()
    data: dict[str, object] = response.json()["data"]
    return data


def results_by_date(db: Session, group: LivestockGroup) -> dict[date, LeleSamplingResult]:
    db.expire_all()
    rows = db.execute(
        select(LeleSampling.sampling_date, LeleSamplingResult)
        .join(LeleSamplingResult, LeleSamplingResult.sampling_id == LeleSampling.id)
        .where(LeleSampling.group_id == group.id)
    ).all()
    return {sampling_date: result for sampling_date, result in rows}


def biomass(client: TestClient, group: LivestockGroup) -> dict[str, Any]:
    response = client.get(f"/api/v1/lele-biomass/{group.id}")
    assert response.status_code == 200, response.json()
    data: dict[str, Any] = response.json()["data"]
    return data


def real_adgs(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [series for series in data["chart"]["series"] if series["type"] == "REAL"]


def series_named(data: dict[str, Any], name: str) -> dict[str, Any]:
    return next(series for series in data["chart"]["series"] if series["name"] == name)


def value_on(data: dict[str, Any], name: str, day: date) -> str | None:
    """Value of a chart series on a date of the X axis."""
    dates = [item["date"] for item in data["chart"]["dates"]]
    value: str | None = series_named(data, name)["values"][dates.index(day.isoformat())]
    return value


def keys(value: object) -> set[str]:
    """Every dictionary key anywhere in a JSON document."""
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in keys(item)}
    return set()


# --- Sampling writes store per-fish weight and real ADG -----------------------------------------


def test_sampling_stores_average_weight_and_adg_between_samplings(biomass_db: Session, client: TestClient) -> None:
    group = make_group(biomass_db, "A")

    first = sample(client, group, days_ago(35), ("0.300", "0.350", "0.400"))
    assert first["result"] == {
        "fish_count": 30,
        "total_weight_kg": "1.050",
        "average_weight_g": "35.000",
        "adg_from": None,
        "previous_sampling_id": None,
        "days_since_previous": None,
        "weight_gain_g": None,
        "adg_g": None,
    }

    second = sample(client, group, days_ago(21), ("0.500", "0.500", "0.500"))
    assert second["result"] == {
        "fish_count": 30,
        "total_weight_kg": "1.500",
        "average_weight_g": "50.000",
        "adg_from": "SAMPLING",
        "previous_sampling_id": first["id"],
        "days_since_previous": 14,
        "weight_gain_g": "15.000",
        "adg_g": "1.071",  # 15 g / 14 days
    }

    # The list and detail endpoints expose the same values.
    listed = client.get("/api/v1/lele-samplings", params={"group_id": group.id}).json()["data"]
    assert [row["result"]["average_weight_g"] for row in listed] == ["50.000", "35.000"]
    assert client.get(f"/api/v1/lele-samplings/{second['id']}").json()["data"]["result"] == second["result"]

    stored = results_by_date(biomass_db, group)
    assert stored[days_ago(21)].average_weight_g == Decimal("50.000")
    assert stored[days_ago(21)].adg_g == Decimal("1.071")


def test_every_real_adg_is_kept_and_the_latest_one_drives_the_projection(
    biomass_db: Session, client: TestClient
) -> None:
    group = make_group(biomass_db, "A")
    s1 = sample(client, group, days_ago(36), ("0.300", "0.350", "0.400"))  # 35 g
    s2 = sample(client, group, days_ago(24), ("0.500", "0.500", "0.500"))  # 50 g
    s3 = sample(client, group, days_ago(12), ("0.700", "0.760", "0.740"))  # 73.333 g

    # 3 samplings -> 2 real ADGs: #1-#2 and #2-#3.
    stored = results_by_date(biomass_db, group)
    adgs = {day: row.adg_g for day, row in stored.items() if row.adg_g is not None}
    assert adgs == {days_ago(24): Decimal("1.250"), days_ago(12): Decimal("1.944")}
    kept = {day: (row.id, row.previous_sampling_id, row.adg_g, row.updated_at) for day, row in stored.items()}
    assert kept[days_ago(24)][1] == s1["id"] and kept[days_ago(12)][1] == s2["id"]

    # A newer sampling adds a 3rd ADG (#3-#4) without rewriting the earlier ones.
    sample(client, group, days_ago(0), ("1.000", "1.050", "0.950"))  # 100 g
    stored = results_by_date(biomass_db, group)
    for day, before in kept.items():
        row = stored[day]
        assert (row.id, row.previous_sampling_id, row.adg_g, row.updated_at) == before
    assert stored[days_ago(0)].previous_sampling_id == s3["id"]
    assert stored[days_ago(0)].adg_g == Decimal("2.222")  # 26.667 g / 12 days
    assert sum(1 for row in stored.values() if row.adg_g is not None) == 3

    # Only the latest real ADG is used for the sell date: (142 - 100) / 2.222 = 18.9 -> 19 days.
    data = biomass(client, group)
    adgs = real_adgs(data)
    assert [(series["name"], series["adg_g"], series["is_latest"]) for series in adgs] == [
        ("Real ADG #1", "1.250", False),
        ("Real ADG #2", "1.944", False),
        ("Real ADG #3", "2.222", True),
    ]
    projection = data["projection"]
    assert (projection["adg_source"], projection["adg_g"], projection["days_to_target"]) == ("ACTUAL", "2.222", 19)
    assert projection["projected_sell_date"] == (TODAY + timedelta(days=19)).isoformat()


def test_backdated_sampling_recomputes_the_next_interval(biomass_db: Session, client: TestClient) -> None:
    group = make_group(biomass_db, "A")
    first = sample(client, group, days_ago(30), ("0.300", "0.300", "0.300"))  # 30 g
    last = sample(client, group, days_ago(10), ("0.900", "0.900", "0.900"))  # 90 g, 60 g / 20 days
    assert last["result"]["adg_g"] == "3.000"

    middle = sample(client, group, days_ago(20), ("0.450", "0.450", "0.450"))  # 45 g
    stored = results_by_date(biomass_db, group)
    assert stored[days_ago(20)].previous_sampling_id == first["id"]
    assert stored[days_ago(20)].adg_g == Decimal("1.500")  # 15 g / 10 days
    assert stored[days_ago(10)].previous_sampling_id == middle["id"]
    assert (stored[days_ago(10)].days_since_previous, stored[days_ago(10)].adg_g) == (10, Decimal("4.500"))


def test_editing_a_sampling_recomputes_itself_and_the_next_one(biomass_db: Session, client: TestClient) -> None:
    group = make_group(biomass_db, "A")
    first = sample(client, group, days_ago(30), ("0.300", "0.300", "0.300"))  # 30 g
    sample(client, group, days_ago(10), ("0.900", "0.900", "0.900"))  # 90 g

    # Correct the first weighing: 60 g, and move it to 20 days ago.
    sample(client, group, days_ago(20), ("0.600", "0.600", "0.600"), sampling_id=int(str(first["id"])))
    stored = results_by_date(biomass_db, group)
    assert set(stored) == {days_ago(20), days_ago(10)}
    assert stored[days_ago(20)].average_weight_g == Decimal("60.000")
    assert stored[days_ago(20)].adg_g is None
    assert (stored[days_ago(10)].days_since_previous, stored[days_ago(10)].adg_g) == (10, Decimal("3.000"))


def test_moving_a_sampling_to_another_group_recomputes_both(biomass_db: Session, client: TestClient) -> None:
    group_a, group_b = make_group(biomass_db, "A"), make_group(biomass_db, "B")
    sample(client, group_a, days_ago(30), ("0.300", "0.300", "0.300"))
    moved = sample(client, group_a, days_ago(20), ("0.600", "0.600", "0.600"))
    sample(client, group_a, days_ago(10), ("0.900", "0.900", "0.900"))
    sample(client, group_b, days_ago(25), ("0.450", "0.450", "0.450"))

    sample(client, group_b, days_ago(20), ("0.600", "0.600", "0.600"), sampling_id=int(str(moved["id"])))

    stored_a = results_by_date(biomass_db, group_a)
    assert set(stored_a) == {days_ago(30), days_ago(10)}
    assert (stored_a[days_ago(10)].days_since_previous, stored_a[days_ago(10)].adg_g) == (20, Decimal("3.000"))
    stored_b = results_by_date(biomass_db, group_b)
    assert set(stored_b) == {days_ago(25), days_ago(20)}
    assert (stored_b[days_ago(20)].days_since_previous, stored_b[days_ago(20)].adg_g) == (5, Decimal("3.000"))


# --- Biomass analytics --------------------------------------------------------------------------


def test_biomass_detail_reports_group_biomass_and_projection(
    biomass_db: Session, actor: User, client: TestClient
) -> None:
    group = make_group(biomass_db, "A", population=1000)  # started 60 days ago
    biomass_db.add(
        PopulationAdjustment(
            group_id=group.id, delta_ekor=-100, effective_date=days_ago(40), reason="Mortality", created_by=actor.id
        )
    )
    rebuild(biomass_db, group, "population_adjusted")
    sample(client, group, days_ago(35), ("0.300", "0.350", "0.400"))  # 35 g
    sample(client, group, days_ago(21), ("0.500", "0.500", "0.500"))  # 50 g
    sample(client, group, days_ago(7), ("0.700", "0.750", "0.800"))  # 75 g

    data = biomass(client, group)

    assert set(data) == {
        "group",
        "target_weight_g",
        "latest_sampling",
        "projection",
        "estimate",
        "samplings",
        "chart",
        "sync",
    }
    assert data["sync"]["pending"] is False
    # Only what matters to the user: no ids or other keys anywhere in the response.
    assert not {key for key in keys(data) if key == "id" or key.endswith("_id") or key == "code"}
    assert data["group"] == {
        "name": "Biomass test A",
        "location": "Kolam 1",
        "status": "ACTIVE",
        "start_date": days_ago(60).isoformat(),
        "closed_at": None,
        "age_days": 60,
        "initial_population": 1000,
        "current_population": 900,
        "initial_weight_g": None,
        "notes": None,
    }
    assert data["target_weight_g"] == "142.000"
    assert data["latest_sampling"] == {
        "sampling_date": days_ago(7).isoformat(),
        "day": 53,
        "average_weight_g": "75.000",
        "population": 900,
        "biomass_kg": "67.500",  # 900 fish * 75 g
    }
    assert data["samplings"][0] == {
        "sampling_date": days_ago(35).isoformat(),
        "day": 25,
        "average_weight_g": "35.000",
        "population": 900,
        "biomass_kg": "31.500",
    }
    assert [row["average_weight_g"] for row in data["samplings"]] == ["35.000", "50.000", "75.000"]
    assert data["projection"] == {
        "baseline": {"source": "SAMPLING", "date": days_ago(7).isoformat(), "average_weight_g": "75.000"},
        "ready_to_sell": False,
        "remaining_weight_g": "67.000",
        "adg_source": "ACTUAL",
        "adg_g": "1.786",  # latest real ADG: (75 - 50) g / 14 days
        "days_to_target": 38,  # 67 / 1.786 = 37.5
        "projected_sell_date": (days_ago(7) + timedelta(days=38)).isoformat(),
        "projected_biomass_kg": "127.800",  # 900 fish * 142 g
    }
    assert data["estimate"] == {
        "as_of": TODAY.isoformat(),
        "capped_at_harvest_date": False,  # the harvest date is still 31 days ahead
        "adg_source": "ACTUAL",
        "days_since_baseline": 7,
        "average_weight_g": "87.502",  # 75 + 7 * 1.786
        "population": 900,
        "biomass_kg": "78.752",
    }


def test_biomass_chart_ends_on_the_harvest_date_of_the_latest_real_adg(biomass_db: Session, client: TestClient) -> None:
    group = make_group(biomass_db, "A")  # started 60 days ago: day N = days_ago(60 - N)
    sample(client, group, days_ago(35), ("0.300", "0.350", "0.400"))  # day 25: 35 g
    sample(client, group, days_ago(21), ("0.500", "0.500", "0.500"))  # day 39: 50 g
    sample(client, group, days_ago(7), ("0.700", "0.750", "0.800"))  # day 53: 75 g

    def day(number: int) -> date:
        return days_ago(60 - number)

    data = biomass(client, group)
    chart = data["chart"]
    # Harvest date = latest real ADG (1.786 g/day) from 75 g on day 53: (142 - 75) / 1.786 = 37.5
    # -> 38 days -> day 91. It is the last date of the chart, not the default ADG's date.
    assert data["projection"]["projected_sell_date"] == day(91).isoformat()
    assert chart["end_date"] == day(91).isoformat()
    assert (chart["y_min_g"], chart["target_weight_g"]) == ("0.000", "142.000")
    assert chart["y_max_g"] == "142.868"  # highest value on the chart

    # X axis: every week from the start date up to the harvest date, plus the sampling days.
    dates = chart["dates"]
    assert [item["day"] for item in dates] == sorted([7 * week for week in range(14)] + [25, 39, 53])
    assert dates[0] == {
        "date": day(0).isoformat(),
        "day": 0,
        "week": 0,
        "is_sampling": False,
        "is_harvest_date": False,
    }
    assert dates[4] == {
        "date": day(25).isoformat(),
        "day": 25,
        "week": None,
        "is_sampling": True,
        "is_harvest_date": False,
    }
    assert dates[-1] == {
        "date": day(91).isoformat(),
        "day": 91,
        "week": 13,
        "is_sampling": False,
        "is_harvest_date": True,
    }
    assert all(len(series["values"]) == len(dates) for series in chart["series"])

    # Lines: actual weight, default ADG, then every real ADG numbered oldest first.
    names = [series["name"] for series in chart["series"]]
    assert names == ["Actual weight", "Default ADG", "Real ADG #1", "Real ADG #2"]
    assert [series["is_latest"] for series in chart["series"]] == [False, False, False, True]

    # Actual weight: measured values only, null between samplings.
    actual = series_named(data, "Actual weight")
    assert [value for value in actual["values"] if value] == ["35.000", "50.000", "75.000"]
    assert value_on(data, "Actual weight", day(0)) is None  # no stocking weight
    assert value_on(data, "Actual weight", day(28)) is None

    # The latest real ADG line reaches the target on the last date ...
    latest = series_named(data, "Real ADG #2")
    assert (latest["adg_g"], latest["start_date"]) == ("1.786", day(53).isoformat())
    assert (latest["days_to_target"], latest["projected_sell_date"]) == (38, day(91).isoformat())
    assert value_on(data, "Real ADG #2", day(49)) is None
    assert value_on(data, "Real ADG #2", day(91)) == "142.868"  # 75 + 38 * 1.786

    # ... and the other lines run to the same date, wherever they are then (here below 142 g).
    default = series_named(data, "Default ADG")
    assert {key: default[key] for key in ("type", "adg_g", "start_date", "start_weight_g", "period")} == {
        "type": "DEFAULT",
        "adg_g": "1.300",
        "start_date": day(53).isoformat(),
        "start_weight_g": "75.000",
        "period": None,
    }
    assert (default["days_to_target"], default["projected_sell_date"]) == (52, day(105).isoformat())
    assert value_on(data, "Default ADG", day(49)) is None
    assert value_on(data, "Default ADG", day(53)) == "75.000"
    assert value_on(data, "Default ADG", day(56)) == "78.900"  # 75 + 3 * 1.3
    assert value_on(data, "Default ADG", day(91)) == "124.400"  # 75 + 38 * 1.3

    first = series_named(data, "Real ADG #1")  # #1 -> #2 sampling, 1.071 g/day from 50 g on day 39
    assert first["type"] == "REAL" and first["adg_g"] == "1.071"
    assert (first["start_date"], first["start_weight_g"]) == (day(39).isoformat(), "50.000")
    assert first["period"] == {
        "from": "SAMPLING",
        "from_date": day(25).isoformat(),
        "to_date": day(39).isoformat(),
        "days": 14,
        "from_weight_g": "35.000",
        "to_weight_g": "50.000",
        "weight_gain_g": "15.000",
    }
    # Its own sell date is after the chart's end; its line stops at the harvest date.
    assert (first["days_to_target"], first["projected_sell_date"]) == (86, day(125).isoformat())
    assert value_on(data, "Real ADG #1", day(35)) is None
    assert value_on(data, "Real ADG #1", day(42)) == "53.213"  # 50 + 3 * 1.071
    assert value_on(data, "Real ADG #1", day(91)) == "105.692"  # 50 + 52 * 1.071


def test_single_sampling_projects_with_the_default_adg(biomass_db: Session, client: TestClient) -> None:
    group = make_group(biomass_db, "A", population=500)
    sample(client, group, days_ago(10), ("0.300", "0.350", "0.400"))  # 35 g

    data = biomass(client, group)
    assert real_adgs(data) == []
    projection = data["projection"]
    assert (projection["adg_source"], projection["adg_g"], projection["days_to_target"]) == ("DEFAULT", "1.300", 83)
    assert projection["projected_sell_date"] == (days_ago(10) + timedelta(days=83)).isoformat()
    assert series_named(data, "Default ADG")["projected_sell_date"] == projection["projected_sell_date"]
    # 35 + 10 days * 1.3 = 48 g, for 500 fish = 24 kg.
    assert (data["estimate"]["average_weight_g"], data["estimate"]["biomass_kg"]) == ("48.000", "24.000")


def test_negative_real_adg_falls_back_to_default_projection(biomass_db: Session, client: TestClient) -> None:
    group = make_group(biomass_db, "A")
    sample(client, group, days_ago(20), ("0.600", "0.600", "0.600"))  # 60 g
    sample(client, group, days_ago(10), ("0.570", "0.570", "0.570"))  # 57 g

    data = biomass(client, group)
    [latest] = real_adgs(data)
    # Charted as measured, but it never reaches the target.
    assert (latest["adg_g"], latest["is_latest"], latest["projected_sell_date"]) == ("-0.300", True, None)
    assert value_on(data, "Real ADG #1", days_ago(10)) == "57.000"
    assert data["projection"]["adg_source"] == "DEFAULT"
    assert data["projection"]["days_to_target"] == 66  # (142 - 57) / 1.3 = 65.4


def test_group_that_reached_the_target_is_ready_to_sell(biomass_db: Session, client: TestClient) -> None:
    group = make_group(biomass_db, "A", start=90)
    sample(client, group, days_ago(20), ("1.200", "1.200", "1.200"))  # 120 g
    sample(client, group, days_ago(5), ("1.500", "1.500", "1.500"))  # 150 g

    projection = biomass(client, group)["projection"]
    assert projection["ready_to_sell"] is True
    assert (projection["remaining_weight_g"], projection["days_to_target"]) == ("0.000", 0)
    assert projection["projected_sell_date"] == days_ago(5).isoformat()
    assert projection["projected_biomass_kg"] == "150.000"  # 1000 fish at their current 150 g


def test_ready_group_with_negative_adg_does_not_estimate_shrinking_fish(
    biomass_db: Session, client: TestClient
) -> None:
    group = make_group(biomass_db, "A", start=90)
    sample(client, group, days_ago(20), ("1.500", "1.500", "1.500"))  # 150 g
    sample(client, group, days_ago(10), ("1.470", "1.470", "1.470"))  # 147 g: -0.3 g/day

    data = biomass(client, group)
    assert data["projection"]["ready_to_sell"] is True
    assert data["projection"]["adg_source"] == "DEFAULT"
    assert data["projection"]["projected_sell_date"] == days_ago(10).isoformat()  # ready on that sampling
    # Never estimated past the harvest date (here the sampling itself), so no shrinking and no growth.
    assert data["estimate"]["as_of"] == days_ago(10).isoformat()
    assert data["estimate"]["capped_at_harvest_date"] is True
    assert data["estimate"]["average_weight_g"] == "147.000"


def test_group_without_weights_has_an_empty_chart(biomass_db: Session, client: TestClient) -> None:
    group = make_group(biomass_db, "A")
    data = biomass(client, group)
    assert data["latest_sampling"] is None and data["projection"] is None and data["estimate"] is None
    assert data["samplings"] == []
    chart = data["chart"]
    assert chart["dates"] == [
        {"date": days_ago(60).isoformat(), "day": 0, "week": 0, "is_sampling": False, "is_harvest_date": False}
    ]
    assert [(series["name"], series["values"]) for series in chart["series"]] == [
        ("Actual weight", [None]),
        ("Default ADG", [None]),
    ]


def test_biomass_detail_only_for_lele_groups(biomass_db: Session, client: TestClient) -> None:
    ayam = make_group(biomass_db, "AYAM", species="AYAM")
    assert client.get(f"/api/v1/lele-biomass/{ayam.id}").status_code == 404
    assert client.get("/api/v1/lele-biomass/999999999").status_code == 404


def test_biomass_list_endpoint_is_removed(biomass_db: Session, client: TestClient) -> None:
    assert client.get("/api/v1/lele-biomass").status_code == 404
    paths = client.get("/openapi.json").json()["paths"]
    assert [path for path in paths if path.startswith("/api/v1/lele-biomass")] == ["/api/v1/lele-biomass/{group_id}"]


def test_biomass_endpoint_requires_authentication(biomass_db: Session) -> None:
    group = make_group(biomass_db, "A")
    with TestClient(app) as anonymous:
        assert anonymous.get(f"/api/v1/lele-biomass/{group.id}").status_code == 401


def test_estimate_stops_at_the_harvest_date(biomass_db: Session, client: TestClient) -> None:
    group = make_group(biomass_db, "A", start=90)
    sample(client, group, days_ago(80), ("0.500", "0.500", "0.500"))  # 50 g
    sample(client, group, days_ago(60), ("1.000", "1.000", "1.000"))  # 100 g: 2.5 g/day

    data = biomass(client, group)
    # Harvest date: (142 - 100) / 2.5 = 16.8 -> 17 days after the latest sampling, long passed.
    harvest = days_ago(60) + timedelta(days=17)
    assert data["projection"]["projected_sell_date"] == harvest.isoformat()
    # The weight is not extrapolated for the 60 days since the sampling (that gave 250 g).
    assert data["estimate"] == {
        "as_of": harvest.isoformat(),
        "capped_at_harvest_date": True,
        "adg_source": "ACTUAL",
        "days_since_baseline": 17,
        "average_weight_g": "142.500",  # 100 + 17 * 2.5
        "population": 1000,
        "biomass_kg": "142.500",
    }
    assert data["group"]["age_days"] == 90


# --- Background rebuild of the read model -------------------------------------------------------

READ_MODEL_TABLES = {
    "lele_biomass_snapshots",
    "lele_biomass_chart_dates",
    "lele_biomass_chart_series",
    "lele_biomass_chart_values",
    "lele_biomass_jobs",
}


def test_get_reads_only_the_precomputed_tables(biomass_db: Session, client: TestClient) -> None:
    group = make_group(biomass_db, "A", initial_weight="5")
    sample(client, group, days_ago(35), ("0.300", "0.350", "0.400"))
    sample(client, group, days_ago(21), ("0.500", "0.500", "0.500"))

    statements: list[str] = []

    def capture(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = client.get(f"/api/v1/lele-biomass/{group.id}")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200
    tables = {name.lower() for name in re.findall(r"\b(?:FROM|JOIN)\s+([a-z_]+)", " ".join(statements), re.I)}
    # Only authentication (users) and the read model: no samplings, groups, mortality, ...
    assert tables - {"users"} <= READ_MODEL_TABLES
    assert "lele_biomass_snapshots" in tables


def test_writes_queue_the_rebuild_and_do_not_compute_it(
    biomass_db: Session, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    group = make_group(biomass_db, "A")
    sample(client, group, days_ago(21), ("0.500", "0.500", "0.500"))
    # Stop the after-response background task, to see what the request itself did.
    monkeypatch.setattr(farm_api, "rebuild_biomass_in_background", lambda: None)

    sample(client, group, days_ago(7), ("0.700", "0.750", "0.800"))
    assert queued(biomass_db) == [(group.id, "sampling_saved")]
    stale = biomass(client, group)
    assert stale["sync"]["pending"] is True
    assert stale["latest_sampling"]["sampling_date"] == days_ago(21).isoformat()  # not rebuilt yet

    assert drain() == 1
    fresh = biomass(client, group)
    assert fresh["sync"]["pending"] is False
    assert fresh["latest_sampling"]["sampling_date"] == days_ago(7).isoformat()
    assert queued(biomass_db) == []


def test_every_change_to_a_lele_group_queues_it(
    biomass_db: Session, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(farm_api, "rebuild_biomass_in_background", lambda: None)
    feed = FeedType(species="LELE", name=f"{CODE_PREFIX} pellet", is_active=True)
    biomass_db.add(feed)
    biomass_db.commit()
    other = make_group(biomass_db, "B")

    created = client.post("/api/v1/livestock-groups", json=group_payload(initial_weight_g="5"))
    group_id = created.json()["data"]["id"]
    group = biomass_db.get(LivestockGroup, group_id)
    assert group is not None
    ayam = client.post("/api/v1/livestock-groups", json=group_payload(species="AYAM", code=f"{CODE_PREFIX}-AYAM-API"))
    assert ayam.status_code == 201
    client.patch(f"/api/v1/livestock-groups/{group_id}", json=edit_payload(group, initial_weight_g="6"))
    client.post(
        f"/api/v1/livestock-groups/{group_id}/population-adjustments",
        json={"delta_ekor": 50, "effective_date": days_ago(40).isoformat(), "reason": "Restock"},
    )
    feeds = [{"feed_type_id": feed.id, "morning_kg": "1", "noon_kg": "1"}]
    daily = client.post(
        "/api/v1/daily-records/bulk",
        json={
            "species": "LELE",
            "rows": [{"group_id": group_id, "record_date": days_ago(30).isoformat(), "mortality": 10, "feeds": feeds}],
        },
    )
    assert daily.status_code == 201, daily.json()
    client.patch(
        f"/api/v1/daily-records/{daily.json()['data'][0]['id']}",
        json={"species": "LELE", "mortality": 12, "feeds": feeds},
    )
    moved = sample(client, group, days_ago(20), ("0.500", "0.500", "0.500"))
    sample(client, other, days_ago(20), ("0.500", "0.500", "0.500"), sampling_id=int(str(moved["id"])))
    closed = client.post(
        f"/api/v1/livestock-groups/{group_id}/close",
        json={"closed_at": days_ago(1).isoformat(), "reason": "Harvested"},
    )
    assert closed.status_code == 200, closed.json()

    assert queued(biomass_db) == [
        (group_id, "group_created"),  # the Ayam group is not queued
        (group_id, "group_updated"),
        (group_id, "population_adjusted"),
        (group_id, "daily_created"),
        (group_id, "daily_updated"),
        (group_id, "sampling_saved"),
        (other.id, "sampling_saved"),  # the sampling moved to the other group ...
        (group_id, "sampling_moved"),  # ... and its old group is rebuilt too
        (group_id, "group_closed"),
    ]
    assert drain() == 2  # several jobs of one group are rebuilt once
    assert queued(biomass_db) == []


def test_mortality_and_closing_are_reflected_in_the_biomass(biomass_db: Session, client: TestClient) -> None:
    feed = FeedType(species="LELE", name=f"{CODE_PREFIX} pellet", is_active=True)
    biomass_db.add(feed)
    biomass_db.commit()
    group = make_group(biomass_db, "A", population=1000)
    sample(client, group, days_ago(10), ("1.000", "1.000", "1.000"))  # 100 g
    assert biomass(client, group)["latest_sampling"]["biomass_kg"] == "100.000"

    client.post(
        "/api/v1/daily-records/bulk",
        json={
            "species": "LELE",
            "rows": [
                {
                    "group_id": group.id,
                    "record_date": days_ago(20).isoformat(),
                    "mortality": 200,
                    "feeds": [{"feed_type_id": feed.id, "morning_kg": "1", "noon_kg": "1"}],
                }
            ],
        },
    )
    data = biomass(client, group)
    assert data["group"]["current_population"] == 800
    assert (data["latest_sampling"]["population"], data["latest_sampling"]["biomass_kg"]) == (800, "80.000")

    client.post(
        f"/api/v1/livestock-groups/{group.id}/close",
        json={"closed_at": days_ago(2).isoformat(), "reason": "Harvested"},
    )
    data = biomass(client, group)
    assert (data["group"]["status"], data["group"]["closed_at"]) == ("CLOSED", days_ago(2).isoformat())
    assert data["estimate"]["as_of"] == days_ago(2).isoformat()


def test_editing_a_sampling_recomputes_every_related_value(biomass_db: Session, client: TestClient) -> None:
    group = make_group(biomass_db, "A")  # started 60 days ago
    sample(client, group, days_ago(35), ("0.300", "0.350", "0.400"))  # day 25: 35 g
    middle = sample(client, group, days_ago(21), ("0.500", "0.500", "0.500"))  # day 39: 50 g
    sample(client, group, days_ago(7), ("0.700", "0.750", "0.800"))  # day 53: 75 g
    before = biomass(client, group)
    assert [line["adg_g"] for line in real_adgs(before)] == ["1.071", "1.786"]

    # Correct the middle weighing: 55 g instead of 50 g. Both ADGs around it change.
    sample(client, group, days_ago(21), ("0.550", "0.550", "0.550"), sampling_id=int(str(middle["id"])))
    after = biomass(client, group)
    assert [line["adg_g"] for line in real_adgs(after)] == ["1.429", "1.429"]  # 20 g / 14 days each
    assert real_adgs(after)[0]["period"]["to_weight_g"] == "55.000"
    assert real_adgs(after)[1]["period"]["from_weight_g"] == "55.000"
    assert value_on(after, "Actual weight", days_ago(21)) == "55.000"
    assert after["samplings"][1]["biomass_kg"] == "55.000"  # 1000 fish * 55 g
    # New harvest date: (142 - 75) / 1.429 = 46.9 -> 47 days after the latest sampling.
    harvest = days_ago(7) + timedelta(days=47)
    assert (after["projection"]["adg_g"], after["projection"]["projected_sell_date"]) == ("1.429", harvest.isoformat())
    assert after["chart"]["end_date"] == harvest.isoformat()
    assert after["chart"]["dates"][-1]["is_harvest_date"] is True
    assert after["estimate"]["average_weight_g"] == "85.003"  # 75 + 7 * 1.429

    # The incremental rebuild equals a full rebuild from scratch.
    backfill(biomass_db)
    rebuilt = biomass(client, group)
    assert {**rebuilt, "sync": None} == {**after, "sync": None}


def test_a_failing_rebuild_stays_queued_and_does_not_block_others(
    biomass_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    group_a, group_b = make_group(biomass_db, "A"), make_group(biomass_db, "B")
    real_rebuild = biomass_jobs.rebuild_snapshot

    def flaky(db: Session, group_id: int, today: date | None = None) -> bool:
        if group_id == group_a.id:
            raise RuntimeError("boom")
        return real_rebuild(db, group_id, today)

    monkeypatch.setattr(biomass_jobs, "rebuild_snapshot", flaky)
    enqueue(biomass_db, group_a, "first")
    enqueue(biomass_db, group_b, "second")
    biomass_db.commit()
    assert drain() == 1
    assert queued(biomass_db) == [(group_a.id, "first")]

    # The after-response entry point never raises.
    monkeypatch.setattr(biomass_jobs, "drain", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    biomass_jobs.drain_in_background()

    monkeypatch.setattr(biomass_jobs, "rebuild_snapshot", real_rebuild)
    assert drain() == 1  # the next trigger picks it up
    assert queued(biomass_db) == []


def test_concurrent_workers_rebuild_every_group(biomass_db: Session) -> None:
    groups = [make_group(biomass_db, f"C{index}", initial_weight="5") for index in range(6)]
    biomass_db.execute(delete(LeleBiomassSnapshot).where(LeleBiomassSnapshot.group_id.in_([g.id for g in groups])))
    for group in groups:
        for reason in ("one", "two", "three"):
            enqueue(biomass_db, group, reason)
    biomass_db.commit()

    errors: list[BaseException] = []

    def work() -> None:
        try:
            drain()
        except BaseException as error:  # surfaced below
            errors.append(error)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert queued(biomass_db) == []
    snapshots = biomass_db.scalars(
        select(LeleBiomassSnapshot.group_id).where(LeleBiomassSnapshot.group_id.in_([g.id for g in groups]))
    ).all()
    assert sorted(snapshots) == sorted(group.id for group in groups)


def test_rebuilding_a_group_twice_in_one_session_leaves_no_stale_rows(biomass_db: Session) -> None:
    group = make_group(biomass_db, "A", initial_weight="5")
    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)  # e.g. "Identity map already had an identity"
        held = biomass_db.scalars(select(LeleBiomassChartDate).where(LeleBiomassChartDate.group_id == group.id)).all()
        first = biomass_db.get(LeleBiomassSnapshot, group.id)
        assert held and first is not None
        # A worker that rebuilds the same group again in the same session (new job meanwhile).
        assert rebuild_snapshot(biomass_db, group.id)
        biomass_db.commit()
        assert rebuild_snapshot(biomass_db, group.id)
        biomass_db.commit()
        again = biomass_db.get(LeleBiomassSnapshot, group.id)
    assert again is not None and again is not first and again.computed_at > first.computed_at


def test_a_group_still_being_computed_returns_202(biomass_db: Session, client: TestClient) -> None:
    group = LivestockGroup(
        species="LELE",
        code=f"{CODE_PREFIX}-NEW",
        name="Biomass test NEW",
        start_date=days_ago(5),
        initial_population=100,
        location="Kolam 1",
        status="ACTIVE",
    )
    biomass_db.add(group)
    biomass_db.flush()
    enqueue(biomass_db, group, "group_created")
    biomass_db.commit()

    pending = client.get(f"/api/v1/lele-biomass/{group.id}")
    assert pending.status_code == 202
    assert pending.json()["code"] == "BIOMASS_PENDING"
    assert worker_main(["--once"]) == 0  # the dedicated worker drains the queue too
    assert client.get(f"/api/v1/lele-biomass/{group.id}").status_code == 200


# --- Initial (stocking) weight on the livestock group ------------------------------------------


def group_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "species": "LELE",
        "code": f"{CODE_PREFIX}-API",
        "name": "Biomass test API",
        "start_date": days_ago(50).isoformat(),
        "initial_population": 1000,
        "location": "Kolam 1",
    }
    payload.update(overrides)
    return payload


def edit_payload(group: LivestockGroup, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": group.name,
        "start_date": group.start_date.isoformat(),
        "initial_population": group.initial_population,
        "location": group.location,
    }
    payload.update(overrides)
    return payload


def test_create_group_with_initial_weight(biomass_db: Session, client: TestClient) -> None:
    response = client.post("/api/v1/livestock-groups", json=group_payload(initial_weight_g="5.25"))
    assert response.status_code == 201, response.json()
    data = response.json()["data"]
    assert data["initial_weight_g"] == "5.250"
    assert client.get(f"/api/v1/livestock-groups/{data['id']}").json()["data"]["initial_weight_g"] == "5.250"
    assert biomass_db.get(LivestockGroup, data["id"]).initial_weight_g == Decimal("5.250")

    # Optional, so existing clients keep working.
    without = client.post("/api/v1/livestock-groups", json=group_payload(code=f"{CODE_PREFIX}-API-2"))
    assert without.status_code == 201 and without.json()["data"]["initial_weight_g"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"initial_weight_g": "0"},
        {"initial_weight_g": "-1"},
        {"initial_weight_g": "100001"},
        {"species": "AYAM", "initial_weight_g": "5"},
    ],
)
def test_create_group_rejects_invalid_initial_weight(
    biomass_db: Session, client: TestClient, overrides: dict[str, object]
) -> None:
    response = client.post("/api/v1/livestock-groups", json=group_payload(**overrides))
    assert response.status_code == 422
    assert "initial_weight_g" in response.json()["errors"]


def test_initial_weight_starts_the_first_real_adg(biomass_db: Session, client: TestClient) -> None:
    group = make_group(biomass_db, "A", start=50, initial_weight="5")
    s1 = sample(client, group, days_ago(36), ("0.300", "0.350", "0.400"))  # 35 g
    s2 = sample(client, group, days_ago(24), ("0.500", "0.500", "0.500"))  # 50 g
    sample(client, group, days_ago(12), ("0.700", "0.760", "0.740"))  # 73.333 g

    # 3 samplings + stocking weight -> 3 real ADGs: stocking-#1, #1-#2, #2-#3.
    assert s1["result"]["adg_from"] == "STOCKING"
    assert s1["result"]["previous_sampling_id"] is None
    assert s1["result"]["days_since_previous"] == 14
    assert s1["result"]["weight_gain_g"] == "30.000"
    assert s1["result"]["adg_g"] == "2.143"  # (35 - 5) g / 14 days
    assert s2["result"]["adg_from"] == "SAMPLING"

    data = biomass(client, group)
    assert data["group"]["initial_weight_g"] == "5.000"
    assert [
        (series["name"], series["period"]["from"], series["period"]["from_date"], series["adg_g"])
        for series in real_adgs(data)
    ] == [
        ("Real ADG #1", "STOCKING", days_ago(50).isoformat(), "2.143"),
        ("Real ADG #2", "SAMPLING", days_ago(36).isoformat(), "1.250"),
        ("Real ADG #3", "SAMPLING", days_ago(24).isoformat(), "1.944"),
    ]
    assert real_adgs(data)[0]["period"]["from_weight_g"] == "5.000"
    # The stocking weight is the first point of the actual weight line.
    assert value_on(data, "Actual weight", days_ago(50)) == "5.000"
    # The latest real ADG still drives the projection: (142 - 73.333) / 1.944 = 35.3 -> 36 days.
    assert (data["projection"]["adg_g"], data["projection"]["days_to_target"]) == ("1.944", 36)

    # The chart ends on that harvest date (day 74); the other lines end above or below 142 g.
    harvest = days_ago(12) + timedelta(days=36)
    assert data["chart"]["end_date"] == harvest.isoformat()
    assert value_on(data, "Real ADG #3", harvest) == "143.317"  # latest: 73.333 + 36 * 1.944
    assert value_on(data, "Real ADG #1", harvest) == "163.580"  # faster: 35 + 60 * 2.143
    assert value_on(data, "Real ADG #2", harvest) == "110.000"  # slower: 50 + 48 * 1.25
    assert value_on(data, "Default ADG", harvest) == "120.133"  # 73.333 + 36 * 1.3
    assert data["chart"]["y_max_g"] == "163.580"


def test_single_sampling_with_initial_weight_projects_with_real_adg(biomass_db: Session, client: TestClient) -> None:
    group = make_group(biomass_db, "A", start=20, initial_weight="5")
    sample(client, group, days_ago(10), ("0.300", "0.300", "0.300"))  # 30 g: 25 g / 10 days

    projection = biomass(client, group)["projection"]
    assert (projection["adg_source"], projection["adg_g"]) == ("ACTUAL", "2.500")
    assert projection["days_to_target"] == 45  # (142 - 30) / 2.5 = 44.8


def test_sampling_on_the_start_date_has_no_stocking_adg(biomass_db: Session, client: TestClient) -> None:
    group = make_group(biomass_db, "A", start=20, initial_weight="5")
    first = sample(client, group, days_ago(20), ("0.300", "0.300", "0.300"))
    second = sample(client, group, days_ago(10), ("0.600", "0.600", "0.600"))
    assert first["result"]["adg_g"] is None and first["result"]["adg_from"] is None
    assert second["result"]["adg_g"] == "3.000"


def test_group_with_initial_weight_and_no_sampling_projects_from_stocking(
    biomass_db: Session, client: TestClient
) -> None:
    group = make_group(biomass_db, "A", start=10, population=2000, initial_weight="12")

    data = biomass(client, group)
    assert data["latest_sampling"] is None and data["samplings"] == []
    assert real_adgs(data) == []
    projection = data["projection"]
    assert projection["baseline"] == {
        "source": "STOCKING",
        "date": days_ago(10).isoformat(),
        "average_weight_g": "12.000",
    }
    assert (projection["adg_source"], projection["days_to_target"]) == ("DEFAULT", 100)  # 130 / 1.3
    assert projection["projected_sell_date"] == (days_ago(10) + timedelta(days=100)).isoformat()
    # Chart: the default line starts at the stocking weight and ends on the harvest date (day 100).
    assert value_on(data, "Actual weight", days_ago(10)) == "12.000"
    assert value_on(data, "Default ADG", days_ago(10)) == "12.000"
    assert data["chart"]["dates"][-1] == {
        "date": (days_ago(10) + timedelta(days=100)).isoformat(),
        "day": 100,
        "week": None,
        "is_sampling": False,
        "is_harvest_date": True,
    }
    assert value_on(data, "Default ADG", days_ago(10) + timedelta(days=100)) == "142.000"  # 12 + 100 * 1.3
    # 12 + 10 days * 1.3 = 25 g, for 2000 fish = 50 kg.
    assert data["estimate"]["days_since_baseline"] == 10
    assert (data["estimate"]["average_weight_g"], data["estimate"]["biomass_kg"]) == ("25.000", "50.000")


def test_editing_initial_weight_recomputes_only_the_first_adg(biomass_db: Session, client: TestClient) -> None:
    group = make_group(biomass_db, "A", start=50)
    sample(client, group, days_ago(36), ("0.300", "0.350", "0.400"))  # 35 g
    sample(client, group, days_ago(24), ("0.500", "0.500", "0.500"))  # 50 g
    later = results_by_date(biomass_db, group)[days_ago(24)]
    later_before = (later.id, later.adg_g, later.updated_at)
    assert results_by_date(biomass_db, group)[days_ago(36)].adg_g is None

    response = client.patch(f"/api/v1/livestock-groups/{group.id}", json=edit_payload(group, initial_weight_g="7"))
    assert response.status_code == 200, response.json()
    assert response.json()["data"]["initial_weight_g"] == "7.000"
    stored = results_by_date(biomass_db, group)
    assert (stored[days_ago(36)].days_since_previous, stored[days_ago(36)].adg_g) == (14, Decimal("2.000"))
    assert (stored[days_ago(24)].id, stored[days_ago(24)].adg_g, stored[days_ago(24)].updated_at) == later_before

    # An edit that omits the field keeps the weight (clients unaware of it never erase it).
    response = client.patch(f"/api/v1/livestock-groups/{group.id}", json=edit_payload(group, name="Renamed"))
    assert response.status_code == 200
    assert response.json()["data"]["initial_weight_g"] == "7.000"
    assert results_by_date(biomass_db, group)[days_ago(36)].adg_g == Decimal("2.000")

    # Explicit null removes it, and with it the stocking ADG.
    response = client.patch(f"/api/v1/livestock-groups/{group.id}", json=edit_payload(group, initial_weight_g=None))
    assert response.status_code == 200
    assert response.json()["data"]["initial_weight_g"] is None
    assert results_by_date(biomass_db, group)[days_ago(36)].adg_g is None

    audits = biomass_db.scalars(
        select(AuditEvent)
        .where(AuditEvent.entity_type == "livestock_groups", AuditEvent.entity_id == group.id)
        .order_by(AuditEvent.id)
    ).all()
    assert [(event.before or {}).get("initial_weight_g") for event in audits] == [None, "7.000", "7.000"]
    assert [event.after["initial_weight_g"] for event in audits if event.after] == ["7.000", "7.000", None]


def test_initial_weight_is_rejected_for_ayam_edits(biomass_db: Session, client: TestClient) -> None:
    ayam = make_group(biomass_db, "AYAM", species="AYAM")
    response = client.patch(f"/api/v1/livestock-groups/{ayam.id}", json=edit_payload(ayam, initial_weight_g="5"))
    assert response.status_code == 422
    assert "initial_weight_g" in response.json()["errors"]
    assert client.patch(f"/api/v1/livestock-groups/{ayam.id}", json=edit_payload(ayam)).status_code == 200


# --- Backfill of samplings created before lele_sampling_results existed ------------------------


def test_backfill_recreates_missing_results_and_is_idempotent(
    biomass_db: Session, client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    group = make_group(biomass_db, "A")
    sample(client, group, days_ago(30), ("0.300", "0.300", "0.300"))
    sample(client, group, days_ago(10), ("0.900", "0.900", "0.900"))
    expected = {day: (row.average_weight_g, row.adg_g) for day, row in results_by_date(biomass_db, group).items()}
    # Simulate samplings recorded before the results table existed.
    biomass_db.execute(delete(LeleSamplingResult))
    biomass_db.commit()

    dry = backfill(biomass_db, dry_run=True)
    assert dry.created >= 2 and dry.dry_run
    assert results_by_date(biomass_db, group) == {}

    result = backfill(biomass_db)
    assert result.created >= 2
    restored = {day: (row.average_weight_g, row.adg_g) for day, row in results_by_date(biomass_db, group).items()}
    assert restored == expected

    again = backfill(biomass_db)
    assert (again.created, again.updated, again.removed) == (0, 0, 0)

    # It also builds the biomass read model of groups that have none (e.g. after the upgrade).
    biomass_db.execute(delete(LeleBiomassSnapshot).where(LeleBiomassSnapshot.group_id == group.id))
    biomass_db.commit()
    assert client.get(f"/api/v1/lele-biomass/{group.id}").status_code == 404
    assert backfill(biomass_db).snapshots >= 1
    assert biomass(client, group)["latest_sampling"]["average_weight_g"] == "90.000"

    assert backfill_main([]) == 0
    assert "already up to date" in capsys.readouterr().out
