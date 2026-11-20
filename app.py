"""OpenF1 telemetry comparison dashboard.

Run it with:   streamlit run app.py

Pick a session, pick two drivers, pick a lap each, and the app overlays their
car traces against distance into the lap, with a delta showing exactly where
one gains on the other.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import openf1
import telemetry as tel

st.set_page_config(page_title="OpenF1 Telemetry", layout="wide")

# Lato everywhere: Streamlit's own chrome via CSS, Plotly via each figure's
# font setting, since Plotly renders to SVG and ignores the page stylesheet.
FONT_STACK = "Lato, sans-serif"
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Lato:wght@300;400;700;900&display=swap');
    html, body, [class*="st-"], [class*="css-"],
    button, input, textarea, select, h1, h2, h3, h4, h5, h6 {
        font-family: 'Lato', sans-serif !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

SEASONS = [2026, 2025, 2024, 2023]
FALLBACK_COLOURS = ["#E8002D", "#00A1E8", "#FFB800", "#00D7B6", "#B36BD4", "#FF6FA8"]


# --- Cached data loaders ---------------------------------------------------
# Streamlit reruns this whole script on every widget change, so anything that
# touches the network is cached. openf1.py also caches to disk, which survives
# restarts; this layer just avoids re-parsing JSON on every interaction.


@st.cache_data(show_spinner=False)
def load_meetings(year: int) -> pd.DataFrame:
    return pd.DataFrame(openf1.meetings(year))


@st.cache_data(show_spinner=False)
def load_sessions(meeting_key: int) -> pd.DataFrame:
    return pd.DataFrame(openf1.sessions(meeting_key))


@st.cache_data(show_spinner=False)
def load_drivers(session_key: int) -> pd.DataFrame:
    return pd.DataFrame(openf1.drivers(session_key))


@st.cache_data(show_spinner=False)
def load_laps(session_key: int) -> pd.DataFrame:
    return tel.laps_frame(session_key)


@st.cache_data(show_spinner=False)
def load_pit(session_key: int) -> pd.DataFrame:
    return pd.DataFrame(openf1.pit(session_key))


@st.cache_data(show_spinner=False)
def load_lap_telemetry(session_key: int, driver_number: int, lap_number: int) -> pd.DataFrame:
    laps = load_laps(session_key)
    match = laps[(laps["driver_number"] == driver_number) & (laps["lap_number"] == lap_number)]
    if match.empty:
        return pd.DataFrame()
    return tel.lap_telemetry(session_key, driver_number, match.iloc[0])


@st.cache_data(show_spinner=False)
def load_track_path(session_key: int, driver_number: int, lap_number: int) -> pd.DataFrame:
    laps = load_laps(session_key)
    match = laps[(laps["driver_number"] == driver_number) & (laps["lap_number"] == lap_number)]
    if match.empty:
        return pd.DataFrame()
    return tel.lap_track_path(session_key, driver_number, match.iloc[0])


def driver_colour(driver_row: pd.Series, index: int) -> str:
    raw = driver_row.get("team_colour")
    if isinstance(raw, str) and len(raw) == 6:
        return "#" + raw
    return FALLBACK_COLOURS[index % len(FALLBACK_COLOURS)]


@st.cache_data(show_spinner=False)
def load_corners(
    circuit_key: int, year: int, session_key: int, driver_number: int, lap_number: int
) -> pd.DataFrame:
    # Keyed on the lap too, since corners are snapped onto that lap's path.
    path = load_track_path(session_key, driver_number, lap_number)
    if path.empty:
        return pd.DataFrame()
    return tel.corner_markers(circuit_key, year, path)


def latest_started(frame: pd.DataFrame, date_column: str) -> int:
    """Index of the last row whose start date has already passed.

    The calendar runs ahead of the data, so defaulting to the final row would
    land on a race that has not happened yet and has no telemetry to show.
    """
    started = frame[pd.to_datetime(frame[date_column], utc=True) <= pd.Timestamp.now(tz="UTC")]
    return int(started.index[-1]) if not started.empty else len(frame) - 1


def driver_label(driver_row: pd.Series) -> str:
    number = int(driver_row["driver_number"])
    team = driver_row.get("team_name") or ""
    return f"{driver_row['name_acronym']} #{number} - {team}"


# --- Sidebar: choose a session and two laps --------------------------------

st.sidebar.header("Session")

year = st.sidebar.selectbox("Season", SEASONS, index=0)

meetings = load_meetings(year)
if meetings.empty:
    st.error(f"OpenF1 has no meetings for {year} yet.")
    st.stop()

meetings = meetings.sort_values("date_start").reset_index(drop=True)
meeting_labels = [
    f"{row['meeting_name']}  ({row['date_start'][:10]})" for _, row in meetings.iterrows()
]
meeting_label = st.sidebar.selectbox(
    "Race weekend", meeting_labels, index=latest_started(meetings, "date_start")
)
meeting = meetings.iloc[meeting_labels.index(meeting_label)]

sessions = load_sessions(int(meeting["meeting_key"]))
if sessions.empty:
    st.error("No sessions listed for this weekend.")
    st.stop()

sessions = sessions.sort_values("date_start").reset_index(drop=True)
session_names = sessions["session_name"].tolist()
session_name = st.sidebar.selectbox(
    "Session", session_names, index=latest_started(sessions, "date_start")
)
session = sessions[sessions["session_name"] == session_name].iloc[0]
session_key = int(session["session_key"])

with st.spinner("Loading lap data from OpenF1..."):
    laps = load_laps(session_key)
    drivers = load_drivers(session_key)

if laps.empty or drivers.empty:
    st.warning(
        "OpenF1 has no lap data for this session yet. "
        "Pick a session that has already been run."
    )
    st.stop()

drivers = drivers[drivers["driver_number"].isin(laps["driver_number"].unique())]
drivers = drivers.sort_values("driver_number").reset_index(drop=True)
labels = [driver_label(row) for _, row in drivers.iterrows()]

st.sidebar.header("Drivers")
label_a = st.sidebar.selectbox("Reference", labels, index=0)
label_b = st.sidebar.selectbox("Challenger", labels, index=min(1, len(labels) - 1))

driver_a = drivers.iloc[labels.index(label_a)]
driver_b = drivers.iloc[labels.index(label_b)]
number_a = int(driver_a["driver_number"])
number_b = int(driver_b["driver_number"])
colour_a = driver_colour(driver_a, 0)
colour_b = driver_colour(driver_b, 1)
code_a = driver_a["name_acronym"]
code_b = driver_b["name_acronym"]


def lap_picker(side: str, number: int, code: str) -> int:
    available = sorted(laps[laps["driver_number"] == number]["lap_number"].astype(int).tolist())
    if not available:
        st.sidebar.warning(f"{code} has no timed laps.")
        st.stop()
    quickest = tel.fastest_lap_number(laps, number) or available[0]
    return st.sidebar.selectbox(
        f"{code} lap", available, index=available.index(quickest), key=f"lap_{side}"
    )


st.sidebar.header("Laps")
st.sidebar.caption("Defaults to each driver's fastest lap.")
lap_a = lap_picker("a", number_a, code_a)
lap_b = lap_picker("b", number_b, code_b)

row_a = laps[(laps["driver_number"] == number_a) & (laps["lap_number"] == lap_a)].iloc[0]
row_b = laps[(laps["driver_number"] == number_b) & (laps["lap_number"] == lap_b)].iloc[0]

# --- Header ----------------------------------------------------------------

st.title(f"{meeting['meeting_name']} {year} - {session_name}")
st.caption(f"{meeting['circuit_short_name']}, {meeting['country_name']}  |  data: OpenF1")

col1, col2, col3 = st.columns(3)
col1.metric(f"{code_a} lap {lap_a}", tel.format_laptime(row_a["lap_duration"]))
col2.metric(f"{code_b} lap {lap_b}", tel.format_laptime(row_b["lap_duration"]))
gap = float(row_b["lap_duration"]) - float(row_a["lap_duration"])
col3.metric("Gap", f"{gap:+.3f} s", help=f"{code_b} relative to {code_a}")

tab_traces, tab_map, tab_laptimes, tab_sectors = st.tabs(
    ["Telemetry", "Track map", "Lap times", "Sectors"]
)

# --- Tab 1: telemetry traces ----------------------------------------------

with tab_traces:
    with st.spinner("Fetching car telemetry..."):
        tele_a = load_lap_telemetry(session_key, number_a, lap_a)
        tele_b = load_lap_telemetry(session_key, number_b, lap_b)

    if tele_a.empty or tele_b.empty:
        st.warning("OpenF1 has no car telemetry for one of these laps.")
    else:
        # Both traces are drawn on a shared, normalised distance axis so the
        # two laps line up at every corner and at the start/finish line.
        track_length = max(float(tele_a["distance_m"].max()), float(tele_b["distance_m"].max()))
        for frame in (tele_a, tele_b):
            frame["x_m"] = frame["lap_fraction"] * track_length
        delta = tel.delta_trace(tele_a, tele_b, track_length_m=track_length)

        channels = [
            ("Delta (s)", None),
            ("Speed (km/h)", "speed"),
            ("Throttle (%)", "throttle"),
            ("Brake", "brake"),
            ("Gear", "n_gear"),
            ("RPM", "rpm"),
        ]
        figure = make_subplots(
            rows=len(channels),
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.03,
            row_heights=[0.18, 0.22, 0.15, 0.12, 0.15, 0.18],
            subplot_titles=[name for name, _ in channels],
        )

        figure.add_trace(
            go.Scatter(
                x=delta["distance_m"],
                y=delta["delta_s"],
                name=f"{code_b} vs {code_a}",
                line=dict(color="#9aa0a6", width=2),
                fill="tozeroy",
                fillcolor="rgba(154,160,166,0.15)",
                showlegend=False,
            ),
            row=1,
            col=1,
        )
        figure.add_hline(y=0, line=dict(color="#666", width=1, dash="dot"), row=1, col=1)

        for row_index, (_, column) in enumerate(channels[1:], start=2):
            for frame, code, colour in ((tele_a, code_a, colour_a), (tele_b, code_b, colour_b)):
                figure.add_trace(
                    go.Scatter(
                        x=frame["x_m"],
                        y=frame[column],
                        name=code,
                        legendgroup=code,
                        showlegend=(row_index == 2),
                        line=dict(color=colour, width=1.6),
                    ),
                    row=row_index,
                    col=1,
                )

        figure.update_layout(
            height=1050,
            hovermode="x unified",
            margin=dict(l=60, r=20, t=50, b=50),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
            font=dict(family=FONT_STACK),
        )
        figure.update_annotations(font=dict(family=FONT_STACK))
        figure.update_xaxes(title_text="Distance into lap (m)", row=len(channels), col=1)
        st.plotly_chart(figure, use_container_width=True)

        if not delta.empty:
            worst = delta.loc[delta["delta_s"].idxmax()]
            st.caption(
                f"Biggest deficit for {code_b}: {worst['delta_s']:+.3f} s at "
                f"{worst['distance_m']:.0f} m into the lap. "
                "A rising delta line is where the reference car is gaining."
            )

        st.info(
            "Distance is integrated from speed, because OpenF1 publishes telemetry "
            "against wall-clock time rather than track position. The docstring in "
            "telemetry.py explains why that is the right x-axis here."
        )

# --- Tab 2: track map ------------------------------------------------------

with tab_map:
    with st.spinner("Fetching track position..."):
        path = load_track_path(session_key, number_a, lap_a)
        map_a = load_lap_telemetry(session_key, number_a, lap_a)
        map_b = load_lap_telemetry(session_key, number_b, lap_b)

    mapped = tel.speed_on_track(path, map_a, map_b)

    if mapped.empty:
        st.warning(
            "OpenF1 has no track position data for this lap. "
            "Coverage of the /location endpoint is thinner than for car telemetry."
        )
    else:
        mode = st.radio(
            "Colour the racing line by",
            [f"Speed delta ({code_b} minus {code_a})", f"{code_a} speed", f"{code_b} speed"],
            horizontal=True,
        )
        annotate = st.checkbox("Label turns and sector boundaries", value=True)

        corners = (
            load_corners(int(meeting["circuit_key"]), int(year), session_key, number_a, lap_a)
            if annotate
            else pd.DataFrame()
        )

        if mode.startswith("Speed delta"):
            values = mapped["speed_delta"]
            limit = max(abs(values.min()), abs(values.max())) or 1
            # Diverging scale running between the two teams' own colours, so a
            # stretch of track is tinted towards whichever driver was quicker.
            scale = [[0.0, colour_a], [0.5, "#2b2b2b"], [1.0, colour_b]]
            colour_range = (-limit, limit)
            bar_title = "km/h"
        else:
            column = "speed_ref" if mode.startswith(code_a) else "speed_cha"
            values = mapped[column]
            scale = "Turbo"
            colour_range = (float(values.min()), float(values.max()))
            bar_title = "km/h"

        figure = go.Figure(
            go.Scatter(
                x=mapped["x_m"],
                y=mapped["y_m"],
                mode="markers",
                marker=dict(
                    size=9,
                    color=values,
                    colorscale=scale,
                    cmin=colour_range[0],
                    cmax=colour_range[1],
                    colorbar=dict(title=bar_title, thickness=14),
                ),
                customdata=mapped[["distance_m", "speed_ref", "speed_cha", "speed_delta"]],
                hovertemplate=(
                    "%{customdata[0]:.0f} m into the lap<br>"
                    + f"{code_a}: " + "%{customdata[1]:.0f} km/h<br>"
                    + f"{code_b}: " + "%{customdata[2]:.0f} km/h<br>"
                    "delta: %{customdata[3]:+.0f} km/h<extra></extra>"
                ),
                showlegend=False,
            )
        )

        # Mark the start of the lap so the direction of travel is readable.
        figure.add_trace(
            go.Scatter(
                x=[mapped["x_m"].iloc[0]],
                y=[mapped["y_m"].iloc[0]],
                mode="markers+text",
                marker=dict(size=13, color="white", symbol="square"),
                text=["start"],
                textposition="top center",
                hoverinfo="skip",
                showlegend=False,
            )
        )

        if annotate:
            boundaries = tel.sector_boundaries(path, row_a)
            if not boundaries.empty:
                # One trace holding both ticks, separated by a None break.
                tick_x, tick_y = [], []
                for _, edge in boundaries.iterrows():
                    tick_x += [edge["x0"], edge["x1"], None]
                    tick_y += [edge["y0"], edge["y1"], None]
                figure.add_trace(
                    go.Scatter(
                        x=tick_x,
                        y=tick_y,
                        mode="lines",
                        line=dict(color="#ffffff", width=2),
                        hoverinfo="skip",
                        showlegend=False,
                    )
                )
                figure.add_trace(
                    go.Scatter(
                        x=boundaries["label_x"],
                        y=boundaries["label_y"],
                        mode="text",
                        text=boundaries["label"],
                        textfont=dict(family=FONT_STACK, size=12, color="#ffffff"),
                        textposition="middle center",
                        hoverinfo="skip",
                        showlegend=False,
                    )
                )

            if not corners.empty:
                figure.add_trace(
                    go.Scatter(
                        x=corners["label_x"],
                        y=corners["label_y"],
                        mode="text",
                        text=corners["number"].astype(int).astype(str),
                        textfont=dict(family=FONT_STACK, size=13, color="#b9bcc2"),
                        customdata=corners[["number"]],
                        hovertemplate="Turn %{customdata[0]}<extra></extra>",
                        showlegend=False,
                    )
                )

        figure.update_layout(
            height=680,
            margin=dict(l=10, r=10, t=10, b=10),
            xaxis=dict(visible=False),
            yaxis=dict(visible=False, scaleanchor="x", scaleratio=1),
            font=dict(family=FONT_STACK),
        )
        st.plotly_chart(figure, use_container_width=True)

        if mode.startswith("Speed delta"):
            best = mapped.loc[mapped["speed_delta"].idxmax()]
            worst = mapped.loc[mapped["speed_delta"].idxmin()]
            left, right = st.columns(2)
            left.metric(
                f"{code_b} best point",
                f"{best['speed_delta']:+.0f} km/h",
                help=f"{best['distance_m']:.0f} m into the lap",
            )
            right.metric(
                f"{code_a} best point",
                f"{worst['speed_delta']:+.0f} km/h",
                help=f"{worst['distance_m']:.0f} m into the lap",
            )
            st.caption(
                f"Each point is tinted toward the team colour of whichever driver "
                f"carried more speed there, so {code_a}'s colour marks {code_a}'s "
                f"strong sections and {code_b}'s marks {code_b}'s. Hover for both "
                f"speeds. Lap measured geometrically at {mapped['distance_m'].max():.0f} m."
            )
        else:
            st.caption(
                f"Lap measured geometrically at {mapped['distance_m'].max():.0f} m, "
                "from the x/y coordinates rather than integrated from speed."
            )


# --- Tab 3: lap times over the session ------------------------------------

with tab_laptimes:
    figure = go.Figure()
    for number, code, colour in ((number_a, code_a, colour_a), (number_b, code_b, colour_b)):
        mine = laps[laps["driver_number"] == number].sort_values("lap_number")
        figure.add_trace(
            go.Scatter(
                x=mine["lap_number"],
                y=mine["lap_duration"],
                mode="lines+markers",
                name=code,
                line=dict(color=colour, width=1.5),
                marker=dict(size=6),
                customdata=mine[["compound", "tyre_age"]],
                hovertemplate=(
                    "Lap %{x}<br>%{y:.3f} s<br>%{customdata[0]}, "
                    "%{customdata[1]:.0f} laps old<extra></extra>"
                ),
            )
        )

    pit_stops = load_pit(session_key)
    if not pit_stops.empty:
        for number, colour in ((number_a, colour_a), (number_b, colour_b)):
            for _, stop in pit_stops[pit_stops["driver_number"] == number].iterrows():
                figure.add_vline(
                    x=int(stop["lap_number"]),
                    line=dict(color=colour, width=1, dash="dot"),
                    opacity=0.5,
                )

    # In practice sessions a "lap" can include sitting in the garage, so the
    # raw range runs to several hundred seconds and flattens everything that
    # matters. Clip to a window around the session best instead.
    both = laps[laps["driver_number"].isin([number_a, number_b])]["lap_duration"]
    fastest = float(both.min())
    show_all = st.checkbox("Show in-laps, out-laps and garage time", value=False)
    y_range = None if show_all else [fastest - 1, fastest * 1.15]

    figure.update_layout(
        height=520,
        hovermode="x unified",
        xaxis_title="Lap",
        yaxis_title="Lap time (s)",
        yaxis_range=y_range,
        margin=dict(l=60, r=20, t=30, b=40),
        font=dict(family=FONT_STACK),
    )
    st.plotly_chart(figure, use_container_width=True)
    st.caption(
        "Dotted vertical lines are pit stops. By default the y-axis is clipped "
        f"to 115% of the session best ({tel.format_laptime(fastest)}), so "
        "in-laps, out-laps and garage time do not squash the scale."
    )

# --- Tab 4: sector comparison ---------------------------------------------

with tab_sectors:
    comparison = pd.DataFrame(
        [tel.sector_table(row_a), tel.sector_table(row_b)], index=[code_a, code_b]
    )
    numeric = ["Lap time", "Sector 1", "Sector 2", "Sector 3"]
    for column in numeric:
        comparison[column] = pd.to_numeric(comparison[column], errors="coerce").round(3)

    difference = comparison.loc[code_b, numeric] - comparison.loc[code_a, numeric]
    comparison.loc["Delta"] = difference.reindex(comparison.columns)

    st.dataframe(comparison, use_container_width=True)
    st.caption(
        f"Delta row is {code_b} minus {code_a}: negative means {code_b} was "
        "quicker in that sector."
    )

    st.subheader("Session best sectors")
    best = {}
    for column in ("duration_sector_1", "duration_sector_2", "duration_sector_3"):
        if column in laps and laps[column].notna().any():
            winner = laps.loc[laps[column].idxmin()]
            match = drivers.loc[drivers["driver_number"] == winner["driver_number"], "name_acronym"]
            who = match.iloc[0] if not match.empty else winner["driver_number"]
            label = column.replace("duration_", "").replace("_", " ").title()
            best[label] = f"{winner[column]:.3f} s  ({who}, lap {int(winner['lap_number'])})"
    if best:
        st.table(pd.Series(best, name="Fastest"))
