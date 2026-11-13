# Data Sources

Every external dataset used by Detroit I-Grid, with its license. Only public data whose terms allow commercial use; no personal information, client data, or social media data.

| Data | Source | License / Terms | Used for |
|---|---|---|---|
| Road network | [OpenStreetMap](https://www.openstreetmap.org) via [osmnx](https://github.com/gboeing/osmnx) | [ODbL 1.0](https://opendatacommons.org/licenses/odbl/) — © OpenStreetMap contributors | Detroit drive network for the traffic simulation |
| Michigan OSM extract | [Geofabrik](https://download.geofabrik.de/north-america/us/michigan.html) | ODbL 1.0 — © OpenStreetMap contributors | Offline source for the Detroit road network (Overpass fallback) |
| Population | [US Census ACS](https://www.census.gov/programs-surveys/acs) | Public domain (US Government work) | Background trip demand by tract |
| Jobs | [LEHD LODES](https://lehd.ces.census.gov/data/) | Public domain (US Government work) | Background trip demand by tract |

_Add a row whenever a new source is used._
