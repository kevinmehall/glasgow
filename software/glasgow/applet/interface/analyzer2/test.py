from contextlib import contextmanager

from amaranth import *
from amaranth.lib import io, wiring
from amaranth.sim import Simulator

from glasgow.gateware.stream import stream_put, stream_get, stream_assert
from glasgow.gateware import octoram
from glasgow.applet import GlasgowAppletV2TestCase
from . import AnalyzerApplet, Sampler, Trigger, Packer, Writer, Reader, FlowControl, AnalyzerCore


class AnalyzerAppletTestCase(GlasgowAppletV2TestCase, applet=AnalyzerApplet):
    # @synthesis_test
    # def test_build(self):
    #     self.assertBuilds()

    @contextmanager
    def run_test(self, dut, name="test"):
        sim = Simulator(dut)
        sim.add_clock(1e-6)
        yield sim
        with sim.write_vcd(f"{name}.vcd"):
            sim.run()

    def test_sampler(self):
        pins = io.SimulationPort("i", 32)
        dut = Sampler(pins)

        async def testbench_pins(ctx):
            for value in range(0, 64):
                ctx.set(pins.i, value)
                await ctx.tick()

        for divisor in (0, 1, 4):
            async def testbench_samples(ctx):
                ctx.set(dut.divisor, divisor)
                await ctx.tick()
                for expected in range(divisor, 64, divisor + 1):
                    await stream_assert(ctx, dut.o_samples, {"data": expected})

            with self.run_test(dut) as sim:
                sim.add_testbench(testbench_pins, background=True)
                sim.add_testbench(testbench_samples)

    def test_trigger_level(self):
        dut = Trigger()

        async def testbench_samples(ctx):
            for value in range(0, 64):
                await stream_put(ctx, dut.i_samples, {"data": value})

        async def testbench_triggers(ctx):
            ctx.set(dut.control.mode[1], {"active": 1, "value": 1, "level": 1})
            for value in range(0, 64):
                ctx.set(dut.control.enable,
                    value == 0 or value == 6)
                await stream_assert(ctx, dut.o_samples, {"data": value, "trig":
                    value == 2 or value == 7})

        with self.run_test(dut) as sim:
            sim.add_testbench(testbench_samples, background=True)
            sim.add_testbench(testbench_triggers)

    def test_trigger_edge(self):
        dut = Trigger()

        async def testbench_samples(ctx):
            for value in range(0, 64):
                await stream_put(ctx, dut.i_samples, {"data": value})

        async def testbench_triggers(ctx):
            ctx.set(dut.control.mode[1], {"active": 1, "value": 1, "level": 0})
            for value in range(0, 64):
                ctx.set(dut.control.enable,
                    value == 0 or value == 6)
                await stream_assert(ctx, dut.o_samples, {"data": value, "trig":
                    value == 2 or value == 10})

        with self.run_test(dut) as sim:
            sim.add_testbench(testbench_samples, background=True)
            sim.add_testbench(testbench_triggers)

    def test_trigger_force(self):
        dut = Trigger()

        async def testbench_samples(ctx):
            for value in range(0, 64):
                await stream_put(ctx, dut.i_samples, {"data": value})

        async def testbench_triggers(ctx):
            for value in range(0, 64):
                ctx.set(dut.control.force,
                    value == 6)
                await stream_assert(ctx, dut.o_samples, {"data": value, "trig":
                    value == 7})

        with self.run_test(dut) as sim:
            sim.add_testbench(testbench_samples, background=True)
            sim.add_testbench(testbench_triggers)

    def test_packer_samples_per_word(self):
        def check_width(width, per_word):
            packer = Packer(width=width)
            packer._MustUse__used = True
            assert packer.samples_per_word == per_word

        check_width(1,  32)
        check_width(2,  16)
        check_width(3,  8)
        check_width(4,  8)
        check_width(5,  4)
        check_width(8,  4)
        check_width(9,  2)
        check_width(16, 2)
        check_width(17, 1)
        check_width(32, 1)

    def test_packer_format(self):
        for width, offset, results in [
            (32, 2, [
                {"data": [0x00,0x00,0x00,0x00], "meta": {"trigger": 0}},
                {"data": [0x01,0x00,0x00,0x00], "meta": {"trigger": 0}},
                {"data": [0x02,0x00,0x00,0x00], "meta": {"trigger": 1, "offset": 0}},
            ]),
            (1, 5, [
                {"data": [0xAA,0xAA,0xAA,0xAA], "meta": {"trigger": 1, "offset": 5}},
            ]),
            (1, 32, [
                {"data": [0xAA,0xAA,0xAA,0xAA], "meta": {"trigger": 0}},
                {"data": [0xAA,0xAA,0xAA,0xAA], "meta": {"trigger": 1, "offset": 0}},
            ]),
            (4, 3, [
                {"data": [0x10,0x32,0x54,0x76], "meta": {"trigger": 1, "offset": 12}},
                {"data": [0x98,0xBA,0xDC,0xFE], "meta": {"trigger": 0}},
            ]),
        ]:
            dut = Packer(width=width)

            async def testbench_triggers(ctx):
                await ctx.tick()
                for value in range(0, 64):
                    await stream_put(ctx, dut.i_samples, {"data": value, "trig": value == offset})

            async def testbench_packed(ctx):
                for result in results:
                    await stream_assert(ctx, dut.o_packed, result)

            with self.subTest(width=width, offset=offset):
                with self.run_test(dut) as sim:
                    sim.add_testbench(testbench_triggers, background=True)
                    sim.add_testbench(testbench_packed)

    def test_writer(self):
        m = Module()
        m.submodules.writer = dut  = Writer(dram_range=range(0, 64), burst_bytes=32)
        m.submodules.dram   = dram = octoram.SimulationController(64)
        wiring.connect(m, dut.dram, dram.bus)

        async def testbench_packed(ctx):
            for packed in [
                {"data": [0x10,0x32,0x54,0x76], "meta": {}},
                {"data": [0x98,0xBA,0xDC,0xFE], "meta": {}},
                {"data": [0x00,0x11,0x22,0x33], "meta": {}},
                {"data": [0x44,0x55,0x66,0x77], "meta": {}},

                {"data": [0x00,0x00,0x11,0x11], "meta": {}},
                {"data": [0x22,0x22,0x33,0x33], "meta": {}},
                {"data": [0x44,0x44,0x55,0x55], "meta": {}},
                {"data": [0x66,0x66,0x77,0x77], "meta": {}},

                {"data": [0xA0,0xA1,0xA2,0xA3], "meta": {}},
                {"data": [0xB0,0xB1,0xB2,0xB3], "meta": {"trigger": 1, "offset": 16}},

                {"data": [0x00,0x00,0x11,0x11], "meta": {}},
                {"data": [0x22,0x22,0x33,0x33], "meta": {}},
                {"data": [0x44,0x44,0x55,0x55], "meta": {}},
                {"data": [0x66,0x66,0x77,0x77], "meta": {}},

                {"data": [0x00,0x00,0x11,0x11], "meta": {}},
                {"data": [0xE0,0xE1,0xE2,0xE3], "meta": {}},

                {"data": [0xF0,0xF1,0xF2,0xF3], "meta": {"overflow": 1}},

                {"data": [0xD0,0xD1,0xD2,0xD3], "meta": {"complete": 1}},
            ]:
                await stream_put(ctx, dut.i_packed, packed)

        async def testbench_blocks(ctx):
            await ctx.tick().repeat(32)
            assert dram.memory[0:4] == b"\x00\x00\x00\x00", dram.memory.hex()
            await stream_assert(ctx, dut.o_blocks,
                {"size": 32, "meta": {}})

            await ctx.tick().repeat(32)
            assert dram.memory[0:4] == b"\x10\x32\x54\x76", dram.memory.hex()
            await stream_assert(ctx, dut.o_blocks,
                {"size":  8, "meta": {"trigger": 1, "offset": 16}})

            await ctx.tick().repeat(32)
            assert dram.memory[32:36] == b"\xA0\xA1\xA2\xA3", dram.memory.hex()

            await stream_assert(ctx, dut.o_blocks,
                {"size": 24, "meta": {}})
            await ctx.tick().repeat(32)
            assert dram.memory[60:64] == b"\xE0\xE1\xE2\xE3", dram.memory.hex()

            await stream_assert(ctx, dut.o_blocks,
                {"size": 4, "meta": {"overflow": 1}})
            await ctx.tick().repeat(32)
            assert dram.memory[0:4] == b"\xF0\xF1\xF2\xF3", dram.memory.hex()

            await stream_assert(ctx, dut.o_blocks,
                {"size": 4, "meta": {"complete": 1}})
            await ctx.tick().repeat(32)
            assert dram.memory[4:8] == b"\xD0\xD1\xD2\xD3", dram.memory.hex()

        with self.run_test(m) as sim:
            sim.add_testbench(testbench_packed)
            sim.add_testbench(testbench_blocks)
            sim.add_testbench(dram.testbench, background=True)

    def test_reader(self):
        m = Module()
        m.submodules.reader = dut  = Reader(dram_range=range(0, 64), burst_bytes=32)
        m.submodules.dram   = dram = octoram.SimulationController(64)
        wiring.connect(m, dut.dram, dram.bus)

        dram.memory[:] = bytes(range(64))

        async def testbench_ranges(ctx):
            await stream_put(ctx, dut.i_blocks, {"skip": 0, "size": 32,
                                                 "meta": {"trigger": 1, "offset": 1}})
            await stream_put(ctx, dut.i_blocks, {"skip": 1, "size": 28})
            await stream_put(ctx, dut.i_blocks, {"skip": 0, "size":  8,
                                                 "meta": {"complete": 1, "offset": 8}})

        async def testbench_octets(ctx):
            for index, expected in enumerate([
                *range(0,  32),
            ]):
                assert (payload := await stream_get(ctx, dut.o_octets)) == {"data": expected}, \
                    f"[{index}]: {payload['data']:02x} != {expected:02x}"

            await stream_assert(ctx, dut.o_octets, {"data": 0x81})
            await stream_assert(ctx, dut.o_octets, {"end":  1})

            for index, expected in enumerate([
                *range(60, 64),
                *range(0,   4),
            ]):
                assert (payload := await stream_get(ctx, dut.o_octets)) == {"data": expected}, \
                    f"[{index}]: {payload['data']:02x} != {expected:02x}"

            await stream_assert(ctx, dut.o_octets, {"data": 0xC8})
            await stream_assert(ctx, dut.o_octets, {"end":  1})

        with self.run_test(m) as sim:
            sim.add_testbench(testbench_ranges, background=True)
            sim.add_testbench(testbench_octets)
            sim.add_testbench(dram.testbench, background=True)

    def test_flow_control(self):
        dut = FlowControl(
            writer_shape=1,
            writer_ratio=2,
            reader_shape=1,
            reader_ratio=1,
            max_credits=10,
        )

        async def testbench_main(ctx):
            ctx.set(dut.free_run, 1)
            ctx.set(dut.initial, 6)

            await stream_put(ctx, dut.i_writer, 0)
            assert ctx.get(dut.credits) == 2
            await stream_put(ctx, dut.i_writer, 0)
            assert ctx.get(dut.credits) == 4
            await stream_put(ctx, dut.i_writer, 0)
            assert ctx.get(dut.credits) == 6
            await stream_put(ctx, dut.i_writer, 0)
            assert ctx.get(dut.credits) == 6

            ctx.set(dut.free_run, 0)

            await stream_put(ctx, dut.i_reader, 0)
            assert ctx.get(dut.credits) == 5
            await stream_put(ctx, dut.i_reader, 0)
            assert ctx.get(dut.credits) == 4

            await stream_put(ctx, dut.i_writer, 0)
            assert ctx.get(dut.credits) == 6
            await stream_put(ctx, dut.i_writer, 0)
            assert ctx.get(dut.credits) == 8
            await stream_put(ctx, dut.i_writer, 0)
            assert ctx.get(dut.credits) == 10
            assert not ctx.get(dut.overflow)
            await stream_put(ctx, dut.i_writer, 0)
            assert ctx.get(dut.credits) == 10
            assert ctx.get(dut.overflow)

        async def testbench_w_sink(ctx):
            while True:
                await stream_get(ctx, dut.o_writer)

        async def testbench_r_sink(ctx):
            while True:
                await stream_get(ctx, dut.o_reader)

        with self.run_test(dut) as sim:
            sim.add_testbench(testbench_main)
            sim.add_testbench(testbench_w_sink, background=True)
            sim.add_testbench(testbench_r_sink, background=True)

    def test_integration(self):
        pins = io.SimulationPort("i", 8)

        m = Module()
        m.submodules.core = dut  = AnalyzerCore(
            pins=pins, dram_range=range(0x100), burst_bytes=32)
        m.submodules.dram = dram = octoram.SimulationController(0x100)
        wiring.connect(m, dut.dram, dram.bus)

        async def testbench_ctrl(ctx):
            ctx.set(dut.divisor, 1)

            ctx.set(dut.readout.prolog_size, 8)
            ctx.set(dut.readout.epilog_size, 8)

            # pins==0x40
            ctx.set(dut.trigger.mode[5], {"active": 1, "value": 1, "level": 0})

            ctx.set(dut.trigger.enable, 1)
            await ctx.tick()
            ctx.set(dut.trigger.enable, 0)

        async def testbench_i(ctx):
            for value in range(0x80):
                await ctx.tick()
                await ctx.tick()
                ctx.set(pins.i, value&0xff)

        async def testbench_o(ctx):
            for expected in [
                {"data": 0x1A}, {"data": 0x1B}, {"data": 0x1C}, {"data": 0x1D},
                {"data": 0x1E}, {"data": 0x1F}, {"data": 0x20}, {"data": 0x21},
                {"data": 0x80|16}, {"end": 1},
                {"data": 0x22}, {"data": 0x23}, {"data": 0x24}, {"data": 0x25},
                {"data": 0x26}, {"data": 0x27}, {"data": 0x28}, {"data": 0x29},
                {"data": 0xC0|24}, {"end": 1},
            ]:
                await stream_assert(ctx, dut.samples, expected)

        with self.run_test(m) as sim:
            sim.add_testbench(testbench_ctrl)
            sim.add_testbench(testbench_i)
            sim.add_testbench(testbench_o)
            sim.add_testbench(dram.testbench, background=True)
