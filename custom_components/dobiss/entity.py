"""Shared base class for Dobiss entities."""
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .dobiss import DobissConnectionError


class DobissEntity(CoordinatorEntity):
    """A Dobiss entity, grouped under the controller's device.

    It is unavailable while the controller can't be polled (handled by
    CoordinatorEntity), and commands that can't reach the controller raise an
    error in the UI instead of silently doing nothing.
    """

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self.dobiss = coordinator.dobiss

    @property
    def device_info(self):
        """Return device info to group all entities under the Dobiss controller."""
        host = getattr(self.dobiss, 'host', 'dobiss')
        port = getattr(self.dobiss, 'port', None)
        ident = f"{host}:{port}" if port is not None else str(host)
        return {
            "identifiers": {(DOMAIN, ident)},
            "name": f"Dobiss Controller {host}",
            "manufacturer": "Dobiss",
        }

    def _output_value(self, output):
        """The last polled value (0-100) of an output, or 0 if unknown."""
        data = self.coordinator.data or {}
        mod_vals = data.get(output['moduleAddress'])
        idx = output['index']
        if not mod_vals or idx >= len(mod_vals):
            return 0
        return mod_vals[idx]

    async def _command(self, coro):
        """Run a controller command and turn connection errors into a UI error."""
        try:
            await coro
        except (DobissConnectionError, OSError, TimeoutError) as err:
            raise HomeAssistantError(f"Cannot reach the Dobiss controller: {err}") from err
