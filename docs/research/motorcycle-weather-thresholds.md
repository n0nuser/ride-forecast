# Motorcycle weather thresholds: research and sources

What `ride.py` treats as a hazard for a motorcyclist, where each number comes from, and which numbers are judgment
calls. Researched on 2026-11-12 from official, rider-training, manufacturer and government sources. Forum and
cycling-blog material was left out on purpose: bicycle speed, weight and tyres are different.

Read this before changing a constant in `ride.py`. The constants carry the same names as the rows below.

## The scale

Every hour, for every source, gets one of four levels. The worst real hazard decides; two CAUTION factors together
make RISKY. The 0-100 score is derived from the level, so a score always sits inside its level's band.

| Level | Score band | Meaning |
|---|---|---|
| 🟢 GO | 70-100 | Ride. Tips (wet roads, gusts, cold tyres) when something is worth knowing. |
| 🟡 CAUTION | 45-69 | Ride with care, in the best window if you can. |
| 🟠 RISKY | 20-44 | Only if you must. |
| 🔴 DON'T RIDE | 0 (a stop) | Real danger. An hour counts as a stop when two or more sources report it. |

## Thresholds

"Source" means the number is anchored in the linked material. "Judgment" means no authority publishes a motorcycle
figure, so the value is a reasoned choice. Change those first if the verdicts feel off.

| Factor (constant) | GO + tip | CAUTION | RISKY | DON'T RIDE | Basis |
|---|---|---|---|---|---|
| Cold, dry (`FROST_T`) | below 8°C: layers, gloves, gentle first km | 3°C or less | | 0°C or less | **Source**: DGT says take extreme care below 3°C. Michelin: cold rubber loses grip near freezing, and tyres need minutes to warm up. |
| Ice, damp or wet (`ICE_DAMP_RH`, `ICE_WET_STOP_T`) | | | 3°C or less and damp or wet | 1°C or less and damp or wet | **Source** for the direction: DGT (shade, humidity), council winter plans (the road surface, not the air, decides; salting starts near 0.5°C road temperature). **Judgment**: the 85% humidity line for "damp". |
| Rain (`RAIN_LEVELS`) | 0.1 to under 1 mm/h | 1 mm/h | 4 mm/h | 15 mm/h | **Source**: Met Office calls 4 mm/h heavy; AEMET's 15 mm/h is a yellow warning in the zones checked. The wet-road effects are sourced (stopping distance at least double, 3 s following gap, first minutes worst). **Judgment**: the 1 mm/h line. |
| Gusts (`GUST_LEVELS`) | 40 km/h | 50 km/h | 60 km/h | 70 km/h | **Source**: AEMET yellow gust warning at 70 km/h in the zones checked; MSF, DGT, Met Office and nidirect all single out gusty crosswind. **Judgment**: 50 and 60. No authority gives a motorcycle number below the warning level. Light bikes and exposed roads deserve lower limits. |
| Visibility (`VIS_LEVELS`) | under 2000 m: mist tip | under 1000 m | under 200 m | under 100 m | **Source**: 100 m is the UK Highway Code's "seriously reduced" visibility for lights; MSF and Luton council advise slowing down and, for riders, avoiding fog. 1000 m is the usual meteorological definition of fog (not looked up in this round). **Judgment**: 200 m. |
| Heat (`HEAT_LEVELS`) | up to 32°C | above 32°C | 36°C and above | 40°C and above | **Judgment**, set by the rider: up to 32°C is tolerable in Spain. Supporting material only says gear gets hot and dehydration matters (CDC, PMC study). |
| Thunderstorm | | | | always | **Source**: NWS and CDC say motorcycles are not shelter from lightning; wait 30 minutes after the last thunder. |
| Snow, freezing rain | | | | always | **Source**: Michelin (no standard motorcycle tyre has meaningful grip on ice), DGT. |
| Fog code without a visibility figure | | CAUTION | | | MET Norway and some models give only a weather symbol. |
| Two CAUTION factors | | | RISKY | | **Judgment**. Rain plus wind plus cold compound. |

The score inside a level is ordered by smaller costs (colder, damper, gustier, rain chance, poorer visibility). Those
weights are in `penalties()` and are judgment calls too.

### Visibility data

Open-Meteo's `visibility` is requested for every model; models that do not provide it return nothing and are
skipped for that hour. MET Norway's compact API has no visibility, so it only contributes a fog symbol. The
consensus for an hour takes the lowest visibility any source reports. The API request with the new variable could not
be checked live on 2026-11-12 (daily request limit reached); the parser is tested against synthetic responses and the
existing real captures.

### Not modelled

