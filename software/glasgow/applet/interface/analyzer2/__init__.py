# The analyzer applet can be broken into a few functional units:
#  * The trigger units analyzes the incoming data and generates the trigger signal.
#  * The packer units converts raw sampled data into 32-bit words.
#  * The storage/readout units stores sample bytes (opaque to this units) and pushes them out
#    to the USB pipe. It keeps track of the trigger pointer and pre-trigger data region. It is,
#    essentially, a FIFO with special handling of read pointer and overflows.
#  * The bridge units TBD
#
# Unusually for Glasgow gateware, the units share a fixed-size, 32-bit data path. This is done
# because the analyzer is designed for interfacing with external software, some of which may be
# written in a way that makes complex and generic bitwise transformations unreasonably difficult.

import asyncio

from amaranth import *
from amaranth.utils import ceil_log2
from amaranth.lib import enum, data, wiring, stream, io
from amaranth.lib.wiring import In, Out, flipped

from glasgow.support import logging
from glasgow.support.endpoint import endpoint, ServerEndpoint
from glasgow.gateware import octoram, cobs
from glasgow.gateware.stream import Queue
from glasgow.abstract import AbstractAssembly, GlasgowPin
from glasgow.applet import GlasgowAppletV2


__all__ = ["AnalyzerCore", "AnalyzerComponent"]


xdebug = Signal(data.StructLayout({
    "r_addr": 32,
    # "r_pend": 16,
    # "r_levl": 16,
    "w_addr": 32,
    # "w_pend": 16,
    # "w_levl": 16,
    "overflow": 1,
    "free_run": 1,
    "credits":  32,
    "queue_size": 32,
}))


class Sampler(wiring.Component):
    """Sampler unit.

    Samples pins at an interval specified by a divisor.
    """

    divisor: In(24)

    o_samples: Out(stream.Signature(data.StructLayout({
        "data": 32,
    }), always_ready=True))

    def __init__(self, pins: io.PortLike):
        self._pins = pins

        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.i_buf = i_buf = io.FFBuffer("i", self._pins)
        # m.d.comb += self.o_samples.p.data.eq(i_buf.i)

        seq = Signal(range(5001), init=5000)
        with m.If(self.o_samples.valid):
            # m.d.sync += self.o_samples.p.data.eq(Mux(seq == 1000, 0x21, 0x20))
            m.d.sync += self.o_samples.p.data.eq(self.o_samples.p.data + 1)

        timer = Signal.like(self.divisor)
        with m.If(timer == 0):
            with m.If(seq > 0):
                m.d.sync += seq.eq(seq - 1)
            m.d.comb += self.o_samples.valid.eq(seq != 0)
            m.d.sync += timer.eq(self.divisor)
        with m.Else():
            m.d.sync += timer.eq(timer - 1)

        return m


class Trigger(wiring.Component):
    """Trigger unit.

    Compares input data with a trigger mode, marking samples that match.
    """

    class Mode(data.Struct):
        active: 1
        value:  1
        level:  1

    CONTROL_SHAPE = data.StructLayout({
        "mode":    data.ArrayLayout(Mode, 32),
        "force":   1,
        "enable":  1,
        "disable": 1,
    })

    control: In(CONTROL_SHAPE)

    i_samples: In(stream.Signature(data.StructLayout({
        "data": 32,
    }), always_ready=True))
    o_samples: Out(stream.Signature(data.StructLayout({
        "data": 32,
        "trig": 1,
    }), always_ready=True))

    def __init__(self):
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.d.comb += [
            self.o_samples.p.data.eq(self.i_samples.p.data),
            self.o_samples.valid.eq(self.i_samples.valid),
        ]

        with m.FSM() as fsm:
            with m.State("Disabled"):
                with m.If(self.control.enable):
                    m.next = "Enabled"
                with m.If(self.control.force):
                    m.next = "Forced"

            with m.State("Enabled"):
                with m.If(self.control.disable):
                    m.next = "Disabled"
                with m.If(self.o_samples.valid & self.o_samples.p.trig):
                    m.next = "Disabled"
                with m.If(self.control.force):
                    m.next = "Forced"

            with m.State("Forced"):
                with m.If(self.control.disable):
                    m.next = "Disabled"
                with m.If(self.o_samples.valid & self.o_samples.p.trig):
                    m.next = "Disabled"

        for mode, curr_bit in zip(self.control.mode, self.i_samples.p.data):
            prev_bit = Signal.like(curr_bit)
            with m.If(self.i_samples.valid):
                m.d.sync += prev_bit.eq(curr_bit)
            with m.If(fsm.ongoing("Enabled") & mode.active & (curr_bit == mode.value)):
                with m.If(mode.level | (prev_bit != curr_bit)):
                    m.d.comb += self.o_samples.p.trig.eq(1)

        with m.If(fsm.ongoing("Forced")):
            m.d.comb += self.o_samples.p.trig.eq(1)

        m.d.comb += platform.request("led", 2).o.eq(self.o_samples.p.trig)
        m.d.comb += platform.request("led", 0).o.eq(self.o_samples.p.data[15])

        return m


