import threading

from app.core.config import ErpFailureMode
from mock_erp.schemas import FailureConfig, FailureState


class FailureInjector:
    """Process-local failure scenario for POST /erp/orders (development only).

    State lives in memory: with several mock ERP processes, each has its own.
    """

    def __init__(self, default_mode: ErpFailureMode, default_delay_seconds: float) -> None:
        """Start in the mode configured by MOCK_ERP_FAILURE_MODE.

        Milestone 4.
        """
        self._default_mode: ErpFailureMode = default_mode
        self._default_delay = default_delay_seconds
        self._lock = threading.Lock()
        self._state = self._initial_state()

    def _initial_state(self) -> FailureState:
        """The default state: configured mode, applied with no count limit.

        Milestone 4.
        """
        return FailureState(
            mode=self._default_mode, remaining=None, delay_seconds=self._default_delay
        )

    def configure(self, config: FailureConfig) -> FailureState:
        """Switch to a failure mode, optionally for the next `count` requests only.

        Milestone 4.
        """
        with self._lock:
            self._state = FailureState(
                mode=config.mode,
                remaining=config.count,
                delay_seconds=(
                    config.delay_seconds
                    if config.delay_seconds is not None
                    else self._default_delay
                ),
            )
            return self._state.model_copy()

    def reset(self) -> FailureState:
        """Back to the configured default mode.

        Milestone 4.
        """
        with self._lock:
            self._state = self._initial_state()
            return self._state.model_copy()

    def snapshot(self) -> FailureState:
        """Return a copy of the current state (for GET /admin/failure-mode).

        Milestone 4.
        """
        with self._lock:
            return self._state.model_copy()

    def consume(self) -> tuple[ErpFailureMode, float]:
        """Return the mode to apply to the current request, decrementing any count.

        Milestone 4.
        """
        with self._lock:
            state = self._state
            mode, delay = state.mode, state.delay_seconds
            if state.remaining is not None:
                state.remaining -= 1
                if state.remaining == 0:
                    self._state = FailureState(
                        mode="normal", remaining=None, delay_seconds=state.delay_seconds
                    )
            return mode, delay
