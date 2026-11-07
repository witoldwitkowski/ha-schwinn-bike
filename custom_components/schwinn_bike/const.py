"""Constants for the Schwinn 170/270 bike integration."""

DOMAIN = "schwinn_bike"

CONF_RIDER = "rider"
DEFAULT_RIDER = "rider"  # who a ride is logged to when the bike does not say who is riding, or the console user is not in the map
CONF_USER_MAP = "user_map"
DEFAULT_USER_MAP = "1=user1, 2=user2, 3=user3"  # console user number -> name/id used in the ride log and the event; edit it in the integration options

MIN_RIDE_S = 300  # a ride with less than 5 minutes of actual pedalling is thrown away (not logged, no event, not on the receipt)
MERGE_WAIT_S = 180  # after a ride ends, wait this long for another one to start: two rides in that window become ONE (then logged + printed)
IDLE_DISCONNECT_S = 60  # no packets at all for this long -> drop the link to free the Bluetooth proxy slot
RECONNECT_COOLDOWN_S = 15  # after an idle disconnect, wait this long before connecting again (so a ride that starts soon is not missed)
QUICK_RETRY_S = 5  # after an unexpected link drop, retry soon so a ride in progress continues
FAILED_CONNECT_RETRY_S = 30

BIKE_LOG = "weight/bike_log.csv"  # relative to the HA config dir; the weekly receipt reads it
PEOPLE_FILE = "weight/people.json"  # optional: per-person max_hr / birth_year for heart-rate zones

EVENT_RIDE_FINISHED = f"{DOMAIN}_ride_finished"
