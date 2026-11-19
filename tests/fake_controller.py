"""A small fake Dobiss LAN controller (DO5437) for tests.

It speaks the request/response protocol used by custom_components.dobiss:
every packet is echoed back padded to 32 bytes, followed by the response
data padded to 32 bytes.
"""
import asyncio

RELAY, DIMMER = 0x08, 0x10
LIGHT, PLUG, FAN, UP, DOWN = 0, 1, 2, 3, 4


def _pad(data: bytes) -> bytes:
    return data + bytes((32 - len(data) % 32) % 32)


class FakeController:
    def __init__(self, modules):
        # modules: {address: (module type, [(name, output type, group), ...])}
        self.modules = modules
        self.values = {addr: [0] * (12 if t == RELAY else 4) for addr, (t, _) in modules.items()}
        self.actions = []
        self.connections = 0
        self.server = None
        self.port = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        self.connections += 1
        try:
            while True:
                packet = await reader.readexactly(16)
                writer.write(_pad(packet) + self._respond(packet))
                if packet[1] == 0x02:
                    data = await reader.readexactly(8)
                    writer.write(_pad(data))
                    self._apply(data)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    def _respond(self, packet) -> bytes:
        cmd, kind, addr = packet[1], packet[2], packet[3]
        if cmd == 0x0B:  # installation: bitmap of module addresses
            bitmap = bytearray(16)
            for a in self.modules:
                bitmap[(a - 1) // 8] |= 1 << ((a - 1) % 8)
            return _pad(bytes(bitmap))
        if cmd == 0x10 and kind == 0xFF:  # module info
            info = bytearray(16)
            info[0], info[2], info[14] = addr, 1, self.modules[addr][0]
            return _pad(bytes(info))
        if cmd == 0x10:  # output names
            mtype, outputs = self.modules[addr]
            count = packet[7]
            lines = b""
            for i in range(count):
                name, otype, group = outputs[i] if i < len(outputs) else (f"Output {i}", LIGHT, 0)
                lines += name.encode().ljust(30, b" ") + bytes((otype, group))
            return _pad(lines)
        if cmd == 0x01:  # status
            return _pad(bytes(self.values[addr]).ljust(16, b"\0"))
        return b""  # 0x02 action header: echo only

    def _apply(self, data):
        addr, index, action, _, _, value = data[:6]
        self.actions.append((addr, index, action, value))
        current = self.values[addr]
        if action == 0:
            current[index] = 0
        elif action == 1:
            current[index] = value if self.modules[addr][0] == DIMMER else 100
        elif action == 2:
            current[index] = 0 if current[index] else 100


def default_installation():
    return {
        1: (RELAY, [
            ("Living", LIGHT, 1),
            ("Stopcontact", PLUG, 1),
            ("Screen up", UP, 2),
            ("Screen down", DOWN, 2),
            ("Ventilatie 0", FAN, 3),
            ("Ventilatie 2", FAN, 3),
            ("Ventilatie 3", FAN, 3),
        ]),
        2: (DIMMER, [("Eettafel", LIGHT, 1)]),
    }
