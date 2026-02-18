from typing import Optional
from pathlib import Path
from datetime import datetime
import json
from loguru import logger


class PositionTracker:
    """
    Tracks position changes based on order fill events.

    Position tracking is callback-based - updates occur when orders are filled.
    Buy orders increase position, sell orders decrease position.
    """

    def __init__(self, start_position: float = 0.0):
        """
        Initialize position tracker.

        Args:
            start_position: Initial position in base currency
        """
        self.start_position = start_position
        self.current_position = 0.0  # Change from start position
        self.quote_position = 0.0  # Net quote spent/received

        # Setup debug log file
        debug_log_dir = Path("tracking")
        debug_log_dir.mkdir(exist_ok=True)
        self.debug_log_path = debug_log_dir / "position_tracker_debug.log"
        self.state_file_path = debug_log_dir / "position_state.json"

        # Create/clear debug log file
        with open(self.debug_log_path, 'w') as f:
            f.write(f"=== Position Tracker Debug Log - Started {datetime.now().isoformat()} ===\n")
            f.write(f"Initial start_position: {start_position:.6f}\n\n")

    def _debug_log(self, message: str) -> None:
        """Write to both logger and debug file."""
        logger.debug(message)
        with open(self.debug_log_path, 'a') as f:
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            f.write(f"[{timestamp}] {message}\n")

    def update_position(self, side, filled_size: float, price: float) -> None:
        """
        Update position based on a trade fill.

        Args:
            side: OrderSide.BUY or OrderSide.SELL
            filled_size: Amount that was filled
            price: Fill price
        """
        # Import here to avoid circular dependency
        import sys
        from pathlib import Path
        our_src = Path(__file__).resolve().parent.parent.parent
        if str(our_src) not in sys.path:
            sys.path.insert(0, str(our_src))
        from mm_bot.kuru_imports import OrderSide

        self._debug_log(
            f"[POSITION] BEFORE fill - current_position: {self.current_position:.2f}, "
            f"start_position: {self.start_position:.2f}, "
            f"total: {self.current_position + self.start_position:.2f}"
        )
        self._debug_log(
            f"[POSITION] Fill details - side: {side.value if hasattr(side, 'value') else side}, "
            f"filled_size: {filled_size:.2f}, price: {price:.6f}"
        )

        if side == OrderSide.BUY:
            self.current_position += filled_size
            self.quote_position -= price * filled_size
            self._debug_log(f"[POSITION] AFTER BUY - current_position: {self.current_position:.2f} (+{filled_size:.2f})")
            logger.info(f"Position update (BUY): +{filled_size} base @ {price} | Total: {self.get_current_position()}")
        elif side == OrderSide.SELL:
            self.current_position -= filled_size
            self.quote_position += price * filled_size
            self._debug_log(f"[POSITION] AFTER SELL - current_position: {self.current_position:.2f} (-{filled_size:.2f})")
            logger.info(f"Position update (SELL): -{filled_size} base @ {price} | Total: {self.get_current_position()}")

        self._debug_log(f"[POSITION] New total position: {self.current_position + self.start_position:.2f}\n")

        # Auto-save state after each position update
        self.save_state()


    def get_current_position(self) -> float:
        """
        Get total current position (start + changes).

        Returns:
            Total position in base currency
        """
        return self.current_position

    def get_quote_position(self) -> float:
        """
        Get net quote position (positive = received, negative = spent).

        Returns:
            Net quote position
        """
        return self.quote_position

    def get_start_position(self) -> float:
        """
        Get the initial starting position.

        Returns:
            Starting position
        """
        return self.start_position

    def save_state(self) -> None:
        """
        Save position state to JSON file for persistence across restarts.
        """
        try:
            state = {
                'start_position': self.start_position,
                'current_position': self.current_position,
                'quote_position': self.quote_position,
                'total_position': self.current_position + self.start_position,
                'last_updated': datetime.now().isoformat()
            }

            with open(self.state_file_path, 'w') as f:
                json.dump(state, f, indent=2)

            logger.debug(f"Position state saved: position={state['total_position']:.2f}")
        except Exception as e:
            logger.error(f"Failed to save position state: {e}")

    @classmethod
    def load_state(cls, state_file_path: Path) -> Optional[dict]:
        """
        Load position state from JSON file.

        Args:
            state_file_path: Path to the state file

        Returns:
            Dict with state data, or None if file doesn't exist or is invalid
        """
        try:
            if not state_file_path.exists():
                logger.info("No saved position state found (first run)")
                return None

            with open(state_file_path, 'r') as f:
                state = json.load(f)

            logger.info(f"Loaded position state: position={state.get('total_position', 0):.2f}, "
                       f"last_updated={state.get('last_updated', 'unknown')}")
            return state
        except Exception as e:
            logger.error(f"Failed to load position state: {e}")
            logger.warning("Starting with fresh position state")
            return None
