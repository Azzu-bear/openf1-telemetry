# OpenF1 Telemetry Comparison

A Streamlit dashboard for comparing two Formula 1 drivers' car telemetry on a
chosen lap, built directly on the [OpenF1](https://openf1.org) REST API.

Pick a session, pick two drivers, pick a lap each. The app overlays speed,
throttle, brake, gear and RPM against distance into the lap, draws a delta
trace showing exactly where one car gains on the other, and paints the circuit
itself with whoever was carrying more speed through each corner.

Four tabs:

- **Telemetry** - speed, throttle, brake, gear and RPM overlaid against distance, with a delta trace on top.
- **Track map** - the racing line drawn from x/y position data, with every turn numbered and the two sector boundaries marked. Colour it by speed delta, by either driver's absolute speed, or by mini-sector dominance.
- **Lap times** - both drivers' lap times across the session, pit stops marked, tyre compound and age on hover.
- **Sectors** - sector-by-sector comparison plus the session's best sectors.

## Why OpenF1 and not FastF1

FastF1 reads F1's own live timing archive at `livetiming.formula1.com/static`.
Since roughly 15 October 2026 that path returns `401 Unauthorized` from
CloudFront for unauthenticated clients, so FastF1 can only serve sessions that
are already in its local HTTP cache. OpenF1 ingests the live timing feed itself
and republishes it, free and without an account, covering 2023 onward. It is
unaffected by the lockdown.

## Running it

```
.venv\Scripts\activate
streamlit run app.py
```

The app opens at http://localhost:8501.

First run of a session downloads a few megabytes of lap and telemetry data.
Everything is cached to `.cache/` on disk, so the second run is instant.

## What each file does

| File | Role |
| --- | --- |
| `openf1.py` | Thin API client. Builds OpenF1's operator-bearing query strings, rate limits politely, caches every response to disk. |
| `telemetry.py` | The analysis. Joins stints onto laps, slices car telemetry to a single lap, integrates distance, computes the delta trace. |
| `app.py` | The Streamlit UI: sidebar selectors, three tabs, Plotly figures. |

## The one idea worth understanding

OpenF1's `car_data` is a **time** series, roughly 3.7 samples per second,
stamped with wall-clock dates. You cannot compare two drivers on a time axis,
because the quicker lap simply ends sooner and every corner on the x-axis is
smeared relative to the other car.

Race engineers plot against **distance**. OpenF1 does not publish distance, so
`telemetry.py` integrates it: between two consecutive samples the car covers
(average speed) x (elapsed time). A cumulative sum of that gives metres into
the lap, which both drivers share as a common x-axis.

The integration drifts slightly from true distance, since 3.7 Hz is coarse and
speed is reported as a rounded integer in km/h. The drift is close to identical
for two cars on the same circuit though, so the comparison stays fair.

The delta trace then interpolates each driver's elapsed time onto a common
distance grid and subtracts. Where the line rises, the reference car is gaining.

Two corrections were needed to make this trustworthy:

- Telemetry samples land up to a quarter second inside the timing line at each
  end of the lap, so a raw delta closed at +0.146 s where the true gap was
  +0.035 s. Stretching each driver's time base to span the official lap
  duration fixes it, and the correction is well under 1%.
- Normalising each driver's integrated distance to 0..1 of their own lap
  cancels most of the integration error, and guarantees the two traces line up
  at the start/finish line.

The **track map** sidesteps the problem entirely. `/location` gives x/y/z in
tenths of a metre, so distance along the racing line can be measured
geometrically. That is the more accurate of the two methods:

| Circuit | True length | Geometric from x/y | Integrated from speed |
| --- | --- | --- | --- |
| Interlagos | 4309 m | 4241 m | 4196 m |
| Las Vegas | 6201 m | 6166 m | ~6000 m |

## OpenF1 endpoints used

| Endpoint | What it gives |
| --- | --- |
| `/meetings` | Race weekends in a season |
| `/sessions` | FP1/FP2/FP3, Qualifying, Sprint, Race within a weekend |
| `/drivers` | Entry list with team names and team colours |
| `/laps` | Lap times, sector times, speed traps, mini-sector segment codes |
| `/stints` | Tyre compound and age per stint |
| `/pit` | Pit stops with stationary time |
| `/car_data` | Speed, throttle, brake, gear, RPM, DRS at ~3.7 Hz |
| `/location` | x/y/z track position at ~3.7 Hz, in tenths of a metre |

There are more worth exploring: `/intervals` (gap to leader, live), `/position`,
`/race_control` (flags, investigations, penalties), `/team_radio` (actual audio
clips) and `/weather`.

Note that live data is not free. OpenF1's community tier covers historical
sessions only, where historical means "ended more than 30 minutes ago", at 3
requests per second. Live data during a session needs their sponsor tier
(EUR 9.90/month), which also unlocks MQTT and WebSocket transports.

### One endpoint that is not OpenF1

Turn numbers come from the MultiViewer circuits API, which every `/meetings`
record points at through its `circuit_info_url` field. It is the same source
FastF1 uses for `get_circuit_info()`. Its coordinates share OpenF1's frame
exactly, so no transform is needed: at Las Vegas the 17 published corners land
a median of 1.3 m from a real car's measured racing line.

Sector boundaries are not published by anyone. The timing feed gives sector
*times*, never sector *positions*, so `sector_boundaries()` derives them:
sector 1 ends at `duration_sector_1` seconds into the lap, so reading the car's
position at that moment puts the boundary on the timing loop. Each one is then
drawn perpendicular to the direction of travel so it reads as a line across the
track.

### Mini-sector dominance

`/laps` returns `segments_sector_1/2/3`, arrays of status codes with one entry
per mini-sector: 2048 yellow, 2049 green (personal best), 2051 purple (session
best), 2064 pit lane. Those codes are judged against session bests, not head to
head, so they cannot answer "who was quicker here". Their *count* can though,
and it gives the circuit's real timing granularity: Las Vegas splits 6 + 7 + 10
into 23 mini-sectors.

So `mini_sector_dominance()` takes that count, cuts the lap into that many
equal slices of distance, and measures each driver's elapsed time through
every slice. The per-slice margins sum to the full lap time difference, which
is the check that nothing has gone missing.

The honest caveat, which the app states on screen: the feed publishes how many
mini-sectors exist but never where they begin and end. The count is real, the
exact boundaries are an approximation.

## Things to build next

- **Stint degradation model.** Fit lap time against tyre age per compound and
  plot the degradation slope. This is the backbone of real strategy work.
- **Undercut calculator.** Combine `/pit` stationary time with the lap time delta
  on fresh versus old tyres to estimate whether an undercut would have worked.
- **More than two drivers.** The mini-sector dominance view is head to head, but
  the same comparison across a whole field gives the classic grid-wide dominance
  map.
- **Live mode.** Needs OpenF1's sponsor tier, which adds MQTT and WebSocket
  transports. Subscribe instead of polling and the same dashboard becomes a live
  timing screen.

## Data source

All data from [OpenF1](https://openf1.org), an open, free, unofficial API for
Formula 1 timing and telemetry. Not associated with Formula 1 or the FIA.
