"""Tests for the Dobiss integration against a fake controller."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL, STATE_UNAVAILABLE
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.dobiss.const import DOMAIN, CONF_COVER_TRAVEL
from custom_components.dobiss.diagnostics import async_get_config_entry_diagnostics

from .fake_controller import FakeController, default_installation

COVER = "cover.screen"


@pytest.fixture
async def controller():
    fake = await FakeController(default_installation()).start()
    yield fake
    await fake.stop()


@pytest.fixture(autouse=True)
def fast_retries():
    # Don't wait for the connection back-off in tests
    with patch("custom_components.dobiss.dobiss.CONNECT_RETRIES", 1):
        yield


def _entry(port, options=None):
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="dobiss-127.0.0.1",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: port, CONF_SCAN_INTERVAL: 10},
        options=options or {},
    )


async def _setup(hass, port, options=None):
    entry = _entry(port, options)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def test_config_flow_tests_connection(hass, controller):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    # Nothing listens on this port
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: 1, CONF_SCAN_INTERVAL: 10})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: controller.port, CONF_SCAN_INTERVAL: 10})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PORT] == controller.port


async def test_setup_retries_when_controller_unreachable(hass):
    entry = await _setup(hass, 1)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_entities_and_unavailable(hass, controller):
    entry = await _setup(hass, controller.port)
    assert entry.state is ConfigEntryState.LOADED

    assert hass.states.get("light.living").state == "off"
    assert hass.states.get("light.eettafel").state == "off"
    assert hass.states.get("switch.stopcontact").state == "off"
    assert hass.states.get("fan.ventilatie") is not None
    assert hass.states.get(COVER) is not None

    await hass.services.async_call("light", "turn_on", {"entity_id": "light.eettafel", "brightness": 128}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert controller.values[2][0] == 50
    assert hass.states.get("light.eettafel").state == "on"

    # Controller goes away: commands raise an error in the UI...
    await controller.stop()
    from homeassistant.exceptions import HomeAssistantError
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("light", "turn_on", {"entity_id": "light.living"}, blocking=True)

    # ...and the next poll marks the entities unavailable
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11), fire_all=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("light.living").state == STATE_UNAVAILABLE

    # And comes back
    controller.server = None
    fake2 = FakeController(default_installation())
    import asyncio
    fake2.server = await asyncio.start_server(fake2._handle, "127.0.0.1", controller.port)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=22), fire_all=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("light.living").state == "off"
    await fake2.stop()
    controller.server = fake2.server  # so the fixture's stop() is harmless
    controller.stop = _noop


async def _noop():
    return None


async def test_diagnostics(hass, controller):
    entry = await _setup(hass, controller.port)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["data"][CONF_HOST] == "**REDACTED**"
    assert diag["last_update_success"] is True
    assert [m["address"] for m in diag["modules"]] == [1, 2]
    assert diag["modules"][0]["type"] == "Relais"
    assert diag["entities"]["covers"][0]["name"] == "Screen"
    assert diag["entities"]["ventilation"][0]["modes"] == {"0": "1.4", "2": "1.5", "3": "1.6"}


async def test_options_flow_sets_cover_travel_times(hass, controller):
    entry = await _setup(hass, controller.port)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: controller.port, CONF_SCAN_INTERVAL: 10})
    assert result["step_id"] == "covers"
    fields = [str(k) for k in result["data_schema"].schema]
    assert fields == ["Screen: open time (s)", "Screen: close time (s)"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"Screen: open time (s)": 20, "Screen: close time (s)": 18})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.options[CONF_COVER_TRAVEL] == {"1.U2.D3": {"up": 20.0, "down": 18.0}}

    # The entry reloaded and the cover now supports positions
    state = hass.states.get(COVER)
    assert state.attributes["travel_time_up"] == 20.0


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.start = dt_util.utcnow()

    def __call__(self):
        return self.now


async def _advance(hass, clock, seconds):
    clock.now += seconds
    async_fire_time_changed(hass, clock.start + timedelta(seconds=clock.now - 1000.0 + 0.01))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_cover_position(hass, controller):
    clock = Clock()
    with patch("custom_components.dobiss.cover.monotonic", clock):
        await _setup(hass, controller.port, {CONF_COVER_TRAVEL: {"1.U2.D3": {"up": 20, "down": 10}}})
        state = hass.states.get(COVER)
        assert state.attributes.get("current_position") is None

        # Unknown position: setting a position is refused
        from homeassistant.exceptions import HomeAssistantError
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call("cover", "set_cover_position", {"entity_id": COVER, "position": 50}, blocking=True)

        # A full close makes the position known
        await hass.services.async_call("cover", "close_cover", {"entity_id": COVER}, blocking=True)
        assert hass.states.get(COVER).state == "closing"
        assert controller.values[1][3] == 100
        await _advance(hass, clock, 5)
        assert hass.states.get(COVER).attributes["current_position"] == 50
        await _advance(hass, clock, 6)
        state = hass.states.get(COVER)
        assert state.state == "closed"
        assert state.attributes["current_position"] == 0

        # The controller ends the run (motor timer)
        controller.values[1][3] = 0

        # Open to 25%: 5 s of the 20 s travel time, then the up relay is switched off
        await hass.services.async_call("cover", "set_cover_position", {"entity_id": COVER, "position": 25}, blocking=True)
        assert controller.values[1][2] == 100
        assert hass.states.get(COVER).state == "opening"
        await _advance(hass, clock, 5)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert controller.values[1][2] == 0
        state = hass.states.get(COVER)
        assert state.state == "open"
        assert state.attributes["current_position"] == 25

        # Someone presses the wall button: the next poll sees the down relay on
        controller.values[1][3] = 100
        await _advance(hass, clock, 10)  # scan interval: poll picks it up
        assert hass.states.get(COVER).state == "closing"
        await _advance(hass, clock, 1)
        controller.values[1][3] = 0  # released after 1 s
        await _advance(hass, clock, 10)  # next poll sees it stopped
        # A stop from the wall is only seen at the next poll, so the estimate
        # keeps running until then (here all the way down).
        state = hass.states.get(COVER)
        assert state.state == "closed"
        assert state.attributes["current_position"] == 0
