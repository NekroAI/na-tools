"""Host-side HTTP daemon for Nekro Agent updates."""

PROTOCOL_VERSION = "na-tools.daemon.v1"
PROVIDER = "na-tools"
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 18081
DEFAULT_SOCKS_BIND_HOST = "0.0.0.0"
DEFAULT_SOCKS_BIND_PORT = 18082
DEFAULT_DAEMON_API_BASE = "http://na-tools.local/v1"
DEFAULT_DAEMON_SOCKS_URL = (
    f"socks5h://host.docker.internal:{DEFAULT_SOCKS_BIND_PORT}"
)
CONTAINER_DAEMON_TOKEN_FILE = "${NEKRO_DATA_DIR}/.na-tools/daemon.token"
JOB_LOG_RETENTION_DAYS = 7
