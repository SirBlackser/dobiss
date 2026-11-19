"""
Helper module for communicating with a Dobiss home automation system.
"""

import re
import logging
import asyncio
from enum import IntEnum

RECV_SIZE = 1024
MAX_NUM_RETRIES = 10
CONNECT_RETRIES = 3  # Connection attempts before giving up (1s, 2s backoff)
CONNECT_TIMEOUT = 5  # Seconds to wait for the TCP connection to open
TIMEOUT = 1  # We can use a short timeout on the LAN
ACK_TIMEOUT = 2  # Seconds to wait for the controller to echo a command
RETRY_DELAY = 1  # Set the delay between retries in seconds

_LOGGER = logging.getLogger(__name__)


class DobissConnectionError(ConnectionError):
    """The Dobiss controller could not be reached or gave no valid answer."""


class DobissSystem:

    def __init__(self, host, port):

        self._host = host
        self._port = port
        self._connected = False

        self._reader = None
        self._writer = None
        self.recvBuffer = bytearray()

        # Serializes access to the single TCP connection: the protocol is a
        # strict request/response exchange, so concurrent callers (e.g. a
        # status poll racing a light toggle) must not interleave their
        # sendData/receiveResponse pairs.
        self._lock = asyncio.Lock()
        # Separate lock so two callers connecting at once share one connection
        # instead of the second overwriting the first one's stream.
        self._connect_lock = asyncio.Lock()

        self.availableModules = []
        self.modules = {}
        self.outputs = []
        self.values = {}

    @property
    def host(self):
        """Return the host of this system."""
        return self._host

    @property
    def port(self):
        """Return the port of this system."""
        return self._port

    @property
    def connected(self):
        """True if the socket is connected"""
        return self._connected

    # Outputs named like "Ventilatie 0", "Ventilatie 2", "Ventilatie 3" are the
    # relays that select one speed of a single ventilation unit.
    _MODE_NAME_RE = re.compile(r'^(?P<base>.*?\S)[\s_\-]*(?P<mode>\d)$')

    def _mode_output_info(self, output):
        """Return (base name, mode) if this output selects a ventilation mode, else None."""
        match = DobissSystem._MODE_NAME_RE.match(output['name'])
        if not match:
            return None
        base = match.group('base').strip()
        if output['type'] != DobissSystem.OutputType.Fan and 'ventil' not in base.lower():
            return None
        return base, int(match.group('mode'))

    @property
    def ventilation_groups(self):
        """Group the per-speed outputs of a ventilation unit into one entry.

        Only one speed can be active at a time. The speed that has no output of
        its own (typically 1) is the unit's default: it runs when all of the
        speed relays are off.
        """
        groups = {}
        for output in self.outputs:
            info = self._mode_output_info(output)
            if info is None:
                continue
            base, mode = info
            group = groups.setdefault(base.lower(), {'name': base, 'modes': {}})
            # If two outputs claim the same mode, keep the first one
            group['modes'].setdefault(mode, output)

        result = []
        for group in groups.values():
            # A single numbered output is not a multi-speed unit
            if len(group['modes']) < 2:
                continue
            modes = group['modes']
            max_mode = max(max(modes), 1)
            missing = [m for m in range(1, max_mode + 1) if m not in modes]
            if missing:
                group['default_mode'] = missing[0]
            else:
                positive = sorted(m for m in modes if m > 0)
                group['default_mode'] = positive[0] if positive else min(modes)
            group['all_modes'] = sorted(set(modes) | set(missing))
            result.append(group)

        return result

    def _grouped_output_keys(self):
        return {
            (output['moduleAddress'], output['index'])
            for group in self.ventilation_groups
            for output in group['modes'].values()
        }

    def _outputs_of_type(self, output_type):
        grouped = self._grouped_output_keys()
        return [
            output for output in self.outputs
            if output['type'] == output_type
            and (output['moduleAddress'], output['index']) not in grouped
        ]

    @property
    def lights(self):
        return self._outputs_of_type(DobissSystem.OutputType.Light)

    @property
    def fans(self):
        return self._outputs_of_type(DobissSystem.OutputType.Fan)

    @property
    def plugs(self):
        return self._outputs_of_type(DobissSystem.OutputType.Plug)

    async def connect(self):
        """Connect to a Dobiss system.
           Tries a few times and raises DobissConnectionError if it can't connect.
        """
        async with self._connect_lock:
            await self._connect_with_retries()
        if not self._connected:
            raise DobissConnectionError(
                f"Cannot connect to the Dobiss controller at {self.host}:{self.port}")

    async def _connect_with_retries(self):
        retry_delay = 1  # Initial delay in seconds
        retries = 0
        while not self._connected and retries < CONNECT_RETRIES:
            try:
                _LOGGER.info("connect through connect_logic")
                await self.connect_logic()
            except (OSError, asyncio.TimeoutError) as e:
                _LOGGER.warning(f"Dobiss socket error while trying to connect: {str(e) or type(e).__name__}")
                self._connected = False
                retries += 1
                if retries < CONNECT_RETRIES:
                    _LOGGER.debug(f"Retrying in {retry_delay} seconds...")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    _LOGGER.error("Maximum retry attempts reached. Connection failed.")
                    break

    async def connect_logic(self):
        _LOGGER.info(f"Connecting to Dobiss system at IP {self.host} and port {self.port}")
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), CONNECT_TIMEOUT)
        self._connected = True
        _LOGGER.info("Connected to Dobiss system.")

    def disconnect(self):
        """Disconnect from the connected Dobiss system."""
        _LOGGER.info("Disconnecting from Dobiss system")
        try:
            if self._writer is not None:
                self._writer.close()
        except OSError as e:
            _LOGGER.error(f"Dobiss socket error {str(e)}")
        finally:
            self._reader = None
            self._writer = None
            self._connected = False
            self.recvBuffer = bytearray()

    async def sendData(self, data):
        """Send data to a Dobiss system.
           Reconnects and retries once if sending fails.
        """
        _LOGGER.debug(f"sendData {str(data)}")

        if self._writer is None:
            raise DobissConnectionError("Not connected to the Dobiss controller")

        try:
            self._writer.write(data)
            await self._writer.drain()
            return True
        except OSError as e:
            _LOGGER.error(f"Dobiss socket error on sending data {str(e)}")
            await self.reconnect(data)

    async def reconnect(self, data):
        _LOGGER.info("Dobiss for the reconnect")
        self.disconnect()
        await self.connect()
        if data:
            await self.sendData(data)

    async def receiveResponse(self, sentDataSize, responseSize):
        """Receive response"""

        # Receive until we have enough data
        # The data consists of the sent data (padded to 32 bytes) and then the response data (padded to 32 bytes)
        sentDataPaddingSize = (32 - (sentDataSize % 32)) % 32
        responsePaddingSize = (32 - (responseSize % 32)) % 32
        totalSize = sentDataSize + sentDataPaddingSize + responseSize + responsePaddingSize

        numRetries = 0

        while (len(self.recvBuffer) < totalSize) and (numRetries < MAX_NUM_RETRIES) and self._reader is not None:
            try:
                received_data = await self._reader.read(RECV_SIZE)

                if received_data:
                    self.recvBuffer += received_data
                else:
                    # Peer closed the connection.
                    numRetries += 1
                    await asyncio.sleep(RETRY_DELAY)

            except OSError as e:
                _LOGGER.error(f"Dobiss socket error while receiving data: {str(e)}")
                return bytearray()

        # We first receive the original packet back
        # TODO Actually check the content
        # original = self.recvBuffer[:sentDataSize]

        # The actual response data
        responseData = bytearray()
        if responseSize > 0:
            start = sentDataSize + sentDataPaddingSize
            end = start + responseSize
            responseData = self.recvBuffer[start:end]

        # Remove the response from the buffer        
        self.recvBuffer = self.recvBuffer[totalSize:]

        return responseData

    async def test_connection(self):
        """Check that the controller answers. Raises DobissConnectionError if not."""
        await self.connect()
        try:
            await self.importInstallation()
        finally:
            self.disconnect()

    async def importFullInstallation(self):
        """Import the installation, all modules, their outputs and their status."""

        # Start from scratch so a re-import doesn't duplicate outputs
        self.modules = {}
        self.outputs = []
        self.values = {}

        # Import installation
        await self.importInstallation()

        # Import modules
        for moduleAddr in self.availableModules:
            await self.importModule(moduleAddr)

        # Outputs and their current value
        for moduleAddr, module in self.modules.items():
            await self.importOutputs(module['address'], module['type'], module['outputCount'])
            await self.requestStatus(module['address'], module['type'], module['outputCount'])

    async def importInstallation(self):
        """Import the installation."""
        data = bytearray.fromhex("AF 0B 00 00 30 00 10 01 10 FF FF FF FF FF FF AF")

        async with self._lock:
            await self.sendData(data)
            installationData = await self.receiveResponse(len(data), 16)

        if len(installationData) != 16:
            raise DobissConnectionError(
                f"Invalid data received trying to import installation: received {len(installationData)} bytes instead of 16")

        # Parse the installation
        self.availableModules = []

        # First 11 bytes (bits 0-81) contain whether or not there is a module with the specific address (1-82)
        for i in range(0, 82):
            byteNum = int(i / 8)
            bitNum = int(i % 8)
            hasModule = (installationData[byteNum] >> bitNum) & 1
            if hasModule:
                channelAddr = i + 1
                self.availableModules.append(channelAddr)

        _LOGGER.debug("Available modules: " + str(self.availableModules))

    class ModuleType(IntEnum):
        """The type of module."""
        Relais = 0x08
        Dimmer = 0x10
        V0_10 = 0x18

    async def importModule(self, moduleAddr):
        """Import a module."""

        # Import the module
        # data = bytearray.fromhex("AF 10 FF " + chr(moduleAddr).encode('hex') + " 00 00 10 01 10 FF FF FF FF FF FF AF")
        data = bytearray.fromhex("AF 10 FF " + f"{moduleAddr:02x}" + " 00 00 10 01 10 FF FF FF FF FF FF AF")

        async with self._lock:
            await self.sendData(data)
            moduleData = await self.receiveResponse(len(data), 16)

        if len(moduleData) != 16:
            raise DobissConnectionError(
                f"Invalid data received trying to import module {moduleAddr}: received {len(moduleData)} bytes instead of 16")

        # moduleAddr = ord(moduleData[0])
        # moduleType = ord(moduleData[14])
        # master = ord(moduleData[2])
        moduleAddr = moduleData[0]
        moduleType = DobissSystem.ModuleType(moduleData[14])
        master = moduleData[2]
        masterLSB = master & 1
        isMaster = (masterLSB == 1)

        # 12 outputs for relais, 4 for dimmers
        if moduleType == DobissSystem.ModuleType.Relais:
            outputCount = 12
        else:
            outputCount = 4

        # Cache the module
        self.modules[moduleAddr] = {
            'address': moduleAddr,
            'type': moduleType,
            'isMaster': isMaster,
            'outputCount': outputCount
        }

        _LOGGER.debug(f"Module {moduleAddr} imported: " + str(self.modules[moduleAddr]))

    class OutputType(IntEnum):
        """The type of output."""
        Light = 0x00
        Plug = 0x01
        Fan = 0x02
        Up = 0x03
        Down = 0x04

    async def importOutputs(self, moduleAddr, moduleType, outputCount):
        """Import the outputs of a module."""

        # Import the module
        # data = bytearray.fromhex("AF 10 " + chr(moduleType).encode('hex') +  + chr(moduleAddr).encode('hex') + " 01 00 20 " + chr(outputCount).encode('hex') + " 20 FF FF FF FF FF FF AF")
        data = bytearray.fromhex(
            "AF 10 " + f"{moduleType.value:02x}" + f"{moduleAddr:02x}" + " 01 00 20 " + f"{outputCount:02x}" + " 20 FF FF FF FF FF FF AF")

        # <module.outputCount> lines of 32 bytes
        # Output names of 30 characters; convert byte array to string;
        # data[30] = icon type (0=light, 1=plug, 2=fan, 3=up, 4=down); data[31] = group index
        async with self._lock:
            await self.sendData(data)
            outputsData = await self.receiveResponse(len(data), 32 * outputCount)

        if len(outputsData) != 32 * outputCount:
            raise DobissConnectionError(
                f"Invalid data received trying to import the outputs of module {moduleAddr}: received {len(outputsData)} bytes instead of {32 * outputCount}")

        for outputIndex in range(0, outputCount):
            line = outputsData[outputIndex * 32: (outputIndex + 1) * 32]
            outputName = line[0:30].strip().decode(errors='replace')
            outputType = DobissSystem.OutputType(line[30])
            groupIndex = line[31]

            # Cache the output
            self.outputs.append({
                'moduleAddress': moduleAddr,
                'index': outputIndex,
                'name': outputName,
                'type': outputType,
                'groupIndex': groupIndex
            })

            _LOGGER.debug(f"Output imported: " + str(self.outputs[len(self.outputs) - 1]))

    async def requestStatus(self, moduleAddr, moduleType, outputCount):
        """Request the status of all outputs of a module."""

        # Request the status
        data = bytearray.fromhex(
            "AF 01 " + f"{moduleType.value:02x}" + f"{moduleAddr:02x}" + " 00 00 00 01 00 FF FF FF FF FF FF AF")

        async with self._lock:
            # A command may have closed the connection while we waited for the lock
            if not self.connected:
                await self.connect()
            await self.sendData(data)
            statusData = await self.receiveResponse(len(data), 16)

        if len(statusData) != 16:
            raise DobissConnectionError(
                f"Invalid data received trying to read the status of module {moduleAddr}: received {len(statusData)} bytes instead of 16")

        if not moduleAddr in self.values:
            self.values[moduleAddr] = []

        for outputIndex in range(0, outputCount):
            value = statusData[outputIndex]

            # Cache the value
            if len(self.values[moduleAddr]) <= outputIndex:
                self.values[moduleAddr].append(value)
            else:
                self.values[moduleAddr][outputIndex] = value

    async def requestAllStatus(self):
        """Request the status of all outputs of all modules."""

        for moduleAddr, module in self.modules.items():
            await self.requestStatus(module['address'], module['type'], module['outputCount'])

    class Action(IntEnum):
        """The type of action."""
        TurnOff = 0x00
        TurnOn = 0x01
        Toggle = 0x02

    async def setOn(self, moduleAddr, outputIndex, brightness=100):
        """Switch an output on."""
        _LOGGER.debug("setOn")
        action = DobissSystem.Action.TurnOn
        await self.sendAction(moduleAddr, outputIndex, action, brightness)

    async def setOff(self, moduleAddr, outputIndex):
        """Switch an output off."""
        _LOGGER.debug("setOff")
        action = DobissSystem.Action.TurnOff
        await self.sendAction(moduleAddr, outputIndex, action)

    async def setExclusive(self, outputs_off, output_on=None):
        """Turn a set of outputs off and then (optionally) one output on,
        using a single connection, so at most one ventilation mode is active."""
        _LOGGER.debug("setExclusive")
        established_here = False
        if not self.connected:
            await self.connect()
            established_here = True
        try:
            # Switch the other modes off first so two relays are never on together
            for output in outputs_off:
                await self.sendAction(output['moduleAddress'], output['index'], DobissSystem.Action.TurnOff)
            if output_on is not None:
                await self.sendAction(output_on['moduleAddress'], output_on['index'], DobissSystem.Action.TurnOn)
        finally:
            if established_here:
                self.disconnect()

    async def toggle(self, moduleAddr, outputIndex):
        """Toggle an output."""
        _LOGGER.debug("toggle")
        action = DobissSystem.Action.Toggle
        await self.sendAction(moduleAddr, outputIndex, action)

    async def sendAction(self, moduleAddr, outputIndex, action, value=100, delayOn=0xFF, delayOff=0xFF, softDim=0xFF,
                   red=0xFF):
        """Generic method to send an action to an output.
        Holds the request lock for the whole exchange so a status poll can't
        interleave with it, waits for the controller to echo each packet, and
        releases the connection afterwards if we established it here.
        """
        _LOGGER.debug("sendAction")
        async with self._lock:
            established_here = False
            if not self.connected:
                await self.connect()
                established_here = True
            try:
                # Send the request header; the controller only echoes it back
                headerData = bytearray.fromhex("AF 02 FF " + f"{moduleAddr:02x}" + " 00 00 08 01 08 FF FF FF FF FF FF AF")
                await self.sendData(headerData)
                await self._receiveAck(len(headerData))

                # Send the request data; again only an echo comes back
                requestData = bytes((moduleAddr, outputIndex, action.value, delayOn, delayOff, int(value), softDim, red))
                await self.sendData(requestData)
                await self._receiveAck(len(requestData))
            finally:
                if established_here:
                    self.disconnect()

    async def _receiveAck(self, sentDataSize):
        """Wait for the controller to echo a packet that has no response data.
        Gives up after ACK_TIMEOUT seconds so a missing echo can't block the lock."""
        try:
            await asyncio.wait_for(self.receiveResponse(sentDataSize, 0), ACK_TIMEOUT)
        except asyncio.TimeoutError:
            _LOGGER.warning("No acknowledgement from the Dobiss controller; continuing")
            # Whatever arrives late would misalign the next response
            self.recvBuffer = bytearray()