class Packer(wiring.Component):
    """Packer unit.

    Converts 32-bit samples with some amount of unused MSBs into 4-byte packed samples that consist
    of a concatenation of used LSBs (and possibly padding). A credit system is used to detect
    overrun conditions; the unitary credit is a single bit of a packed sample (i.e. including
    padding bits).

    Currently, the (real) sample width is configured statically. If it is not a power-of-2 amount
    of bits, padding is added after each sample so that they start at power-of-2 bit indices.
    """

    META_SHAPE = data.StructLayout({
        "offset":   range(32),
        "trigger":  1,
        "overflow": 1,
        "complete": 1,
    })

    i_samples: In(stream.Signature(data.StructLayout({
        "data": 32,
        "trig": 1,
    }), always_ready=True))
    o_packed: Out(stream.Signature(data.StructLayout({
        "data": data.ArrayLayout(8, 4),
        "meta": META_SHAPE,
    }), always_ready=True))

    def __init__(self, *, width: int):
        assert width in range(1, 33)

        self._width = width
        self._stride = 1 << ceil_log2(self._width)

        super().__init__()

    @property
    def width(self):
        return self._width

    @property
    def stride(self):
        return self._stride

    @property
    def samples_per_word(self):
        return len(self.i_samples.p.data) // self._stride

    def elaborate(self, platform):
        m = Module()

        pack = Signal(self._stride)
        m.d.comb += pack.eq(self.i_samples.p.data[:self._width])

        packs = self.samples_per_word
        count = Signal(range(packs))
        meta  = Signal(self.META_SHAPE)

        for index in range(packs - 1):
            with m.If(self.i_samples.valid & (count == index)):
                m.d.sync += self.o_packed.p.data.as_value().word_select(index, len(pack)).eq(pack)
                with m.If(self.i_samples.p.trig & ~meta.trigger):
                    m.d.sync += meta.trigger.eq(1)
                    m.d.sync += meta.offset.eq(count * len(pack))

        m.d.comb += self.o_packed.p.data.as_value().word_select(packs - 1, len(pack)).eq(pack)
        with m.If(self.i_samples.p.trig & ~meta.trigger):
            m.d.comb += self.o_packed.p.meta.trigger.eq(self.i_samples.p.trig)
            m.d.comb += self.o_packed.p.meta.offset.eq((packs - 1) * len(pack))
        with m.Else():
            m.d.comb += self.o_packed.p.meta.eq(meta)

        with m.If(self.i_samples.valid):
            m.d.sync += count.eq(count + 1)
            with m.If(count + 1 == packs):
                m.d.sync += meta.eq(0)
                m.d.comb += self.o_packed.valid.eq(1)

        return m


