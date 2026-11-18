"""Dobiss Fan Control"""
import logging
from .dobiss import DobissSystem
from .const import DOMAIN

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)


_LOGGER = logging.getLogger(__name__)

        
async def async_setup_entry(hass, config_entry, async_add_entities):
    """Setup the Dobiss Fan platform."""
    coordinator = hass.data[DOMAIN]["coordinator"]

    fans = coordinator.dobiss.fans
    _LOGGER.info("Adding fans...")
    _LOGGER.debug(str(fans))

    # Ventilation units whose speeds are wired as separate outputs
    # ("Ventilatie 0", "Ventilatie 2", ...) become one fan with exclusive modes
    groups = coordinator.dobiss.ventilation_groups
    _LOGGER.debug(str(groups))

    # Add devices
    entities = [HomeAssistantDobissFan(coordinator, fan) for fan in fans]
    entities += [HomeAssistantDobissVentilation(coordinator, group) for group in groups]
    async_add_entities(entities)
    
    _LOGGER.info("Dobiss fans added.")


class HomeAssistantDobissFan(CoordinatorEntity, FanEntity):
    """Representation of a Dobiss fan in HomeAssistant."""

    def __init__(self, coordinator, fan):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)

        """Initialize a DobissFan."""
        self.dobiss = coordinator.dobiss
        self._fan = fan
        self._name = fan['name']


    @property
    def unique_id(self):
        return "{}.{}".format(self._fan['moduleAddress'], self._fan['index'])

    @property
    def device_extra_attributes(self):
        """Return device specific state attributes."""
        return self._fan
    
    @property
    def name(self):
        """Return the display name of this fan."""
        return self._name

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

    @property
    def is_on(self):
        """Return true if the fan is on."""
        mod = self._fan['moduleAddress']
        idx = self._fan['index']
        mod_vals = self.coordinator.data.get(mod)
        if not mod_vals or idx >= len(mod_vals):
            return False
        val = mod_vals[idx]
        return val > 0

    async def async_turn_on(self, **kwargs):
        """Instruct the fan to turn on.
        """
        await self.dobiss.setOn(self._fan['moduleAddress'], self._fan['index'])

        # Poll states
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        """Instruct the fan to turn off."""
        await self.dobiss.setOff(self._fan['moduleAddress'], self._fan['index'])

        # Poll states
        await self.coordinator.async_request_refresh()


class HomeAssistantDobissVentilation(CoordinatorEntity, FanEntity):
    """A ventilation unit with exclusive speed modes (e.g. 0 to 3).

    Each mode that has its own Dobiss output is a relay; the mode without an
    output (typically 1) is the default and is active when all relays are off.
    Selecting a mode switches every other mode's relay off.
    """

    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, coordinator, group):
        super().__init__(coordinator)
        self.dobiss = coordinator.dobiss
        self._group = group
        self._name = group['name']
        self._mode_outputs = group['modes']            # {mode: output}
        self._default_mode = group['default_mode']
        self._all_modes = group['all_modes']           # e.g. [0, 1, 2, 3]
        # Speeds used for the percentage slider: every mode except 0
        self._speeds = [str(m) for m in self._all_modes if m > 0]

        first = min(self._mode_outputs.values(), key=lambda o: (o['moduleAddress'], o['index']))
        self._unique_id = "ventilation.{}.{}".format(first['moduleAddress'], first['index'])

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        return self._name

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

    @property
    def supported_features(self):
        features = FanEntityFeature.PRESET_MODE | FanEntityFeature.TURN_ON
        if self._speeds:
            features |= FanEntityFeature.SET_SPEED
        if 0 in self._mode_outputs:
            features |= FanEntityFeature.TURN_OFF
        return features

    @property
    def extra_state_attributes(self):
        return {
            "mode": self.current_mode,
            "default_mode": self._default_mode,
            "outputs": {
                str(mode): "{}.{}".format(o['moduleAddress'], o['index'])
                for mode, o in sorted(self._mode_outputs.items())
            },
        }

    def _output_is_on(self, output):
        data = self.coordinator.data or {}
        mod_vals = data.get(output['moduleAddress'])
        idx = output['index']
        if not mod_vals or idx >= len(mod_vals):
            return False
        return mod_vals[idx] > 0

    @property
    def current_mode(self):
        """The active mode: the relay that is on, or the default mode if none is."""
        active = [mode for mode, o in self._mode_outputs.items() if self._output_is_on(o)]
        if active:
            # Should be only one; if several are on, the highest wins
            return max(active)
        return self._default_mode

    @property
    def preset_modes(self):
        return [str(m) for m in self._all_modes]

    @property
    def preset_mode(self):
        return str(self.current_mode)

    @property
    def speed_count(self):
        return max(len(self._speeds), 1)

    @property
    def percentage(self):
        mode = str(self.current_mode)
        if mode not in self._speeds:
            return 0
        return ordered_list_item_to_percentage(self._speeds, mode)

    @property
    def is_on(self):
        return self.current_mode != 0

    async def _async_set_mode(self, mode):
        if mode not in self._all_modes:
            raise ValueError(f"Invalid ventilation mode {mode} for {self._name}")
        target = self._mode_outputs.get(mode)  # None for the default mode
        others = [o for m, o in self._mode_outputs.items() if m != mode]
        await self.dobiss.setExclusive(others, target)
        await self.coordinator.async_request_refresh()

    async def async_set_preset_mode(self, preset_mode):
        await self._async_set_mode(int(preset_mode))

    async def async_set_percentage(self, percentage):
        if percentage == 0 or not self._speeds:
            await self.async_turn_off()
            return
        await self._async_set_mode(int(percentage_to_ordered_list_item(self._speeds, percentage)))

    async def async_turn_on(self, percentage=None, preset_mode=None, **kwargs):
        if preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)
        elif percentage is not None:
            await self.async_set_percentage(percentage)
        else:
            await self._async_set_mode(self._default_mode)

    async def async_turn_off(self, **kwargs):
        if 0 in self._mode_outputs:
            await self._async_set_mode(0)
        else:
            # No "off" output: fall back to the default mode
            await self._async_set_mode(self._default_mode)
