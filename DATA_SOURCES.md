# Data Sources

Every external dataset, map service, and planning assumption used by Detroit I-Grid.
Per the hackathon rules, we use only public data whose terms allow commercial use,
and **no** personal information, client data, or social-media data.

## Datasets

| Data | Source | License / Terms | Used for |
|---|---|---|---|
| Detroit road network | [OpenStreetMap](https://www.openstreetmap.org), via the [Geofabrik Michigan extract](https://download.geofabrik.de/north-america/us/michigan.html) (2026-11-12), cut to Downtown / Midtown / New Center / Corktown with [osmium](https://osmcode.org/osmium-tool/) and loaded with [osmnx](https://github.com/gboeing/osmnx) | [ODbL 1.0](https://opendatacommons.org/licenses/odbl/) — © OpenStreetMap contributors. Commercial use allowed with attribution and share-alike for derived databases. | Road graph (`data/detroit.graphml`): streets, lanes, speeds, road names and curve geometry for the traffic simulation and the map |

`data/detroit.graphml` is a derived database of OpenStreetMap data and is likewise
available under the ODbL.

## Map services

| Service | Source | Terms | Used for |
|---|---|---|---|
| Basemap tiles & labels | [CARTO Dark Matter](https://github.com/CartoDB/basemap-styles) (`basemaps.cartocdn.com`) | [CARTO basemap terms](https://carto.com/legal/basemap-terms): commercial use free up to 1M tile requests/month; attribution "© OpenStreetMap contributors © CARTO". CARTO's README states an API key is required; the public style URL currently works without one — add a key before any production deployment. | Dark background map with streets, buildings, water and labels |

## Planning assumptions (not datasets)

These are approximate, planning-level figures written into the code. They are
**estimates**, not data copied from a proprietary source, and the app labels them
as such.

| Assumption | Value | Where |
|---|---|---|
| Apartment trip generation | ≈ 5.4 trips / unit / day | `backend/sim.py` |
| Grocery / retail trip generation | ≈ 100 trips / 1,000 sq ft / day | `backend/sim.py` |
| Office trip generation | ≈ 10.84 trips / 1,000 sq ft / day | `backend/sim.py` |
| Peak-hour share of daily trips | 10 % | `backend/sim.py` |
| Heavy truck passenger-car equivalent | 2.5 | `backend/sim.py` |
| Lane capacity | 1,800 vehicles / hour / lane | `backend/network.py` |
| Congestion delay | BPR function, α = 0.15, β = 4 | `backend/sim.py` |
| Utility demand | water ≈ 100 gal/person/day, sewer ≈ 90 % of water, power ≈ 1.2 kW peak/unit | `backend/routers/utilities.py` |
| Background traffic demand | 150 synthetic origin–destination zones on arterial intersections, calibrated so primary roads average ≈ 46 % of capacity at baseline | `backend/sim.py` |
| Scenario presets (Renaissance Center, I-375, Hudson's site, Michigan Central, District Detroit) | Floor areas, occupancy, truck counts, lane closures and dates are **illustrative assumptions**, not official project figures | `frontend/app.js` |

Background demand is synthetic. No Census, employment or traffic-count data is
used at present; if any is added later, add a row to **Datasets** above.

## Software (open source, not data)

| Library | License | Used for |
|---|---|---|
| [MapLibre GL JS](https://maplibre.org/) 4.1.3 | BSD-3-Clause | Map rendering |
| [deck.gl](https://deck.gl/) 9.0.11 | MIT | Congestion, vehicle, trail and work-zone layers |
| [osmnx](https://github.com/gboeing/osmnx), [networkx](https://networkx.org/) | MIT, BSD-3-Clause | Road graph and routing |
| [FastAPI](https://fastapi.tiangolo.com/) | MIT | Backend API |

_Add a row whenever a new source is used._
