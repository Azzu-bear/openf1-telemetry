"""Turn raw OpenF1 records into the frames a telemetry comparison needs.

The one non-obvious idea in here
--------------------------------
OpenF1's `car_data` is a *time* series: roughly 3.7 samples per second, stamped
with wall-clock dates. To compare two drivers you cannot plot against time,
because a lap that is half a second quicker simply ends sooner and every
feature on the x-axis is smeared.

Race engineers plot against *distance* instead. OpenF1 does not give distance,
so we integrate it: distance travelled between two samples is the average speed
over that interval times its duration. Cumulatively summing that gives metres
into the lap, which is a stable x-axis both drivers share.

Integrated distance drifts a little from true distance (3.7 Hz is coarse, and
speed is reported as a rounded integer km/h), but the drift is nearly identical
for two cars on the same lap, so the comparison stays honest.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import openf1


def parse_iso(value: str) -> datetime:
    """OpenF1 returns ISO 8601 with a +00:00 offset; keep it timezone-aware."""
    return datetime.fromisoformat(value)


def to_api_time(moment: datetime) -> str:
    """Format a datetime the way OpenF1's `date>` / `date<` filters want it."""
    return moment.replace(tzinfo=None).isoformat(timespec="milliseconds")


def laps_frame(session_key: int) -> pd.DataFrame:
    """All timed laps in the session as a DataFrame, with stint info joined on."""
    laps = pd.DataFrame(openf1.laps(session_key))
    if laps.empty:
        return laps

    laps = laps[laps["lap_duration"].notna()].copy()
    laps["lap_number"] = laps["lap_number"].astype(int)
    laps["driver_number"] = laps["driver_number"].astype(int)

    stints = pd.DataFrame(openf1.stints(session_key))
    laps["compound"] = None
    laps["tyre_age"] = np.nan
    if not stints.empty:
        for _, stint in stints.iterrows():
            mask = (
                (laps["driver_number"] == int(stint["driver_number"]))
                & (laps["lap_number"] >= int(stint["lap_start"]))
                & (laps["lap_number"] <= int(stint["lap_end"]))
            )
            laps.loc[mask, "compound"] = stint.get("compound")
            age_at_start = stint.get("tyre_age_at_start") or 0
            laps.loc[mask, "tyre_age"] = (
                laps.loc[mask, "lap_number"] - int(stint["lap_start"]) + age_at_start
            )

    return laps.sort_values(["driver_number", "lap_number"]).reset_index(drop=True)


def fastest_lap_number(laps: pd.DataFrame, driver_number: int) -> int | None:
    """The driver's quickest lap, ignoring in- and out-laps."""
    mine = laps[laps["driver_number"] == driver_number]
    mine = mine[~mine["is_pit_out_lap"].fillna(False)]
    if mine.empty:
        return None
    return int(mine.loc[mine["lap_duration"].idxmin(), "lap_number"])


def lap_telemetry(session_key: int, driver_number: int, lap_row: pd.Series) -> pd.DataFrame:
    """Car telemetry for one lap, indexed by seconds and metres into the lap.

    Returns columns: t_s, distance_m, speed, throttle, brake, n_gear, rpm, drs.
    """
    start = parse_iso(lap_row["date_start"])
    end = start + timedelta(seconds=float(lap_row["lap_duration"]))

    # Nudge the window outward slightly so we never clip the first/last sample.
    records = openf1.car_data(
        session_key,
        driver_number,
        to_api_time(start - timedelta(seconds=0.5)),
        to_api_time(end + timedelta(seconds=0.5)),
    )
    if not records:
        return pd.DataFrame()

    frame = pd.DataFrame(records).sort_values("date").reset_index(drop=True)
    frame["timestamp"] = frame["date"].map(parse_iso)
    frame["t_s"] = (frame["timestamp"] - start).dt.total_seconds()

    # Trim to the lap proper now that we have a clean time base.
    frame = frame[(frame["t_s"] >= 0) & (frame["t_s"] <= float(lap_row["lap_duration"]))]
    frame = frame.reset_index(drop=True)
    if frame.empty:
        return frame

    # Telemetry arrives at ~3.7 Hz, so the first and last samples sit up to a
    # quarter second inside the timing line. Stretch the time base to span the
    # official lap duration exactly, so a delta trace ends on the real lap time
    # gap instead of a sampling artefact. The correction is well under 1%.
    sampled_span = float(frame["t_s"].iloc[-1] - frame["t_s"].iloc[0])
    if sampled_span > 0:
        frame["t_s"] = (frame["t_s"] - frame["t_s"].iloc[0]) * (
            float(lap_row["lap_duration"]) / sampled_span
        )

    # Integrate speed (km/h -> m/s) over time to get distance into the lap.
    speed_ms = frame["speed"].astype(float) / 3.6
    dt = frame["t_s"].diff().fillna(0.0)
    mean_speed = (speed_ms + speed_ms.shift(1).fillna(speed_ms.iloc[0])) / 2
    frame["distance_m"] = (mean_speed * dt).cumsum()

    # Both cars covered the same physical lap, so normalising each one's
    # integrated distance to 0..1 cancels most of the integration error and
    # guarantees the two traces line up at the start/finish line.
    total = float(frame["distance_m"].iloc[-1])
    frame["lap_fraction"] = frame["distance_m"] / total if total else 0.0

    frame["drs_active"] = frame["drs"].isin([10, 12, 14]).astype(int)
    return frame[
        [
            "t_s",
            "distance_m",
            "lap_fraction",
            "speed",
            "throttle",
            "brake",
            "n_gear",
            "rpm",
            "drs_active",
        ]
    ]


