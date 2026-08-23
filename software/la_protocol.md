# Overall framing

Commands:

```
H>D: type:u8 [arg:u32le]
D>H: <COBS> 0:u8 [ret:u32le] </COBS>
```

`type`:
- bit 7: whether there is a return value
- bit 6: whether there is an argument
- bit 5: whether the command is a common or a trigger command
- bit 4..0: command word

Data:

```
D>H: <COBS> 1:u8 [data:u32le]* trailer:u8 </COBS>
```

* `trailer` = how far off into the last word is the trigger position, in bits


# Commands (common)

## `0b10_000000`: IDENTIFY

* `ret` = 0x30414c47 (fourcc `GLA0`)

## `0b10_000001`: GET_CLK_FREQUENCY

* `ret` = base frequency in Hz (e.g. 48'000'000 for 48 MHz)

## `0b01_000010`: SET_CLK_DIVISOR

* `arg` = clock divisor - 1 (e.g. 3 for DIV/4)

## `0b10_000011`: GET_BUFFER_SIZE

* `ret` = capture buffer size in 32-bit words (e.g. 0x40000 for 1 MB)

## `0b01_000100`: SET_PROLOG_SIZE

* `arg` = capture buffer size in 32-bit words

## `0b01_000101`: SET_EPILOG_SIZE

* `arg[0..30]` = capture buffer size in 32-bit words
* `arg[31]` = whether to continue capture after outputting prolog and epilog

## `0b10_000110`: GET_DATA_FORMAT

* `ret[0..4]`  = width = how many bits are valid for each sample
* `ret[8..12]` = stride = how many bits to advance to get to the next sample

Stride must be an integer divisor of word size (32).

## `0b01_010000`: SET_TRIGGER

* `arg` = fourcc of requested trigger block

When `arg` designates a valid trigger block, configures and resets this trigger block.

## `0b10_010000`: GET_TRIGGER

* `ret` = fourcc of the configured trigger block

## `0b11_011111`: SYNCHRONIZE

* `arg` = any value
* `ret` = `arg`

## `0b00_000000`: ARM_TRIGGER

## `0b00_000001`: FORCE_TRIGGER

## `0b00_000010`: DISARM_TRIGGER

## `0b00_000011`: INTERRUPT

Stops any capture in progress and also disarms the trigger.


# Commands (trigger `BASI`, 0x49534142)

Basic trigger.

## `0b01_100000`: TRG_BASIC_SET_ACTIVE

* `arg[n]` = whether the trigger for channel `n` is active

## `0b01_100001`: TRG_BASIC_SET_VALUE

* `arg[n]` = whether the trigger for channel `n` is compared to 0 or 1

## `0b01_100010`: TRG_BASIC_SET_LEVEL

* `arg[n]` = whether the trigger for channel `n` is compared by level (1) or by edge (0)
