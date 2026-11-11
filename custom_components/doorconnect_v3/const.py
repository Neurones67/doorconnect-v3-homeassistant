"""Constants for the DoorConnect v3 integration (Philips WelcomeEye Connect 3)."""

DOMAIN = "doorconnect_v3"

CONF_DEVICE_PASSWORD = "device_password"
CONF_CLOUD_USER = "cloud_user"
CONF_CLOUD_PASSWORD = "cloud_password"
CONF_MEDIA_CLIENT_ID = "media_client_id"
CONF_CLOUD_CLIENT_ID = "cloud_client_id"
CONF_STREAM_TOKEN = "stream_token"

OPT_HISTORY_INTERVAL = "history_interval"
OPT_STREAM_BIND = "stream_bind"
OPT_STREAM_PORT = "stream_port"

DEFAULT_HISTORY_INTERVAL = 5  # minutes
DEFAULT_STREAM_BIND = "127.0.0.1"
DEFAULT_STREAM_PORT = 0  # 0 = pick a free port at startup

# Home Assistant event fired for every new cloud history entry
EVENT_DOORCONNECT = "doorconnect_v3_event"

# Outputs driven by set.device.opendoor (<locknumber>)
LOCKS = {1: "strike", 2: "gate"}
