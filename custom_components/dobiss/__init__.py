"""Dobiss Control Systems integration."""
import asyncio
from datetime import timedelta
import logging

from .dobiss import DobissSystem, DobissConnectionError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, PLATFORMS, DEFAULT_PORT, DEFAULT_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

# Importing the installation reads every module and its output names
IMPORT_TIMEOUT = 60
# A status poll should be quick on the LAN
POLL_TIMEOUT = 10

SERVICE_IMPORT_INSTALLATION = "importInstallation"


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Dobiss component. Configuration is done in the UI."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Dobiss from a config entry."""
    # Prefer options for connection params if present
    host = entry.options.get(CONF_HOST, entry.data[CONF_HOST])
    port = entry.options.get(CONF_PORT, entry.data.get(CONF_PORT, DEFAULT_PORT))

    # Prefer options (set via Options Flow), then data, then default
    scan_seconds = entry.options.get(CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    update_interval = timedelta(seconds=scan_seconds)

    _LOGGER.info(f"Setting up Dobiss Control entry with data {str(entry.data)} and options {str(entry.options)}")

    coordinator = DobissDataUpdateCoordinator(
        hass, entry, host=host, port=port, update_interval=update_interval)

    # The entities are created from the imported installation, so it has to
    # succeed first. If the controller can't be reached, Home Assistant retries
    # the setup later instead of loading without any entities.
    try:
        await coordinator.importInstallation()
    except (DobissConnectionError, OSError, TimeoutError) as err:
        coordinator.dobiss.disconnect()
        raise ConfigEntryNotReady(
            f"Cannot read the installation from the Dobiss controller at {host}:{port}: {err}") from err

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    if "coordinator" in hass.data[DOMAIN]:
        _LOGGER.warning("Only one Dobiss controller is supported; the previous one will be replaced")
    hass.data[DOMAIN]["coordinator"] = coordinator
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Re-import the installation (for example after outputs were renamed or
    # added in the Dobiss programming) by reloading, so new entities show up.
    async def handle_import_installation(call: ServiceCall):
        _LOGGER.info("Re-importing Dobiss installation via service call")
        for config_entry in hass.config_entries.async_entries(DOMAIN):
            await hass.config_entries.async_reload(config_entry.entry_id)

    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT_INSTALLATION):
        hass.services.async_register(DOMAIN, SERVICE_IMPORT_INSTALLATION, handle_import_installation)

    # Any option change (host, port, interval, cover travel times) reloads the entry
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Reload the entry so changed options take effect."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    _LOGGER.info("Unloading Dobiss Control entries")
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    cfg = hass.data.get(DOMAIN, {})
    if unload_ok:
        coordinator = cfg.pop(entry.entry_id, None)
        if coordinator is not None and cfg.get("coordinator") is coordinator:
            cfg.pop("coordinator")
        if coordinator is not None:
            coordinator.dobiss.disconnect()
        if not hass.config_entries.async_loaded_entries(DOMAIN):
            hass.services.async_remove(DOMAIN, SERVICE_IMPORT_INSTALLATION)

    return unload_ok


class DobissDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Dobiss data from the LAN controller."""

    def __init__(self, hass, entry, host, port, update_interval):
        """Initialize."""
        _LOGGER.info(f"Initializing Dobiss System with host {host} and port {port}...")
        self.dobiss = DobissSystem(host, port)

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=update_interval,
        )

    async def importInstallation(self):
        """Import the installation: modules, output names and types."""
        _LOGGER.info("Importing Dobiss installation...")
        async with asyncio.timeout(IMPORT_TIMEOUT):
            await self.dobiss.connect()
            try:
                await self.dobiss.importFullInstallation()
            finally:
                # Release connection so other clients (e.g., Dobiss Pro app) can use the controller
                self.dobiss.disconnect()
        _LOGGER.info("Importing Dobiss installation done")

    async def _async_update_data(self):
        """Query states.

        Raising UpdateFailed marks every Dobiss entity unavailable until the
        controller answers again, so automations don't act on stale states.
        """
        try:
            async with asyncio.timeout(POLL_TIMEOUT):
                # Connect, poll, and disconnect to avoid holding the controller exclusively
                _LOGGER.debug("Requesting all statuses...")
                await self.dobiss.connect()
                try:
                    await self.dobiss.requestAllStatus()
                finally:
                    # Ensure we release the connection for other apps
                    self.dobiss.disconnect()
                _LOGGER.debug("Requesting all statuses done")
        except (DobissConnectionError, OSError, TimeoutError) as err:
            raise UpdateFailed(f"Cannot read the output states from the Dobiss controller: {err}") from err

        # Copy so listeners can compare old and new states
        return {module: list(values) for module, values in self.dobiss.values.items()}