class Writer(wiring.Component):
    """Writer unit.

    Writes 4-byte packed samples to memory in bursts. Essentially a specialized write-combining
    cache. Outputs blocks (ranges of bytes with an optional trigger) once the write is committed
    to memory; the trigger, if active, is always at the last sample of the block.
    """

    dram: Out(octoram.Signature())

    i_packed: In(stream.Signature(data.StructLayout({
        "data": data.ArrayLayout(8, 4),
        "meta": Packer.META_SHAPE,
    })))
    o_blocks: Out(stream.Signature(data.StructLayout({
        "size": 8,  # 4-byte multiple
        "meta": Packer.META_SHAPE,
    })))

    def __init__(self, *, dram_range: range, burst_bytes: int):
        assert dram_range.start % burst_bytes == 0
        assert dram_range.stop % burst_bytes == 0
        assert dram_range.step == 1

        self._dram_range  = dram_range
        self._burst_bytes = burst_bytes

        super().__init__()

    def elaborate(self, platform):
        m = Module()

        # Queue blocks once enough samples are buffered. This smoothes over the intervals when
        # the writer loses arbitration (it has priority, but reads are uninterruptible) and allows
        # filling the entire write queue.
        m.submodules.block_queue = block_queue = Queue(
            shape=data.StructLayout({"addr": 32, "size": 8, "meta": Packer.META_SHAPE}), depth=4)
        block_ready = Signal()
        m.d.comb += [
            # Output the fact that block has been committed...
            self.o_blocks.p.size.eq(block_queue.o.p.size),
            self.o_blocks.p.meta.eq(block_queue.o.p.meta),
            self.o_blocks.valid.eq(block_queue.o.valid & block_ready),
            block_queue.o.ready.eq(self.o_blocks.ready & block_ready),
            # ... and write the block itself...
            self.dram.commands.p.type.eq(octoram.Command.WriteMemRow),
            self.dram.commands.p.addr.eq(block_queue.o.p.addr),
            self.dram.commands.p.size.eq(block_queue.o.p.size >> 1),
            self.dram.commands.valid.eq(block_queue.o.valid & block_ready),
            block_queue.o.ready.eq(self.dram.commands.ready & block_ready),
            # ... at the exact same time.
            block_ready.eq(self.dram.commands.ready & self.o_blocks.ready),
        ]

        # Buffer 4-byte packed sample bursts. Sized for two bursts worth of samples so that one
        # half can be filled while the other half is being drained.
        m.submodules.w_queue = w_queue = Queue(
            shape=data.ArrayLayout(8, 4), depth=(self._burst_bytes >> 2) * 4)
        m.d.comb += [
            w_queue.i.payload.eq(Cat(self.i_packed.p.data)),
            w_queue.i.valid.eq(self.i_packed.valid & block_queue.i.ready),
            self.i_packed.ready.eq(w_queue.i.ready & block_queue.i.ready),
        ]

        # Convert 4-byte packed samples into 2-byte memory beats.
        w_phase = Signal()
        m.d.comb += [
            self.dram.w_data.p[0].data.eq(Mux(w_phase, w_queue.o.payload[2], w_queue.o.payload[0])),
            self.dram.w_data.p[1].data.eq(Mux(w_phase, w_queue.o.payload[3], w_queue.o.payload[1])),
            self.dram.w_data.valid.eq(w_queue.o.valid),
            w_queue.o.ready.eq(self.dram.w_data.ready & w_phase),
        ]
        with m.If(self.dram.w_data.valid & self.dram.w_data.ready):
            m.d.sync += w_phase.eq(~w_phase)

        # Issue memory write commands.
        w_pointer = Signal(self._dram_range, init=self._dram_range.start)
        w_advance = Signal(range(self._burst_bytes + 1))
        with m.If(w_advance):
            m.d.sync += w_pointer.eq(w_pointer + w_advance)
            with m.If(w_pointer + w_advance == self._dram_range.stop):
                m.d.sync += w_pointer.eq(w_pointer.init)
        m.d.comb += [
            block_queue.i.p.addr.eq(w_pointer),
            block_queue.i.p.size.eq(w_advance),
            block_queue.i.p.meta.eq(self.i_packed.p.meta),
            block_queue.i.valid.eq(w_advance != 0),
        ]

        # Maintain a count of entries (not bytes) pushed to the write queue but not yet committed
        # to a burst.
        w_pending = Signal.like(w_queue.level)
        m.d.sync += w_pending.eq(w_pending
            + (w_queue.i.valid & w_queue.i.ready)
            - (w_advance // len(w_queue.i.payload)))

        # Drain (write) the buffer if the next burst would cross a burst-aligned block boundary.
        # This takes care of these three important cases:
        #  * Complete burst must be written.
        #  * Partial burst must be written to avoid crossing a (2K) row boundary.
        #  * Partial burst must be written to wrap at the end of memory region.
        # Sometimes the buffer will be drained without it being strictly required, but that's OK.
        # Also drain on explicit flush.
        next_level_bytes = (w_pending + 1) * len(w_queue.i.payload)
        flush_block = (self.i_packed.p.meta.trigger | self.i_packed.p.meta.overflow |
                       self.i_packed.p.meta.complete)
        with m.If((((w_pointer + next_level_bytes) & (self._burst_bytes - 1)) == 0) |
                  flush_block):
            with m.If(w_queue.i.valid & w_queue.i.ready):
                m.d.comb += w_advance.eq(next_level_bytes)

        m.d.sync += xdebug.w_addr.eq(w_pointer)
        # m.d.sync += xdebug.w_pend.eq(w_pending)
        # m.d.sync += xdebug.w_levl.eq(w_queue.level)

        return m


class Reader(wiring.Component):
    """Reader unit.

    Reads the contents of the specified ranges from memory.
    """

    dram: Out(octoram.Signature())

    i_blocks: In(stream.Signature(data.StructLayout({
        "skip": 1, # whether to read range contents or only advance the pointer
        "size": 8, # 4-byte multiple
        "meta": Packer.META_SHAPE,
    })))
    o_octets: Out(stream.Signature(data.StructLayout({
        "data": 8,
        "end":  1,
    })))

    def __init__(self, *, dram_range: range, burst_bytes: int):
        assert dram_range.start % burst_bytes == 0
        assert dram_range.stop % burst_bytes == 0
        assert dram_range.step == 1

        self._dram_range  = dram_range
        self._burst_bytes = burst_bytes

        super().__init__()

    def elaborate(self, platform):
        m = Module()

        # Buffer 2-byte memory beats. Sized for two bursts worth of samples so that one half can
        # be filled while the other half is being drained.
        m.submodules.r_queue = r_queue = Queue(
            shape=data.ArrayLayout(8, 2), depth=(self._burst_bytes >> 1) * 2)
        wiring.connect(m, flipped(self.dram.r_data), r_queue.i)

        # Queue blocks so that trailers can be inserted as appropriate.
        m.submodules.block_queue = block_queue = Queue(
            shape=self.i_blocks.p.shape(), depth=2)

        with m.FSM(name="octet_fsm"):
            # Convert 2-byte memory beats into output octets.
            with m.State("Data"):
                r_phase = Signal()
                r_count = Signal.like(block_queue.o.p.size)
                m.d.comb += [
                    self.o_octets.p.data.eq(r_queue.o.payload[r_phase]),
                    self.o_octets.valid.eq(block_queue.o.valid & r_queue.o.valid),
                    r_queue.o.ready.eq(block_queue.o.valid & self.o_octets.ready & r_phase),
                ]
                with m.If(self.o_octets.valid & self.o_octets.ready):
                    m.d.sync += r_phase.eq(~r_phase)
                    m.d.sync += r_count.eq(r_count + 1)
                    with m.If(r_count + 1 == block_queue.o.p.size):
                        m.d.sync += r_count.eq(0)
                        with m.If(block_queue.o.p.meta.trigger |
                                  block_queue.o.p.meta.overflow |
                                  block_queue.o.p.meta.complete):
                            m.next = "Trailer"
                        with m.Else():
                            m.d.comb += block_queue.o.ready.eq(1)

            # Append a trailer and end the stream.
            with m.State("Trailer"):
                with m.If(block_queue.o.p.meta.trigger):
                    m.d.comb += self.o_octets.p.data.eq(0x80 | block_queue.o.p.meta.offset)
                with m.Elif(block_queue.o.p.meta.overflow):
                    m.d.comb += self.o_octets.p.data.eq(0x40 | block_queue.o.p.meta.offset)
                with m.Elif(block_queue.o.p.meta.complete):
                    m.d.comb += self.o_octets.p.data.eq(0xC0 | block_queue.o.p.meta.offset)
                m.d.comb += self.o_octets.valid.eq(1)
                with m.If(self.o_octets.valid & self.o_octets.ready):
                    m.d.comb += block_queue.o.ready.eq(1)
                    m.next = "End"

            with m.State("End"):
                m.d.comb += self.o_octets.p.end.eq(1)
                m.d.comb += self.o_octets.valid.eq(1)
                with m.If(self.o_octets.valid & self.o_octets.ready):
                    m.next = "Data"

        # Issue memory read commands.
        r_length  = Signal.like(self.i_blocks.p.size)
        r_pointer = Signal(self._dram_range)
        r_advance = Signal(range(self._burst_bytes + 1))
        r_do_read = Signal()
        r_do_skip = Signal()
        with m.If(~(r_do_read | r_do_skip)):
            m.d.sync += r_length.eq(0)
        with m.Elif(self.dram.commands.ready | r_do_skip):
            m.d.sync += r_length.eq(r_length + r_advance)
            m.d.sync += r_pointer.eq(r_pointer + r_advance)
            with m.If(r_pointer + r_advance == self._dram_range.stop):
                m.d.sync += r_pointer.eq(self._dram_range.start)
        m.d.comb += [
            self.dram.commands.p.type.eq(octoram.Command.ReadMemRow),
            self.dram.commands.p.addr.eq(r_pointer),
            self.dram.commands.p.size.eq(r_advance >> 1),
            self.dram.commands.valid.eq((r_advance != 0) & ~r_do_skip),
        ]

        # Maintain a count of entries (not bytes) committed to a burst that have not yet arrived
        # into the read queue.
        r_pending = Signal.like(r_queue.level)
        m.d.sync += r_pending.eq(r_pending
            + (Mux(self.dram.commands.ready, r_advance // len(r_queue.i.payload), 0))
            - (r_queue.o.valid & r_queue.o.ready))

        # Refill the buffer up to the next burst boundary (or until the end of the requested range);
        # or, skip to the next burst boundary. (To reduce resource usage, skipping reuses the same
        # logic as reading, and is done on up to one burst boundary ahead per cycle.)
        free_queue_bytes = (r_queue.depth - r_pending) * len(r_queue.i.payload)
        with m.If((r_do_read & (free_queue_bytes > self._burst_bytes)) | r_do_skip):
            remainder_bytes  = self.i_blocks.p.size - r_length
            next_burst_bytes = self._burst_bytes - (r_pointer & (self._burst_bytes - 1))
            with m.If(next_burst_bytes < remainder_bytes):
                m.d.comb += r_advance.eq(next_burst_bytes)
            with m.Elif(remainder_bytes < self._burst_bytes):
                m.d.comb += r_advance.eq(remainder_bytes)
            with m.Else():
                m.d.comb += r_advance.eq(self._burst_bytes)

        m.d.comb += block_queue.i.payload.eq(self.i_blocks.payload)
        with m.FSM(name="block_fsm"):
            with m.State("Idle"):
                m.d.comb += block_queue.i.valid.eq(self.i_blocks.valid & ~self.i_blocks.p.skip)
                with m.If(self.i_blocks.valid & block_queue.i.ready):
                    with m.If(self.i_blocks.p.skip):
                        m.d.sync += r_do_skip.eq(1)
                    with m.Else():
                        m.d.sync += r_do_read.eq(1)
                    m.next = "Busy"

            with m.State("Busy"):
                with m.If(r_length + r_advance == self.i_blocks.p.size):
                    with m.If(self.dram.commands.ready | r_do_skip):
                        m.d.comb += self.i_blocks.ready.eq(1)
                        m.d.sync += r_do_read.eq(0)
                        m.d.sync += r_do_skip.eq(0)
                        m.next = "Idle"

        m.d.sync += xdebug.r_addr.eq(r_pointer)
        # m.d.sync += xdebug.r_pend.eq(r_pending)
        # m.d.sync += xdebug.r_levl.eq(r_queue.level)

        return m


class Arbiter(wiring.Component):
    """Reader/writer arbitration unit.

    A specialized arbiter that takes the particular access patterns of the analyzer into account,
    i.e. that the reader never writes and the writer never reads. The writer has priority.
    """

    dram: Out(octoram.Signature())

    writer: In(octoram.Signature())
    reader: In(octoram.Signature())

    def elaborate(self, platform):
        m = Module()

        wiring.connect(m, flipped(self.writer.w_data), flipped(self.dram.w_data))
        wiring.connect(m, flipped(self.dram.r_data), flipped(self.reader.r_data))

        with m.FSM():
            with m.State("Writer"):
                wiring.connect(m, flipped(self.writer.commands), flipped(self.dram.commands))
                with m.If(~self.writer.commands.valid & self.reader.commands.valid):
                    m.next = "Reader"

            with m.State("Reader"):
                wiring.connect(m, flipped(self.reader.commands), flipped(self.dram.commands))
                with m.If(~self.reader.commands.valid):
                    m.next = "Writer"

        return m


class FlowControl(wiring.Component):
    """Flow control unit.

    Has two modes: free-run and budgeted. In free-run mode, no credits are taken or given; writes
    proceed undisturbed. In budgeted mode, credits are given to the writer (one ``writer_ratio``
    per stream transfer) and taken from the reader (one ``reader_ratio`` per stream transfer).
    The amount of outstanding credits is tracked, and must be below ``max_credits`` at all times
    in order for writes to proceed. If a write arrives and the outstanding credits are over budget,
    the sticky ``overflow`` output is set.

    At the cycle where ``free_run`` goes low, the value of ``initial`` is used as the starting
    amount of outstanding credits. This represents the amount of already written data behind
    the starting address of the writer's next burst that must be preserved for readout.
    """

    def __init__(self, *, writer_shape, reader_shape, writer_ratio: int, reader_ratio: int,
                 max_credits: int):
        self._writer_ratio = writer_ratio
        self._reader_ratio = reader_ratio
        self._max_credits = max_credits

        super().__init__({
            "i_writer": In(stream.Signature(writer_shape, always_ready=True)),
            "o_writer": Out(stream.Signature(writer_shape)),
            "i_reader": In(stream.Signature(reader_shape)),
            "o_reader": Out(stream.Signature(reader_shape)),

            "free_run": In(1),
            "initial":  In(range(max_credits + 1)),
            "credits":  Out(range(max_credits + 1)),
            "overflow": Out(1),
        })

    def elaborate(self, platform):
        m = Module()

        do_writes = Signal()

        m.d.comb += [
            self.o_writer.payload.eq(self.i_writer.payload),
            self.o_writer.valid.eq(self.i_writer.valid & do_writes),
            self.o_reader.payload.eq(self.i_reader.payload),
            self.o_reader.valid.eq(self.i_reader.valid),
            self.i_reader.ready.eq(self.o_reader.ready),
        ]

        credits = self.credits
        credits1 = credits  + Mux(self.o_writer.valid & self.o_writer.ready, self._writer_ratio, 0)
        credits2 = credits1 - Mux(self.o_reader.valid & self.o_reader.ready, self._reader_ratio, 0)

        with m.If(~self.overflow):
            with m.If(self.free_run):
                m.d.comb += do_writes.eq(1)
                m.d.sync += credits.eq(Mux(credits1 < self.initial, credits1, self.initial))
            with m.Else():
                with m.If(credits <= self._max_credits - self._writer_ratio):
                    m.d.comb += do_writes.eq(self.o_writer.ready)
                with m.If(credits2 > 0): # XXX
                    m.d.sync += credits.eq(credits2)

        with m.If(self.i_writer.valid & ~self.o_writer.valid):
            m.d.sync += self.overflow.eq(1)

        m.d.sync += xdebug.overflow.eq(self.overflow)
        m.d.sync += xdebug.free_run.eq(self.free_run)
        m.d.sync += xdebug.credits.eq(credits)

        return m


class Readout(wiring.Component):
    """Readout control unit.

    While the flow control unit caps the (conservative estimate of) size of the packed sample queue
    to the DRAM region size, it does not function as a barrier: the fact that a credit has been
    issued to write a byte enables the readout of one byte, but actually reading this byte may well
    cause the reader to race ahead of the writer. This unit solves this problem.

    Whenever the writer commits to writing a block, it outputs the extent of it (and trigger offset,
    if any) via ``o_blocks``. At this point, the data in the block can be considered written (it may
    still be in progress but not in a way where the reader can interrupt it and get ahead). Whenever
    this happens, this unit expands the readout region by the block size (potentially capped to
    the prolog size if it's still in a pre-trigger state).
    """

    CONTROL_SHAPE = data.StructLayout({
        "prolog_size": 32, # 4-byte multiple
        "epilog_size": 32, # 4-byte multiple
        "streaming":   1,
    })

    control: In(CONTROL_SHAPE) # does not change after reset

    w_blocks: In(stream.Signature(data.StructLayout({
        "size": 8, # 4-byte multiple
        "meta": Packer.META_SHAPE,
    })))
    r_blocks: Out(stream.Signature(data.StructLayout({
        "skip": 1, # whether to read range contents or only advance the pointer
        "size": 8, # 4-byte multiple
        "meta": Packer.META_SHAPE,
    })))

    free_run: Out(1)

    def __init__(self, *, dram_range: range, burst_bytes: int, sample_stride: int):
        assert dram_range.step == 1
        assert burst_bytes % 4 == 0

        self._dram_range    = dram_range
        self._burst_bytes   = burst_bytes
        self._sample_stride = sample_stride

        super().__init__()

    def elaborate(self, platform):
        m = Module()

        queue_size = Signal(range(len(self._dram_range)))
        read_count = Signal(range(len(self._dram_range)))
        saved_meta = Signal.like(self.w_blocks.p.meta)

        m.d.sync += queue_size.eq(queue_size
            + Mux(self.w_blocks.valid & self.w_blocks.ready, self.w_blocks.p.size, 0)
            - Mux(self.r_blocks.valid & self.r_blocks.ready, self.r_blocks.p.size, 0))

        with m.FSM() as fsm:
            with m.State("Pre-Trigger/Write"):
                m.d.comb += self.free_run.eq(1)
                m.d.comb += self.w_blocks.ready.eq(1)
                with m.If(self.w_blocks.valid):
                    m.d.sync += saved_meta.eq(self.w_blocks.p.meta)
                    m.next = "Pre-Trigger/Skip"

            with m.State("Pre-Trigger/Skip"):
                m.d.comb += self.free_run.eq(1)
                with m.If(queue_size <= self.control.prolog_size):
                    with m.If(saved_meta.trigger):
                        m.next = "Fencepost"
                    with m.Else():
                        m.next = "Pre-Trigger/Write"
                with m.Else():
                    m.d.comb += [
                        self.r_blocks.p.skip.eq(1),
                        self.r_blocks.p.size.eq(queue_size - self.control.prolog_size),
                        self.r_blocks.valid.eq(1),
                    ]
                    with m.If(self.r_blocks.valid & self.r_blocks.ready):
                        with m.If(saved_meta.trigger):
                            m.next = "Fencepost"
                        with m.Else():
                            m.next = "Pre-Trigger/Write"

            with m.State("Fencepost"):
                m.d.sync += read_count.eq(queue_size)
                m.next = "Read-Prolog"

            with m.State("Read-Prolog"):
                m.d.comb += self.w_blocks.ready.eq(1)
                m.d.comb += self.r_blocks.valid.eq(queue_size >= self.r_blocks.p.size)
                with m.If(read_count > self._burst_bytes):
                    m.d.comb += self.r_blocks.p.size.eq(self._burst_bytes)
                    with m.If(self.r_blocks.valid & self.r_blocks.ready):
                        m.d.sync += read_count.eq(read_count - self._burst_bytes)
                with m.Else():
                    m.d.comb += self.r_blocks.p.size.eq(read_count)
                    m.d.comb += self.r_blocks.p.meta.eq(saved_meta)
                    with m.If(self.r_blocks.valid & self.r_blocks.ready):
                        m.d.sync += read_count.eq(self.control.epilog_size)
                        m.next = "Read-Epilog"

            with m.State("Read-Epilog"):
                m.d.comb += self.w_blocks.ready.eq(1)
                m.d.comb += self.r_blocks.valid.eq(queue_size >= self.r_blocks.p.size)
                with m.If(read_count > self._burst_bytes):
                    m.d.comb += self.r_blocks.p.size.eq(self._burst_bytes)
                    with m.If(self.r_blocks.valid & self.r_blocks.ready):
                        m.d.sync += read_count.eq(read_count - self._burst_bytes)
                with m.Else():
                    m.d.comb += self.r_blocks.p.size.eq(read_count)
                    m.d.comb += self.r_blocks.p.meta.complete.eq(1)
                    m.d.comb += self.r_blocks.p.meta.offset.eq(32 - self._sample_stride)
                    with m.If(self.r_blocks.valid & self.r_blocks.ready):
                        m.next = "Pre-Trigger/Write"

        m.d.sync += xdebug.queue_size.eq(queue_size)

        return m


class AnalyzerCore(wiring.Component):
    dram: Out(octoram.Signature())

    divisor: In(24)
    trigger: In(Trigger.CONTROL_SHAPE)
    readout: In(Readout.CONTROL_SHAPE)
    samples: Out(stream.Signature(data.StructLayout({
        "data": 8,
        "end":  1,
    })))

    interrupt: In(1)

    def __init__(self, *, pins: io.PortLike, dram_range: range, burst_bytes: int):
        self._pins = pins

        self._dram_range  = dram_range
        self._burst_bytes = burst_bytes

        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.sampler = sampler = ResetInserter(self.interrupt)(Sampler(self._pins))
        m.d.comb += sampler.divisor.eq(self.divisor)

        m.submodules.trigger = trigger = Trigger()
        m.d.comb += trigger.control.eq(self.trigger)
        wiring.connect(m, sampler.o_samples, trigger.i_samples)

        m.submodules.packer = packer = Packer(width=len(self._pins))
        wiring.connect(m, trigger.o_samples, packer.i_samples)

        m.submodules.writer = writer = Writer(
            dram_range=self._dram_range, burst_bytes=self._burst_bytes)
        m.submodules.reader = reader = Reader(
            dram_range=self._dram_range, burst_bytes=self._burst_bytes)
        m.submodules.arbiter = arbiter = Arbiter()
        wiring.connect(m, arbiter.dram, flipped(self.dram))
        wiring.connect(m, arbiter.writer, writer.dram)
        wiring.connect(m, arbiter.reader, reader.dram)

        m.submodules.flow_ctrl = flow_ctrl = ResetInserter(self.interrupt)(FlowControl(
            writer_shape=writer.i_packed.p.shape(),
            writer_ratio=len(writer.i_packed.p.data),
            reader_shape=reader.o_octets.p.shape(),
            reader_ratio=1,
            max_credits=len(self._dram_range),
        ))
        wiring.connect(m, packer.o_packed, flow_ctrl.i_writer)
        wiring.connect(m, flow_ctrl.o_writer, writer.i_packed)
        wiring.connect(m, reader.o_octets, flow_ctrl.i_reader)

        m.submodules.readout = readout = ResetInserter(self.interrupt)(Readout(
            dram_range=self._dram_range, burst_bytes=self._burst_bytes,
            sample_stride=packer.stride))
        m.d.comb += readout.control.eq(self.readout)
        wiring.connect(m, writer.o_blocks, readout.w_blocks)
        wiring.connect(m, readout.r_blocks, reader.i_blocks)

        m.d.comb += [
            flow_ctrl.initial.eq(self.readout.prolog_size),
            flow_ctrl.free_run.eq(readout.free_run),
        ]

        wiring.connect(m, flow_ctrl.o_reader, flipped(self.samples))

        return m


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


class TriggerCommand(enum.Enum, shape=8):
    SET_ACTIVE          = 0b01_100000
    SET_VALUE           = 0b01_100001
    SET_LEVEL           = 0b01_100010


class CommandHandler(wiring.Component):
    i: In(stream.Signature(data.StructLayout({
        "cmd": data.StructLayout({
            "opcode":  5,
            "trigger": 1,
            "has_arg": 1,
            "has_ret": 1,
        }),
        "arg": 32,
    })))
    o: Out(stream.Signature(data.StructLayout({
        "ret": 32,
    })))

    divisor: Out(16)
    readout: Out(Readout.CONTROL_SHAPE)
    trigger: Out(Trigger.CONTROL_SHAPE)

    cfg_update: Out(1) # asserted for one cycle
    interrupt:  Out(1) # asserted for one cycle

    def __init__(self, *, clk_frequency, buffer_size, data_width, data_stride):
        self._clk_frequency = clk_frequency
        self._buffer_size   = buffer_size
        self._data_width    = data_width
        self._data_stride   = data_stride

        super().__init__()

    def elaborate(self, platform):
        m = Module()

        with m.If(self.i.p.cmd.has_ret):
            m.d.comb += self.o.valid.eq(1)
            m.d.comb += self.i.ready.eq(self.o.ready)
        with m.Else():
            m.d.comb += self.i.ready.eq(1)

        m.d.sync += self.cfg_update.eq(0)
        with m.If(self.i.valid):
            with m.Switch(self.i.p.cmd):
                # Commands (common)
                #
                with m.Case(Command.IDENTIFY):
                    m.d.comb += self.o.p.ret.eq(int.from_bytes(b"GLA0", "little"))

                with m.Case(Command.GET_CLK_FREQUENCY):
                    m.d.comb += self.o.p.ret.eq(self._clk_frequency)

                with m.Case(Command.GET_BUFFER_SIZE):
                    m.d.comb += self.o.p.ret.eq(self._buffer_size)

                with m.Case(Command.GET_DATA_FORMAT):
                    m.d.comb += self.o.p.ret.eq(self._data_width | (self._data_stride << 8))

                with m.Case(Command.SET_CLK_DIVISOR):
                    m.d.sync += self.divisor.eq(self.i.p.arg)
                    m.d.sync += self.cfg_update.eq(1)

                with m.Case(Command.SET_PROLOG_SIZE):
                    m.d.sync += self.readout.prolog_size.eq(self.i.p.arg << 2)
                    m.d.sync += self.cfg_update.eq(1)

                with m.Case(Command.SET_EPILOG_SIZE):
                    m.d.sync += self.readout.epilog_size.eq(self.i.p.arg[:31] << 2)
                    m.d.sync += self.readout.streaming.eq(self.i.p.arg[31])
                    m.d.sync += self.cfg_update.eq(1)

                with m.Case(Command.GET_TRIGGER):
                    m.d.comb += self.o.p.ret.eq(int.from_bytes(b"BASI", "little"))

                with m.Case(Command.SET_TRIGGER):
                    pass # Do nothing; we only support the basic trigger mode

                with m.Case(Command.SYNCHRONIZE):
                    m.d.comb += self.o.p.ret.eq(self.i.p.arg)

                with m.Case(Command.ARM_TRIGGER):
                    m.d.comb += self.trigger.enable.eq(1)

                with m.Case(Command.FORCE_TRIGGER):
                    m.d.comb += self.trigger.force.eq(1)

                with m.Case(Command.DISARM_TRIGGER):
                    m.d.comb += self.trigger.disable.eq(1)

                with m.Case(Command.INTERRUPT):
                    m.d.comb += self.interrupt.eq(1)
                    # TODO: disable trigger?

                # Commands (basic trigger)
                #
                with m.Case(TriggerCommand.SET_ACTIVE):
                    m.d.sync += Cat(self.trigger.mode[i].active for i in range(32)).eq(self.i.p.arg)

                with m.Case(TriggerCommand.SET_VALUE):
                    m.d.sync += Cat(self.trigger.mode[i].value for i in range(32)).eq(self.i.p.arg)

                with m.Case(TriggerCommand.SET_LEVEL):
                    m.d.sync += Cat(self.trigger.mode[i].level for i in range(32)).eq(self.i.p.arg)

        return m


class CommandParser(wiring.Component):
    command: Out(stream.Signature(data.StructLayout({
        "cmd": data.StructLayout({
            "opcode":  5,
            "trigger": 1,
            "has_arg": 1,
            "has_ret": 1,
        }),
        "arg": 32,
    })))
    response: In(stream.Signature(data.StructLayout({
        "ret": 32,
    })))

    i_stream: In(stream.Signature(8))
    o_stream: Out(stream.Signature(data.StructLayout({
        "data": 8,
        "end":  1,
    })))

    def elaborate(self, platform):
        m = Module()

        index = Signal(2)

        with m.FSM():
            with m.State("Command"):
                m.d.sync += self.command.p.cmd.eq(self.i_stream.payload)
                m.d.comb += self.i_stream.ready.eq(1)
                with m.If(self.i_stream.valid & self.i_stream.ready):
                    m.next = "Argument"

            with m.State("Argument"):
                with m.If(self.command.p.cmd.has_arg):
                    m.d.sync += self.command.p.arg.word_select(index, 8).eq(self.i_stream.payload)
                    m.d.comb += self.i_stream.ready.eq(1)
                    with m.If(self.i_stream.valid & self.i_stream.ready):
                        m.d.sync += index.eq(index + 1)
                        with m.If(index == 3):
                            m.next = "Return"
                with m.Else():
                    m.next = "Return"

            with m.State("Return"):
                m.d.comb += self.command.valid.eq(1)
                with m.If(self.command.p.cmd.has_ret):
                    m.d.comb += self.o_stream.p.data.eq(self.response.p.ret.word_select(index, 8))
                    m.d.comb += self.o_stream.valid.eq(self.response.valid)
                    with m.If(self.o_stream.valid & self.o_stream.ready):
                        m.d.sync += index.eq(index + 1)
                        with m.If(index == 3):
                            m.d.comb += self.response.ready.eq(1)
                            m.next = "End"
                with m.Else():
                    m.d.comb += self.response.ready.eq(1)
                    m.next = "Command"

            with m.State("End"):
                m.d.comb += self.o_stream.p.end.eq(1)
                m.d.comb += self.o_stream.valid.eq(1)
                with m.If(self.o_stream.valid & self.o_stream.ready):
                    m.next = "Command"

        return m


class OutputMultiplexer(wiring.Component):
    i_stream_cmd: In(stream.Signature(data.StructLayout({
        "data": 8,
        "end":  1,
    })))
    i_stream_data: In(stream.Signature(data.StructLayout({
        "data": 8,
        "end":  1,
    })))

    o_stream: Out(stream.Signature(data.StructLayout({
        "data": 8,
        "end":  1,
    })))

    def elaborate(self, platform):
        m = Module()

        # Used to ensure that data substreams are only interrupted at a word boundary.
        data_index = Signal(range(4))

        with m.FSM():
            with m.State("Idle"):
                with m.If(self.i_stream_cmd.valid):
                    m.d.comb += [
                        self.o_stream.p.data.eq(0),
                        self.o_stream.valid.eq(1),
                    ]
                    with m.If(self.o_stream.valid & self.o_stream.ready):
                        m.next = "Command"
                with m.Elif(self.i_stream_data.valid):
                    m.d.comb += [
                        self.o_stream.p.data.eq(1),
                        self.o_stream.valid.eq(1),
                    ]
                    with m.If(self.o_stream.valid & self.o_stream.ready):
                        m.d.sync += data_index.eq(0)
                        m.next = "Data"

            with m.State("Command"):
                wiring.connect(m, flipped(self.i_stream_cmd), flipped(self.o_stream))
                with m.If(self.i_stream_cmd.valid & self.i_stream_cmd.ready &
                          self.i_stream_cmd.p.end):
                    m.next = "Idle"

            with m.State("Data"):
                wiring.connect(m, flipped(self.i_stream_data), flipped(self.o_stream))
                with m.If(self.i_stream_data.valid & self.i_stream_data.ready):
                    with m.If(self.i_stream_data.p.end):
                        m.next = "Idle"
                    with m.Else():
                        m.d.sync += data_index.eq(data_index + 1)
                        with m.If(self.i_stream_cmd.valid & (data_index == 3)):
                            m.next = "Data-Interrupt"
                with m.Elif(self.i_stream_cmd.valid & (data_index == 0)):
                    m.next = "Data-Interrupt"

            with m.State("Data-Interrupt"):
                m.d.comb += [
                    # Inject a "continuation" trailer.
                    self.o_stream.p.data.eq(0x00),
                    self.o_stream.valid.eq(1),
                ]
                with m.If(self.o_stream.valid & self.o_stream.ready):
                    m.next = "End"

            with m.State("End"):
                m.d.comb += [
                    self.o_stream.p.end.eq(1),
                    self.o_stream.valid.eq(1),
                ]
                with m.If(self.o_stream.valid & self.o_stream.ready):
                    m.next = "Idle"

        return m


class AnalyzerComponent(wiring.Component):
    i_stream: In(stream.Signature(8))
    o_stream: Out(stream.Signature(8))
    o_flush: Out(1)

    def __init__(self, *, pins: io.PortLike, dram_bus: wiring.PureInterface, dram_range: range):
        self._pins       = pins
        self._dram_bus   = dram_bus
        self._dram_range = dram_range

        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.core = core = AnalyzerCore(
            pins=self._pins, dram_range=self._dram_range, burst_bytes=32)
        wiring.connect(m, core.dram, self._dram_bus)

        m.submodules.handler = handler  = CommandHandler(
            # TODO: frequency
            clk_frequency=48_000_000, buffer_size=len(self._dram_range),
            data_width=len(self._pins), data_stride=1 << ceil_log2(len(self._pins)),
        )
        m.d.comb += [
            core.divisor.eq(handler.divisor),
            core.readout.eq(handler.readout),
            core.trigger.eq(handler.trigger),
            core.interrupt.eq(handler.cfg_update | handler.interrupt),
        ]

        m.submodules.parser = parser = CommandParser()
        wiring.connect(m, flipped(self.i_stream), parser.i_stream)
        wiring.connect(m, parser.command, handler.i)
        wiring.connect(m, handler.o, parser.response)

        m.submodules.output_mux = output_mux = OutputMultiplexer()
        wiring.connect(m, parser.o_stream, output_mux.i_stream_cmd)
        wiring.connect(m, core.samples, output_mux.i_stream_data)

        m.submodules.cobs_encoder = cobs_encoder = cobs.Encoder()
        wiring.connect(m, output_mux.o_stream, cobs_encoder.i)
        wiring.connect(m, cobs_encoder.o, flipped(self.o_stream))
        m.d.comb += self.o_flush.eq(self.o_stream.payload == 0x00)

        return m


class AnalyzerInterface:
    def __init__(self, logger: logging.Logger, assembly: AbstractAssembly, *,
                 pins: list[GlasgowPin]):
        self._logger = logger
        self._level  = logging.DEBUG if self._logger.name == __name__ else logging.TRACE

        ports = assembly.add_port_group(pins=pins)
        dram_bus, dram_range = assembly.add_dynamic_memory()
        component = assembly.add_submodule(AnalyzerComponent(
            pins=ports.pins, dram_bus=dram_bus, dram_range=dram_range))
        self._pipe = assembly.add_inout_pipe(
            component.o_stream, component.i_stream, in_flush=component.o_flush)
        self._xdebug = assembly.add_ro_register(xdebug)

    def _log(self, message: str, *args):
        self._logger.log(self._level, "analyzer: " + message, *args)


class AnalyzerApplet(GlasgowAppletV2):
    logger = logging.getLogger(__name__)
    help = "logic analyzer"
    preview = True
    description = """
    TODO
    """

    @classmethod
    def add_build_arguments(cls, parser, access):
        access.add_voltage_argument(parser)

        # TODO: improve this to parse eg `run analyzer clk=A0 data=A1`
        access.add_pins_argument(parser, "pins", width=range(1, 33), required=True)

    def build(self, args):
        with self.assembly.add_applet(self):
            self.assembly.use_voltage(args.voltage)
            self.analyzer_iface = AnalyzerInterface(self.logger, self.assembly,
                pins=args.pins)

    @classmethod
    def add_setup_arguments(cls, parser):
        pass

    async def setup(self, args):
        pass

    @classmethod
    def add_run_arguments(cls, parser):
        parser.add_argument(
            "endpoint", metavar="ENDPOINT", type=endpoint, nargs="?",
            default=("tcp", "localhost", 5555),
            help="listen at ENDPOINT, either unix:PATH or tcp:HOST:PORT "
                 "(default: tcp:localhost:5555)"
        )

    async def run(self, args):
        def pvalue(x):
            v = []
            for n, fld in x.shape():
                if isinstance(fld.shape, data.Layout):
                    v.append(f"{n}={pvalue(x[n])}")
                else:
                    v.append(f"{n}={x[n]:{'#x' if Shape.cast(fld.shape).width > 1 else ''}}")
            return ", ".join(v)

        async def poll_debug():
            while True:
                print(pvalue(await self.analyzer_iface._xdebug.get()))
                await asyncio.sleep(1)

        a = asyncio.create_task(poll_debug())

        endpoint = await ServerEndpoint("socket", self.logger, args.endpoint)
        await endpoint.attach_to_pipe(self.analyzer_iface._pipe)

        a = a

    @classmethod
    def tests(cls):
        from . import test
        return test.AnalyzerAppletTestCase
