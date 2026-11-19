"""Diagnostics download for the Dobiss integration."""
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .cover import pair_covers

TO_REDACT = {CONF_HOST}


def _output(output):
    return {
        "module": output["moduleAddress"],
        "index": output["index"],
        "name": output["name"],
        "type": output["type"].name if hasattr(output["type"], "name") else output["type"],
        "group": output["groupIndex"],
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry):
    """Return the imported installation and the last polled states."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    diagnostics = {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
    }
    if coordinator is None:
        diagnostics["error"] = "The integration is not loaded"
        return diagnostics

    dobiss = coordinator.dobiss
    diagnostics.update({
        "last_update_success": coordinator.last_update_success,
        "last_exception": repr(coordinator.last_exception) if coordinator.last_exception else None,
        "modules": [
            {
                "address": module["address"],
                "type": module["type"].name if hasattr(module["type"], "name") else module["type"],
                "is_master": module["isMaster"],
                "output_count": module["outputCount"],
                "values": (coordinator.data or {}).get(address),
            }
            for address, module in sorted(dobiss.modules.items())
        ],
        "outputs": [_output(output) for output in dobiss.outputs],
        "entities": {
            "lights": [f"{o['moduleAddress']}.{o['index']}" for o in dobiss.lights],
            "plugs": [f"{o['moduleAddress']}.{o['index']}" for o in dobiss.plugs],
            "fans": [f"{o['moduleAddress']}.{o['index']}" for o in dobiss.fans],
            "ventilation": [
                {
                    "name": group["name"],
                    "default_mode": group["default_mode"],
                    "modes": {
                        str(mode): f"{o['moduleAddress']}.{o['index']}"
                        for mode, o in sorted(group["modes"].items())
                    },
                }
                for group in dobiss.ventilation_groups
            ],
            "covers": [
                {
                    "unique_id": cover["unique_id"],
                    "name": cover["name"],
                    "up": f"{cover['up']['moduleAddress']}.{cover['up']['index']}" if cover["up"] else None,
                    "down": f"{cover['down']['moduleAddress']}.{cover['down']['index']}" if cover["down"] else None,
                }
                for cover in pair_covers(dobiss.outputs)
            ],
        },
    })
    return diagnostics
