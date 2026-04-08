"""Console output. Prints alerts to terminal with color coding."""

from __future__ import annotations

from reconcile.schema import Alert

SEVERITY_COLORS = {
    "critical": "\033[91m",  # red
    "suspect":  "\033[93m",  # yellow
    "elevated": "\033[96m",  # cyan
    "info":     "\033[37m",  # white
}
RESET = "\033[0m"


class ConsoleOutput:
    async def emit(self, alert: Alert) -> None:
        color = SEVERITY_COLORS.get(alert.severity, "")
        print(
            f"{color}[{alert.severity.upper():>8}]{RESET} "
            f"{alert.timestamp.strftime('%H:%M:%S')} "
            f"({alert.detector}) {alert.title}"
        )
