"""Encode 800 kHz GRB pixels for the Raspberry Pi OS SPI0 interface."""

SPI_LED_PIN = 10
SPI_LED_LIMIT = 144
SPI_SPEED = 6_400_000
RESET_BYTES = bytes(256)  # 320 microseconds low at SPI_SPEED.
BIT_ZERO = 0b11000000
BIT_ONE = 0b11110000


class SpiGlow:
    def __init__(self, count: int) -> None:
        self._count = count
        self._device = None

    @staticmethod
    def _open_device():
        try:
            from spidev import SpiDev
        except ImportError as exc:
            raise RuntimeError("Install python3-spidev for the Python environment running Vacation Ghost") from exc
        device = SpiDev()
        try:
            device.open(0, 0)
            device.mode = 0
            device.lsbfirst = False
            device.bits_per_word = 8
            device.max_speed_hz = SPI_SPEED
        except OSError as exc:
            device.close()
            raise RuntimeError(f"Could not configure /dev/spidev0.0. Enable SPI and check device access. {exc}") from exc
        return device

    def check(self) -> None:
        self._open_device().close()

    def show(self, rgb: tuple[int, int, int]) -> None:
        if self._device is None:
            self._device = self._open_device()
        red, green, blue = rgb
        pixel = bytes(
            BIT_ONE if channel & (1 << bit) else BIT_ZERO
            for channel in (green, red, blue)
            for bit in range(7, -1, -1)
        )
        frame = RESET_BYTES + pixel * self._count + RESET_BYTES
        # Keep the frame within the default 4096-byte spidev buffer so it
        # transmits in one write. Splitting it could latch partial pixel data.
        try:
            self._device.writebytes(list(frame))
        except (OSError, SystemError) as exc:
            # A short write can surface as SystemError in py-spidev.
            self.close()
            raise RuntimeError(f"LED frame write to /dev/spidev0.0 failed: {exc}") from exc

    def close(self) -> None:
        if self._device is not None:
            device, self._device = self._device, None
            device.close()
