"""Dobiss Cover (screens/roller shutters) Control"""
import logging
from typing import Dict, List, Optional, Tuple

from time import monotonic
from datetime import timedelta

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, CONF_COVER_TRAVEL, CONF_TRAVEL_UP, CONF_TRAVEL_DOWN
from .dobiss import DobissSystem
from .entity import DobissEntity

_LOGGER = logging.getLogger(__name__)

# After we start a cover, ignore polls that still show its relay off for this
# long: they may have been read just before the command reached the controller.
MOVE_GRACE = 3
# How often the position is updated in the UI while a cover moves
POSITION_TICK = timedelta(seconds=1)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up the Dobiss Cover platform."""
    coordinator = hass.data[DOMAIN]["coordinator"]

    # Build covers by pairing Up/Down outputs.
    covers: List[Dict] = pair_covers(coordinator.dobiss.outputs)

    _LOGGER.info("Adding covers (roller shutters/screens)...")
    _LOGGER.debug(str(covers))

    travel = config_entry.options.get(CONF_COVER_TRAVEL, {})
    async_add_entities(
        HomeAssistantDobissCover(coordinator, cover, travel.get(cover["unique_id"], {}))
        for cover in covers
    )
    _LOGGER.info("Dobiss covers added.")


def pair_covers(outputs: List[Dict]) -> List[Dict]:
    """Pair Up/Down outputs into single cover descriptors.

    Strategy:
    - Prefer pairing by (moduleAddress, groupIndex), assuming Dobiss assigns the
      same groupIndex to the Up/Down of a single physical cover. Support multiple
      covers sharing the same groupIndex by pairing greedily within each key.
    - If that doesn't yield a pair, fall back to name-based heuristic:
      strip a trailing " up"/" down" suffix (case-insensitive) and pair by base name,
      keeping per-module groupings and supporting duplicate names.
    - As a last resort, any single unpaired Up or Down becomes a one-direction
      cover entity (you will only be able to drive the available direction).
    """
    from collections import defaultdict, deque

    def norm_name(n: str) -> str:
        n = (n or "").strip().lower()
        for suffix in (" up", " down"):
            if n.endswith(suffix):
                return n[: -len(suffix)].strip()
        return n

    # First pass: index by (moduleAddress, groupIndex) and keep all entries per key
    ups_map: Dict[Tuple[int, int], List[Dict]] = defaultdict(list)
    downs_map: Dict[Tuple[int, int], List[Dict]] = defaultdict(list)

    for out in outputs:
        if out.get("type") == DobissSystem.OutputType.Up:
            ups_map[(out["moduleAddress"], out["groupIndex"])].append(out)
        elif out.get("type") == DobissSystem.OutputType.Down:
            downs_map[(out["moduleAddress"], out["groupIndex"])].append(out)

    covers: List[Dict] = []

    # Greedy pairing within each (module, groupIndex)
    leftover_ups: List[Dict] = []
    leftover_downs: List[Dict] = []

    all_keys = set(ups_map.keys()) | set(downs_map.keys())
    for key in all_keys:
        ups_list = ups_map.get(key, [])
        downs_list = downs_map.get(key, [])
        if ups_list and downs_list:
            # Use name-based pairing first within the key
            ups_by_name: Dict[str, deque] = defaultdict(deque)
            downs_by_name: Dict[str, deque] = defaultdict(deque)
            for u in sorted(ups_list, key=lambda x: x.get("index", 0)):
                ups_by_name[norm_name(u.get("name"))].append(u)
            for d in sorted(downs_list, key=lambda x: x.get("index", 0)):
                downs_by_name[norm_name(d.get("name"))].append(d)

            matched_names = set(ups_by_name.keys()) & set(downs_by_name.keys())
            for nm in sorted(matched_names):
                while ups_by_name[nm] and downs_by_name[nm]:
                    u = ups_by_name[nm].popleft()
                    d = downs_by_name[nm].popleft()
                    covers.append(_build_cover_descriptor(u, d))

            # Collect remaining in lists by index order to pair loosely
            remaining_ups = [u for q in ups_by_name.values() for u in q]
            remaining_downs = [d for q in downs_by_name.values() for d in q]
            remaining_ups.sort(key=lambda x: x.get("index", 0))
            remaining_downs.sort(key=lambda x: x.get("index", 0))

            # Pair by closest indices greedily
            iu = id = 0
            while iu < len(remaining_ups) and id < len(remaining_downs):
                u = remaining_ups[iu]
                d = remaining_downs[id]
                covers.append(_build_cover_descriptor(u, d))
                iu += 1
                id += 1

            # Any remainder goes to global leftovers
            leftover_ups.extend(remaining_ups[iu:])
            leftover_downs.extend(remaining_downs[id:])
        else:
            # Move all to leftovers if only one direction exists under this key
            leftover_ups.extend(ups_list)
            leftover_downs.extend(downs_list)

    # Cross-key name-based pairing while preserving module boundary and supporting duplicates
    from collections import defaultdict as dd
    up_index: Dict[Tuple[int, str], deque] = dd(deque)
    down_index: Dict[Tuple[int, str], deque] = dd(deque)

    for u in sorted(leftover_ups, key=lambda x: (x["moduleAddress"], x.get("index", 0))):
        up_index[(u["moduleAddress"], norm_name(u.get("name")))].append(u)
    for d in sorted(leftover_downs, key=lambda x: (x["moduleAddress"], x.get("index", 0))):
        down_index[(d["moduleAddress"], norm_name(d.get("name")))].append(d)

    common_keys = set(up_index.keys()) & set(down_index.keys())
    for k in sorted(common_keys):
        uq = up_index[k]
        dq = down_index[k]
        while uq and dq:
            covers.append(_build_cover_descriptor(uq.popleft(), dq.popleft()))

    # Single-direction entities for any remaining
    for uq in up_index.values():
        while uq:
            covers.append(_build_cover_descriptor(uq.popleft(), None))
    for dq in down_index.values():
        while dq:
            covers.append(_build_cover_descriptor(None, dq.popleft()))

    return covers


def _build_cover_descriptor(up: Optional[Dict], down: Optional[Dict]) -> Dict:
    """Create a cover descriptor dictionary from Up/Down outputs."""
    name_parts = []
    module_addr = up["moduleAddress"] if up else down["moduleAddress"]
    if up:
        name_parts.append(up.get("name") or "")
    if down and (not up or down.get("name") != up.get("name")):
        name_parts.append(down.get("name") or "")

    # Derive a cleaned base name
    def tidy(n: str) -> str:
        n = (n or "").strip()
        if n.lower().endswith(" up"):
            return n[:-3].strip()
        if n.lower().endswith(" down"):
            return n[:-5].strip()
        return n

    base_name = tidy(up.get("name") if up else down.get("name")) or "Cover"

    # unique id from available components
    uid_parts = [str(module_addr)]
    if up:
        uid_parts.append(f"U{up['index']}")
    if down:
        uid_parts.append(f"D{down['index']}")

    return {
        "moduleAddress": module_addr,
        "name": base_name,
        "up": up,
        "down": down,
        "unique_id": ".".join(uid_parts),
    }


class HomeAssistantDobissCover(DobissEntity, CoverEntity, RestoreEntity):
    """Representation of a Dobiss cover (screen or roller shutter).

    Dobiss only reports whether the up and down relays are on. When the travel
    times are set in the options, the position is estimated from how long the
    cover has been moving, which also allows moving it to a given position.
    """

    def __init__(self, coordinator, cover: Dict, travel: Optional[Dict] = None):
        super().__init__(coordinator)
        self._cover = cover
        self._name = cover["name"]

        travel = travel or {}
        self._travel_up = float(travel.get(CONF_TRAVEL_UP) or 0) if cover.get("up") else 0.0
        self._travel_down = float(travel.get(CONF_TRAVEL_DOWN) or 0) if cover.get("down") else 0.0

        # Estimated position: 0 = closed, 100 = open, None = unknown
        self._position: Optional[float] = None
        # +1 opening, -1 closing, 0 standing still
        self._direction = 0
        self._move_started_at = 0.0
        self._move_start_position = 0.0
        self._target = 0.0
        self._unsub_stop = None
        self._unsub_tick = None

    @property
    def name(self):
        return self._name

    @property
    def unique_id(self):
        return self._cover["unique_id"]

    @property
    def _tracks_position(self) -> bool:
        """True when every available direction has a travel time."""
        up, down = self._cover.get("up"), self._cover.get("down")
        return (
            (up is None or self._travel_up > 0)
            and (down is None or self._travel_down > 0)
            and (self._travel_up > 0 or self._travel_down > 0)
        )

    @property
    def supported_features(self):
        features = CoverEntityFeature.STOP
        if self._cover.get("up"):
            features |= CoverEntityFeature.OPEN
        if self._cover.get("down"):
            features |= CoverEntityFeature.CLOSE
        if self._tracks_position:
            features |= CoverEntityFeature.SET_POSITION
        return features

    @property
    def extra_state_attributes(self):
        if not self._tracks_position:
            return None
        return {"travel_time_up": self._travel_up, "travel_time_down": self._travel_down}

    # ----- state -----

    def _relay_on(self, key) -> bool:
        output = self._cover.get(key)
        return bool(output) and self._output_value(output) > 0

    def _live_position(self) -> Optional[float]:
        if self._direction == 0 or self._position is None:
            return self._position
        travel = self._travel_up if self._direction > 0 else self._travel_down
        elapsed = monotonic() - self._move_started_at
        position = self._move_start_position + self._direction * elapsed / travel * 100
        return max(0.0, min(100.0, position))

    @property
    def current_cover_position(self):
        if not self._tracks_position:
            return None
        position = self._live_position()
        return None if position is None else round(position)

    @property
    def is_closed(self):
        if not self._tracks_position:
            # Unknown without position feedback
            return None
        position = self.current_cover_position
        return None if position is None else position <= 0

    @property
    def is_opening(self):
        if self._tracks_position:
            return self._direction > 0
        return self._relay_on("up")

    @property
    def is_closing(self):
        if self._tracks_position:
            return self._direction < 0
        return self._relay_on("down")

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if not self._tracks_position:
            return
        last_state = await self.async_get_last_state()
        if last_state is not None:
            position = last_state.attributes.get(ATTR_CURRENT_POSITION)
            if position is not None:
                self._position = float(position)
        self._sync_with_relays()

    async def async_will_remove_from_hass(self):
        self._cancel_timers()
        await super().async_will_remove_from_hass()

    # ----- position tracking -----

    def _cancel_timers(self):
        if self._unsub_stop:
            self._unsub_stop()
            self._unsub_stop = None
        if self._unsub_tick:
            self._unsub_tick()
            self._unsub_tick = None

    def _begin_tracking(self, direction: int, target: Optional[float] = None):
        """Start estimating the position of a cover that just started moving."""
        start = self._live_position()
        self._cancel_timers()
        if start is None:
            # Unknown position: assume it starts from the other end, so a full
            # run leaves the position known.
            start = 0.0 if direction > 0 else 100.0
        if target is None:
            target = 100.0 if direction > 0 else 0.0

        self._position = start
        self._move_start_position = start
        self._direction = direction
        self._target = target
        self._move_started_at = monotonic()

        travel = self._travel_up if direction > 0 else self._travel_down
        duration = abs(target - start) / 100 * travel
        self._unsub_stop = async_call_later(self.hass, duration, self._async_reached_target)
        self._unsub_tick = async_track_time_interval(self.hass, self._async_tick, POSITION_TICK)

    def _end_tracking(self):
        """Freeze the position where the cover is now."""
        self._position = self._live_position()
        self._direction = 0
        self._cancel_timers()

    @callback
    def _async_tick(self, _now):
        self.async_write_ha_state()

    async def _async_reached_target(self, _now):
        self._unsub_stop = None
        target = self._target
        direction = self._direction
        self._end_tracking()
        self._position = target
        self.async_write_ha_state()
        if target in (0.0, 100.0):
            # A full run ends at the motor's end stop; leave the relay to the controller
            return
        output = self._cover.get("up" if direction > 0 else "down")
        try:
            await self._command(self.dobiss.setOff(output["moduleAddress"], output["index"]))
        except HomeAssistantError as err:
            _LOGGER.error("Could not stop cover '%s' at %s%%: %s", self._name, round(target), err)
        await self.coordinator.async_request_refresh()

    def _sync_with_relays(self):
        """Follow moves started or stopped outside Home Assistant (wall buttons, timers)."""
        up_on = self._relay_on("up")
        down_on = self._relay_on("down")

        if self._direction != 0:
            moving_relay_on = up_on if self._direction > 0 else down_on
            if moving_relay_on or monotonic() - self._move_started_at < MOVE_GRACE:
                return
            # The relay went off before we expected: the cover stopped
            self._end_tracking()

        position = self._position
        if up_on and not down_on and (position is None or position < 100):
            self._begin_tracking(1)
        elif down_on and not up_on and (position is None or position > 0):
            self._begin_tracking(-1)

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._tracks_position:
            self._sync_with_relays()
        super()._handle_coordinator_update()

    # ----- commands -----

    async def async_open_cover(self, **kwargs):
        up = self._cover.get("up")
        if not up:
            _LOGGER.warning("No Up output available for cover '%s'", self._name)
            return
        await self._move(1)

    async def async_close_cover(self, **kwargs):
        down = self._cover.get("down")
        if not down:
            _LOGGER.warning("No Down output available for cover '%s'", self._name)
            return
        await self._move(-1)

    async def async_set_cover_position(self, **kwargs):
        target = float(kwargs[ATTR_POSITION])
        if not self._tracks_position:
            raise HomeAssistantError(f"Set the travel times of '{self._name}' in the Dobiss options first")
        current = self._live_position()
        if current is None:
            raise HomeAssistantError(
                f"The position of '{self._name}' is unknown: fully open or close it once first")
        if target >= 100:
            await self.async_open_cover()
        elif target <= 0:
            await self.async_close_cover()
        elif abs(target - current) >= 1:
            await self._move(1 if target > current else -1, target)

    async def async_stop_cover(self, **kwargs):
        if self._tracks_position:
            self._end_tracking()
            self.async_write_ha_state()
        # Stop by turning both directions off
        await self._turn_dir(off=self._cover.get("up"))
        await self._turn_dir(off=self._cover.get("down"))
        await self.coordinator.async_request_refresh()

    async def _move(self, direction: int, target: Optional[float] = None):
        on = self._cover.get("up" if direction > 0 else "down")
        off = self._cover.get("down" if direction > 0 else "up")
        if self._tracks_position and self._direction != 0:
            self._end_tracking()
        # Ensure the other direction is off before starting this one
        await self._turn_dir(off=off)
        await self._turn_dir(on=on)
        if self._tracks_position:
            self._begin_tracking(direction, target)
            self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def _turn_dir(self, on: Optional[Dict] = None, off: Optional[Dict] = None):
        if off:
            await self._command(self.dobiss.setOff(off["moduleAddress"], off["index"]))
        if on:
            # Use relay-type action: 100% on
            await self._command(self.dobiss.setOn(on["moduleAddress"], on["index"], 100))