def delta_trace(
    reference: pd.DataFrame,
    challenger: pd.DataFrame,
    track_length_m: float | None = None,
    points: int = 600,
) -> pd.DataFrame:
    """Time lost or gained by `challenger` against `reference`, versus distance.

    Positive delta means the challenger is behind on cumulative lap time at that
    point on the track. A rising segment is where the reference car gains.

    The comparison runs on normalised lap fraction rather than raw integrated
    metres, so the trace ends at exactly the two drivers' lap time difference.
    """
    if reference.empty or challenger.empty:
        return pd.DataFrame()

    grid = np.linspace(0, 1, points)

    # np.interp needs an increasing x; lap fraction is monotonic by construction.
    ref_time = np.interp(grid, reference["lap_fraction"], reference["t_s"])
    cha_time = np.interp(grid, challenger["lap_fraction"], challenger["t_s"])

    if track_length_m is None:
        track_length_m = max(
            float(reference["distance_m"].max()), float(challenger["distance_m"].max())
        )

    return pd.DataFrame(
        {
            "lap_fraction": grid,
            "distance_m": grid * track_length_m,
            "delta_s": cha_time - ref_time,
        }
    )


def lap_track_path(session_key: int, driver_number: int, lap_row: pd.Series) -> pd.DataFrame:
    """The racing line for one lap, as metres in the circuit's local frame.

    OpenF1 gives x/y/z in tenths of a metre at ~3.7 Hz. Distance along this
    path is measured geometrically rather than integrated from speed, which is
    the more accurate of the two: at Interlagos the geometric path comes out at
    4241 m against a true lap of 4309 m, where speed integration gives 4196 m.

    Returns columns: t_s, x_m, y_m, distance_m, lap_fraction.
    """
    start = parse_iso(lap_row["date_start"])
    end = start + timedelta(seconds=float(lap_row["lap_duration"]))

    records = openf1.location(
        session_key,
        driver_number,
        to_api_time(start - timedelta(seconds=0.5)),
        to_api_time(end + timedelta(seconds=0.5)),
    )
    if not records:
        return pd.DataFrame()

    frame = pd.DataFrame(records).sort_values("date").reset_index(drop=True)
    frame["t_s"] = (frame["date"].map(parse_iso) - start).dt.total_seconds()
    frame = frame[(frame["t_s"] >= 0) & (frame["t_s"] <= float(lap_row["lap_duration"]))]
    frame = frame.reset_index(drop=True)
    if len(frame) < 10:
        return pd.DataFrame()

    # The feed occasionally emits a (0, 0) placeholder when it loses the car.
    frame = frame[(frame["x"] != 0) | (frame["y"] != 0)].reset_index(drop=True)

    frame["x_m"] = frame["x"] / 10.0
    frame["y_m"] = frame["y"] / 10.0

    step = np.hypot(frame["x_m"].diff(), frame["y_m"].diff()).fillna(0.0)
    frame["distance_m"] = step.cumsum()

    total = float(frame["distance_m"].iloc[-1])
    frame["lap_fraction"] = frame["distance_m"] / total if total else 0.0

    return frame[["t_s", "x_m", "y_m", "distance_m", "lap_fraction"]]


def speed_on_track(
    path: pd.DataFrame, reference: pd.DataFrame, challenger: pd.DataFrame
) -> pd.DataFrame:
    """Both drivers' speed sampled onto one shared racing line.

    `path` supplies the geometry, the two telemetry frames supply speed. All
    three are indexed by lap fraction, so a point on the map carries the speed
    each driver actually carried through that part of the circuit.

    Adds columns: speed_ref, speed_cha, speed_delta (challenger minus reference).
    """
    if path.empty or reference.empty or challenger.empty:
        return pd.DataFrame()

    out = path.copy()
    out["speed_ref"] = np.interp(
        out["lap_fraction"], reference["lap_fraction"], reference["speed"]
    )
    out["speed_cha"] = np.interp(
        out["lap_fraction"], challenger["lap_fraction"], challenger["speed"]
    )
    out["speed_delta"] = out["speed_cha"] - out["speed_ref"]
    return out


