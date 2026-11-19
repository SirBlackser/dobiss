"""Constants for Dobiss integration"""

DOMAIN = "dobiss"
PLATFORMS = ["light", "fan", "switch", "cover"]

DEFAULT_PORT = 10001
DEFAULT_SCAN_INTERVAL = 10

# Options: seconds each cover takes to fully open / close, keyed by cover unique id
CONF_COVER_TRAVEL = "cover_travel"
CONF_TRAVEL_UP = "up"
CONF_TRAVEL_DOWN = "down"
