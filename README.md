# Route Planner
Stage 1: automatic clean routes (no U-turns or tunnels), road by road tables, Gate 1.
Stage 2: Street View + Claude screening, TomTom roadworks, D1/D2, landmarks, Gate 2.
Stage 3: revised times (50 km/h formula), graph, Gate 3.
Stage 4: rest stops (Google Places, 7 point check), route with stops, Gate 4.
Stage 5: frozen driver reference: lane to be in until each turn, when to filter, what tells you to turn.
Stage 6: map plan, maps drawn over Google Static Maps, consistency checks, Gate 6.

v6 changes
- Street View UNKNOWN_ERROR is retried (3 pauses), then that point is skipped and retried on the next run. Gate 2 cannot be approved while points are skipped. REQUEST_DENIED and OVER_QUERY_LIMIT still stop the run, with their own advice.
- Screening cache key is now v4 (bus lane reading added), so the first run after upgrading re-screens every point once.
- Default start is the Chong Pang Camp entrance (1.430472, 103.826444).
- Destination and start accept decimal coordinates, degrees minutes seconds, a pasted Google Maps link, a 6 digit postal code, an address or a name.
- Departure time (Singapore time) is entered on the home page and can be changed in Stage 3. Stage 3 shows the clock time at every bus lane and says USABLE, NOT USABLE or CHECK SIGN. The Stage 5 driver reference repeats the verdict on the road row.
- Maps: labels keep 14 px clear of the route line and their leader lines avoid it. START and END sit on the exact ends of the line (checked on every map). A hollow ring shows the point you typed when it is away from the road point the route uses.
