[![HACS Compliance](https://github.com/avlemos/dobiss/actions/workflows/validate.yaml/badge.svg "HACS Compliance")]([https://www.markdownguide.org](https://github.com/avlemos/dobiss/actions/workflows/validate.yaml))
[![Validate with hassfest](https://github.com/avlemos/dobiss/actions/workflows/hassfest.yaml/badge.svg "hassfest Compliance")]([https://www.markdownguide.org](https://github.com/avlemos/dobiss/actions/workflows/hassfest.yaml)) 

# Intro

This is a project for [Home Assistant](https://github.com/home-assistant) which enables you to connect to your [Dobiss Ambience PRO](https://products.dobiss.com/nl/onze-oplossingen/per-gamma/ambiance-pro) system, by using the **DO5437** module, which makes a bridge betweent the CAN bus that Dobiss provides, and your network, via an RJ45 interface.


# Usage
With this, you can check the state of the lights, and toggle them. There are other uses for the Dobiss system, but the only hardware I have are the lights, so if you find anything broken outside of lights, feel free to do a PR.

After installing the Integration, you should find a new Dobiss Domotics entry, where you will be able to configure the IP address of the **DO5437** module.

# Fork
This is a fork from [OpenJeDi/HomeAssistantFiles](https://github.com/OpenJeDi/HomeAssistantFiles) without the configuration files (thank you [@OpenJeDi](https://github.com/OpenJeDi) for doing 99.9% of the work).

You can see the fork [here](https://github.com/avlemos/HomeAssistantFiles), which is(was) 4 commits ahead, as of this writing. I only use this for lights, so other elements may be broken.


# Differences
The main differences for this fork are:

1. async usage so that it doesn't bog down on HA (this was the main reason why I forked)
1. HACS compatibility, so it's easier to keep up to date
1. trying to keep up with HA deprecation/new methods


## Setup

When you add the integration, it checks that the controller answers before saving. If the controller can't be reached when Home Assistant starts, the integration retries in the background instead of loading without entities.

While the controller doesn't answer a poll, all Dobiss entities show as unavailable, and commands show an error instead of silently doing nothing.

## Polling and state updates

- The integration uses Home Assistant's DataUpdateCoordinator to poll the Dobiss controller for all output states.
- Default polling interval is 10 seconds.
- You can change the polling interval in Home Assistant at any time:
  - Go to Settings > Devices & Services > Dobiss Domotics > Configure > Options.
  - Adjust "Scan interval (seconds)" to your preferred value and save.
- After you operate an entity (turn on/off, set brightness, open/close/stop), the integration also triggers an immediate refresh so the UI updates quickly.
- Each poll opens a short-lived TCP connection to the controller, requests all statuses, and immediately disconnects again. This avoids locking the controller so the official Dobiss Pro app can keep working.

Recommendations:
- For most setups, 5–10 seconds balances responsiveness and controller load well.
- Setting very low intervals (e.g., <3 seconds) may increase network/CPU load and could make the controller less responsive if multiple clients are connected.


## Cover positions

Dobiss only reports whether a cover's up or down relay is on, so by default covers can open, close and stop but have no position. To get a position:

1. Time how many seconds each cover takes to fully open and to fully close.
2. Go to Settings > Devices & Services > Dobiss Domotics > Configure, save the first page, and enter the times on the second page.

Home Assistant then estimates the position from how long the cover has been moving, and you can move a cover to a given position (for example 40%). Good to know:

- After setting the times, fully open or close each cover once so the position is known.
- Moves started with a wall button are followed, but they are only seen at the next poll, so the estimate can be off by up to one scan interval. A full open or close corrects it.
- Leave a time at 0 to keep that cover without position.

## Diagnostics

Settings > Devices & Services > Dobiss Domotics > ⋮ > Download diagnostics gives a file with the modules, outputs, how they were grouped into entities, and the last states. Attach it to bug reports. The controller's IP address is left out.

## Energy usage

The Dobiss controller doesn't measure power. To see estimated usage in the Energy dashboard, install [Powercalc](https://github.com/bramstroker/homeassistant-powercalc) from HACS and give each Dobiss light or socket a fixed wattage.

## Ventilation

Ventilation units whose speeds are wired as separate Dobiss outputs (for example `Ventilatie 0`, `Ventilatie 2` and `Ventilatie 3`) are combined into a single fan entity instead of separate switches. Outputs are grouped when they are of the fan type, or when their name contains "ventil", and their names differ only in a trailing digit.

- Only one mode can be active: selecting a mode switches the other modes' outputs off first.
- The mode without an output of its own (usually 1) is the default, which runs when all mode outputs are off.
- Modes are exposed as presets (`0`–`3`) and as the speed slider (1 = 33%, 2 = 66%, 3 = 100%). Turning the fan off selects mode 0; turning it on selects the default mode.

The old per-mode entities will show as unavailable after updating and can be removed from Home Assistant.

## Development

The tests run the integration in Home Assistant against a simulated controller:

```
pip install pytest-homeassistant-custom-component
pytest
```