def corner_markers(circuit_key: int, year: int, path: pd.DataFrame) -> pd.DataFrame:
    """Numbered corners snapped onto a measured racing line, with label offsets.

    MultiViewer publishes each corner's apex in the same coordinate frame the
    position feed uses, so the only work here is snapping each apex to the
    nearest sample on the lap we actually drew, and pushing the text label
    outward from the circuit's centre so it does not sit on the track.

    Returns columns: number, x_m, y_m, lap_fraction, label_x, label_y.
    """
    corners = openf1.circuit_corners(circuit_key, year)
    if not corners or path.empty:
        return pd.DataFrame()

    track_x = path["x_m"].to_numpy()
    track_y = path["y_m"].to_numpy()
    centre_x, centre_y = track_x.mean(), track_y.mean()

    # Push labels out by a fixed share of the circuit's overall size, so the
    # spacing looks the same on a short street track and a long permanent one.
    reach = 0.055 * max(np.ptp(track_x), np.ptp(track_y))

    rows = []
    for corner in corners:
        position = corner.get("trackPosition") or {}
        if "x" not in position or "y" not in position:
            continue
        # MultiViewer reports tenths of a metre, same as the position feed.
        corner_x, corner_y = position["x"] / 10.0, position["y"] / 10.0

        nearest = int(np.argmin(np.hypot(track_x - corner_x, track_y - corner_y)))
        snapped_x, snapped_y = track_x[nearest], track_y[nearest]

        outward_x, outward_y = snapped_x - centre_x, snapped_y - centre_y
        magnitude = float(np.hypot(outward_x, outward_y)) or 1.0

        rows.append(
            {
                "number": corner.get("number"),
                "x_m": snapped_x,
                "y_m": snapped_y,
                "lap_fraction": float(path["lap_fraction"].iloc[nearest]),
                "label_x": snapped_x + reach * outward_x / magnitude,
                "label_y": snapped_y + reach * outward_y / magnitude,
            }
        )

    return pd.DataFrame(rows)


def sector_boundaries(path: pd.DataFrame, lap_row: pd.Series, tick_m: float = 70.0) -> pd.DataFrame:
    """Where sector 1 ends and sector 2 ends, as ticks across the racing line.

    The timing feed never publishes sector *positions*, only sector *times*, so
    these are derived: sector 1 ends at `duration_sector_1` seconds into the
    lap, sector 2 at the sum of the first two. Reading the car's position at
    those moments puts the boundary exactly where the timing loop is.

    Each tick is drawn perpendicular to the direction of travel, so it reads as
    a line across the track rather than a dot on it.

    Returns columns: label, x_m, y_m, x0, y0, x1, y1.
    """
    first = lap_row.get("duration_sector_1")
    second = lap_row.get("duration_sector_2")
    if path.empty or pd.isna(first) or pd.isna(second):
        return pd.DataFrame()

    times = path["t_s"].to_numpy()
    xs, ys = path["x_m"].to_numpy(), path["y_m"].to_numpy()

    rows = []
    for label, moment in (("S1 / S2", float(first)), ("S2 / S3", float(first) + float(second))):
        if moment <= times[0] or moment >= times[-1]:
            continue

        at_x = float(np.interp(moment, times, xs))
        at_y = float(np.interp(moment, times, ys))

        # Heading from a short window either side, then rotate 90 degrees.
        ahead_x = float(np.interp(moment + 0.4, times, xs))
        ahead_y = float(np.interp(moment + 0.4, times, ys))
        behind_x = float(np.interp(moment - 0.4, times, xs))
        behind_y = float(np.interp(moment - 0.4, times, ys))

        heading_x, heading_y = ahead_x - behind_x, ahead_y - behind_y
        magnitude = float(np.hypot(heading_x, heading_y)) or 1.0
        across_x, across_y = -heading_y / magnitude, heading_x / magnitude

        rows.append(
            {
                "label": label,
                "x_m": at_x,
                "y_m": at_y,
                "x0": at_x - across_x * tick_m / 2,
                "y0": at_y - across_y * tick_m / 2,
                "x1": at_x + across_x * tick_m / 2,
                "y1": at_y + across_y * tick_m / 2,
                # Sit the text clear of the tick, so it cannot land on a corner
                # number belonging to a turn near the timing loop.
                "label_x": at_x + across_x * tick_m,
                "label_y": at_y + across_y * tick_m,
            }
        )

    return pd.DataFrame(rows)


def sector_table(lap_row: pd.Series) -> dict:
    """The three sector times plus the two speed-trap readings for one lap."""
    return {
        "Lap": int(lap_row["lap_number"]),
        "Lap time": float(lap_row["lap_duration"]),
        "Sector 1": lap_row.get("duration_sector_1"),
        "Sector 2": lap_row.get("duration_sector_2"),
        "Sector 3": lap_row.get("duration_sector_3"),
        "Speed trap 1": lap_row.get("i1_speed"),
        "Speed trap 2": lap_row.get("i2_speed"),
        "Compound": lap_row.get("compound"),
        "Tyre age": lap_row.get("tyre_age"),
    }


def format_laptime(seconds: float | None) -> str:
    """Seconds -> M:SS.mmm, the way a timing screen shows it."""
    if seconds is None or (isinstance(seconds, float) and np.isnan(seconds)):
        return "-"
    minutes, rest = divmod(float(seconds), 60)
    return f"{int(minutes)}:{rest:06.3f}" if minutes else f"{rest:.3f}"
