"""What the hospitals look like right now, for the desk's panel.

One read, answering the question a hospital desk actually opens a screen to
ask: what has changed in the last few minutes, and does any of it land on
somebody who is already travelling.

**Plural, because a journey is.** A family treated in Mumbai may have her
bloods done in Gaya, and the Mumbai system cannot see the Gaya result that is
blocking her Mumbai appointment. So this returns one board per hospital, each
naming which system answered and when it was last heard from, and the panel
shows them side by side. That juxtaposition is the product: two hospitals that
never talk to each other, and one place that sees both.

Three things are deliberately *not* here. There is no decision — the panel
shows the situation and the suggestions come from `/api/disruptions/pending`,
which is already gated on a person. There is no clinical content, the same
boundary every other module draws. And there is no composite score: each feed
is ordered by two stated rules, newest first with anything touching a booked
patient lifted above it, so a desk can say why a row is at the top rather than
trusting a number.

The `live` flag is load-bearing rather than decorative. A demo that shows a
hospital pane without saying whether it is reading a real system is making a
claim it has not earned.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter

from ..domain.models import Disruption, Resource
from .access import AnyStaff
from .deps import ContainerDep

router = APIRouter()


def _resource_rows(state: ContainerDep, today: date, hospital_id: str | None) -> list[dict]:
    """Every bookable thing at one hospital, and whether a desk reported it down.

    Calls `HospitalTool.resource_status` directly rather than through
    `Toolbox.run`. The wrapper's job is to write a ledger row and publish a
    timed event for every fact, which is right for a planning decision and
    wrong for a panel that re-reads itself every few seconds: the audit trail
    would fill with a hundred identical rows saying the MRI is still working.
    """
    tools = getattr(state.runner, "tools", None)
    hospital_tool = getattr(tools, "hospital", None)
    disruptions: tuple[Disruption, ...] = tuple(state.world.disruptions.values())
    resources: dict[str, Resource] = state.world.resources

    rows: list[dict] = []
    for resource in resources.values():
        if hospital_id is not None and resource.hospital_id != hospital_id:
            continue
        row = {
            "resource_id": resource.id,
            "display_name": resource.display_name,
            "department": resource.department,
            "kind": resource.kind.value,
            "hospital_id": resource.hospital_id,
            "up": True,
            "down_from": None,
            "down_until": None,
            "note": "",
            "reported_by": "",
        }
        if hospital_tool is not None:
            result = hospital_tool.resource_status(resource, disruptions, today)
            if result is not None:
                row.update(result.value.to_json())
                row["provider"] = result.provider
        rows.append(row)

    # Anything out of service first, then by name so the list does not reshuffle
    # under a reader's eyes on every poll.
    rows.sort(key=lambda r: (r["up"], r["display_name"]))
    return rows


def _feed(sync: object) -> list[dict]:
    """One hospital's situation feed: newest first, booked-patient hits on top.

    The lift is the rule the demo plan states and the reason the board is worth
    looking at: a cancelled slot for a family already on a train is not the
    same news as a cancelled slot for one who has not left home, and sorting
    purely by time would bury it under whatever happened since.
    """
    recent: list[dict] = list(getattr(sync, "recent", []) or [])
    # The ring is already newest-first, and Python's sort is stable, so this
    # single key lifts the urgent rows without disturbing the order inside
    # either group.
    recent.sort(key=lambda row: not row.get("hits_booked_patients"))
    return recent


def _hospital_names(state: ContainerDep) -> dict[str, str]:
    registry = getattr(state, "registry", None)
    if registry is None:
        return {}
    try:
        return {h.id: h.name for h in registry.hospitals()}
    except Exception:  # noqa: BLE001 - a panel must not fail on a name lookup
        return {}


def _board(state: ContainerDep, hospital_id: str, today: date, names: dict) -> dict:
    """One hospital: which system answers for it, what moved, what is down."""
    systems = getattr(state, "hospitals", None)
    system = systems(hospital_id) if systems is not None else None
    sync = state.runner.hospital_syncs.get(hospital_id)

    connector = {
        "hospital_id": hospital_id,
        "hospital_name": names.get(hospital_id, hospital_id),
        "system": getattr(system, "name", "desks"),
        "live": bool(getattr(system, "live", False)),
        "primed": False,
        "appointments": 0,
        "last_read_at": None,
        "last_error": "",
        "counts": {},
    }
    if sync is not None:
        snapshot = sync.snapshot_json()
        connector.update(
            {
                "primed": snapshot["primed"],
                "appointments": snapshot["appointments"],
                "last_read_at": snapshot["last_read_at"],
                "last_error": snapshot["last_error"],
                "counts": snapshot["counts"],
                "window_days": snapshot["window_days"],
            }
        )

    return {
        "connector": connector,
        "resources": _resource_rows(state, today, hospital_id),
        "feed": _feed(sync),
    }


@router.get("/hospital/now", dependencies=[AnyStaff])
def hospital_now(state: ContainerDep) -> dict:
    """Every hospital's live view, and every connector's own state.

    Hospitals are listed even when nothing is connected to them, because a
    desk needs to see that its own hospital is unwatched rather than quiet.
    """
    today = state.world.today
    names = _hospital_names(state)

    # Every hospital we know of, from three sources, so none is silently
    # missing: the registry, the systems actually configured, and whatever
    # hospital the registered resources belong to.
    known: list[str] = []
    for hospital_id in (
        *names,
        *(state.hospitals.all() if state.hospitals is not None else {}),
        *(r.hospital_id for r in state.world.resources.values()),
    ):
        if hospital_id and hospital_id not in known:
            known.append(hospital_id)

    boards = [_board(state, hospital_id, today, names) for hospital_id in known]
    # The hospitals actually being read come first; a desk looking for live
    # movement should not have to scroll past the ones nothing watches.
    boards.sort(key=lambda b: not b["connector"]["live"])

    # Flattened across every hospital, so the panel can show one combined
    # stream as well as the per-hospital boards. This is the view no single
    # HMS can produce, and it is the whole argument for the product.
    #
    # Two passes rather than one compound key: timestamps are ISO strings and
    # cannot be negated, so sort newest-first, then lift the urgent rows with
    # a stable sort that leaves that order intact inside each group.
    combined = sorted(
        (row for board in boards for row in board["feed"]),
        key=lambda row: row.get("at", ""),
        reverse=True,
    )
    combined.sort(key=lambda row: not row.get("hits_booked_patients"))

    return {
        "today": today.isoformat(),
        "hospitals": boards,
        "feed": combined,
    }
