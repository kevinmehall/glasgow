import struct
import asyncio
import logging as pyLogging
from dataclasses import dataclass

from amaranth.lib import enum
from amaranth.utils import ceil_log2

from glasgow.support import logging
from glasgow.support.endpoint import ServerEndpoint
from glasgow.gateware import cobs


logger = logging.getLogger("ols")
pyLogging.basicConfig(level=0)


class Command(enum.Enum, shape=8):
    IDENTIFY            = 0b10_000000
    GET_CLK_FREQUENCY   = 0b10_000001
    SET_CLK_DIVISOR     = 0b01_000010
    GET_BUFFER_SIZE     = 0b10_000011
    SET_PROLOG_SIZE     = 0b01_000100
    SET_EPILOG_SIZE     = 0b01_000101
    GET_DATA_FORMAT     = 0b10_000110
    SET_TRIGGER         = 0b01_010000
    GET_TRIGGER         = 0b10_010000
    SYNCHRONIZE         = 0b11_011111

    ARM_TRIGGER         = 0b00_000000
    FORCE_TRIGGER       = 0b00_000001
    DISARM_TRIGGER      = 0b00_000010
    INTERRUPT           = 0b00_000011

    SET_ACTIVE          = 0b01_100000
    SET_VALUE           = 0b01_100001
    SET_LEVEL           = 0b01_100010


@dataclass
class Analyzer:
    clk_frequency: int = 48_000_000
    clk_divisor: int = 0
    buffer_size: int = 0x100000
    prolog_size: int = buffer_size >> 1
    epilog_size: int = buffer_size >> 1
    streaming: bool = False
    sample_width: int = 7

    trigger_offset: int = 0
    acquire_task: asyncio.Task | None = None

    @property
    def sample_stride(self) -> int:
        return 1 << ceil_log2(self.sample_width)

    def check(self):
        assert self.prolog_size + self.epilog_size <= self.buffer_size

    # 𝑁(𝑆) → (𝑝, 𝑆′)
    def make_buffer(self, length):
        buffer = bytearray()
        val = 1
        for i in range(length):
            buffer.append(i&0xff)
            val = (val * 157 + 3) & 0xFF
        return buffer

    async def generate(self, endpoint):
        trailer = struct.pack("B", 0x80 + self.trigger_offset)
        self.trigger_offset = (self.trigger_offset + 1) % 4

        prolog = self.make_buffer(self.prolog_size * 4)
        await endpoint.send(cobs.encode(b"\x01" + prolog + trailer) + b"\x00")
        if self.epilog_size > 1000:
            epilog = self.make_buffer(1000 * 4)
            await endpoint.send(cobs.encode(b"\x01" + epilog + b"\x00") + b"\x00")
            epilog = self.make_buffer((self.epilog_size - 1000) * 4)
            await endpoint.send(cobs.encode(b"\x01" + epilog + b"\x00") + b"\x00")
        else:
            epilog = self.make_buffer(self.epilog_size * 4)
            await endpoint.send(cobs.encode(b"\x01" + epilog + b"\x00") + b"\x00")
        if self.streaming:
            while True:
                await asyncio.sleep(0.1)
                epilog = self.make_buffer(10240)
                await endpoint.send(cobs.encode(b"\x01" + epilog + b"\x00") + b"\x00")

    def start(self, endpoint):
        self.stop()
        self.acquire_task = asyncio.create_task(self.generate(endpoint))

    def stop(self):
        if self.acquire_task is not None:
            self.acquire_task.cancel()
            self.acquire_task = None


async def handle(analyzer, endpoint, command, argument):
    match command:
        case Command.IDENTIFY:
            return 0x30414c47 # GLA0

        case Command.GET_CLK_FREQUENCY:
            return analyzer.clk_frequency

        case Command.SET_CLK_DIVISOR:
            analyzer.clk_divisor = argument

        case Command.GET_BUFFER_SIZE:
            return analyzer.buffer_size

        case Command.SET_PROLOG_SIZE:
            analyzer.prolog_size = argument

        case Command.SET_EPILOG_SIZE:
            analyzer.epilog_size = argument & 0x7FFFFFFF
            analyzer.streaming = bool(argument & 0x80000000)

        case Command.GET_DATA_FORMAT:
            return analyzer.sample_width | (analyzer.sample_stride << 8)

        case Command.SET_TRIGGER:
            pass

        case Command.GET_TRIGGER:
            return 0x49534142 # BASI

        case Command.ARM_TRIGGER:
            analyzer.check()
            analyzer.start(endpoint)

        case Command.FORCE_TRIGGER:
            analyzer.check()
            analyzer.start(endpoint)

        case Command.DISARM_TRIGGER:
            pass # FIXME

        case Command.INTERRUPT:
            analyzer.stop()

        case Command.SYNCHRONIZE:
            return argument

        case Command.SET_ACTIVE:
            pass # FIXME

        case Command.SET_VALUE:
            pass # FIXME

        case Command.SET_LEVEL:
            pass # FIXME

        case _:
            logger.error(f"unknown command {command:02x}")

    assert False


async def main():
    analyzer = Analyzer()
    endpoint = await ServerEndpoint("analyzer", logger, ("tcp", "", 5556))

    while True:
        try:
            command, = await endpoint.recv(1)
            if command & 0x40:
                argument, = struct.unpack("<L", await endpoint.recv(4))
            else:
                argument = None
            result = await handle(analyzer, endpoint, Command(command), argument)
            print(analyzer)
            if command & 0x80:
                await endpoint.send(cobs.encode(struct.pack("<BL", 0, result)) + b"\x00")
        except EOFError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
