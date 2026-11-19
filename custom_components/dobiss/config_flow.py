"""Config and options flow for the Dobiss integration."""
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import callback

from .const import (
    DOMAIN,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    CONF_COVER_TRAVEL,
    CONF_TRAVEL_UP,
    CONF_TRAVEL_DOWN,
)
from .dobiss import DobissSystem, DobissConnectionError

_LOGGER = logging.getLogger(__name__)


async def _async_test_connection(host, port):
    """Return an error key, or None if the controller answered."""
    try:
        await DobissSystem(host, port).test_connection()
    except (DobissConnectionError, OSError, TimeoutError) as err:
        _LOGGER.warning("Cannot connect to the Dobiss controller at %s:%s: %s", host, port, err)
        return "cannot_connect"
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Unexpected error while testing the Dobiss connection")
        return "unknown"
    return None


class DobissConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """The config flow for the Dobiss Domotics integration."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            # Don't configure the same controller twice
            await self.async_set_unique_id(f"dobiss-{user_input[CONF_HOST]}")
            self._abort_if_unique_id_configured()

            error = await _async_test_connection(user_input[CONF_HOST], user_input[CONF_PORT])
            if error is None:
                return self.async_create_entry(
                    title=f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}",
                    data={
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: user_input[CONF_PORT],
                        CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                    },
                )
            errors["base"] = error

        user_input = user_input or {}
        data_schema = vol.Schema({
            vol.Required(CONF_HOST, default=user_input.get(CONF_HOST, vol.UNDEFINED)): str,
            vol.Optional(CONF_PORT, default=user_input.get(CONF_PORT, DEFAULT_PORT)): int,
            vol.Optional(CONF_SCAN_INTERVAL, default=user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)): int,
        })
        return self.async_show_form(step_id="user", data_schema=data_schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return DobissOptionsFlowHandler()


class DobissOptionsFlowHandler(config_entries.OptionsFlow):
    """Options: connection settings, then the travel time of each cover."""

    def __init__(self) -> None:
        self._options = {}
        # Form field label -> (cover unique id, direction key)
        self._travel_fields = {}

    async def async_step_init(self, user_input=None):
        errors = {}
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            changed = (
                user_input[CONF_HOST] != current.get(CONF_HOST)
                or user_input[CONF_PORT] != current.get(CONF_PORT)
            )
            error = None
            if changed:
                error = await _async_test_connection(user_input[CONF_HOST], user_input[CONF_PORT])
            if error is None:
                self._options = {**self.config_entry.options, **user_input}
                if self._covers():
                    return await self.async_step_covers()
                return self.async_create_entry(title="", data=self._options)
            errors["base"] = error

        defaults = user_input or current
        data_schema = vol.Schema({
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Optional(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): int,
            vol.Optional(CONF_SCAN_INTERVAL, default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)): int,
        })
        return self.async_show_form(step_id="init", data_schema=data_schema, errors=errors)

    async def async_step_covers(self, user_input=None):
        """Ask how many seconds each cover takes to fully open and fully close."""
        covers = self._covers()
        travel = self.config_entry.options.get(CONF_COVER_TRAVEL, {})

        if user_input is not None:
            new_travel = {}
            for label, value in user_input.items():
                if label not in self._travel_fields:
                    continue
                uid, key = self._travel_fields[label]
                new_travel.setdefault(uid, {})[key] = float(value or 0)
            self._options[CONF_COVER_TRAVEL] = new_travel
            return self.async_create_entry(title="", data=self._options)

        # The form shows one field per direction, labelled with the cover's
        # name, since the covers are only known once the installation is read.
        schema = {}
        self._travel_fields = {}
        used = set()
        for cover in covers:
            name = cover["name"]
            if name in used:
                name = f"{name} ({cover['unique_id']})"
            used.add(name)
            current = travel.get(cover["unique_id"], {})
            for key, label, output in (
                (CONF_TRAVEL_UP, "open time (s)", cover.get("up")),
                (CONF_TRAVEL_DOWN, "close time (s)", cover.get("down")),
            ):
                if output is None:
                    continue
                field = f"{name}: {label}"
                self._travel_fields[field] = (cover["unique_id"], key)
                schema[vol.Optional(field, default=current.get(key, 0))] = vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=600))

        return self.async_show_form(step_id="covers", data_schema=vol.Schema(schema))

    def _covers(self):
        """The covers of the loaded installation."""
        from .cover import pair_covers

        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None:
            return []
        return pair_covers(coordinator.dobiss.outputs)
