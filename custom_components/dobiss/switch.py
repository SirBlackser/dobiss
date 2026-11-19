"""Dobiss Plug Control"""
import logging
from .dobiss import DobissSystem
from .const import DOMAIN
from .entity import DobissEntity

from homeassistant.components.switch import SwitchEntity, SwitchDeviceClass


_LOGGER = logging.getLogger(__name__)

        
async def async_setup_entry(hass, config_entry, async_add_entities):
    """Setup the Dobiss Plug platform."""
    coordinator = hass.data[DOMAIN]["coordinator"]

    plugs = coordinator.dobiss.plugs
    _LOGGER.info("Adding plugs...")
    _LOGGER.debug(str(plugs))

    # Add devices
    async_add_entities(
        HomeAssistantDobissPlug(coordinator, plug) for plug in plugs
    )
    
    _LOGGER.info("Dobiss plugs added.")


class HomeAssistantDobissPlug(DobissEntity, SwitchEntity):
    """Representation of a Dobiss plug in HomeAssistant."""

    def __init__(self, coordinator, plug):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)

        """Initialize a DobissPlug."""
        self._plug = plug
        self._name = plug['name']


    @property
    def unique_id(self):
        return "{}.{}".format(self._plug['moduleAddress'], self._plug['index'])

    @property
    def device_extra_attributes(self):
        """Return device specific state attributes."""
        return self._plug
    
    @property
    def name(self):
        """Return the display name of this plug."""
        return self._name

    @property
    def device_class(self):
        """Return the class of this device, from component DEVICE_CLASSES."""
        return SwitchDeviceClass.SWITCH

    @property
    def is_on(self):
        """Return true if the plug is on."""
        mod = self._plug['moduleAddress']
        idx = self._plug['index']
        mod_vals = self.coordinator.data.get(mod)
        if not mod_vals or idx >= len(mod_vals):
            return False
        val = mod_vals[idx]
        return val > 0

    async def async_turn_on(self, **kwargs):
        """Instruct the plug to switch on.
        """
        await self._command(self.dobiss.setOn(self._plug['moduleAddress'], self._plug['index']))

        # Poll states
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        """Instruct the plug to turn off."""
        await self._command(self.dobiss.setOff(self._plug['moduleAddress'], self._plug['index']))

        # Poll states
        await self.coordinator.async_request_refresh()
