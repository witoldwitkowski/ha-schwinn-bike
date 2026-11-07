# Changelog

## 0.1.0
First release. Local push over Bluetooth (any Home Assistant adapter or ESPHome Bluetooth proxy).
- Sensors: speed, cadence, resistance level, power (the bike's own estimate), heart rate, ride calories / distance / time, last ride, rider. Binary sensor: moving.
- Rides: ended by the console's End, 3 minutes without pedalling, a counter reset, or the integration unloading. Rides that follow within 3 minutes are merged
  (same console user only); a ride with under 5 minutes of pedalling is thrown away. Finished rides are appended to a CSV and fire `schwinn_bike_ride_finished`.
- Console users: the bike reports which user is riding; the integration maps the user number to a name.
- Heart rate: the handgrip sensor's zeros and bogus bursts are filtered.