Darkness and low sun, road surface temperature, wind direction (crosswind exposure), rider and bike weight, tyre type,
traffic and road works. A forecast "3°C" is air temperature, not the road.

## Sources

Official and rider-training bodies first, then manufacturers and other government pages.

1. DGT, winter driving: ice below 3°C, rain, wind, cold on motorcycles. <https://revista.dgt.es/es/educacion-formacion/conducir-mejor/2024/1217-Conduccion-invernal.shtml>
2. DGT, motorcycle safe-driving course instructor guide. <https://www.dgt.es/export/sites/web-DGT/.galleries/downloads/nuestros_servicios/para-colaboradores-y-empresas/GUIA-MONITORES-MOTOCICLETA-NIPO.pdf>
3. Motorcycle Safety Foundation, Motorcycle Operator Manual: 3 s following gap on slippery pavement, first minutes of rain, oil in the lane centre, fog. <https://msf-usa.org/wp-content/uploads/2023/02/motorcycle-operator-manual.pdf>
4. Motorcycle Safety Foundation, riding tips 2023: gusty wind is the worst case, lane position, front brake share. <https://msf-usa.org/wp-content/uploads/2024/07/MSF-You-and-Your-Motorcycle-Riding-Tips-2023.pdf>
5. UK Highway Code, adverse weather (226 to 237): wet stopping distance at least double, fog visibility. <https://www.gov.uk/guidance/the-highway-code/driving-in-adverse-weather-conditions-226-to-237>
6. Met Office, travelling in storms, rain and wind: motorcyclists are blown around more by side winds. <https://weather.metoffice.gov.uk/warnings-and-advice/seasonal-advice/travel/travelling-in-storms-rain-and-wind>
7. Met Office Hazard Manager guide: heavy rain is 4 mm/h or more. <https://www.metoffice.gov.uk/binaries/content/assets/metofficegovuk/pdf/services/government/using-the-map-and-accessing-the-weather-layers-beta.pdf>
8. AEMET, Meteoalerta warning thresholds (annex 1): per-zone rain per hour and gust levels. <https://www.aemet.es/documentos/es/eltiempo/prediccion/avisos/plan_meteoalerta/METEOALERTA_ANX1_Umbrales_y_niveles_de_aviso.pdf>
9. Michelin, motorcycle snow tyres: cold rubber, no grip on ice. <https://www.michelinman.com/motorcycle/tips-and-advice/how-to-choose-the-right-motorcycle-tire/motorcycle-snow-tires>
10. Michelin, checking tyre pressure: cold lowers pressure; grip is lowest in the first minutes. <https://www.michelinman.com/motorcycle/tips-and-advice/tips-to-take-care-of-motorcycle-tires/check-your-tire-pressure>
11. US National Weather Service, lightning safety: motorcycles give no protection. <https://www.weather.gov/safety/lightning-safety-overview>
12. CDC, lightning FAQ: open vehicles such as motorcycles are not safe. <https://www.cdc.gov/lightning/faq/index.html>
13. nidirect, strong winds advice: gusts push motorcyclists off course; stay back from high-sided vehicles. <https://www.nidirect.gov.uk/news/strong-winds-weather-warning-information-and-advice>
14. RoSPA, safe riding tips for motorcyclists. <https://www.rospa.com/road-safety/road-safety-information/bike-safety/safe-ride>
15. Luton Borough Council, motorcycling in fog, rain and wind. <https://m.luton.gov.uk/Page/Show/Transport_and_streets/Road_safety/Motorcycling/Riding%20in%20different%20weather%20conditions/Pages/Visibility,%20rain%20and%20wind.aspx?redirectToMobile=True>
16. Devon County Council winter service plan: road surface temperature versus air temperature, frost definitions. <https://www.devon.gov.uk/roads-and-transport/document/winter-service-and-emergency-plan/>
17. West Berkshire Council, safer driving in winter: salting decided on road temperature. <https://www.westberks.gov.uk/article/43434/Safer-driving-in-winter>
18. Thermal stress of motorcycle riding (PMC): protective gear and heat. <https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4580804/>
19. CDC Yellow Book, heat and cold illness: dehydration. <https://www.cdc.gov/yellow-book/hcp/environmental-hazards-risks/heat-and-cold-illness-in-travelers.html>
20. US National Weather Service, wind chill brochure (read, not used: its index assumes walking speed, so it understates a rider's exposure). <https://www.weather.gov/media/safety/wind-chill-brochure.pdf>

Caveats: the MSF documents are US material and the Highway Code, Met Office and nidirect are UK; only DGT and AEMET are
Spanish. The AEMET figures are per zone and from a 2022 annex; recent warnings still use 70 km/h for a yellow gust
warning in several zones, but the value for Salamanca's zone was not checked.
